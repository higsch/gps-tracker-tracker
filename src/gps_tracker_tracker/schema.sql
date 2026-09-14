-- Applied on every write connection. Every statement is idempotent, so there is
-- no separate migration step.

CREATE TABLE IF NOT EXISTS schema_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR
);

CREATE TABLE IF NOT EXISTS devices (
    serialnumber    VARCHAR PRIMARY KEY,
    name            VARCHAR,
    tracker_type    VARCHAR,
    generation      VARCHAR,
    icon_url        VARCHAR,
    first_seen_at   TIMESTAMPTZ,
    last_updated_at TIMESTAMPTZ
);

-- The GPS track. sampled_at is the fix time reported by the tracker, so
-- re-polling a stationary device inserts nothing.
CREATE TABLE IF NOT EXISTS positions (
    serialnumber    VARCHAR NOT NULL,
    sampled_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ,
    lat             DOUBLE NOT NULL,
    lng             DOUBLE NOT NULL,
    accuracy        INTEGER,
    battery         INTEGER,
    inside_geofence BOOLEAN,
    ingested_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (serialnumber, sampled_at)
);

CREATE TABLE IF NOT EXISTS device_state (
    serialnumber         VARCHAR NOT NULL,
    observed_at          TIMESTAMPTZ NOT NULL,
    battery              INTEGER,
    charging             BOOLEAN,
    inside_geofence      BOOLEAN,
    led_brightness       INTEGER,
    deep_sleep           INTEGER,
    energy_saving        INTEGER,
    led_activatable      BOOLEAN,
    servicebooking_until TIMESTAMPTZ,
    ingested_at          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (serialnumber, observed_at)
);

-- The untouched API response, so a field we did not model above can still be
-- recovered later. payload_hash is taken over a canonicalised copy with the
-- self-drifting fields removed, otherwise every snapshot would look unique.
CREATE TABLE IF NOT EXISTS raw_snapshots (
    serialnumber VARCHAR NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL,
    payload      JSON NOT NULL,
    payload_hash VARCHAR NOT NULL,
    PRIMARY KEY (serialnumber, payload_hash)
);

-- One row per device per poll attempt, successful or not. This is what tells you
-- whether a gap in the track is a sleeping tracker or a broken poller.
CREATE TABLE IF NOT EXISTS poll_log (
    polled_at     TIMESTAMPTZ NOT NULL,
    serialnumber  VARCHAR,
    ok            BOOLEAN NOT NULL,
    error         VARCHAR,
    new_positions INTEGER,
    duration_ms   INTEGER
);

-- One row per live-tracking request. The API's enable_live_tracking call puts
-- the device into a 10-minute live mode (a fix every ~20s instead of every ~10
-- minutes); the poller sends it only while the fixes show motion, and this is
-- how it knows when the current window runs out. `reason` is the displacement
-- that justified the request.
CREATE TABLE IF NOT EXISTS live_tracking_log (
    requested_at TIMESTAMPTZ NOT NULL,
    serialnumber VARCHAR NOT NULL,
    ok           BOOLEAN NOT NULL,
    message      VARCHAR,
    reason       VARCHAR
);
