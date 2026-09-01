"""Tests for the read-only commands and the CLI's exit codes."""

import copy
import csv
import io
import json
from pathlib import Path

import pytest

from gps_tracker_tracker.cli import (
    EXIT_FAILURE,
    EXIT_NO_CREDENTIALS,
    EXIT_OK,
    main,
)
from gps_tracker_tracker.store import open_store, write_snapshot

FIXTURES = Path(__file__).parent / "fixtures"

# Two fixes on one day and one imprecise fix on the next.
FIXES = [
    ("2025-12-02T20:25:30.000+01:00", 52.520008, 13.404954, 10),
    ("2025-12-02T20:31:12.000+01:00", 52.521500, 13.406100, 12),
    ("2025-12-03T08:02:05.000+01:00", 52.530000, 13.410000, 480),
]


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    payload = json.loads((FIXTURES / "get_tracker_response.json").read_text())
    db_path = tmp_path / "tracker.duckdb"
    with open_store(db_path) as conn:
        for sampled_at, lat, lng, accuracy in FIXES:
            snapshot = copy.deepcopy(payload)
            snapshot["position"] = dict(
                snapshot["position"], sampled_at=sampled_at, lat=lat, lng=lng, accuracy=accuracy
            )
            snapshot["last_seen_timestamp"] = sampled_at
            write_snapshot(conn, snapshot["serialnumber"], snapshot)
    monkeypatch.setenv("GTT_DB_PATH", str(db_path))
    monkeypatch.setenv("GTT_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    return db_path


def test_stats_summarises_the_archive(seeded_db: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["stats"]) == EXIT_OK
    out = capsys.readouterr().out
    assert "231511297" in out
    assert "Test Pet" in out
    assert "fixes      3" in out
    # The per-day breakdown needs a TIMESTAMPTZ -> TIME cast DuckDB rejects unless
    # it goes via a naive TIMESTAMP first.
    assert "2025-12-02" in out
    assert "2025-12-03" in out


def test_export_csv(seeded_db: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["export", "--format", "csv"]) == EXIT_OK
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 3
    assert rows[0]["lat"] == "52.520008"
    assert rows[0]["sampled_at"] == "2025-12-02T19:25:30+00:00"


def test_export_geojson_is_valid_and_lng_first(
    seeded_db: Path, capsys: pytest.CaptureFixture
) -> None:
    assert main(["export", "--format", "geojson"]) == EXIT_OK
    document = json.loads(capsys.readouterr().out)
    assert document["type"] == "FeatureCollection"
    assert len(document["features"]) == 3
    # GeoJSON is [longitude, latitude], which is the opposite of how we store it.
    assert document["features"][0]["geometry"]["coordinates"] == [13.404954, 52.520008]


def test_export_max_accuracy_drops_imprecise_fixes(
    seeded_db: Path, capsys: pytest.CaptureFixture
) -> None:
    assert main(["export", "--format", "geojson", "--max-accuracy", "50"]) == EXIT_OK
    document = json.loads(capsys.readouterr().out)
    assert len(document["features"]) == 2


def test_export_since_filters_by_day(seeded_db: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["export", "--format", "csv", "--since", "2025-12-03"]) == EXIT_OK
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 1
    assert rows[0]["accuracy"] == "480"


def test_export_gpx(seeded_db: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["export", "--format", "gpx"]) == EXIT_OK
    out = capsys.readouterr().out
    assert out.startswith("<?xml")
    assert out.count("<trkpt") == 3
    assert out.count("<trk>") == 1
    assert out.rstrip().endswith("</gpx>")


def test_poll_without_credentials_exits_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("GTT_DB_PATH", str(tmp_path / "tracker.duckdb"))
    monkeypatch.setenv("GTT_CREDENTIALS_PATH", str(tmp_path / "nope.json"))
    assert main(["poll"]) == EXIT_NO_CREDENTIALS
    assert "gtt login" in capsys.readouterr().err


def test_stats_before_any_poll_explains_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("GTT_DB_PATH", str(tmp_path / "missing.duckdb"))
    assert main(["stats"]) == EXIT_FAILURE
    assert "gtt poll" in capsys.readouterr().err


def test_bad_interval_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("GTT_POLL_INTERVAL", "nonsense")
    assert main(["stats"]) == EXIT_FAILURE
    assert "GTT_POLL_INTERVAL" in capsys.readouterr().err
