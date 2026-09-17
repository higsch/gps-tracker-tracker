"""End-to-end test of poll_once against a local stand-in for the tracker API.

This is the part worth testing for real: the raw response is captured through an
httpx event hook, and if that ever stops working the archive would silently store
the reparsed model instead of what the server actually sent.
"""

import json
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import fressnapftracker.fressnapftracker as upstream
import pytest

from gps_tracker_tracker.auth import DeviceCredential, save_credentials
from gps_tracker_tracker.cli import EXIT_OK, main
from gps_tracker_tracker.config import Config
from gps_tracker_tracker.poller import poll_once
from gps_tracker_tracker.store import open_store

FIXTURES = Path(__file__).parent / "fixtures"
SERIAL = "231511297"

# Mutable so a test can change what the next request returns.
_response: dict = {}
_live_response: dict = {}
_history_response: list | dict = []
_history_status = 200
_requests: list[str] = []
_live_requests: list[str] = []
_history_requests: list[str] = []

# ~222m north of the fixture's position.
_WALK_LAT = 52.522008


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        if "/positions?" in self.path:
            _history_requests.append(self.path)
            self._send_json(_history_response, status=_history_status)
            return
        _requests.append(self.path)
        self._send_json(_response)

    def do_PUT(self) -> None:  # noqa: N802
        _live_requests.append(self.path)
        self._send_json(_live_response)

    def _send_json(self, payload: dict | list, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _stamp(seconds_later: int) -> str:
    """An API timestamp `seconds_later` after the fixture's fix."""
    minute, second = divmod(30 + seconds_later, 60)
    return f"2025-12-02T20:{25 + minute:02d}:{second:02d}.000+01:00"


def _move_to(lat: float, *, seconds_later: int) -> None:
    """Make the next response report a new fix `seconds_later` after the fixture's."""
    stamp = _stamp(seconds_later)
    _response["position"] = dict(_response["position"], lat=lat, sampled_at=stamp)
    _response["last_seen_timestamp"] = stamp


def _history_row(lat: float, *, seconds_later: int, accuracy: int = 3) -> dict:
    """A row as the positions endpoint returns it: strings for the coordinates."""
    return {"lat": str(lat), "lng": "13.404954", "h_pos_error": accuracy, "created_at": _stamp(seconds_later)}


@pytest.fixture
def api_server(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Serve the fixture on localhost and point the upstream client at it."""
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/api/pet_tracker/v2"
    # _device_request looks this up as a module global at call time.
    monkeypatch.setattr(upstream, "API_BASE_URL", base_url)
    try:
        yield base_url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def config(tmp_path: Path) -> Config:
    credentials_path = tmp_path / "credentials.json"
    save_credentials(credentials_path, [DeviceCredential(serialnumber=SERIAL, token="tok-abcd")])
    return Config(
        db_path=tmp_path / "tracker.duckdb",
        credentials_path=credentials_path,
        poll_interval=300,
        request_timeout=10,
        locale="de",
        log_level="INFO",
        email=None,
        password=None,
        # These tests run at whatever hour CI happens to be; the window itself is
        # covered in test_live_tracking.py.
        live_from=None,
        live_until=None,
    )


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    _response.clear()
    _response.update(json.loads((FIXTURES / "get_tracker_response.json").read_text()))
    _live_response.clear()
    _live_response.update({"success": True, "message": "Live Tracking enabled for 10 minutes."})
    global _history_response, _history_status
    _history_response = []
    _history_status = 200
    _requests.clear()
    _live_requests.clear()
    _history_requests.clear()
    yield


def test_poll_stores_the_fix_and_the_verbatim_payload(config: Config, api_server: str) -> None:
    result = poll_once(config)

    assert result.ok
    assert result.new_positions == 1
    assert [o.serialnumber for o in result.outcomes] == [SERIAL]
    # The device token goes in the query string, not a header.
    assert "devicetoken=tok-abcd" in _requests[0]

    with open_store(config.db_path, read_only=True) as conn:
        (lat, lng) = conn.execute("SELECT lat, lng FROM positions").fetchone()
        assert (lat, lng) == (52.520008, 13.404954)

        (payload_text,) = conn.execute("SELECT payload FROM raw_snapshots").fetchone()
        stored = json.loads(payload_text)
        # Verbatim: fields the upstream model does not keep must survive, which is
        # the whole point of raw_snapshots.
        assert stored == _response
        assert "additional_parameters" in stored
        assert stored["led_activatable"]["seen_recently"] is False

        (ok, new_positions) = conn.execute(
            "SELECT ok, new_positions FROM poll_log"
        ).fetchone()
        assert (ok, new_positions) == (True, 1)


def test_second_poll_of_an_unchanged_tracker_adds_nothing(config: Config, api_server: str) -> None:
    assert poll_once(config).new_positions == 1
    second = poll_once(config)

    assert second.ok
    assert second.new_positions == 0

    with open_store(config.db_path, read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 1
        # Both attempts are still logged, so the gap is explainable.
        assert conn.execute("SELECT count(*) FROM poll_log").fetchone()[0] == 2


def test_a_moved_tracker_adds_a_fix(config: Config, api_server: str) -> None:
    poll_once(config)
    _response["position"] = dict(
        _response["position"],
        lat=52.53,
        sampled_at="2025-12-02T20:40:30.000+01:00",
    )
    _response["last_seen_timestamp"] = "2025-12-02T20:40:31.000+01:00"

    assert poll_once(config).new_positions == 1

    with open_store(config.db_path, read_only=True) as conn:
        assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 2


def test_the_first_poll_starts_live_tracking(config: Config, api_server: str) -> None:
    result = poll_once(config)

    assert result.ok
    assert len(_live_requests) == 1
    assert _live_requests[0].startswith(f"/api/pet_tracker/v2/devices/{SERIAL}/enable_live_tracking")
    assert "devicetoken=tok-abcd" in _live_requests[0]
    (outcome,) = result.outcomes
    assert outcome.live_tracking == "live tracking enabled (start)"

    with open_store(config.db_path, read_only=True) as conn:
        (ok, message, reason) = conn.execute(
            "SELECT ok, message, reason FROM live_tracking_log"
        ).fetchone()
        assert ok is True
        assert "10 minutes" in message
        assert reason == "start"


def test_live_tracking_is_requested_even_without_a_new_fix(config: Config, api_server: str) -> None:
    _response["position"] = None

    result = poll_once(config)

    assert result.ok
    assert result.new_positions == 0
    assert len(_live_requests) == 1


def test_live_tracking_is_not_re_requested_while_it_is_running(
    config: Config, api_server: str
) -> None:
    poll_once(config)
    assert len(_live_requests) == 1

    # A new fix 20s later, but the ten-minute window has barely begun.
    _move_to(_WALK_LAT, seconds_later=20)
    result = poll_once(config)

    assert result.new_positions == 1
    assert len(_live_requests) == 1
    assert result.outcomes[0].live_tracking is None


def test_a_refused_live_tracking_request_is_recorded_not_fatal(
    config: Config, api_server: str
) -> None:
    _live_response.clear()
    _live_response["error"] = "Something else went wrong"

    result = poll_once(config)

    # The fix itself is still archived; only the extra request failed.
    assert result.ok
    assert result.new_positions == 1
    assert result.outcomes[0].live_tracking.startswith("live tracking failed:")
    with open_store(config.db_path, read_only=True) as conn:
        (ok, message) = conn.execute("SELECT ok, message FROM live_tracking_log").fetchone()
        assert ok is False
        assert "Something else went wrong" in message


def test_a_failed_fetch_does_not_request_live_tracking(config: Config, api_server: str) -> None:
    _response.clear()
    _response["error"] = "broken"

    result = poll_once(config)

    assert not result.ok
    assert _live_requests == []


def test_live_tracking_is_not_requested_outside_the_live_hours(
    config: Config, api_server: str
) -> None:
    # A one-hour window that started two hours ago, whatever the hour is now.
    start = (datetime.now(UTC) - timedelta(hours=2)).time().replace(second=0, microsecond=0)
    end = time((start.hour + 1) % 24, start.minute)
    config = replace(config, live_from=start, live_until=end)

    assert poll_once(config).new_positions == 1
    assert _live_requests == []


def test_live_tracking_can_be_switched_off(config: Config, api_server: str) -> None:
    config = replace(config, live_tracking=False)

    assert poll_once(config).new_positions == 1
    assert _live_requests == []


def test_history_is_requested_for_a_full_day_when_the_archive_is_empty(
    config: Config, api_server: str
) -> None:
    poll_once(config)

    assert len(_history_requests) == 1
    assert _history_requests[0].startswith(f"/api/pet_tracker/v2/devices/{SERIAL}/positions?")
    assert "devicetoken=tok-abcd" in _history_requests[0]
    assert "hours_ago=24" in _history_requests[0]
    # Every fix, never the server-side thinning.
    assert "sample=false" in _history_requests[0]


def test_history_fixes_fill_the_gaps_and_the_live_fix_keeps_its_row(
    config: Config, api_server: str
) -> None:
    global _history_response
    _history_response = [
        _history_row(52.520008, seconds_later=0, accuracy=99),  # same fix as the payload
        _history_row(52.5203, seconds_later=20),
        _history_row(52.5206, seconds_later=40),
    ]

    result = poll_once(config)

    assert result.ok
    (outcome,) = result.outcomes
    assert outcome.new_position is True
    assert outcome.backfilled == 2
    assert result.new_positions == 3

    with open_store(config.db_path, read_only=True) as conn:
        rows = conn.execute(
            "SELECT lat, accuracy, battery FROM positions ORDER BY sampled_at"
        ).fetchall()
        # The live payload's row wins: its accuracy and battery survive the duplicate.
        assert rows[0] == (52.520008, 10, 85)
        assert rows[1:] == [(52.5203, 3, None), (52.5206, 3, None)]
        (new_positions,) = conn.execute("SELECT new_positions FROM poll_log").fetchone()
        assert new_positions == 3

    # Re-polling the same history adds nothing.
    assert poll_once(config).new_positions == 0


def test_a_failed_history_fetch_does_not_fail_the_poll(config: Config, api_server: str) -> None:
    global _history_response, _history_status
    _history_response = {"error": "hours_ago out of range"}
    _history_status = 400

    result = poll_once(config)

    assert result.ok
    assert result.new_positions == 1
    (outcome,) = result.outcomes
    assert outcome.backfilled == 0
    assert outcome.history_error is not None
    assert "hours_ago out of range" in outcome.history_error
    # The error body must not be mistaken for the tracker payload.
    assert outcome.payload == _response
    with open_store(config.db_path, read_only=True) as conn:
        (payload_text,) = conn.execute("SELECT payload FROM raw_snapshots").fetchone()
        assert json.loads(payload_text) == _response


def test_backfill_can_be_switched_off(config: Config, api_server: str) -> None:
    config = replace(config, backfill=False)

    assert poll_once(config).new_positions == 1
    assert _history_requests == []


def test_watch_runs_the_requested_number_of_polls(
    config: Config, api_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTT_DB_PATH", str(config.db_path))
    monkeypatch.setenv("GTT_CREDENTIALS_PATH", str(config.credentials_path))

    assert main(["watch", "--interval", "1", "--max-iterations", "2"]) == EXIT_OK

    with open_store(config.db_path, read_only=True) as conn:
        # Two attempts logged, but the tracker never moved, so only one fix.
        assert conn.execute("SELECT count(*) FROM poll_log").fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 1


def test_poll_skips_cleanly_when_another_process_holds_the_lock(
    config: Config, api_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GTT_DB_PATH", str(config.db_path))
    monkeypatch.setenv("GTT_CREDENTIALS_PATH", str(config.credentials_path))
    poll_once(config)  # create the database

    with open_store(config.db_path):
        # An overlapping launchd tick must not be an error, and must not have
        # bothered the API at all.
        requests_before = len(_requests)
        assert main(["poll"]) == EXIT_OK
        assert len(_requests) == requests_before


def test_device_error_is_recorded_not_raised(config: Config, api_server: str) -> None:
    _response.clear()
    _response["error"] = "Invalid devicetoken"

    result = poll_once(config)

    assert not result.ok
    assert result.auth_failure
    with open_store(config.db_path, read_only=True) as conn:
        (ok, error) = conn.execute("SELECT ok, error FROM poll_log").fetchone()
        assert ok is False
        assert "devicetoken" in error.lower()
        assert conn.execute("SELECT count(*) FROM positions").fetchone()[0] == 0
