"""Keeping the tracker in live mode.

Left alone, the tracker reports a fix roughly every ten minutes while it sits
still and in bursts of one every ~20s while it moves. The Fressnapf app's
"Live-Tracking" button is a single call --

    PUT /devices/{serial}/enable_live_tracking?devicetoken=...

-- which puts the device into a 10-minute live mode of a fix every ~20s. There
is no disable call; the mode simply expires.

Whether the poller keeps that mode running is a switch, `LiveSwitch`, flipped
through the control API (see api.py) and kept in a small JSON file next to the
database. While it is on, the poller requests live mode on the first poll and
again shortly before every ten-minute grant runs out. Switching it off only
stops the renewals, so live mode dies on its own within ten minutes. An
activation can carry an end time; past it the switch counts as off. Live mode
drains a full battery in about ten hours.

The switch is a file rather than a table because the poller holds the
database's exclusive lock for the whole of a poll, HTTP calls included: the API
could not write to DuckDB without waiting for it, and a one-shot `gtt poll`
from launchd has to see the switch too.

The route was taken from the app's bundle; the upstream client does not know it.
"""

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import fressnapftracker.fressnapftracker as upstream
from fressnapftracker import ApiClient
from fressnapftracker.exceptions import FressnapfTrackerError

log = logging.getLogger(__name__)

# The server grants ten minutes per request. Re-arm two minutes early so a poll
# interval of up to ~90s, plus the request itself, never leaves a gap.
LIVE_TRACKING_DURATION = timedelta(minutes=10)
RENEW_AFTER = timedelta(minutes=8)


@dataclass(frozen=True, slots=True)
class LiveSwitch:
    """Whether live mode should be kept running, and until when."""

    enabled: bool = False
    # None while enabled means until switched off.
    until: datetime | None = None
    changed_at: datetime | None = None

    def active(self, now: datetime) -> bool:
        return self.enabled and (self.until is None or now < self.until)


def _parse_time(text: str) -> datetime:
    """An ISO timestamp, always timezone-aware.

    The API writes an offset, but a hand-edited file may not. A time without
    one is taken as local time -- the host's, or the container's TZ -- since
    that is what a person typing it means. Left naive, it could not be compared
    with the aware clock and would crash every reader of the switch.
    """
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo is not None else parsed.astimezone()


def load_switch(path: Path) -> LiveSwitch:
    """The stored switch; off if it was never set or the file is unreadable."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        until = data.get("until")
        changed_at = data.get("changed_at")
        return LiveSwitch(
            enabled=data["enabled"] is True,
            until=_parse_time(until) if until else None,
            changed_at=_parse_time(changed_at) if changed_at else None,
        )
    except FileNotFoundError:
        return LiveSwitch()
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        # Failing safe: a broken file must not leave the battery draining.
        log.warning("ignoring unreadable live-tracking switch %s: %s", path, exc)
        return LiveSwitch()


def save_switch(path: Path, switch: LiveSwitch) -> None:
    """Write the switch atomically, so a concurrent reader never sees half a file."""
    data = {
        "enabled": switch.enabled,
        "until": switch.until.isoformat() if switch.until else None,
        "changed_at": switch.changed_at.isoformat() if switch.changed_at else None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def live_tracking_due(last_requested_at: datetime | None, *, now: datetime) -> bool:
    """Whether a request now would start or extend live mode, rather than be a no-op."""
    return last_requested_at is None or now - last_requested_at >= RENEW_AFTER


def live_tracking_reason(last_requested_at: datetime | None, *, now: datetime) -> str | None:
    """Why to request live mode now, or None while the current window still has time.

    "start" when no window is running any more (or none was ever requested),
    "renew" when the running one is about to expire. The distinction is only
    for the log; both send the same request.
    """
    if not live_tracking_due(last_requested_at, now=now):
        return None
    if last_requested_at is None or now - last_requested_at >= LIVE_TRACKING_DURATION:
        return "start"
    return "renew"


class LiveTrackingClient(ApiClient):
    """The upstream device client plus the calls it is missing."""

    async def get_positions(self, *, hours_ago: int, sample: bool = False) -> list[dict[str, Any]]:
        """The fixes of the last `hours_ago` hours (at most 24), oldest first.

        Each is `{"lat": "53.45", "lng": "9.94", "h_pos_error": 2, "created_at": ...}`
        -- coordinates as strings, accuracy in metres. `sample=True` asks the
        server to thin the list to roughly one fix a minute; the poller never
        does, it wants every fix. See history.py.
        """
        # Built by hand rather than through _device_request so that the extra
        # query parameters travel alongside devicetoken. API_BASE_URL is read at
        # call time on purpose: the tests point it at a local server.
        url = f"{upstream.API_BASE_URL}/devices/{self._serial_number}/positions"
        params = {
            "devicetoken": self._device_token,
            "hours_ago": hours_ago,
            "sample": "true" if sample else "false",
        }
        result = await self._request("GET", url, self._get_device_headers(), params=params)
        if isinstance(result, dict):
            # {"error": ...} for a bad hours_ago or token; anything else is unexpected.
            self._handle_device_error(result)
            raise FressnapfTrackerError(f"unexpected positions response: {result!r}")
        return result

    async def enable_live_tracking(self) -> str:
        """Ask the server to put the device into live mode. Returns its message."""
        # Same base URL, headers, `devicetoken` query and error mapping as
        # get_tracker(); the route is the only thing upstream does not know.
        result = await self._device_request("PUT", "/enable_live_tracking")
        if isinstance(result, dict) and result.get("success") is False:
            raise FressnapfTrackerError(result.get("message") or "live tracking refused")
        message = result.get("message") if isinstance(result, dict) else None
        return message or "live tracking enabled"
