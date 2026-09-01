"""Command line interface: login | devices | poll | watch | stats | export."""

import argparse
import asyncio
import csv
import getpass
import json
import logging
import random
import re
import signal
import sys
import threading
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import Any
from xml.sax.saxutils import escape

from . import __version__
from .auth import (
    LoginError,
    NoCredentialsError,
    load_credentials,
    login,
    redact,
    save_credentials,
)
from .config import Config
from .poller import PollResult, poll_once
from .store import DatabaseMissingError, StoreBusyError, open_store

log = logging.getLogger("gtt")

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_AUTH_REQUIRED = 2
EXIT_NO_CREDENTIALS = 3

_WATCH_JITTER = 0.1
_WATCH_MAX_BACKOFF = 1800
_RELATIVE_SINCE = re.compile(r"^(\d+)([smhdw])$")
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _setup_logging(level: str) -> None:
    resolved = getattr(logging, level, logging.INFO)
    logging.basicConfig(
        level=resolved,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Both APIs pass secrets as query parameters -- `devicetoken` on every poll,
    # `user_access_token` during login -- and httpx logs whole URLs at INFO. At
    # the default level that would write live tokens into logs/poller.log every
    # five minutes, so only let them through when DEBUG is asked for explicitly.
    if resolved > logging.DEBUG:
        logging.getLogger("httpx").setLevel(logging.WARNING)


def parse_since(value: str) -> datetime:
    """Parse '7d' / '24h' / '2026-08-01' / a full ISO timestamp."""
    match = _RELATIVE_SINCE.match(value.strip())
    if match:
        amount, unit = int(match.group(1)), match.group(2)
        return datetime.now(UTC) - timedelta(**{_SINCE_UNITS[unit]: amount})
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"{value!r} is neither a relative offset (7d, 24h, 30m) nor an ISO timestamp"
        ) from exc
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _describe(result: PollResult) -> str:
    parts = []
    for outcome in result.outcomes:
        if outcome.ok:
            state = "new fix" if outcome.new_position else "no new fix"
            parts.append(f"{outcome.serialnumber}: {state} ({outcome.duration_ms}ms)")
        else:
            parts.append(f"{outcome.serialnumber}: FAILED {outcome.error}")
    return "; ".join(parts) or "no devices"


def _position_query(args: argparse.Namespace) -> tuple[str, list[Any]]:
    clauses, params = [], []
    if getattr(args, "serial", None):
        clauses.append("serialnumber = ?")
        params.append(args.serial)
    if getattr(args, "since", None):
        clauses.append("sampled_at >= ?")
        params.append(args.since)
    if getattr(args, "max_accuracy", None) is not None:
        # NULL accuracy is kept: unknown is not the same as bad.
        clauses.append("(accuracy IS NULL OR accuracy <= ?)")
        params.append(args.max_accuracy)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"""
        SELECT serialnumber, sampled_at, lat, lng, accuracy, battery, inside_geofence
        FROM positions
        {where}
        ORDER BY serialnumber, sampled_at
    """
    return sql, params


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #

def cmd_login(args: argparse.Namespace, config: Config) -> int:
    """Run the email magic-link flow and store the device tokens."""
    email = args.email or config.email or input("Fressnapf shop email: ").strip()
    password = config.password or getpass.getpass("Fressnapf shop password: ")
    if not email or not password:
        print("email and password are required", file=sys.stderr)
        return EXIT_FAILURE

    def on_link_sent(address: str) -> None:
        print(
            f"\nA sign-in link was emailed to {address}.\n"
            "Open it on any device, then leave this running -- it will continue "
            "automatically.\n",
            file=sys.stderr,
        )

    try:
        devices = asyncio.run(
            login(
                email,
                password,
                locale=config.locale,
                request_timeout=config.request_timeout,
                on_link_sent=on_link_sent,
            )
        )
    except LoginError as exc:
        print(f"login failed: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    save_credentials(config.credentials_path, devices)
    print(f"\nStored {len(devices)} tracker(s) in {config.credentials_path}:")
    for device in devices:
        print(f"  {device.serialnumber}  token {redact(device.token)}")
    print("\nRun `gtt poll` to record the first position.")
    return EXIT_OK


def cmd_devices(args: argparse.Namespace, config: Config) -> int:
    """List the trackers we hold tokens for."""
    devices = load_credentials(config.credentials_path)
    print(f"{'serialnumber':<16} {'token':<10}")
    for device in devices:
        print(f"{device.serialnumber:<16} {redact(device.token):<10}")
    return EXIT_OK


def cmd_poll(args: argparse.Namespace, config: Config) -> int:
    """Fetch once and append to the archive."""
    try:
        result = poll_once(config)
    except StoreBusyError as exc:
        # An overlapping launchd tick is not a failure; the next one will catch up.
        log.info("skipping: %s", exc)
        return EXIT_OK

    log.info("polled %d device(s) -- %s", len(result.outcomes), _describe(result))
    if result.auth_failure:
        log.error("authentication rejected -- run `gtt login` again")
        return EXIT_AUTH_REQUIRED
    return EXIT_OK if result.ok else EXIT_FAILURE


def cmd_watch(args: argparse.Namespace, config: Config) -> int:
    """Poll on a loop until interrupted."""
    interval = args.interval or config.poll_interval
    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        # Only sets a flag: an in-flight write finishes before we exit.
        log.info("received %s, finishing current poll then exiting", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    log.info("watching every %ds (Ctrl-C to stop)", interval)
    iterations = 0
    consecutive_failures = 0
    exit_code = EXIT_OK

    while not stop.is_set():
        try:
            result = poll_once(config)
        except StoreBusyError as exc:
            log.info("skipping: %s", exc)
            result = None
        except NoCredentialsError as exc:
            log.error("%s", exc)
            return EXIT_NO_CREDENTIALS

        if result is not None:
            log.info("%s", _describe(result))
            if result.auth_failure:
                # Waiting cannot fix this, so stop rather than spin.
                log.error("authentication rejected -- run `gtt login` again; stopping")
                return EXIT_AUTH_REQUIRED
            consecutive_failures = 0 if result.ok else consecutive_failures + 1
            # Reflect the most recent attempt, so a supervisor can see trouble.
            exit_code = EXIT_OK if result.ok else EXIT_FAILURE
        else:
            consecutive_failures = 0

        iterations += 1
        if args.max_iterations and iterations >= args.max_iterations:
            log.info("reached --max-iterations %d, exiting", args.max_iterations)
            break

        delay = interval * random.uniform(1 - _WATCH_JITTER, 1 + _WATCH_JITTER)
        if consecutive_failures:
            delay = min(delay * 2**consecutive_failures, _WATCH_MAX_BACKOFF)
            log.warning(
                "%d consecutive failure(s), backing off to %.0fs", consecutive_failures, delay
            )
        stop.wait(delay)

    return exit_code


def cmd_stats(args: argparse.Namespace, config: Config) -> int:
    """Summarise what the archive holds."""
    with open_store(config.db_path, read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT d.serialnumber, d.name, count(p.sampled_at) AS fixes,
                   min(p.sampled_at) AS first_fix, max(p.sampled_at) AS last_fix
            FROM devices d
            LEFT JOIN positions p ON p.serialnumber = d.serialnumber
            GROUP BY 1, 2
            ORDER BY 1
            """
        ).fetchall()

        print("Trackers")
        if not rows:
            print("  (none yet)")
        for serial, name, fixes, first_fix, last_fix in rows:
            print(f"  {serial}  {name or '?'}")
            print(f"    fixes      {fixes}")
            print(f"    first      {first_fix or '-'}")
            print(f"    last       {last_fix or '-'}")

        polls, failures, snapshots, states = conn.execute(
            """
            SELECT (SELECT count(*) FROM poll_log),
                   (SELECT count(*) FROM poll_log WHERE NOT ok),
                   (SELECT count(*) FROM raw_snapshots),
                   (SELECT count(*) FROM device_state)
            """
        ).fetchone()
        print("\nArchive")
        print(f"  poll attempts    {polls} ({failures} failed)")
        print(f"  raw snapshots    {snapshots}")
        print(f"  state rows       {states}")

        # TIMESTAMPTZ has no direct cast to TIME, so drop to a naive TIMESTAMP
        # first. The session timezone is UTC, so this is UTC wall-clock time.
        daily = conn.execute(
            """
            SELECT sampled_at::TIMESTAMP::DATE AS day,
                   count(*) AS fixes,
                   min(sampled_at::TIMESTAMP)::TIME AS first,
                   max(sampled_at::TIMESTAMP)::TIME AS last
            FROM positions
            GROUP BY 1
            ORDER BY 1 DESC
            LIMIT 14
            """
        ).fetchall()
        if daily:
            print("\nFixes per day (most recent 14, UTC)")
            for day, fixes, first, last in daily:
                print(f"  {day}  {fixes:>5}  {first}-{last}")
    return EXIT_OK


def cmd_export(args: argparse.Namespace, config: Config) -> int:
    """Dump the track as GeoJSON, CSV or GPX on stdout."""
    sql, params = _position_query(args)
    with open_store(config.db_path, read_only=True) as conn:
        cursor = conn.execute(sql, params)
        if args.format == "csv":
            _export_csv(cursor)
        elif args.format == "geojson":
            _export_geojson(cursor)
        else:
            _export_gpx(cursor)
    return EXIT_OK


def _export_csv(cursor: Any) -> None:
    writer = csv.writer(sys.stdout)
    writer.writerow(
        ["serialnumber", "sampled_at", "lat", "lng", "accuracy", "battery", "inside_geofence"]
    )
    for row in cursor.fetchall():
        serial, sampled_at, lat, lng, accuracy, battery, geofence = row
        writer.writerow([serial, sampled_at.isoformat(), lat, lng, accuracy, battery, geofence])


def _export_geojson(cursor: Any) -> None:
    """Stream a FeatureCollection of points, keeping the per-fix properties."""
    sys.stdout.write('{"type":"FeatureCollection","features":[')
    for index, row in enumerate(cursor.fetchall()):
        serial, sampled_at, lat, lng, accuracy, battery, geofence = row
        feature = {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "serialnumber": serial,
                "sampled_at": sampled_at.isoformat(),
                "accuracy": accuracy,
                "battery": battery,
                "inside_geofence": geofence,
            },
        }
        sys.stdout.write(("," if index else "") + json.dumps(feature))
    sys.stdout.write("]}\n")


def _export_gpx(cursor: Any) -> None:
    sys.stdout.write(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<gpx version="1.1" creator="gps-tracker-tracker" '
        'xmlns="http://www.topografix.com/GPX/1/1">\n'
    )
    current_serial = None
    for row in cursor.fetchall():
        serial, sampled_at, lat, lng, _accuracy, _battery, _geofence = row
        if serial != current_serial:
            if current_serial is not None:
                sys.stdout.write("</trkseg></trk>\n")
            sys.stdout.write(f"<trk><name>{escape(str(serial))}</name><trkseg>\n")
            current_serial = serial
        sys.stdout.write(
            f'<trkpt lat="{lat}" lon="{lng}">'
            f"<time>{sampled_at.isoformat()}</time></trkpt>\n"
        )
    if current_serial is not None:
        sys.stdout.write("</trkseg></trk>\n")
    sys.stdout.write("</gpx>\n")


# --------------------------------------------------------------------------- #
# argument parsing
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gtt",
        description="Archive a Fressnapf pet GPS tracker's positions in DuckDB.",
    )
    parser.add_argument("--version", action="version", version=f"gps-tracker-tracker {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="log at DEBUG level")
    subparsers = parser.add_subparsers(dest="command", required=True)

    login_parser = subparsers.add_parser("login", help="sign in by email and store device tokens")
    login_parser.add_argument("--email", help="Fressnapf shop email (else $FRESSNAPF_EMAIL/prompt)")
    login_parser.set_defaults(func=cmd_login)

    devices_parser = subparsers.add_parser("devices", help="list known trackers")
    devices_parser.set_defaults(func=cmd_devices)

    poll_parser = subparsers.add_parser("poll", help="fetch once and store")
    poll_parser.set_defaults(func=cmd_poll)

    watch_parser = subparsers.add_parser("watch", help="fetch on a loop")
    watch_parser.add_argument(
        "--interval", type=int, default=None, help="seconds between polls (else $GTT_POLL_INTERVAL)"
    )
    watch_parser.add_argument(
        "--max-iterations", type=int, default=0, help="stop after N polls (0 = forever)"
    )
    watch_parser.set_defaults(func=cmd_watch)

    stats_parser = subparsers.add_parser("stats", help="summarise the archive")
    stats_parser.set_defaults(func=cmd_stats)

    export_parser = subparsers.add_parser("export", help="dump the track")
    export_parser.add_argument(
        "--format", choices=("geojson", "csv", "gpx"), default="geojson"
    )
    export_parser.add_argument("--serial", help="limit to one tracker")
    export_parser.add_argument(
        "--since", type=parse_since, help="only fixes at or after this point (7d, 24h, ISO date)"
    )
    export_parser.add_argument(
        "--max-accuracy",
        type=int,
        help="drop fixes with an accuracy radius worse than this many metres",
    )
    export_parser.set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = Config.from_env()
    except ValueError as exc:
        print(f"bad configuration: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    _setup_logging("DEBUG" if args.verbose else config.log_level)

    try:
        return args.func(args, config)
    except NoCredentialsError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_NO_CREDENTIALS
    except DatabaseMissingError as exc:
        print(f"{exc}", file=sys.stderr)
        return EXIT_FAILURE
    except StoreBusyError as exc:
        print(f"{exc} -- try again in a moment", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
