"""Fetch the current tracker state and append it to the archive.

Shared by both `gtt poll` (one shot, scheduled by launchd) and `gtt watch`
(internal loop), so the two modes cannot drift apart.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from fressnapftracker import ApiClient
from fressnapftracker.exceptions import (
    FressnapfTrackerAuthenticationError,
    FressnapfTrackerError,
    FressnapfTrackerInvalidDeviceTokenError,
    FressnapfTrackerInvalidSerialNumberError,
    FressnapfTrackerInvalidTokenError,
)

from .auth import DeviceCredential, load_credentials, redact
from .config import Config
from .live_tracking import LiveTrackingClient, in_live_hours, live_tracking_reason
from .store import (
    acquire_write_lock,
    last_live_tracking_request,
    log_live_tracking,
    log_poll,
    write_connection,
    write_snapshot,
)

log = logging.getLogger(__name__)

# These will not fix themselves by waiting -- the user has to sign in again.
_AUTH_ERRORS = (
    FressnapfTrackerAuthenticationError,
    FressnapfTrackerInvalidDeviceTokenError,
    FressnapfTrackerInvalidSerialNumberError,
    FressnapfTrackerInvalidTokenError,
)


@dataclass
class DeviceOutcome:
    """What happened for one tracker during one poll."""

    serialnumber: str
    ok: bool
    error: str | None = None
    auth_failure: bool = False
    payload: dict[str, Any] | None = None
    duration_ms: int = 0
    new_position: bool = False
    # Human-readable note when this poll asked for live tracking, e.g.
    # "live tracking enabled (renew)" or "live tracking failed: ...".
    live_tracking: str | None = None


@dataclass
class PollResult:
    """Aggregate result of one poll across all trackers."""

    outcomes: list[DeviceOutcome] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.outcomes) and all(o.ok for o in self.outcomes)

    @property
    def new_positions(self) -> int:
        return sum(1 for o in self.outcomes if o.new_position)

    @property
    def auth_failure(self) -> bool:
        return any(o.auth_failure for o in self.outcomes)


def _make_capturing_client(sink: list[dict[str, Any]]) -> httpx.AsyncClient:
    """An httpx client that records every JSON object the API returns.

    `get_tracker()` hands back a validated pydantic model, which has already
    dropped anything the library does not model. We want the bytes the server
    actually sent for `raw_snapshots`, so grab them via a response event hook --
    reading the body here is the documented httpx pattern and leaves the content
    cached for the library's own `.json()` call.
    """

    async def capture(response: httpx.Response) -> None:
        await response.aread()
        if "application/json" not in response.headers.get("content-type", ""):
            return
        try:
            body = response.json()
        except ValueError:
            return
        if isinstance(body, dict):
            sink.append(body)

    return httpx.AsyncClient(event_hooks={"response": [capture]})


async def _fetch_device(credential: DeviceCredential, request_timeout: int) -> DeviceOutcome:
    """Fetch one tracker, converting every failure mode into an outcome."""
    captured: list[dict[str, Any]] = []
    started = time.perf_counter()
    log.debug(
        "fetching %s (token %s)", credential.serialnumber, redact(credential.token)
    )
    try:
        async with _make_capturing_client(captured) as http_client:
            async with ApiClient(
                serial_number=credential.serialnumber,
                device_token=credential.token,
                request_timeout=request_timeout,
                client=http_client,
            ) as api:
                tracker = await api.get_tracker()
    except _AUTH_ERRORS as exc:
        return DeviceOutcome(
            serialnumber=credential.serialnumber,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            auth_failure=True,
            duration_ms=_elapsed_ms(started),
        )
    except FressnapfTrackerError as exc:
        # Connection trouble, or an invalid response -- upstream added a dedicated
        # error for the latter because broken trackers do emit garbage.
        return DeviceOutcome(
            serialnumber=credential.serialnumber,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
            duration_ms=_elapsed_ms(started),
        )

    if captured:
        payload = captured[-1]
    else:
        log.warning(
            "%s: could not capture the raw response, storing the parsed model instead",
            credential.serialnumber,
        )
        payload = tracker.model_dump(mode="json")

    return DeviceOutcome(
        serialnumber=credential.serialnumber,
        ok=True,
        payload=payload,
        duration_ms=_elapsed_ms(started),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


async def _fetch_all(
    credentials: list[DeviceCredential], request_timeout: int
) -> list[DeviceOutcome]:
    return [await _fetch_device(credential, request_timeout) for credential in credentials]


async def _enable_live_tracking(
    credential: DeviceCredential, request_timeout: int
) -> tuple[bool, str]:
    """Request live mode for one tracker. Returns (ok, server message or error)."""
    try:
        async with LiveTrackingClient(
            serial_number=credential.serialnumber,
            device_token=credential.token,
            request_timeout=request_timeout,
        ) as api:
            return True, await api.enable_live_tracking()
    except FressnapfTrackerError as exc:
        # Includes the API's rate limiter, which answers 429 text/plain "Retry
        # later" -- the next poll simply tries again.
        return False, f"{type(exc).__name__}: {exc}"


async def _enable_all(
    requests: list[tuple[DeviceCredential, str]], request_timeout: int
) -> list[tuple[bool, str]]:
    return [await _enable_live_tracking(credential, request_timeout) for credential, _ in requests]


def _live_tracking_reason(
    conn: Any, serialnumber: str, config: Config, *, now: datetime
) -> str | None:
    """Why this poll should request live tracking for the device, or None.

    None outside the configured daily hours -- a running grant is then simply
    left to expire -- and while a grant is still running: renewing early would
    be a wasted request, and the API rate-limits them.
    """
    if not in_live_hours(now, start=config.live_from, end=config.live_until):
        return None
    return live_tracking_reason(last_live_tracking_request(conn, serialnumber), now=now)


def _request_live_tracking(
    config: Config,
    credentials: dict[str, DeviceCredential],
    wanted: list[tuple[DeviceOutcome, str]],
) -> None:
    """Send the live-tracking requests and record what came back."""
    requests = [(credentials[outcome.serialnumber], reason) for outcome, reason in wanted]
    responses = asyncio.run(_enable_all(requests, config.request_timeout))
    now = datetime.now(UTC)
    with write_connection(config.db_path) as conn:
        for (outcome, reason), (ok, message) in zip(wanted, responses, strict=True):
            log_live_tracking(
                conn,
                serialnumber=outcome.serialnumber,
                ok=ok,
                message=message,
                reason=reason,
                now=now,
            )
            if ok:
                outcome.live_tracking = f"live tracking enabled ({reason})"
                log.info("%s: %s -- %s", outcome.serialnumber, reason, message)
            else:
                outcome.live_tracking = f"live tracking failed: {message}"
                log.warning("%s: %s, but live tracking failed: %s", outcome.serialnumber, reason, message)


def poll_once(config: Config) -> PollResult:
    """Fetch every known tracker once and persist the results.

    The write lock is taken *before* the HTTP calls so that an overlapping
    launchd tick skips without touching the API at all -- but the duckdb
    connection itself is only opened afterwards, for the write, so it is never
    held open for the duration of the fetches. The same goes for the
    live-tracking request: decided with the connection open, sent after it is
    closed, and its outcome written through a second short-lived connection.

    Raises:
        NoCredentialsError: `gtt login` has not been run.
        StoreBusyError: another poller holds the lock.

    """
    credentials = load_credentials(config.credentials_path)
    result = PollResult()
    wanted: list[tuple[DeviceOutcome, str]] = []

    with acquire_write_lock(config.db_path):
        outcomes = asyncio.run(_fetch_all(credentials, config.request_timeout))
        now = datetime.now(UTC)
        with write_connection(config.db_path) as conn:
            for outcome in outcomes:
                if outcome.ok and outcome.payload is not None:
                    outcome.new_position = write_snapshot(
                        conn, outcome.serialnumber, outcome.payload, now=now
                    )
                log_poll(
                    conn,
                    serialnumber=outcome.serialnumber,
                    ok=outcome.ok,
                    error=outcome.error,
                    new_positions=1 if outcome.new_position else 0,
                    duration_ms=outcome.duration_ms,
                    now=now,
                )
                if config.live_tracking and outcome.ok:
                    reason = _live_tracking_reason(conn, outcome.serialnumber, config, now=now)
                    if reason:
                        wanted.append((outcome, reason))
                result.outcomes.append(outcome)

        if wanted:
            _request_live_tracking(
                config, {c.serialnumber: c for c in credentials}, wanted
            )

    return result
