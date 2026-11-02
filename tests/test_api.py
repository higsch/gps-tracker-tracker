"""The live-tracking control API, against a real server on a free port."""

import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import gps_tracker_tracker.api as api
from gps_tracker_tracker.api import start_api_server
from gps_tracker_tracker.config import Config
from gps_tracker_tracker.live_tracking import LiveSwitch, load_switch, save_switch
from gps_tracker_tracker.store import acquire_write_lock, log_live_tracking, open_store

TOKEN = "test-token"
SERIAL = "231511297"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return Config(
        db_path=tmp_path / "tracker.duckdb",
        credentials_path=tmp_path / "credentials.json",
        poll_interval=30,
        request_timeout=10,
        locale="de",
        log_level="INFO",
        email=None,
        password=None,
        api_token=TOKEN,
        api_host="127.0.0.1",
        api_port=0,  # any free port
        live_max_minutes=120,
    )


@pytest.fixture
def woken() -> threading.Event:
    return threading.Event()


@pytest.fixture
def client(config: Config, woken: threading.Event) -> Iterator[httpx.Client]:
    server = start_api_server(config, on_activate=woken.set)
    host, port = server.server_address[:2]
    try:
        with httpx.Client(
            base_url=f"http://{host}:{port}", headers={"Authorization": f"Bearer {TOKEN}"}
        ) as http:
            yield http
    finally:
        server.shutdown()
        server.server_close()


def test_every_route_needs_the_token(client: httpx.Client) -> None:
    for headers in ({"Authorization": ""}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}):
        response = client.get("/live-tracking", headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"].startswith("Bearer")
    assert client.post("/live-tracking/activate", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_live_tracking_starts_switched_off(client: httpx.Client) -> None:
    response = client.get("/live-tracking")

    assert response.status_code == 200
    assert response.json() == {"enabled": False, "until": None, "changed_at": None, "devices": []}


def test_activating_without_a_duration_lasts_until_deactivated(
    client: httpx.Client, config: Config, woken: threading.Event
) -> None:
    response = client.post("/live-tracking/activate")

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["until"] is None
    assert body["changed_at"] is not None
    switch = load_switch(config.live_switch_path)
    assert switch.enabled and switch.until is None
    # The watch loop is told, so live mode starts now rather than next poll.
    assert woken.is_set()
    assert client.get("/live-tracking").json()["enabled"] is True


def test_activating_for_a_number_of_minutes(client: httpx.Client, config: Config) -> None:
    before = datetime.now(UTC)
    body = client.post("/live-tracking/activate", json={"minutes": 45}).json()
    after = datetime.now(UTC)

    until = datetime.fromisoformat(body["until"])
    assert before + timedelta(minutes=45) <= until <= after + timedelta(minutes=45)
    assert load_switch(config.live_switch_path).until == until


@pytest.mark.parametrize(
    "body", [{"minutes": 0}, {"minutes": 121}, {"minutes": "30"}, {"minutes": 1.5}, {"minutes": True}, {"hours": 1}]
)
def test_bad_activations_are_refused_and_change_nothing(
    client: httpx.Client, config: Config, woken: threading.Event, body: dict
) -> None:
    response = client.post("/live-tracking/activate", json=body)

    assert response.status_code == 400
    assert "error" in response.json()
    assert not config.live_switch_path.exists()
    assert not woken.is_set()


def test_a_body_that_is_not_a_json_object_is_refused(client: httpx.Client) -> None:
    for content in (b"{nope", b"[1]"):
        response = client.post(
            "/live-tracking/activate", content=content, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 400


def test_deactivating_switches_off_but_reports_the_running_grant(
    client: httpx.Client, config: Config
) -> None:
    client.post("/live-tracking/activate")
    granted = datetime.now(UTC) - timedelta(minutes=3)
    with open_store(config.db_path) as conn:
        log_live_tracking(conn, serialnumber=SERIAL, ok=True, message="ok", reason="start", now=granted)

    body = client.post("/live-tracking/deactivate").json()

    assert body["enabled"] is False
    assert load_switch(config.live_switch_path).enabled is False
    # There is no off call: the tracker stays live until the grant runs out.
    (device,) = body["devices"]
    assert device["serialnumber"] == SERIAL
    assert datetime.fromisoformat(device["live_until"]) == granted + timedelta(minutes=10)


def test_an_expired_grant_or_activation_is_reported_as_over(client: httpx.Client, config: Config) -> None:
    save_switch(
        config.live_switch_path,
        LiveSwitch(enabled=True, until=datetime.now(UTC) - timedelta(seconds=1)),
    )
    with open_store(config.db_path) as conn:
        log_live_tracking(
            conn, serialnumber=SERIAL, ok=True, message="ok", reason="start",
            now=datetime.now(UTC) - timedelta(minutes=30),
        )

    body = client.get("/live-tracking").json()

    assert body["enabled"] is False
    assert body["until"] is None
    assert body["devices"] == [{"serialnumber": SERIAL, "live_until": None}]


def test_unknown_routes_and_methods(client: httpx.Client) -> None:
    assert client.get("/nope").status_code == 404
    assert client.get("/live-tracking/activate").status_code == 405
    assert client.delete("/live-tracking").status_code == 405


def test_the_server_refuses_to_start_without_a_token(config: Config) -> None:
    with pytest.raises(ValueError, match="GTT_API_TOKEN"):
        start_api_server(replace(config, api_token=None), on_activate=lambda: None)


# --------------------------------------------------------------------------- #
# /devices
# --------------------------------------------------------------------------- #

def _seed(config: Config, now: datetime) -> None:
    """One tracker: two state reports, and fixes spread over the last week."""
    with open_store(config.db_path) as conn:
        conn.execute("INSERT INTO devices (serialnumber, name) VALUES (?, 'Minka')", [SERIAL])
        for minutes_ago, battery, charging in ((90, 80, False), (5, 76, True)):
            conn.execute(
                """
                INSERT INTO device_state (serialnumber, observed_at, battery, charging, inside_geofence, ingested_at)
                VALUES (?, ?, ?, ?, true, ?)
                """,
                [SERIAL, now - timedelta(minutes=minutes_ago), battery, charging, now],
            )
        fixes = [
            (timedelta(days=8), 1.0, 5),     # older than the heatmap
            (timedelta(days=2), 2.0, 5),     # heatmap
            (timedelta(days=1), 3.0, 400),   # heatmap, but a cell-tower fix
            (timedelta(hours=3), 4.0, None), # trail, unknown accuracy is kept
            (timedelta(hours=2), 5.0, 50),   # trail, but too imprecise
            (timedelta(minutes=10), 6.0, 3), # trail, the current fix
        ]
        for ago, lat, accuracy in fixes:
            conn.execute(
                "INSERT INTO positions (serialnumber, sampled_at, lat, lng, accuracy, ingested_at) VALUES (?, ?, ?, 13.4, ?, ?)",
                [SERIAL, now - ago, lat, accuracy, now],
            )


def test_devices_is_empty_before_the_first_poll(client: httpx.Client) -> None:
    response = client.get("/devices")

    assert response.status_code == 200
    assert response.json() == {
        "live_tracking": {"enabled": False, "until": None, "changed_at": None, "devices": []},
        "devices": [],
    }


def test_devices_splits_trail_and_heatmap_and_drops_imprecise_fixes(
    client: httpx.Client, config: Config
) -> None:
    _seed(config, datetime.now(UTC))

    (device,) = client.get("/devices").json()["devices"]

    assert device["serialnumber"] == SERIAL
    assert device["name"] == "Minka"
    # The latest state report, not the first.
    assert (device["battery"], device["charging"], device["inside_geofence"]) == (76, True, True)
    assert [fix["lat"] for fix in device["trail"]] == [4.0, 6.0]
    assert device["trail"][0]["accuracy"] is None
    assert device["current"] == device["trail"][-1]
    datetime.fromisoformat(device["current"]["sampled_at"])  # an ISO timestamp
    assert device["heatmap"] == [{"lat": 2.0, "lng": 13.4}]


def test_devices_takes_its_windows_and_accuracy_from_the_query(
    client: httpx.Client, config: Config
) -> None:
    _seed(config, datetime.now(UTC))

    (device,) = client.get("/devices", params={"trail_hours": 48, "heatmap_days": 0, "max_accuracy": 0}).json()["devices"]

    assert [fix["lat"] for fix in device["trail"]] == [3.0, 4.0, 5.0, 6.0]
    assert device["heatmap"] == []


def test_a_tracker_without_fixes_has_an_empty_trail(client: httpx.Client, config: Config) -> None:
    with open_store(config.db_path) as conn:
        conn.execute("INSERT INTO devices (serialnumber) VALUES (?)", [SERIAL])

    (device,) = client.get("/devices").json()["devices"]

    assert device["trail"] == [] and device["current"] is None and device["battery"] is None


@pytest.mark.parametrize(
    "params", [{"trail_hours": "0"}, {"trail_hours": "-1"}, {"heatmap_days": "x"}, {"max_accuracy": "1.5"}, {"limit": "5"}]
)
def test_bad_device_queries_are_refused(client: httpx.Client, params: dict) -> None:
    assert client.get("/devices", params=params).status_code == 400


def test_devices_answers_503_while_a_poll_holds_the_database(
    client: httpx.Client, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api, "_READ_RETRY_FOR", 0.3)
    _seed(config, datetime.now(UTC))

    with acquire_write_lock(config.db_path):
        response = client.get("/devices")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "2"


def test_devices_carries_the_same_live_tracking_status_as_its_own_route(
    client: httpx.Client, config: Config
) -> None:
    _seed(config, datetime.now(UTC))
    granted = datetime.now(UTC) - timedelta(minutes=2)
    with open_store(config.db_path) as conn:
        log_live_tracking(conn, serialnumber=SERIAL, ok=True, message="ok", reason="start", now=granted)
    activated = client.post("/live-tracking/activate", json={"minutes": 30}).json()

    live = client.get("/devices").json()["live_tracking"]

    assert live == activated == client.get("/live-tracking").json()
    assert live["enabled"] is True
    assert datetime.fromisoformat(live["devices"][0]["live_until"]) == granted + timedelta(minutes=10)
