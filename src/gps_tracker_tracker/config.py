"""Configuration, read from the environment with an optional .env fallback."""

import os
from dataclasses import dataclass
from datetime import time
from pathlib import Path

DEFAULT_DB_PATH = "./data/tracker.duckdb"
DEFAULT_CREDENTIALS_PATH = "~/.config/gps-tracker-tracker/credentials.json"
DEFAULT_POLL_INTERVAL = 300
DEFAULT_REQUEST_TIMEOUT = 10
DEFAULT_LOCALE = "de"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LIVE_TRACKING = True
# Pull the API's positions history on every poll to fill gaps, see history.py.
DEFAULT_BACKFILL = True
# Live mode is only kept running inside this daily window, in UTC. Set both env
# vars empty for around the clock.
DEFAULT_LIVE_FROM = "15:00"
DEFAULT_LIVE_UNTIL = "22:00"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from a .env file without overriding the real environment."""
    path = path or Path(".env")
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # setdefault, not assignment: an exported variable must win over the file.
        os.environ.setdefault(key.strip(), value)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _env_path(name: str, default: str) -> Path:
    raw = os.environ.get(name, "").strip() or default
    return Path(raw).expanduser()


def _env_str(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be one of 1/0, true/false, yes/no, on/off, got {raw!r}")


def _env_time(name: str, default: str) -> time | None:
    """An HH:MM clock time in UTC; a set-but-empty variable means "no bound"."""
    raw = os.environ.get(name)
    if raw is None:
        raw = default
    raw = raw.strip()
    if not raw:
        return None
    try:
        parsed = time.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a UTC clock time like 15:00, got {raw!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError(f"{name} is always UTC; drop the offset from {raw!r}")
    return parsed


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved runtime configuration."""

    db_path: Path
    credentials_path: Path
    poll_interval: int
    request_timeout: int
    locale: str
    log_level: str
    email: str | None
    password: str | None
    # Keep the tracker in live mode between live_from and live_until (UTC clock
    # times, None for no bound), see live_tracking.py. Defaulted so the tests'
    # hand-built Configs keep working.
    live_tracking: bool = DEFAULT_LIVE_TRACKING
    live_from: time | None = time.fromisoformat(DEFAULT_LIVE_FROM)
    live_until: time | None = time.fromisoformat(DEFAULT_LIVE_UNTIL)
    backfill: bool = DEFAULT_BACKFILL

    @classmethod
    def from_env(cls, *, dotenv: Path | None = None) -> "Config":
        """Build a config from the environment, loading .env first."""
        load_dotenv(dotenv)
        poll_interval = _env_int("GTT_POLL_INTERVAL", DEFAULT_POLL_INTERVAL)
        if poll_interval < 1:
            raise ValueError("GTT_POLL_INTERVAL must be at least 1 second")
        request_timeout = _env_int("GTT_REQUEST_TIMEOUT", DEFAULT_REQUEST_TIMEOUT)
        if request_timeout < 1:
            raise ValueError("GTT_REQUEST_TIMEOUT must be at least 1 second")
        live_from = _env_time("GTT_LIVE_FROM", DEFAULT_LIVE_FROM)
        live_until = _env_time("GTT_LIVE_UNTIL", DEFAULT_LIVE_UNTIL)
        if (live_from is None) != (live_until is None):
            raise ValueError("GTT_LIVE_FROM and GTT_LIVE_UNTIL must be set together, or both empty")
        if live_from is not None and live_from == live_until:
            raise ValueError("GTT_LIVE_FROM and GTT_LIVE_UNTIL are equal; leave both empty for all day")
        return cls(
            db_path=_env_path("GTT_DB_PATH", DEFAULT_DB_PATH),
            credentials_path=_env_path("GTT_CREDENTIALS_PATH", DEFAULT_CREDENTIALS_PATH),
            poll_interval=poll_interval,
            request_timeout=request_timeout,
            locale=_env_str("GTT_LOCALE", DEFAULT_LOCALE),
            log_level=_env_str("GTT_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
            email=os.environ.get("FRESSNAPF_EMAIL", "").strip() or None,
            password=os.environ.get("FRESSNAPF_PASSWORD") or None,
            live_tracking=_env_bool("GTT_LIVE_TRACKING", DEFAULT_LIVE_TRACKING),
            live_from=live_from,
            live_until=live_until,
            backfill=_env_bool("GTT_BACKFILL", DEFAULT_BACKFILL),
        )
