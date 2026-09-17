"""Environment parsing for the live-hours window."""

from datetime import time
from pathlib import Path

import pytest

from gps_tracker_tracker.config import Config


@pytest.fixture
def no_dotenv(tmp_path: Path) -> Path:
    """A .env path that does not exist, so the repo's own .env cannot leak in."""
    return tmp_path / "absent.env"


def test_live_hours_default_to_the_afternoon_and_evening_utc(
    monkeypatch: pytest.MonkeyPatch, no_dotenv: Path
) -> None:
    monkeypatch.delenv("GTT_LIVE_FROM", raising=False)
    monkeypatch.delenv("GTT_LIVE_UNTIL", raising=False)
    config = Config.from_env(dotenv=no_dotenv)
    assert (config.live_from, config.live_until) == (time(15), time(22))


def test_live_hours_are_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, no_dotenv: Path
) -> None:
    monkeypatch.setenv("GTT_LIVE_FROM", "06:30")
    monkeypatch.setenv("GTT_LIVE_UNTIL", "23:15")
    config = Config.from_env(dotenv=no_dotenv)
    assert (config.live_from, config.live_until) == (time(6, 30), time(23, 15))


def test_empty_live_hours_mean_around_the_clock(
    monkeypatch: pytest.MonkeyPatch, no_dotenv: Path
) -> None:
    monkeypatch.setenv("GTT_LIVE_FROM", "")
    monkeypatch.setenv("GTT_LIVE_UNTIL", "")
    config = Config.from_env(dotenv=no_dotenv)
    assert (config.live_from, config.live_until) == (None, None)


@pytest.mark.parametrize(
    ("start", "end", "complaint"),
    [
        ("15:00", "", "set together"),
        ("", "22:00", "set together"),
        ("15:00", "15:00", "equal"),
        ("3pm", "22:00", "clock time"),
        ("15:00+02:00", "22:00", "always UTC"),
    ],
)
def test_bad_live_hours_are_rejected(
    monkeypatch: pytest.MonkeyPatch, no_dotenv: Path, start: str, end: str, complaint: str
) -> None:
    monkeypatch.setenv("GTT_LIVE_FROM", start)
    monkeypatch.setenv("GTT_LIVE_UNTIL", end)
    with pytest.raises(ValueError, match=complaint):
        Config.from_env(dotenv=no_dotenv)
