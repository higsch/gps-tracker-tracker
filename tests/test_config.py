"""Environment parsing."""

from pathlib import Path

import pytest

from gps_tracker_tracker.config import Config


@pytest.fixture
def no_dotenv(tmp_path: Path) -> Path:
    """A .env path that does not exist, so the repo's own .env cannot leak in."""
    return tmp_path / "absent.env"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GTT_API_TOKEN", "GTT_API_HOST", "GTT_API_PORT", "GTT_LIVE_MAX_MINUTES"):
        monkeypatch.delenv(name, raising=False)


def test_the_api_is_off_without_a_token(clean_env: None, no_dotenv: Path) -> None:
    config = Config.from_env(dotenv=no_dotenv)
    assert config.api_token is None
    assert (config.api_host, config.api_port) == ("0.0.0.0", 8080)
    assert config.live_max_minutes == 600


def test_api_settings_are_read_from_the_environment(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, no_dotenv: Path
) -> None:
    monkeypatch.setenv("GTT_API_TOKEN", "  s3cret  ")
    monkeypatch.setenv("GTT_API_HOST", "127.0.0.1")
    monkeypatch.setenv("GTT_API_PORT", "9000")
    monkeypatch.setenv("GTT_LIVE_MAX_MINUTES", "120")
    config = Config.from_env(dotenv=no_dotenv)
    assert config.api_token == "s3cret"
    assert (config.api_host, config.api_port) == ("127.0.0.1", 9000)
    assert config.live_max_minutes == 120


@pytest.mark.parametrize(
    ("name", "value", "complaint"),
    [
        ("GTT_API_PORT", "0", "between 1 and 65535"),
        ("GTT_API_PORT", "70000", "between 1 and 65535"),
        ("GTT_API_PORT", "http", "integer"),
        ("GTT_LIVE_MAX_MINUTES", "0", "at least 1"),
    ],
)
def test_bad_api_settings_are_rejected(
    clean_env: None,
    monkeypatch: pytest.MonkeyPatch,
    no_dotenv: Path,
    name: str,
    value: str,
    complaint: str,
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(ValueError, match=complaint):
        Config.from_env(dotenv=no_dotenv)


def test_the_live_switch_sits_next_to_the_database(clean_env: None, monkeypatch: pytest.MonkeyPatch, no_dotenv: Path) -> None:
    monkeypatch.setenv("GTT_DB_PATH", "/data/tracker.duckdb")
    assert Config.from_env(dotenv=no_dotenv).live_switch_path == Path("/data/tracker.duckdb.live.json")


def test_backfill_is_on_by_default_and_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch, no_dotenv: Path
) -> None:
    monkeypatch.delenv("GTT_BACKFILL", raising=False)
    assert Config.from_env(dotenv=no_dotenv).backfill is True
    monkeypatch.setenv("GTT_BACKFILL", "0")
    assert Config.from_env(dotenv=no_dotenv).backfill is False
