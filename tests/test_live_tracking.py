"""The keep-live-mode-running rule, in isolation.

The end-to-end poller tests cover the wiring; the timing dimension -- what
happens eight or ten minutes into a live window -- cannot be driven through
`poll_once` without waiting, so it is exercised here with an injected clock.
"""

import json
import time as time_module
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from gps_tracker_tracker.config import Config
from gps_tracker_tracker.live_tracking import (
    LIVE_TRACKING_DURATION,
    RENEW_AFTER,
    LiveSwitch,
    live_tracking_due,
    live_tracking_reason,
    load_switch,
    save_switch,
)
from gps_tracker_tracker.poller import _live_tracking_reason
from gps_tracker_tracker.store import log_live_tracking, open_store

T0 = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)


def test_live_tracking_is_due_when_never_requested_or_when_the_window_is_nearly_over() -> None:
    now = T0 + timedelta(minutes=30)
    assert live_tracking_due(None, now=now)
    assert not live_tracking_due(now - timedelta(minutes=5), now=now)
    assert live_tracking_due(now - RENEW_AFTER, now=now)
    assert live_tracking_due(now - timedelta(minutes=25), now=now)


def test_reason_distinguishes_a_fresh_window_from_a_renewal() -> None:
    now = T0 + timedelta(minutes=30)
    assert live_tracking_reason(None, now=now) == "start"
    assert live_tracking_reason(now - timedelta(minutes=5), now=now) is None
    assert live_tracking_reason(now - RENEW_AFTER, now=now) == "renew"
    assert live_tracking_reason(now - LIVE_TRACKING_DURATION, now=now) == "start"
    assert live_tracking_reason(now - timedelta(minutes=25), now=now) == "start"


def test_the_switch_is_active_until_its_end_time() -> None:
    assert not LiveSwitch().active(T0)
    assert LiveSwitch(enabled=True).active(T0)
    timed = LiveSwitch(enabled=True, until=T0 + timedelta(hours=1))
    assert timed.active(T0)
    assert timed.active(T0 + timedelta(minutes=59, seconds=59))
    assert not timed.active(T0 + timedelta(hours=1))
    assert not LiveSwitch(enabled=False, until=T0 + timedelta(hours=1)).active(T0)


def test_the_switch_round_trips_through_its_file(tmp_path: Path) -> None:
    path = tmp_path / "data" / "tracker.duckdb.live.json"
    berlin = timezone(timedelta(hours=1))
    switch = LiveSwitch(enabled=True, until=T0.astimezone(berlin), changed_at=T0)

    save_switch(path, switch)

    assert load_switch(path) == switch
    # No temporary files left behind next to it.
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_a_hand_written_time_without_an_offset_is_local_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    time_module.tzset()
    try:
        path = tmp_path / "live.json"
        path.write_text('{"enabled": true, "until": "2026-11-04T22:00:00", "changed_at": "2026-11-04T20:00:00"}')
        switch = load_switch(path)
    finally:
        monkeypatch.delenv("TZ")
        time_module.tzset()

    # 22:00 in Berlin in November (CET) is 21:00 UTC.
    assert switch.until == datetime(2026, 11, 4, 21, 0, tzinfo=UTC)
    assert switch.changed_at == datetime(2026, 11, 4, 19, 0, tzinfo=UTC)
    # Comparable with the aware clock, rather than raising TypeError.
    assert switch.active(datetime(2026, 11, 4, 20, 59, tzinfo=UTC))
    assert not switch.active(datetime(2026, 11, 4, 21, 0, tzinfo=UTC))


def test_a_missing_or_broken_switch_file_means_off(tmp_path: Path) -> None:
    path = tmp_path / "live.json"
    assert load_switch(path) == LiveSwitch()
    for garbage in ("{not json", "[]", json.dumps({"until": None}), json.dumps({"enabled": "yes"})):
        path.write_text(garbage)
        assert not load_switch(path).active(T0), garbage


# --------------------------------------------------------------------------- #
# the decision against a real database
# --------------------------------------------------------------------------- #

SERIAL = "234458952"


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
    )


ON = LiveSwitch(enabled=True)


def test_a_running_live_window_is_renewed_only_when_it_is_about_to_end(config: Config) -> None:
    with open_store(config.db_path) as conn:
        assert _live_tracking_reason(conn, SERIAL, ON, now=T0) == "start"

        started = T0 + timedelta(seconds=15)
        log_live_tracking(conn, serialnumber=SERIAL, ok=True, message="ok", reason="start", now=started)

        # Five minutes in: already live -- nothing to do.
        assert _live_tracking_reason(conn, SERIAL, ON, now=started + timedelta(minutes=5)) is None
        # Eight minutes in: renew before the server lets the window expire.
        assert _live_tracking_reason(conn, SERIAL, ON, now=started + RENEW_AFTER) == "renew"
        # Long after a poller outage: a fresh window, not a renewal.
        assert _live_tracking_reason(conn, SERIAL, ON, now=started + timedelta(hours=1)) == "start"


def test_nothing_is_requested_or_renewed_while_switched_off(config: Config) -> None:
    with open_store(config.db_path) as conn:
        assert _live_tracking_reason(conn, SERIAL, LiveSwitch(), now=T0) is None

        # Switched off mid-grant: the grant is left to expire, not renewed.
        log_live_tracking(conn, serialnumber=SERIAL, ok=True, message="ok", reason="start", now=T0)
        assert _live_tracking_reason(conn, SERIAL, LiveSwitch(), now=T0 + RENEW_AFTER) is None
        # Same for an activation whose end time has passed ...
        ended = LiveSwitch(enabled=True, until=T0 + timedelta(minutes=5))
        assert _live_tracking_reason(conn, SERIAL, ended, now=T0 + RENEW_AFTER) is None
        # ... while one still running renews as usual.
        running = LiveSwitch(enabled=True, until=T0 + timedelta(hours=1))
        assert _live_tracking_reason(conn, SERIAL, running, now=T0 + RENEW_AFTER) == "renew"


def test_a_failed_request_does_not_count_as_a_running_window(config: Config) -> None:
    with open_store(config.db_path) as conn:
        log_live_tracking(
            conn, serialnumber=SERIAL, ok=False, message="429", reason="start", now=T0 + timedelta(seconds=15)
        )

        # The rate limiter said no a moment ago; the next poll tries again.
        assert _live_tracking_reason(conn, SERIAL, ON, now=T0 + timedelta(seconds=50)) == "start"
