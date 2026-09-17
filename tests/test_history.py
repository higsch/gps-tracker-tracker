"""Sizing the positions-history request from what the archive already has."""

from datetime import UTC, datetime, timedelta

from gps_tracker_tracker.history import MAX_HISTORY_HOURS, backfill_hours

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def test_an_empty_archive_asks_for_the_whole_day() -> None:
    assert backfill_hours(None, now=NOW) == MAX_HISTORY_HOURS == 24


def test_a_recent_fix_asks_for_one_hour() -> None:
    assert backfill_hours(NOW, now=NOW) == 1
    assert backfill_hours(NOW - timedelta(minutes=20), now=NOW) == 1
    assert backfill_hours(NOW - timedelta(minutes=60), now=NOW) == 1


def test_an_outage_stretches_the_request_but_never_past_the_server_cap() -> None:
    assert backfill_hours(NOW - timedelta(minutes=61), now=NOW) == 2
    assert backfill_hours(NOW - timedelta(hours=5, minutes=1), now=NOW) == 6
    assert backfill_hours(NOW - timedelta(hours=23, minutes=59), now=NOW) == 24
    assert backfill_hours(NOW - timedelta(days=3), now=NOW) == 24
