# gps-tracker-tracker

Polls a **Fressnapf pet GPS tracker** and archives every position it reports into a
[DuckDB](https://duckdb.org) database.

The tracker's cloud API only ever returns the *current* position — there is no history endpoint.
So this tool doesn't mirror a remote archive, **it is the archive**: it polls on a schedule and
deduplicates on the GPS fix time. Nothing is retroactive, the track starts the day you start
polling. Since Fressnapf discontinued the product at the end of 2025 with no further development,
having your own copy is worth something.

> This uses an undocumented API, with an app token extracted from the Fressnapf Android app. It is
> not affiliated with or endorsed by Fressnapf, and you should point it only at your own tracker.
> API details come from [`fressnapftracker`](https://github.com/eifinger/fressnapftracker), the
> client behind the official Home Assistant integration.

## Requirements

- macOS or Linux
- [`uv`](https://docs.astral.sh/uv/) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Python 3.13 (`uv python install 3.13` — the upstream API client requires it)

## Setup

```bash
uv sync
cp .env.example .env      # optional; every setting has a default
```

Sign in once. Fressnapf dropped phone-number authentication, so this is an email magic link: you
give your **shop** credentials, Fressnapf emails you a link, you open it, and the tracker cloud
hands back a long-lived device token.

```bash
uv run gtt login
```

The device token is written to `~/.config/gps-tracker-tracker/credentials.json` with mode `0600`.
Polling uses only that token — it never re-authenticates and never needs your password again.

## Use

```bash
uv run gtt poll                  # fetch once, append, exit  (what launchd runs)
uv run gtt watch                 # fetch on a loop, every GTT_POLL_INTERVAL seconds
uv run gtt stats                 # what the archive holds
uv run gtt devices               # known trackers
uv run gtt export --format geojson > track.geojson
uv run gtt export --format gpx --since 7d --max-accuracy 50 > week.gpx
uv run gtt export --format csv --since 2026-08-01 > august.csv
```

`--since` takes a relative offset (`30m`, `24h`, `7d`, `2w`) or an ISO date/timestamp.
`--max-accuracy` drops imprecise fixes — the tracker falls back to cell-tower positioning indoors,
which produces accuracy radii in the hundreds of metres and badly inflates any distance you compute.

Exit codes: `0` success (including a skipped poll), `1` transient failure, `2` authentication
rejected — re-run `gtt login`, `3` no credentials yet.

### Live tracking

The tracker reports a fix about every ten minutes while it sits still and every ~20 seconds
while it moves, but it only keeps that cadence up for a short burst. The app's *Live-Tracking*
button is a single call, `PUT /devices/{serial}/enable_live_tracking`, that puts the device into
a ten-minute live mode of a fix every ~20 seconds. There is no "off" call; it just expires.

Both `poll` and `watch` use that call to keep live mode running during a daily window,
`GTT_LIVE_FROM` to `GTT_LIVE_UNTIL` in UTC (default 15:00–22:00): it is requested on the first
successful poll inside the window and renewed two minutes before each ten-minute grant runs
out. Outside the window nothing is renewed, so live mode expires on its own within ten minutes.
Live mode drains a full battery in about ten hours, which is what the window is for. Set both
variables empty for around the clock, or `GTT_LIVE_TRACKING=0` to turn the feature off.

Every request is recorded in `live_tracking_log` (`reason` is `start` or `renew`). To actually
catch 20-second fixes, poll at `GTT_POLL_INTERVAL=30`, as the Docker setup does.

### Run it on a schedule

```bash
scripts/install-launchd.sh
tail -f logs/poller.log
```

That installs a launchd agent firing `gtt poll` every 5 minutes, at login and after wake. Use
`gtt poll` under launchd rather than `gtt watch` — launchd owns the scheduling, restarts, and
catch-up after sleep. To remove it:

```bash
launchctl bootout gui/$(id -u)/com.higsch.gps-tracker-tracker
```

Laptop sleep produces gaps in the track rather than a stuck agent. `poll_log` tells you which
kind of gap you are looking at.

### Run it in Docker (Raspberry Pi)

A Pi is a better home for this than a laptop — it doesn't sleep, so the track has no gaps. Here
`gtt watch` is the right command: the container is the supervisor, and `restart: unless-stopped`
plays the role launchd plays on macOS.

**64-bit Raspberry Pi OS is required.** DuckDB publishes `aarch64` wheels but no `armv7` ones, so
on a 32-bit system the build fails with no wheel to install. Check with:

```bash
dpkg --print-architecture     # must print arm64
```

Clone the repo onto the Pi, then:

```bash
mkdir -p data config
docker compose build
docker compose run --rm tracker login    # interactive; writes config/credentials.json
docker compose up -d
docker compose logs -f
```

After pulling new changes, rebuild the image and recreate the container to pick them up:

```bash
scripts/deploy.sh
```

`login` is a one-off: it prompts for your shop email and password, prints the address it emailed,
and waits for you to open the link. Nothing is stored in the image — the device token lands in
`config/credentials.json` on the host, which the long-running container mounts. You can equally
copy that file over from a machine you have already run `gtt login` on and skip straight to
`up -d`. The shop password is never needed again, and deliberately isn't in the container.

`data/` and `config/` are bind mounts owned by uid 1000, the default Pi user. If yours differs,
build with your own ids:

```bash
PUID=$(id -u) PGID=$(id -g) docker compose build
```

Ad-hoc commands run against the same archive without disturbing the watcher:

```bash
docker compose exec tracker gtt stats
docker compose run --rm -T tracker export --format gpx --since 7d > week.gpx
```

`-T` matters whenever you redirect: `compose run` allocates a TTY by default, which would put
carriage returns through your GPX. Leave it off for `login`, which needs the TTY to prompt.

Two exits `watch` treats as unrecoverable — a rejected token (`2`) and missing credentials (`3`) —
turn into a restart loop under `unless-stopped`, since Docker cannot tell them from a crash. It
backs off to one attempt a minute, so it is cheap, but if `docker compose logs` shows the same
`authentication rejected` or `no credentials` line every minute, waiting won't fix it: run
`docker compose run --rm tracker login` again.

### Reaching the archive from outside the container

The bind mount is the whole trick: `data/tracker.duckdb` is an ordinary file on the Pi, not
something trapped in the container's writable layer. Install `duckdb` on the Pi and query it
directly.

Do open it **read-only**, though:

```bash
duckdb -readonly data/tracker.duckdb
```

DuckDB permits a single writer and a read-write connection would contend with the poller. This
works at all only because of the design described under [Concurrency](#concurrency) — `watch`
opens the database, writes, and closes it within each poll rather than holding it across the
sleep, so for almost all of every interval the file is free. Read-only is a shared lock and
cannot block the poller; read-write can, and `-readonly` turns a possible collision into a
clear refusal.

The directory needs to stay writable by the container even for reads, since DuckDB puts its
`.wal` alongside the database — so mount `data/`, never the `.duckdb` file on its own.

From another machine, treat it as the file it is: `scp pi@raspberrypi:.../data/tracker.duckdb .`
gives you a snapshot to query locally, or export a smaller slice with
`docker compose run --rm -T tracker export --format geojson --since 7d`.

## Configuration

All via environment variables, or a `.env` file in the working directory (real environment
variables win). See [.env.example](.env.example).

| Variable | Default | Meaning |
|---|---|---|
| `GTT_DB_PATH` | `./data/tracker.duckdb` | The archive |
| `GTT_CREDENTIALS_PATH` | `~/.config/gps-tracker-tracker/credentials.json` | Device tokens, mode `0600` |
| `GTT_POLL_INTERVAL` | `300` | Seconds between polls in `watch` |
| `GTT_REQUEST_TIMEOUT` | `10` | HTTP timeout per request |
| `GTT_LOCALE` | `de` | Language of the sign-in email |
| `GTT_LOG_LEVEL` | `INFO` | `DEBUG` logs full request URLs, which carry device tokens |
| `GTT_LIVE_TRACKING` | `1` | Keep the tracker in live mode, see [Live tracking](#live-tracking) |
| `GTT_LIVE_FROM` / `GTT_LIVE_UNTIL` | `15:00` / `22:00` | Daily window, UTC clock times, in which live mode is kept running; both empty means all day |
| `FRESSNAPF_EMAIL` / `FRESSNAPF_PASSWORD` | — | `login` only; prompted if unset |

## Schema

All timestamps are `TIMESTAMPTZ` stored in UTC. See [schema.sql](src/gps_tracker_tracker/schema.sql).

| Table | Grain | Notes |
|---|---|---|
| `positions` | one GPS fix | PK `(serialnumber, sampled_at)` — the track |
| `device_state` | one device report | PK `(serialnumber, observed_at)` — battery, charging, modes |
| `raw_snapshots` | one distinct response | PK `(serialnumber, payload_hash)` — verbatim JSON |
| `devices` | one tracker | Name, type, generation |
| `poll_log` | one poll attempt | Success/failure, duration — explains gaps |
| `live_tracking_log` | one live-mode request | Whether the server accepted it, and whether it started or renewed a window |

Every insert is `ON CONFLICT DO NOTHING`, so polling is idempotent: a retry, an overlapping
schedule, or a stationary tracker can never duplicate a fix.

`raw_snapshots` exists so a field that isn't modelled above can still be recovered later. Its hash
is taken over a *canonicalised* copy of the response, with `last_seen`, `last_position` and
`servicebooking.days_until_servicebooking_ends` removed — the API renders the first two as
human-readable relative strings (`"about 2 hours"`) that change on their own, so hashing the
payload verbatim would make every snapshot unique and store a duplicate every five minutes.

### Concurrency

DuckDB allows one writer per database file, and that also blocks read-only connections from other
processes. So the connection is opened, written, and closed *per poll* — never held across
`watch`'s sleep — and an advisory `flock` in front of it turns an overlapping poll into a clean
"skipping" log line instead of an exception. A concurrent `gtt poll` and `gtt watch` is safe; one
of them simply skips that tick.

## Querying

`uv run gtt stats` covers the basics without extra tooling. For real analysis, `brew install duckdb`
and query the file directly (while nothing is mid-write):

```sql
-- Recent fixes, in local time
SELECT sampled_at AT TIME ZONE 'Europe/Berlin' AS local_time, lat, lng, accuracy, battery
FROM positions ORDER BY sampled_at DESC LIMIT 20;
```

Distance travelled per day, via haversine, ignoring imprecise fixes:

```sql
WITH fixes AS (
    SELECT sampled_at, lat, lng,
           lag(lat) OVER w AS prev_lat,
           lag(lng) OVER w AS prev_lng
    FROM positions
    WHERE accuracy IS NULL OR accuracy <= 50
    WINDOW w AS (PARTITION BY serialnumber ORDER BY sampled_at)
)
SELECT sampled_at::DATE AS day,
       round(sum(2 * 6371000 * asin(sqrt(
           pow(sin(radians(lat - prev_lat) / 2), 2) +
           cos(radians(prev_lat)) * cos(radians(lat)) *
           pow(sin(radians(lng - prev_lng) / 2), 2)
       ))) / 1000, 2) AS km
FROM fixes
WHERE prev_lat IS NOT NULL
GROUP BY 1 ORDER BY 1 DESC;
```

Or let the spatial extension do the geometry, which also reads and writes GeoJSON/GPX directly:

```sql
INSTALL spatial; LOAD spatial;
SELECT round(sum(ST_Distance_Sphere(ST_Point(lat, lng), ST_Point(prev_lat, prev_lng))) / 1000, 2) AS km
FROM ( /* the `fixes` CTE above */ ) WHERE prev_lat IS NOT NULL;
```

> **Watch the argument order.** DuckDB's `ST_Distance_Sphere` reads the *first* ordinate as
> latitude, which is the opposite of the usual GIS `(longitude, latitude)` convention and of what
> `gtt export --format geojson` emits. `ST_Point(lng, lat)` does not error — it silently returns
> wrong distances (verified against duckdb 1.5.5 / spatial `eb1e57c`: a 1° east-west step at 60°N
> is 55.60 km, but comes back as 111.19 km with the arguments swapped).

Battery drain per day, and gaps in coverage:

```sql
SELECT observed_at::DATE AS day, min(battery), max(battery), count(*) FILTER (WHERE charging)
FROM device_state GROUP BY 1 ORDER BY 1 DESC;

-- Where did polling fail, rather than the tracker going quiet?
SELECT polled_at, serialnumber, error FROM poll_log WHERE NOT ok ORDER BY polled_at DESC LIMIT 20;
```

Anything not modelled is still in `raw_snapshots`:

```sql
SELECT fetched_at, payload->>'$.tracker_settings.features.live_tracking' AS live_tracking
FROM raw_snapshots ORDER BY fetched_at DESC LIMIT 5;
```

## Tests

```bash
uv run pytest
```

Offline — the DuckDB layer is driven against recorded API responses in `tests/fixtures/`, covering
deduplication, the self-drifting-field hash, missing positions, timestamp fallbacks and lock
contention.
