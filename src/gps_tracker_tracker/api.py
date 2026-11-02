"""The live-tracking control API, served by `gtt watch`.

    GET  /devices                   live-tracking status, and every tracker's
                                    latest state, trail and heatmap
    GET  /live-tracking             the switch, and how long each tracker stays live
    POST /live-tracking/activate    switch on; optional body {"minutes": 60}
    POST /live-tracking/deactivate  switch off

Every request needs `Authorization: Bearer $GTT_API_TOKEN`.

`/devices` answers `{"live_tracking": <status document>, "devices": [...]}`,
so a dashboard needs one request per refresh. It takes `trail_hours` (default 6), `heatmap_days` (default 7, 0 for
none) and `max_accuracy` in metres (default 10, 0 to keep every fix), see
`store.device_overview`. It answers 503 with Retry-After while a poll keeps
the database busy for longer than a few seconds. It exists so other apps never
open the DuckDB file themselves: a reader in another process bypasses the
poller's lock and can make its writes fail.

The three live-tracking routes answer with the status document alone:

    {
      "enabled": true,
      "until": "2026-11-02T19:00:00+00:00",     # null: until deactivated
      "changed_at": "2026-11-02T18:00:00+00:00",
      "devices": [
        {"serialnumber": "231511297", "live_until": "2026-11-02T18:10:02+00:00"}
      ]
    }

The API only flips the switch (see live_tracking.py); the poller does the
actual requesting, so there is one place that talks to the tracker and logs
it. Activating wakes the watch loop so live mode starts within a second rather
than at the next scheduled poll. Deactivating cannot stop the tracker -- the
Fressnapf API has no off call -- it stops the renewals, and `live_until` shows
when the last grant runs out. An activation with an end time may overrun it by
up to one grant, ten minutes, for the same reason.

Its `devices` is read from the database. The poller holds its lock for the
whole of a poll, so the read waits for that and gives up after a few seconds,
in which case `devices` is null; the switch itself never waits.

Standard library only: this is three routes, and the Pi it runs on should not
need another dependency.
"""

import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, TypeVar
from urllib.parse import parse_qs

from .config import Config
from .live_tracking import LIVE_TRACKING_DURATION, LiveSwitch, load_switch, save_switch
from .store import (
    DatabaseMissingError,
    StoreBusyError,
    device_overview,
    last_live_tracking_grants,
    open_store,
)

log = logging.getLogger(__name__)

_MAX_BODY = 4096
# How long a status read waits for a running poll to release the database.
_READ_RETRY_FOR = 5.0
_READ_RETRY_EVERY = 0.25

# /devices query parameters: (default, smallest, largest).
_DEVICE_PARAMS = {
    "trail_hours": (6, 1, 24 * 31),
    "heatmap_days": (7, 0, 366),
    "max_accuracy": (10, 0, 100_000),
}

T = TypeVar("T")


class ControlServer(ThreadingHTTPServer):
    """The HTTP server, carrying what the handlers need."""

    daemon_threads = True

    def __init__(self, config: Config, on_activate: Callable[[], None]) -> None:
        if not config.api_token:
            raise ValueError("the control API needs GTT_API_TOKEN")
        self.config = config
        self.on_activate = on_activate
        # Serialises read-modify-write of the switch across handler threads.
        self.switch_lock = threading.Lock()
        super().__init__((config.api_host, config.api_port), _Handler)


def start_api_server(config: Config, *, on_activate: Callable[[], None]) -> ControlServer:
    """Bind the control API and serve it on a daemon thread.

    Stop it with `server.shutdown()` then `server.server_close()`.

    Raises:
        OSError: the address could not be bound.

    """
    server = ControlServer(config, on_activate)
    thread = threading.Thread(target=server.serve_forever, name="gtt-api", daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    log.info("live-tracking API listening on http://%s:%d/live-tracking", host, port)
    return server


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value else None


def _read(config: Config, query: Callable[[Any], T], *, empty: T) -> T:
    """Run a read-only query, waiting out a running poll for a few seconds.

    `empty` is the answer before the first poll has created the database.

    Raises:
        StoreBusyError: the database was still busy at the deadline.

    """
    deadline = time.monotonic() + _READ_RETRY_FOR
    while True:
        try:
            with open_store(config.db_path, read_only=True) as conn:
                return query(conn)
        except DatabaseMissingError:
            return empty
        except StoreBusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_READ_RETRY_EVERY)


def _device_grants(config: Config) -> dict[str, datetime] | None:
    """Last successful live-mode request per tracker, or None if the db stayed busy."""
    try:
        return _read(config, last_live_tracking_grants, empty={})
    except StoreBusyError:
        log.info("database busy, answering live-tracking status without devices")
        return None


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return _iso(value) or ""
    raise TypeError(f"{type(value).__name__} is not JSON serialisable")


def status(config: Config, switch: LiveSwitch, *, now: datetime) -> dict[str, Any]:
    """The live-tracking status document, reading the grants from the database."""
    return _status_document(switch, _device_grants(config), now=now)


def _status_document(
    switch: LiveSwitch, grants: dict[str, datetime] | None, *, now: datetime
) -> dict[str, Any]:
    active = switch.active(now)
    devices = None
    if grants is not None:
        devices = []
        for serialnumber, requested_at in sorted(grants.items()):
            live_until = requested_at + LIVE_TRACKING_DURATION
            devices.append(
                {"serialnumber": serialnumber, "live_until": _iso(live_until) if live_until > now else None}
            )
    return {
        "enabled": active,
        "until": _iso(switch.until) if active else None,
        "changed_at": _iso(switch.changed_at),
        "devices": devices,
    }


class _BadRequest(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class _Handler(BaseHTTPRequestHandler):
    server: ControlServer
    protocol_version = "HTTP/1.1"
    server_version = "gps-tracker-tracker"

    # ------------------------------------------------------------- routing --

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        routes: dict[str, dict[str, Callable[[], Any]]] = {
            "/devices": {"GET": self._get_devices},
            "/live-tracking": {"GET": self._get_status},
            "/live-tracking/activate": {"POST": self._activate},
            "/live-tracking/deactivate": {"POST": self._deactivate},
        }
        path, _, query = self.path.partition("?")
        path = path.rstrip("/") or "/"
        self._query = parse_qs(query)
        try:
            if not self._authorized():
                raise _BadRequest(HTTPStatus.UNAUTHORIZED, "missing or wrong bearer token")
            if path not in routes:
                raise _BadRequest(HTTPStatus.NOT_FOUND, f"no such route: {path}")
            action = routes[path].get(method)
            if action is None:
                allowed = ", ".join(routes[path])
                raise _BadRequest(HTTPStatus.METHOD_NOT_ALLOWED, f"{path} takes {allowed}")
            self._send(HTTPStatus.OK, action())
        except _BadRequest as exc:
            self._send(exc.status, {"error": str(exc)})
        except Exception:
            log.exception("live-tracking API: %s %s failed", method, path)
            self._send(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal error"})

    def _authorized(self) -> bool:
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        expected = self.server.config.api_token or ""
        return (
            scheme.lower() == "bearer"
            and bool(expected)
            and hmac.compare_digest(token.strip().encode(), expected.encode())
        )

    # ------------------------------------------------------------- actions --

    def _get_devices(self) -> dict[str, Any]:
        values = {}
        for name, (default, low, high) in _DEVICE_PARAMS.items():
            raw = self._query.get(name, [None])[-1]
            if raw is None or raw == "":
                values[name] = default
                continue
            if not raw.isdigit() or not low <= int(raw) <= high:
                raise _BadRequest(
                    HTTPStatus.BAD_REQUEST, f"{name} must be a whole number from {low} to {high}"
                )
            values[name] = int(raw)
        unknown = set(self._query) - set(_DEVICE_PARAMS)
        if unknown:
            raise _BadRequest(HTTPStatus.BAD_REQUEST, f"unknown parameters: {', '.join(sorted(unknown))}")

        now = datetime.now(UTC)
        trail_since = now - timedelta(hours=values["trail_hours"])
        heatmap_since = now - timedelta(days=values["heatmap_days"]) if values["heatmap_days"] else None
        switch = load_switch(self.server.config.live_switch_path)
        try:
            # One connection for both, so the trail and the grants agree.
            overview, grants = _read(
                self.server.config,
                lambda conn: (
                    device_overview(
                        conn,
                        trail_since=trail_since,
                        heatmap_since=heatmap_since,
                        max_accuracy=values["max_accuracy"] or None,
                    ),
                    last_live_tracking_grants(conn),
                ),
                empty=([], {}),
            )
        except StoreBusyError as exc:
            raise _BadRequest(HTTPStatus.SERVICE_UNAVAILABLE, "database busy with a poll, retry shortly") from exc
        return {
            "live_tracking": _status_document(switch, grants, now=now),
            "devices": overview,
        }

    def _get_status(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        return status(self.server.config, load_switch(self.server.config.live_switch_path), now=now)

    def _activate(self) -> dict[str, Any]:
        body = self._json_body()
        minutes = body.get("minutes")
        limit = self.server.config.live_max_minutes
        if minutes is not None and (
            isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= limit
        ):
            raise _BadRequest(
                HTTPStatus.BAD_REQUEST, f"minutes must be a whole number from 1 to {limit}"
            )
        unknown = set(body) - {"minutes"}
        if unknown:
            raise _BadRequest(HTTPStatus.BAD_REQUEST, f"unknown fields: {', '.join(sorted(unknown))}")
        now = datetime.now(UTC)
        until = now + timedelta(minutes=minutes) if minutes else None
        switch = LiveSwitch(enabled=True, until=until, changed_at=now)
        with self.server.switch_lock:
            save_switch(self.server.config.live_switch_path, switch)
        log.info("live tracking switched on %s", f"until {_iso(until)}" if until else "until switched off")
        # Read the status before waking the poller, or the woken poll would
        # find the database held by this very read and skip.
        response = status(self.server.config, switch, now=now)
        self.server.on_activate()
        return response

    def _deactivate(self) -> dict[str, Any]:
        self._json_body()  # drain and validate, so the connection stays usable
        now = datetime.now(UTC)
        switch = LiveSwitch(enabled=False, changed_at=now)
        with self.server.switch_lock:
            save_switch(self.server.config.live_switch_path, switch)
        log.info("live tracking switched off; the current grant runs out on its own")
        return status(self.server.config, switch, now=now)

    # ------------------------------------------------------------- plumbing --

    def _json_body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise _BadRequest(HTTPStatus.BAD_REQUEST, "bad Content-Length") from exc
        if length > _MAX_BODY:
            self.close_connection = True
            raise _BadRequest(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            return {}
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise _BadRequest(HTTPStatus.BAD_REQUEST, "body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise _BadRequest(HTTPStatus.BAD_REQUEST, "body must be a JSON object")
        return body

    def _send(self, status_code: HTTPStatus, payload: Any) -> None:
        body = (json.dumps(payload, default=_json_default) + "\n").encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if status_code == HTTPStatus.UNAUTHORIZED:
            self.send_header("WWW-Authenticate", 'Bearer realm="gps-tracker-tracker"')
        if status_code == HTTPStatus.SERVICE_UNAVAILABLE:
            self.send_header("Retry-After", "2")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # The request line only; the token travels in a header and never lands here.
        log.debug("%s - %s", self.address_string(), format % args)
