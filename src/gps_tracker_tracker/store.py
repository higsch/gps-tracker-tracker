"""DuckDB persistence for tracker snapshots.

Two things drive the design here:

* DuckDB allows exactly one process to hold the write lock on a database file,
  and that also blocks read-only connections from other processes. So a
  connection is opened, written, and closed per poll -- never held across the
  sleep in `gtt watch`, or you could never query the archive while it runs.
* An advisory `flock` sits in front of the connection so that an overlapping
  launchd tick degrades into a clean "skipping" message instead of a DuckDB
  exception. Writers take an exclusive lock, readers a shared one.
"""

import contextlib
import fcntl
import hashlib
import json
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb

log = logging.getLogger(__name__)

SCHEMA_VERSION = "2"

# Fields that change on their own between polls: the API renders two of them as
# human-readable relative strings ("about 2 hours") and counts the third down
# daily. Hashing them would make every snapshot unique and defeat deduplication.
_VOLATILE_TOP_LEVEL = ("last_seen", "last_position")
_VOLATILE_SERVICEBOOKING = ("days_until_servicebooking_ends",)

# duckdb.IOException is what a contended database file raises. Fall back to the
# base error class if a future version renames it.
_LOCK_ERRORS: tuple[type[Exception], ...] = (
    getattr(duckdb, "IOException", duckdb.Error),
)


class StoreBusyError(RuntimeError):
    """Another process is using the database."""


class DatabaseMissingError(RuntimeError):
    """A read-only command was run before the database existed."""


def _load_schema() -> str:
    return resources.files(__package__).joinpath("schema.sql").read_text(encoding="utf-8")


def _split_statements(script: str) -> list[str]:
    """Split a DDL script into statements, dropping line comments.

    Adequate because the schema contains no string literals with ';' or '--'.
    """
    lines = [line.split("--", 1)[0] for line in script.splitlines()]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def _connect_with_retry(
    db_path: Path,
    *,
    read_only: bool,
    attempts: int = 3,
    backoff: float = 0.5,
) -> duckdb.DuckDBPyConnection:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return duckdb.connect(str(db_path), read_only=read_only)
        except _LOCK_ERRORS as exc:
            last_error = exc
            if attempt == attempts:
                break
            delay = backoff * (2 ** (attempt - 1))
            log.debug("database busy (attempt %d/%d), retrying in %.1fs", attempt, attempts, delay)
            time.sleep(delay)
    raise StoreBusyError(f"could not open {db_path}: {last_error}") from last_error


@contextlib.contextmanager
def _hold_lock(db_path: Path, *, read_only: bool) -> Iterator[None]:
    """Take the advisory flock without opening a duckdb connection.

    Split out from `open_store` so `poll_once` can hold the exclusive lock
    across its HTTP fetches -- to skip an overlapping tick before touching the
    API -- without also keeping a duckdb connection open for that whole time.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = db_path.with_name(db_path.name + ".lock")
    lock_mode = fcntl.LOCK_SH if read_only else fcntl.LOCK_EX

    with lock_path.open("w") as lock_file:
        try:
            fcntl.flock(lock_file, lock_mode | fcntl.LOCK_NB)
        except OSError as exc:
            raise StoreBusyError(
                f"another gps-tracker-tracker process is using {db_path}"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


@contextlib.contextmanager
def _connect(db_path: Path, *, read_only: bool) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open a duckdb connection for the duration of the `with` block only."""
    conn = _connect_with_retry(db_path, read_only=read_only)
    try:
        # Deterministic rendering of TIMESTAMPTZ regardless of the host's zone.
        conn.execute("SET TimeZone='UTC'")
        if not read_only:
            for statement in _split_statements(_load_schema()):
                conn.execute(statement)
            conn.execute(
                "INSERT INTO schema_meta VALUES ('schema_version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                [SCHEMA_VERSION],
            )
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def open_store(db_path: Path, *, read_only: bool = False) -> Iterator[duckdb.DuckDBPyConnection]:
    """Open the archive, applying the schema on write connections.

    Holds the lock and the connection for the duration of the `with` block --
    fine for a single quick read or write. `poll_once` uses `acquire_write_lock`
    and `write_connection` separately instead, so the connection itself is only
    open around the write, not across its HTTP fetches too.

    Raises:
        StoreBusyError: another poller holds the lock.
        DatabaseMissingError: read_only was requested but no database exists yet.

    """
    if read_only and not db_path.exists():
        raise DatabaseMissingError(
            f"no database at {db_path} -- run `gtt poll` at least once first"
        )

    with _hold_lock(db_path, read_only=read_only):
        with _connect(db_path, read_only=read_only) as conn:
            yield conn


def acquire_write_lock(db_path: Path) -> contextlib.AbstractContextManager[None]:
    """Take the exclusive lock without opening a connection.

    Meant to wrap `poll_once`'s HTTP fetches: an overlapping poller sees
    `StoreBusyError` immediately, before it spends time on the API, but the
    duckdb connection itself is opened separately (`write_connection`) only
    once there is something to write.
    """
    return _hold_lock(db_path, read_only=False)


def write_connection(db_path: Path) -> contextlib.AbstractContextManager[duckdb.DuckDBPyConnection]:
    """Open a write connection for the duration of the `with` block only.

    Assumes the caller already holds the write lock (see `acquire_write_lock`).
    """
    return _connect(db_path, read_only=False)


def parse_timestamp(value: str | None) -> datetime | None:
    """Parse an API timestamp such as '2025-12-02T20:25:30.000+01:00'."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        log.warning("could not parse timestamp %r", value)
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def payload_fingerprint(payload: dict[str, Any]) -> str:
    """SHA-256 over the payload with the self-drifting fields stripped out."""
    stable = {k: v for k, v in payload.items() if k not in _VOLATILE_TOP_LEVEL}
    servicebooking = stable.get("servicebooking")
    if isinstance(servicebooking, dict):
        stable["servicebooking"] = {
            k: v for k, v in servicebooking.items() if k not in _VOLATILE_SERVICEBOOKING
        }
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _inserted(conn: duckdb.DuckDBPyConnection, sql: str, params: list[Any]) -> bool:
    """Run an INSERT ... ON CONFLICT DO NOTHING RETURNING and report if a row landed."""
    return bool(conn.execute(sql, params).fetchall())


def _nested_value(payload: dict[str, Any], outer: str, inner: str) -> Any:
    nested = payload.get(outer)
    return nested.get(inner) if isinstance(nested, dict) else None


def upsert_device(
    conn: duckdb.DuckDBPyConnection,
    serialnumber: str,
    payload: dict[str, Any],
    *,
    now: datetime,
) -> None:
    """Record or refresh the device's descriptive metadata."""
    conn.execute(
        """
        INSERT INTO devices (
            serialnumber, name, tracker_type, generation, icon_url,
            first_seen_at, last_updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (serialnumber) DO UPDATE SET
            name            = excluded.name,
            tracker_type    = excluded.tracker_type,
            generation      = excluded.generation,
            icon_url        = excluded.icon_url,
            last_updated_at = excluded.last_updated_at
        """,
        [
            serialnumber,
            payload.get("name"),
            _nested_value(payload, "tracker_settings", "type"),
            _nested_value(payload, "tracker_settings", "generation"),
            payload.get("icon"),
            now,
            now,
        ],
    )


def insert_raw_snapshot(
    conn: duckdb.DuckDBPyConnection,
    serialnumber: str,
    payload: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    """Store the verbatim response. Returns True if it differed from what we had."""
    return _inserted(
        conn,
        """
        INSERT INTO raw_snapshots (serialnumber, fetched_at, payload, payload_hash)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (serialnumber, payload_hash) DO NOTHING
        RETURNING 1
        """,
        [serialnumber, now, json.dumps(payload, default=str), payload_fingerprint(payload)],
    )


def insert_device_state(
    conn: duckdb.DuckDBPyConnection,
    serialnumber: str,
    payload: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    """Store battery/charging/mode state, keyed on the device's last-seen time."""
    observed_at = parse_timestamp(payload.get("last_seen_timestamp")) or now
    return _inserted(
        conn,
        """
        INSERT INTO device_state (
            serialnumber, observed_at, battery, charging, inside_geofence,
            led_brightness, deep_sleep, energy_saving, led_activatable,
            servicebooking_until, ingested_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (serialnumber, observed_at) DO NOTHING
        RETURNING 1
        """,
        [
            serialnumber,
            observed_at,
            payload.get("battery"),
            payload.get("charging"),
            payload.get("inside_geofence"),
            _nested_value(payload, "led_brightness", "value"),
            _nested_value(payload, "deep_sleep", "value"),
            _nested_value(payload, "energy_saving", "value"),
            _nested_value(payload, "led_activatable", "overall"),
            parse_timestamp(_nested_value(payload, "servicebooking", "servicebooking_until")),
            now,
        ],
    )


def insert_position(
    conn: duckdb.DuckDBPyConnection,
    serialnumber: str,
    payload: dict[str, Any],
    *,
    now: datetime,
) -> bool:
    """Store the current fix. Returns True only if this fix was not already known.

    A tracker that has never reported, or has just been reset, sends no position
    at all -- that is not an error, there is simply nothing to add to the track.
    """
    position = payload.get("position")
    if not isinstance(position, dict):
        log.debug("%s: response carries no position", serialnumber)
        return False

    lat, lng = position.get("lat"), position.get("lng")
    if lat is None or lng is None:
        log.warning("%s: position without coordinates, skipping", serialnumber)
        return False

    # sampled_at is the GPS fix time and the value we deduplicate on. The API
    # marks it optional, so fall back rather than inventing a timestamp.
    sampled_at = (
        parse_timestamp(position.get("sampled_at"))
        or parse_timestamp(position.get("timestamp"))
        or parse_timestamp(position.get("created_at"))
        or parse_timestamp(payload.get("last_position_timestamp"))
    )
    if sampled_at is None:
        log.warning("%s: position without any usable timestamp, skipping", serialnumber)
        return False

    return _inserted(
        conn,
        """
        INSERT INTO positions (
            serialnumber, sampled_at, created_at, lat, lng, accuracy,
            battery, inside_geofence, ingested_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (serialnumber, sampled_at) DO NOTHING
        RETURNING 1
        """,
        [
            serialnumber,
            sampled_at,
            parse_timestamp(position.get("created_at")),
            float(lat),
            float(lng),
            position.get("accuracy"),
            payload.get("battery"),
            payload.get("inside_geofence"),
            now,
        ],
    )


def log_poll(
    conn: duckdb.DuckDBPyConnection,
    *,
    serialnumber: str | None,
    ok: bool,
    error: str | None,
    new_positions: int,
    duration_ms: int,
    now: datetime,
) -> None:
    """Append an audit row for one device's poll attempt."""
    conn.execute(
        """
        INSERT INTO poll_log (polled_at, serialnumber, ok, error, new_positions, duration_ms)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [now, serialnumber, ok, error, new_positions, duration_ms],
    )


def write_snapshot(
    conn: duckdb.DuckDBPyConnection,
    serialnumber: str,
    payload: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Persist one tracker response in full. Returns True if a new fix was added."""
    now = now or datetime.now(UTC)
    upsert_device(conn, serialnumber, payload, now=now)
    insert_raw_snapshot(conn, serialnumber, payload, now=now)
    insert_device_state(conn, serialnumber, payload, now=now)
    return insert_position(conn, serialnumber, payload, now=now)


def recent_fixes(
    conn: duckdb.DuckDBPyConnection, serialnumber: str, *, limit: int = 60
) -> list[tuple[datetime, float, float, int | None]]:
    """The newest fixes as (sampled_at, lat, lng, accuracy), newest first."""
    return conn.execute(
        """
        SELECT sampled_at, lat, lng, accuracy
        FROM positions
        WHERE serialnumber = ?
        ORDER BY sampled_at DESC
        LIMIT ?
        """,
        [serialnumber, limit],
    ).fetchall()


def last_live_tracking_request(
    conn: duckdb.DuckDBPyConnection, serialnumber: str
) -> datetime | None:
    """When live tracking was last switched on successfully, if ever."""
    (requested_at,) = conn.execute(
        "SELECT max(requested_at) FROM live_tracking_log WHERE serialnumber = ? AND ok",
        [serialnumber],
    ).fetchone()
    return requested_at


def log_live_tracking(
    conn: duckdb.DuckDBPyConnection,
    *,
    serialnumber: str,
    ok: bool,
    message: str | None,
    reason: str,
    now: datetime,
) -> None:
    """Append an audit row for one live-tracking request."""
    conn.execute(
        """
        INSERT INTO live_tracking_log (requested_at, serialnumber, ok, message, reason)
        VALUES (?, ?, ?, ?, ?)
        """,
        [now, serialnumber, ok, message, reason],
    )
