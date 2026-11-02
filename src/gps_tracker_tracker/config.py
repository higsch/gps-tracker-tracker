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
# Longest single activation through the control API, in minutes. Live mode
# drains a full battery in about ten hours.
DEFAULT_LIVE_MAX_MINUTES = 600
DEFAULT_API_HOST = "0.0.0.0"
DEFAULT_API_PORT = 8080
# Pull the API's positions history on every poll to fill gaps, see history.py.
DEFAULT_BACKFILL = True
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
    # Defaulted from here on so the tests' hand-built Configs keep working.
    backfill: bool = DEFAULT_BACKFILL
    # The live-tracking control API served by `gtt watch`, see api.py. It only
    # starts when a token is set: there is no unauthenticated mode.
    api_token: str | None = None
    api_host: str = DEFAULT_API_HOST
    api_port: int = DEFAULT_API_PORT
    live_max_minutes: int = DEFAULT_LIVE_MAX_MINUTES

    @property
    def live_switch_path(self) -> Path:
        """Where the live-tracking switch is kept, next to the database."""
        return self.db_path.with_name(self.db_path.name + ".live.json")

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
        api_port = _env_int("GTT_API_PORT", DEFAULT_API_PORT)
        if not 0 < api_port < 65536:
            raise ValueError("GTT_API_PORT must be between 1 and 65535")
        live_max_minutes = _env_int("GTT_LIVE_MAX_MINUTES", DEFAULT_LIVE_MAX_MINUTES)
        if live_max_minutes < 1:
            raise ValueError("GTT_LIVE_MAX_MINUTES must be at least 1")
        return cls(
            db_path=_env_path("GTT_DB_PATH", DEFAULT_DB_PATH),
            credentials_path=_env_path("GTT_CREDENTIALS_PATH", DEFAULT_CREDENTIALS_PATH),
            poll_interval=poll_interval,
            request_timeout=request_timeout,
            locale=_env_str("GTT_LOCALE", DEFAULT_LOCALE),
            log_level=_env_str("GTT_LOG_LEVEL", DEFAULT_LOG_LEVEL).upper(),
            email=os.environ.get("FRESSNAPF_EMAIL", "").strip() or None,
            password=os.environ.get("FRESSNAPF_PASSWORD") or None,
            backfill=_env_bool("GTT_BACKFILL", DEFAULT_BACKFILL),
            api_token=os.environ.get("GTT_API_TOKEN", "").strip() or None,
            api_host=_env_str("GTT_API_HOST", DEFAULT_API_HOST),
            api_port=api_port,
            live_max_minutes=live_max_minutes,
        )
