"""End-to-end test of poll_once against a local stand-in for the tracker API.

This is the part worth testing for real: the raw response is captured through an
httpx event hook, and if that ever stops working the archive would silently store
the reparsed model instead of what the server actually sent.
"""

import json
import threading
from collections.abc import Iterator
from dataclasses import replace
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
_requests: list[str] = []
_live_requests: list[str] = []

# ~222m north of the fixture's position: unmistakably a walk.
_WALK_LAT = 52.522008
# ~11m: within the 30m default, i.e. GPS jitter on a sleeping pet.
_JITTER_LAT = 52.520108


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's naming
        _requests.append(self.path)
        self._send_json(_response)

    def do_PUT(self) -> None:  # noqa: N802
        _live_requests.append(self.path)
        self._send_json(_live_response)

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def _move_to(lat: float, *, seconds_later: int) -> None:
    """Make the next response report a new fix `seconds_later` after the fixture's."""
    minute, second = divmod(30 + seconds_later, 60)
    stamp = f"2025-12-02T20:{25 + minute:02d}:{second:02d}.000+01:00"
    _response["position"] = dict(_response["position"], lat=lat, sampled_at=stamp)
    _response["last_seen_timestamp"] = stamp


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
    )


@pytest.fixture(autouse=True)
def reset_state() -> Iterator[None]:
    _response.clear()
    _response.update(json.loads((FIXTURES / "get_tracker_response.json").read_text()))
    _live_response.clear()
    _live_response.update({"success": True, "message": "Live Tracking enabled for 10 minutes."})
    _requests.clear()
    _live_requests.clear()
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


def test_the_first_fix_alone_does_not_start_live_tracking(config: Config, api_server: str) -> None:
    poll_once(config)

    # One fix says nothing about motion, and a stationary re-poll adds no fix at all.
    poll_once(config)
    assert _live_requests == []


def test_a_walking_tracker_gets_live_tracking(config: Config, api_server: str) -> None:
    poll_once(config)
    _move_to(_WALK_LAT, seconds_later=20)

    result = poll_once(config)

    assert result.ok
    assert len(_live_requests) == 1
    assert _live_requests[0].startswith(f"/api/pet_tracker/v2/devices/{SERIAL}/enable_live_tracking")
    assert "devicetoken=tok-abcd" in _live_requests[0]
    (outcome,) = result.outcomes
    assert outcome.live_tracking is not None
    assert outcome.live_tracking.startswith("live tracking enabled (moved 222m in 20s)")

    with open_store(config.db_path, read_only=True) as conn:
        (ok, message, reason) = conn.execute(
            "SELECT ok, message, reason FROM live_tracking_log"
        ).fetchone()
        assert ok is True
        assert "10 minutes" in message
        assert reason == "moved 222m in 20s"


def test_gps_jitter_is_not_motion(config: Config, api_server: str) -> None:
    poll_once(config)
    _move_to(_JITTER_LAT, seconds_later=20)

    result = poll_once(config)

    assert result.new_positions == 1
    assert _live_requests == []
    assert result.outcomes[0].live_tracking is None


def test_live_tracking_is_not_re_requested_while_it_is_running(
    config: Config, api_server: str
) -> None:
    poll_once(config)
    _move_to(_WALK_LAT, seconds_later=20)
    poll_once(config)
    assert len(_live_requests) == 1

    # Still walking 20s later, but the ten-minute window has barely begun.
    _move_to(_WALK_LAT + 0.002, seconds_later=40)
    result = poll_once(config)

    assert result.new_positions == 1
    assert len(_live_requests) == 1
    assert result.outcomes[0].live_tracking is None


def test_a_refused_live_tracking_request_is_recorded_not_fatal(
    config: Config, api_server: str
) -> None:
    _live_response.clear()
    _live_response["error"] = "Something else went wrong"
    poll_once(config)
    _move_to(_WALK_LAT, seconds_later=20)

    result = poll_once(config)

    # The fix itself is still archived; only the extra request failed.
    assert result.ok
    assert result.new_positions == 1
    assert result.outcomes[0].live_tracking.startswith("live tracking failed:")
    with open_store(config.db_path, read_only=True) as conn:
        (ok, message) = conn.execute("SELECT ok, message FROM live_tracking_log").fetchone()
        assert ok is False
        assert "Something else went wrong" in message


def test_live_tracking_can_be_switched_off(config: Config, api_server: str) -> None:
    config = replace(config, live_tracking=False)
    poll_once(config)
    _move_to(_WALK_LAT, seconds_later=20)

    assert poll_once(config).new_positions == 1
    assert _live_requests == []


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
