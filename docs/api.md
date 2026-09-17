# Fressnapf tracker API

Route map of the cloud API behind the Fressnapf GPS tracker, as used by the Fressnapf Tracker
app (Android package `com.iotventure.fressnapf`, version 2.9.5, React Native / Hermes) and by the
`fressnapftracker` Python library this project builds on. Routes marked **verified** were called
against a generation 2.1 cat tracker on 2026-09-17; the rest are taken from the decompiled app
bundle and have not been exercised. Nothing here is officially documented by Fressnapf or IoT
Venture, so any of it can change without notice.

## Hosts and authentication

| Host | Purpose | Auth |
|---|---|---|
| `https://itsmybike.cloud/api/pet_tracker/v2` | Device API: one tracker at a time | Static header `authorization: Token token=<CLOUD_AUTH_TOKEN>` plus `?devicetoken=<device token>` on every call |
| `https://user.iot-pet-tracking.cloud/api/app/v1` | User API: account, devices, pets, notifications | `?user_id=<id>&user_access_token=<token>` from the magic-link login; a staging twin exists at `user.iot-pet-tracking-staging.cloud` |
| `https://api.os.fressnapf.com` | Fressnapf shop: only used to look up the customer id during login | OAuth password grant with the shop credentials |

The device API host is not fixed. The user API's device list returns a `domain` per device and the
app calls `<domain>/api/pet_tracker/v2/...`; for this tracker that is `itsmybike.cloud`.

The device token is long-lived and is all the poller keeps. The user access token is only held
during `gtt login` and is not stored, so the user API routes below are not reachable from the
archive today.

**Rate limit.** Roughly 40 requests in a short burst from one IP answer `429` with a `text/plain`
body of `Retry later` for a couple of minutes. The Pi poller and any experiments from a laptop on
the same connection share that budget.

## Device API

All paths are relative to `https://itsmybike.cloud/api/pet_tracker/v2/devices/{serial}` and take
`devicetoken` as a query parameter.

### `GET ""` — current state — verified

The payload the archive stores in `raw_snapshots`. Notable fields:

| Field | Type | Notes |
|---|---|---|
| `name`, `serialnumber`, `icon` | string | |
| `battery` | int | Percent |
| `charging` | bool | Also mirrored inverted as `led_activatable.not_charging`. Only ever observed `false` on this device, including at 100 % |
| `position` | object or null | `lat`, `lng` (floats), `accuracy` (metres, int), `created_at`, `sampled_at` (ISO 8601 with offset), `timestamp` (null) |
| `last_position_accuracy`, `last_position_timestamp`, `last_seen_timestamp` | int / string | |
| `last_seen`, `last_position` | string | Human-readable relative strings such as `"4 minuten"`; they change on their own, so the archive strips them before hashing |
| `inside_geofence` | bool | Always `false` on this device; `tracker_settings.features.geofence` is `false` too |
| `tracker_settings` | object | `generation` (`"2.1"`), `type` (`"cat"`), `sales_channel`, `features.{flash_light, sleep_mode, energy_saving_mode, geofence, resale_price_prediction}`. The library models a `live_tracking` feature flag that this device does not send |
| `led_brightness`, `deep_sleep`, `energy_saving` | object | Each `{value: int, status: "ok"}` |
| `led_activatable` | object | `has_led`, `seen_recently`, `nonempty_battery`, `not_charging`, `overall` |
| `servicebooking` | object | `has_current_servicebooking`, `servicebooking_until`, `days_until_servicebooking_ends` |
| `additional_parameters` | string | JSON-in-a-string with pet profile bits such as breed |

### `GET /positions` — position history — verified

| Parameter | Values | Notes |
|---|---|---|
| `hours_ago` | 1 – 24 | Required. 48 and 168 return `400 {"error": ...}`; the app never asks for more than 24 |
| `sample` | `true` / `false` | `true` thins the list to roughly one fix per minute (146 fixes became 50 for a 3 h window) |

Response: a JSON array, oldest first, of

```json
{"lat": "53.452455", "lng": "9.942515", "h_pos_error": 2, "created_at": "2026-09-17T19:01:23.000+02:00"}
```

`lat` and `lng` are strings here, unlike in the current-state payload. `h_pos_error` is the
horizontal accuracy in metres. In live mode the list contains every 20-second fix, so this is the
route for backfilling anything the poller missed, and the poller does exactly that on every poll
(see [history.py](../src/gps_tracker_tracker/history.py) and `GTT_BACKFILL`). The app's "cat
activity" screen is computed client-side from this data; there is no separate activity endpoint.

### `PUT /enable_live_tracking/` — start or renew live mode — verified

No body. Response:

```json
{"success": true, "message": "Live Tracking enabled for 10 minutes."}
```

The tracker then reports a fix every ~20 s for ten minutes. There is no disable route; re-sending
renews the grant. The app polls the current state every 15 s normally and every 5 s while live
mode is on. Continuous live mode drains a full battery in about ten hours. The library does not
know this route; it lives in [live_tracking.py](../src/gps_tracker_tracker/live_tracking.py).

### `GET /live_tracking_datapoints` — fixes of the current live session — verified

```json
{"datapoints": [{"lat": "53.452568", "lng": "9.942635", "created_at": "2026-09-17T21:39:51.000+02:00",
                 "sampled_at": "2026-09-17T21:39:51.000+02:00", "battery": 4190}]}
```

Only the fixes of the running live session. `battery` is in **millivolts** (4190 ≈ a full lithium
cell); it is the only place the API exposes voltage rather than percent, so a rising voltage between
points is a usable charging signal.

### Geofences — `/fences` verified, the rest from the app

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/fences` | | Returns `{"fences": [...]}`; empty for this device |
| `GET` | `/fences/{id}` | | One fence |
| `PATCH` | `/fences/{id}` | `{"fence": {...}}` | Update; fences are polygons, the exact field list was not recovered |
| `DELETE` | `/fences/{id}` | | |

No create route was found in the bundle, so fences are presumably created through the user API or
the PATCH is an upsert. The app labels the feature as beta and warns that GPS inaccuracy can cause
false enter/leave pushes. The route works with the device token even though this device reports
`features.geofence: false`.

### Settings

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `/led_brightness_status` | | Same `{value, status}` object as in the current-state payload |
| `PUT` | `/change_led_brightness` | `{"value": <int>}` | In the library as `set_led_brightness` |
| `GET` | `/deep_sleep_status` | | |
| `PUT` | `/change_deep_sleep` | `{"value": 0 or 1}` | In the library as `set_deep_sleep` |
| `PATCH` | `/energy_saving/{mode}` | | `mode` is the state to switch to; in the library as `set_energy_saving` |

These only change or re-read what the current-state payload already contains, so there is nothing
extra to archive from them.

## User API

All paths are relative to `https://user.iot-pet-tracking.cloud/api/app/v1/` and take
`user_id` and `user_access_token` as query parameters unless noted. Only the login routes are used
by this project, via the library; the rest come from the app bundle and are untested.

### Login flow (library)

| Method | Path | Notes |
|---|---|---|
| `POST` | `https://api.os.fressnapf.com/authorizationserver/oauth/token` | Shop OAuth password grant with the Fressnapf shop credentials |
| `GET` | `https://api.os.fressnapf.com/rest/v2/FressnapfDE/users/{email}` | Fetches the shop customer id, which is embedded in `additional_parameters` |
| `POST` | `magic_link_auth` | Body `{"user": {email, locale, tracker_service: "fressnapf", user_token: {push_token, app_version, app_platform, platform_version, phone_name}, additional_parameters}}`. Fressnapf then emails a magic link |
| `GET` | `magic_link_auth` | Polled with `Token token="<user access token>"` until the link was clicked |
| `PATCH` | `users/update` | Completes the login; body `{"user": {additional_parameters, notification_enabled}}` |
| `GET` | `devices/` | Lists the account's trackers as `{serialnumber, token, ...}` plus the per-device `domain`. This is where the device tokens come from |

Phone-number routes (`users/request_sms_code`, `users/request_sms_code_for_existing_user`,
`users/verify_phone_number`) still exist in both the app and the library but the SMS flow is dead;
see the README.

### Account and pets (app only)

| Method | Path | Body | Notes |
|---|---|---|---|
| `GET` | `users/myself` | | Profile: `id`, `email`, `phone`, `notification_enabled`, `locale`, `additional_parameters`, `tracker_service`, `app_staging_allowed` |
| `PATCH` | `users/update` | `{"user": {...}}` | |
| `DELETE` | `users/{id}` | | Account deletion |
| `GET` | `pet_profile` | | Pet details shown in the app (breed, weight, health screens are built on it) |
| `GET` | `pet_tags` | | The separate "Tag" product |
| `POST` | `devices/claim` | `{"device": {"claim_token": ...}}` | Adds a tracker scanned from its key card |
| `DELETE` | `devices/{serial}` | | Removes a tracker from the account |
| `GET` | `qr_code/` | | QR code for the missing-pet poster |
| `GET` | `mobile_app_settings/fressnapf` | | Remote app configuration |
| `POST` | `user_migrations/trigger` | | Migration to the newer pet user layer |

### Notifications and missing-pet flow (app only)

| Method | Path | Notes |
|---|---|---|
| `GET` | `messages/` | Notification history; items carry a `notification_type`. The only route that would let you archive battery and geofence alerts |
| `POST` | `register` / `DELETE` `unregister` | Push token registration for the phone |
| `PATCH` | `user_tokens/update`, `DELETE` `user_tokens/destroy` | Push token bookkeeping |
| `GET` | `missing_case` | The active "pet missing" case |
| `GET` | `missing_case/missing_case_messages` | Messages from finders |
| `POST` | `missing_case/set_as_missed` | Opens a case |
| `POST` | `missing_case/set_finder_as_contacted` | |
| `PATCH` | `missing_case/close` | |

## What is not there

- No websocket, server-sent-events or push channel for positions. Every client polls.
- No activity, step or health endpoint; those screens derive from `positions` and the pet profile.
- No history beyond 24 hours. The archive is the only long-term record.

## Reproducing this

```bash
brew install apkeep
apkeep -a com.iotventure.fressnapf -d apk-pure .
unzip com.iotventure.fressnapf.xapk -d xapk && unzip xapk/com.iotventure.fressnapf.apk 'assets/*' -d base
uvx --from hermes-dec hbc-decompiler base/assets/index.android.bundle bundle.dec.js
grep -o -E "'[^']*(devicetoken=|user_id=)[^']*'" bundle.dec.js | sort -u
```

Plain `strings` on the Hermes bundle is misleading: the string table is one contiguous blob, so
neighbouring literals run together. Grep the decompiled JavaScript instead.
