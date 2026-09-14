"""Tests for the DuckDB layer. No network and no credentials needed."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from gps_tracker_tracker.cli import parse_since
from gps_tracker_tracker.store import (
    SCHEMA_VERSION,
    DatabaseMissingError,
    StoreBusyError,
    open_store,
    parse_timestamp,
    payload_fingerprint,
    write_snapshot,
)

FIXTURES = Path(__file__).parent / "fixtures"
TABLES = ("devices", "positions", "device_state", "raw_snapshots", "poll_log")


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def payload() -> dict:
    return _fixture("get_tracker_response.json")


@pytest.fixture
def null_payload() -> dict:
    return _fixture("get_tracker_response_null_position.json")


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "tracker.duckdb"


def counts(conn) -> dict[str, int]:
    return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def test_first_insert_writes_every_table(db_path: Path, payload: dict) -> None:
    with open_store(db_path) as conn:
        assert write_snapshot(conn, payload["serialnumber"], payload) is True
        assert counts(conn) == {
            "devices": 1,
            "positions": 1,
            "device_state": 1,
            "raw_snapshots": 1,
            "poll_log": 0,
        }

        serial, sampled_at, lat, lng, accuracy, battery = conn.execute(
            "SELECT serialnumber, sampled_at, lat, lng, accuracy, battery FROM positions"
        ).fetchone()
        assert serial == "231511297"
        # 20:25:30+01:00 is 19:25:30 UTC.
        assert sampled_at == datetime(2025, 12, 2, 19, 25, 30, tzinfo=UTC)
        assert (lat, lng, accuracy, battery) == (52.520008, 13.404954, 10, 85)

        name, tracker_type, generation = conn.execute(
            "SELECT name, tracker_type, generation FROM devices"
        ).fetchone()
        assert (name, tracker_type, generation) == ("Test Pet", "dog", "2.1")


def test_repolling_the_same_fix_is_a_noop(db_path: Path, payload: dict) -> None:
    with open_store(db_path) as conn:
        assert write_snapshot(conn, payload["serialnumber"], payload) is True
        # Same fix, polled again five minutes later: nothing new to record.
        assert write_snapshot(conn, payload["serialnumber"], payload) is False
        assert counts(conn)["positions"] == 1
        assert counts(conn)["device_state"] == 1
        assert counts(conn)["raw_snapshots"] == 1


def test_self_drifting_fields_do_not_create_a_new_snapshot(db_path: Path, payload: dict) -> None:
    """`last_seen` is a relative string that changes on its own."""
    drifted = copy.deepcopy(payload)
    drifted["last_seen"] = "about 3 hours"
    drifted["last_position"] = "about 3 hours"
    drifted["servicebooking"]["days_until_servicebooking_ends"] = 183

    assert payload_fingerprint(drifted) == payload_fingerprint(payload)

    with open_store(db_path) as conn:
        write_snapshot(conn, payload["serialnumber"], payload)
        write_snapshot(conn, drifted["serialnumber"], drifted)
        assert counts(conn)["raw_snapshots"] == 1


def test_a_real_change_does_create_a_new_snapshot(db_path: Path, payload: dict) -> None:
    changed = copy.deepcopy(payload)
    changed["battery"] = 84

    assert payload_fingerprint(changed) != payload_fingerprint(payload)

    with open_store(db_path) as conn:
        write_snapshot(conn, payload["serialnumber"], payload)
        write_snapshot(conn, changed["serialnumber"], changed)
        assert counts(conn)["raw_snapshots"] == 2


def test_null_position_records_state_but_no_fix(db_path: Path, null_payload: dict) -> None:
    with open_store(db_path) as conn:
        assert write_snapshot(conn, null_payload["serialnumber"], null_payload) is False
        result = counts(conn)
        assert result["positions"] == 0
        assert result["device_state"] == 1
        assert result["devices"] == 1

        battery, charging = conn.execute(
            "SELECT battery, charging FROM device_state"
        ).fetchone()
        assert (battery, charging) == (100, True)


def test_missing_sampled_at_falls_back_to_created_at(db_path: Path, payload: dict) -> None:
    payload["position"]["sampled_at"] = None
    with open_store(db_path) as conn:
        assert write_snapshot(conn, payload["serialnumber"], payload) is True
        (sampled_at,) = conn.execute("SELECT sampled_at FROM positions").fetchone()
        # created_at is 20:25:31+01:00 -> 19:25:31 UTC.
        assert sampled_at == datetime(2025, 12, 2, 19, 25, 31, tzinfo=UTC)


def test_position_without_any_timestamp_is_skipped(db_path: Path, payload: dict) -> None:
    payload["position"]["sampled_at"] = None
    payload["position"]["created_at"] = None
    payload["last_position_timestamp"] = None
    with open_store(db_path) as conn:
        assert write_snapshot(conn, payload["serialnumber"], payload) is False
        assert counts(conn)["positions"] == 0
        # The snapshot is still archived, so nothing is lost.
        assert counts(conn)["raw_snapshots"] == 1


def test_a_moving_tracker_appends_fixes(db_path: Path, payload: dict) -> None:
    later = copy.deepcopy(payload)
    later["position"]["sampled_at"] = "2025-12-02T20:30:30.000+01:00"
    later["position"]["lat"] = 52.521
    later["last_seen_timestamp"] = "2025-12-02T20:30:31.000+01:00"

    with open_store(db_path) as conn:
        assert write_snapshot(conn, payload["serialnumber"], payload) is True
        assert write_snapshot(conn, later["serialnumber"], later) is True
        assert counts(conn)["positions"] == 2
        assert counts(conn)["device_state"] == 2
        # Still one device: the same tracker moved, it did not multiply.
        assert counts(conn)["devices"] == 1


def test_schema_survives_reopening(db_path: Path, payload: dict) -> None:
    with open_store(db_path) as conn:
        write_snapshot(conn, payload["serialnumber"], payload)
    with open_store(db_path) as conn:
        assert counts(conn)["positions"] == 1
        (version,) = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        assert version == SCHEMA_VERSION


def test_read_only_before_the_database_exists(db_path: Path) -> None:
    with pytest.raises(DatabaseMissingError):
        with open_store(db_path, read_only=True):
            pass


def test_a_second_writer_is_turned_away(db_path: Path, payload: dict) -> None:
    with open_store(db_path) as conn:
        write_snapshot(conn, payload["serialnumber"], payload)
        with pytest.raises(StoreBusyError):
            with open_store(db_path):
                pass


def test_parse_timestamp() -> None:
    assert parse_timestamp("2025-12-02T20:25:30.000+01:00") == datetime(
        2025, 12, 2, 19, 25, 30, tzinfo=UTC
    )
    # A naive timestamp is assumed to be UTC rather than rejected.
    assert parse_timestamp("2025-12-02T20:25:30") == datetime(
        2025, 12, 2, 20, 25, 30, tzinfo=UTC
    )
    assert parse_timestamp(None) is None
    assert parse_timestamp("not a date") is None


def test_parse_since() -> None:
    now = datetime.now(UTC)
    assert (now - parse_since("24h")).total_seconds() == pytest.approx(86400, abs=5)
    assert (now - parse_since("7d")).total_seconds() == pytest.approx(604800, abs=5)
    assert parse_since("2026-08-01") == datetime(2026, 8, 1, tzinfo=UTC)
    assert parse_since("2026-08-01T12:00:00+02:00").hour == 12
