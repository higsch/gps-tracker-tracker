"""The keep-live-mode-running rule, in isolation.

The end-to-end poller tests cover the wiring; the timing dimension -- what
happens eight or ten minutes into a live window -- cannot be driven through
`poll_once` without waiting, so it is exercised here with an injected clock.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gps_tracker_tracker.config import Config
from gps_tracker_tracker.live_tracking import (
    LIVE_TRACKING_DURATION,
    RENEW_AFTER,
    live_tracking_due,
    live_tracking_reason,
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


def test_a_running_live_window_is_renewed_only_when_it_is_about_to_end(config: Config) -> None:
    with open_store(config.db_path) as conn:
        assert _live_tracking_reason(conn, SERIAL, now=T0) == "start"

        started = T0 + timedelta(seconds=15)
        log_live_tracking(conn, serialnumber=SERIAL, ok=True, message="ok", reason="start", now=started)

        # Five minutes in: already live -- nothing to do.
        assert _live_tracking_reason(conn, SERIAL, now=started + timedelta(minutes=5)) is None
        # Eight minutes in: renew before the server lets the window expire.
        assert _live_tracking_reason(conn, SERIAL, now=started + RENEW_AFTER) == "renew"
        # Long after a poller outage: a fresh window, not a renewal.
        assert _live_tracking_reason(conn, SERIAL, now=started + timedelta(hours=1)) == "start"


def test_a_failed_request_does_not_count_as_a_running_window(config: Config) -> None:
    with open_store(config.db_path) as conn:
        log_live_tracking(
            conn, serialnumber=SERIAL, ok=False, message="429", reason="start", now=T0 + timedelta(seconds=15)
        )

        # The rate limiter said no a moment ago; the next poll tries again.
        assert _live_tracking_reason(conn, SERIAL, now=T0 + timedelta(seconds=50)) == "start"
