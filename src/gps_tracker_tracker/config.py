"""Configuration, read from the environment with an optional .env fallback."""

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = "./data/tracker.duckdb"
DEFAULT_CREDENTIALS_PATH = "~/.config/gps-tracker-tracker/credentials.json"
DEFAULT_POLL_INTERVAL = 300
DEFAULT_REQUEST_TIMEOUT = 10
DEFAULT_LOCALE = "de"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LIVE_TRACKING = True
# A fix has to land this far from a recent one to count as motion. Stationary GPS
# jitter is well under this; a cat walking for 20s covers it easily.
DEFAULT_LIVE_MOTION_METRES = 10
# How far back (seconds) the fixes a new one is compared with may reach. Wide
# enough that a short pause mid-walk does not read as "stopped".
DEFAULT_LIVE_MOTION_WINDOW = 180

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
    # Motion-gated live tracking, see live_tracking.py. Defaults here so the
    # tests' hand-built Configs keep working.
    live_tracking: bool = DEFAULT_LIVE_TRACKING
    live_motion_metres: int = DEFAULT_LIVE_MOTION_METRES
    live_motion_window: int = DEFAULT_LIVE_MOTION_WINDOW

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
        live_motion_metres = _env_int("GTT_LIVE_MOTION_METRES", DEFAULT_LIVE_MOTION_METRES)
        if live_motion_metres < 1:
            raise ValueError("GTT_LIVE_MOTION_METRES must be at least 1 metre")
        live_motion_window = _env_int("GTT_LIVE_MOTION_WINDOW", DEFAULT_LIVE_MOTION_WINDOW)
        if live_motion_window < 1:
            raise ValueError("GTT_LIVE_MOTION_WINDOW must be at least 1 second")
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
            live_motion_metres=live_motion_metres,
            live_motion_window=live_motion_window,
        )
