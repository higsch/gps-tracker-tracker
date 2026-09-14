"""The motion rule and the renew-only-while-moving rule, in isolation.

The end-to-end poller tests cover the wiring; the timing dimension -- what
happens eight or ten minutes into a live window -- cannot be driven through
`poll_once` without waiting, so it is exercised here with an injected clock.
"""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gps_tracker_tracker.config import Config
from gps_tracker_tracker.live_tracking import (
    RENEW_AFTER,
    Fix,
    haversine_m,
    live_tracking_due,
    motion_reason,
    reference_fixes,
)
from gps_tracker_tracker.poller import _live_tracking_reason
from gps_tracker_tracker.store import log_live_tracking, open_store

T0 = datetime(2026, 9, 14, 18, 0, tzinfo=UTC)
LAT, LNG = 53.4526, 9.9422
# One degree of latitude is ~111km, so 0.001° is ~111m.
NORTH_100M = 0.0009
WINDOW = timedelta(seconds=180)


def fix(seconds: int, lat: float = LAT, lng: float = LNG, accuracy: int | None = 10) -> Fix:
    return Fix(T0 + timedelta(seconds=seconds), lat, lng, accuracy)


def test_haversine_is_right_to_the_metre() -> None:
    # 0.001° of latitude at any longitude.
    assert haversine_m(53.0, 9.0, 53.001, 9.0) == pytest.approx(111.2, abs=0.2)
    assert haversine_m(53.0, 9.0, 53.0, 9.0) == 0.0


def test_reference_fixes_always_include_the_previous_one() -> None:
    # A single fix after a quiet 25 minutes: nothing inside the window, but the
    # previous fix still counts -- that is what "started moving" looks like.
    fixes = [fix(1500), fix(0)]
    assert reference_fixes(fixes, window=WINDOW) == [fix(0)]


def test_reference_fixes_take_everything_inside_the_window() -> None:
    fixes = [fix(200), fix(180), fix(100), fix(20), fix(0)]
    # 200-180=20s and 200-100=100s are inside; 200-20=180s is on the edge and in;
    # 200-0 is out.
    assert reference_fixes(fixes, window=WINDOW) == [fix(180), fix(100), fix(20)]


def test_a_displaced_fix_is_motion() -> None:
    fixes = [fix(20, lat=LAT + NORTH_100M), fix(0)]
    assert motion_reason(fixes, threshold_m=30, window=WINDOW) == "moved 100m in 20s"


def test_jitter_below_the_threshold_is_not_motion() -> None:
    fixes = [fix(20, lat=LAT + 0.0002), fix(0)]  # ~22m
    assert motion_reason(fixes, threshold_m=30, window=WINDOW) is None


def test_a_displacement_inside_the_accuracy_radius_is_not_motion() -> None:
    # A cell-tower fallback fix 100m off, but the fix admits to being 400m uncertain.
    fixes = [fix(20, lat=LAT + NORTH_100M, accuracy=400), fix(0)]
    assert motion_reason(fixes, threshold_m=30, window=WINDOW) is None
    # Same when the *earlier* fix was the imprecise one.
    fixes = [fix(20, lat=LAT + NORTH_100M), fix(0, accuracy=400)]
    assert motion_reason(fixes, threshold_m=30, window=WINDOW) is None


def test_a_pause_mid_walk_still_counts_as_motion_within_the_window() -> None:
    # Walked 100m north by t=60, then sat still: the last three fixes coincide,
    # but the one from 100s ago is still inside the window.
    fixes = [
        fix(160, lat=LAT + NORTH_100M),
        fix(140, lat=LAT + NORTH_100M),
        fix(120, lat=LAT + NORTH_100M),
        fix(60, lat=LAT + NORTH_100M),
        fix(0),
    ]
    assert motion_reason(fixes, threshold_m=30, window=WINDOW) == "moved 100m in 160s"


def test_settled_for_longer_than_the_window_is_not_motion() -> None:
    # Same walk, but now every fix inside the window (and the previous one) is at
    # the new spot; the old spot is 200s back, outside a 180s window.
    fixes = [
        fix(200, lat=LAT + NORTH_100M),
        fix(180, lat=LAT + NORTH_100M),
        fix(100, lat=LAT + NORTH_100M),
        fix(30, lat=LAT + NORTH_100M),
        fix(0),
    ]
    assert motion_reason(fixes, threshold_m=30, window=WINDOW) is None


def test_fewer_than_two_fixes_is_never_motion() -> None:
    assert motion_reason([], threshold_m=30, window=WINDOW) is None
    assert motion_reason([fix(0)], threshold_m=30, window=WINDOW) is None


def test_live_tracking_is_due_when_never_requested_or_when_the_window_is_nearly_over() -> None:
    now = T0 + timedelta(minutes=30)
    assert live_tracking_due(None, now=now)
    assert not live_tracking_due(now - timedelta(minutes=5), now=now)
    assert live_tracking_due(now - RENEW_AFTER, now=now)
    assert live_tracking_due(now - timedelta(minutes=25), now=now)


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


def _insert_fixes(conn, fixes: list[Fix]) -> None:
    for f in fixes:
        conn.execute(
            "INSERT INTO positions (serialnumber, sampled_at, lat, lng, accuracy, ingested_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [SERIAL, f.sampled_at, f.lat, f.lng, f.accuracy, f.sampled_at],
        )


def test_a_running_live_window_suppresses_the_motion_check(config: Config) -> None:
    with open_store(config.db_path) as conn:
        _insert_fixes(conn, [fix(0), fix(20, lat=LAT + NORTH_100M)])
        started = T0 + timedelta(seconds=15)
        log_live_tracking(conn, serialnumber=SERIAL, ok=True, message="ok", reason="x", now=started)

        # Five minutes in: moving, but already live -- nothing to do.
        assert _live_tracking_reason(conn, SERIAL, config, now=started + timedelta(minutes=5)) is None
        # Eight minutes in: renew, because the newest fixes still show motion.
        assert (
            _live_tracking_reason(conn, SERIAL, config, now=started + RENEW_AFTER)
            == "moved 100m in 20s"
        )


def test_a_pet_that_stopped_does_not_get_the_window_renewed(config: Config) -> None:
    with open_store(config.db_path) as conn:
        started = T0
        log_live_tracking(conn, serialnumber=SERIAL, ok=True, message="ok", reason="x", now=started)
        # Walked north at first, then sat still for the last four minutes of the
        # window -- every fix a new one is compared with is at the same spot.
        fixes = [fix(0)] + [
            fix(s, lat=LAT + NORTH_100M) for s in range(60, 8 * 60 + 1, 20)
        ]
        _insert_fixes(conn, fixes)

        assert _live_tracking_reason(conn, SERIAL, config, now=started + RENEW_AFTER) is None


def test_a_failed_request_does_not_count_as_a_running_window(config: Config) -> None:
    with open_store(config.db_path) as conn:
        _insert_fixes(conn, [fix(0), fix(20, lat=LAT + NORTH_100M)])
        log_live_tracking(
            conn, serialnumber=SERIAL, ok=False, message="429", reason="x", now=T0 + timedelta(seconds=15)
        )

        # The rate limiter said no a moment ago; the next moving fix tries again.
        assert (
            _live_tracking_reason(conn, SERIAL, config, now=T0 + timedelta(seconds=50))
            == "moved 100m in 20s"
        )
