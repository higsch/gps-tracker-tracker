# Cat tracker HTTP API: integration guide

This app shows a pet GPS tracker on a map. The data comes from a separate service,
**gps-tracker-tracker**, which polls the tracker's cloud API and archives every position in a
DuckDB file. That service also serves an HTTP API, and this app should use it for everything:
reading tracker data and switching live tracking on and off.

**Do not open the DuckDB file directly** (no `duckdb` package, no `CAT_TRACKER_DB_PATH`). A reader
in another process bypasses the service's lock and can make its writes fail. The API reads through
that lock.

## Configuration

Two server-side environment variables, read with `$env/dynamic/private`:

| Variable | Example | Meaning |
|---|---|---|
| `CAT_TRACKER_API_URL` | `http://raspberrypi.local:8080` | Base URL of the service. No trailing path |
| `CAT_TRACKER_API_TOKEN` | 64 hex characters | Bearer token, the same value as the service's `GTT_API_TOKEN` |

The token is a secret. Use it only in server code (`+server.js`, `+page.server.js`,
`$lib/server/*`). It must never reach the browser, so the browser always goes through this app's
own endpoints, which call the tracker API.

## Conventions

- **Authentication:** every request sends `Authorization: Bearer <CAT_TRACKER_API_TOKEN>`.
- **Format:** responses are JSON. Request bodies, where accepted, are JSON objects.
- **Field names** are snake_case. Map them to camelCase in this app's server code if the frontend
  expects camelCase.
- **Timestamps** are ISO 8601 strings in UTC with an explicit offset, sometimes with fractional
  seconds: `"2026-11-02T18:04:11+00:00"` or `"2026-11-02T18:04:11.482113+00:00"`. Parse them with
  `new Date(...)`.
- **Nullable fields** are marked "or null" below. Handle `null` everywhere it can appear.
- **Errors** come back as `{"error": "<human-readable message>"}` with a matching status code:

| Status | When |
|---|---|
| `400` | Bad query parameter or body: unknown name, out of range, not a whole number, not a JSON object |
| `401` | Missing or wrong token |
| `404` | Unknown route |
| `405` | Wrong method for a known route |
| `503` | `GET /devices` only: the service was busy writing for more than 5 s. Comes with `Retry-After: 2`; just try again on the next refresh |
| `500` | Unexpected server error |

## Routes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/devices` | Everything for the dashboard: live-tracking status and all trackers with trail and heatmap |
| `GET` | `/live-tracking` | The live-tracking status alone |
| `POST` | `/live-tracking/activate` | Switch live tracking on, optionally for a number of minutes |
| `POST` | `/live-tracking/deactivate` | Switch live tracking off |

### `GET /devices`

One request returns everything the dashboard needs, so call this on every refresh.

Query parameters, all optional:

| Parameter | Default | Range | Meaning |
|---|---|---|---|
| `trail_hours` | `6` | 1–744 | `trail` holds every fix from the last this many hours |
| `heatmap_days` | `7` | 0–366 | `heatmap` holds positions from this many days ago up to where the trail starts. `0` means no heatmap |
| `max_accuracy` | `10` | 0–100000 | Drop fixes with an accuracy radius above this many metres. `0` keeps every fix. Fixes with unknown accuracy are always kept |

Values must be whole numbers. Any other parameter name gets a `400`.

Response:

```json
{
  "live_tracking": {
    "enabled": true,
    "until": null,
    "changed_at": "2026-11-02T17:40:00+00:00",
    "devices": [
      {"serialnumber": "231511297", "live_until": "2026-11-02T18:02:30+00:00"}
    ]
  },
  "devices": [
    {
      "serialnumber": "231511297",
      "name": "Minka",
      "battery": 76,
      "charging": false,
      "inside_geofence": true,
      "observed_at": "2026-11-02T18:04:11+00:00",
      "trail": [
        {"sampled_at": "2026-11-02T17:55:02+00:00", "lat": 52.520008, "lng": 13.404954, "accuracy": 3}
      ],
      "current": {"sampled_at": "2026-11-02T17:55:02+00:00", "lat": 52.520008, "lng": 13.404954, "accuracy": 3},
      "heatmap": [
        {"lat": 52.51, "lng": 13.41}
      ]
    }
  ]
}
```

`live_tracking` is the live-tracking status document, described under
[`GET /live-tracking`](#get-live-tracking).

Each entry in `devices` is one tracker, sorted by `serialnumber`:

| Field | Type | Meaning |
|---|---|---|
| `serialnumber` | string | The tracker's ID. Use it as the key |
| `name` | string or null | The pet's name as set in the Fressnapf app |
| `battery` | integer or null | Battery in percent, from the latest state report |
| `charging` | boolean or null | Whether it is on the charger |
| `inside_geofence` | boolean or null | Whether it is inside the home zone |
| `observed_at` | timestamp or null | When that latest state report was taken |
| `trail` | array | Recent fixes, oldest first: `sampled_at` (timestamp of the GPS fix), `lat`, `lng` (numbers), `accuracy` (radius in metres, integer or null) |
| `current` | object or null | The last element of `trail`, the same object. `null` when the trail is empty |
| `heatmap` | array | Older positions as `{lat, lng}`, in no particular order. They never overlap the trail. Can hold thousands of points |

An empty `devices` array means the service hasn't completed its first poll yet.

### `GET /live-tracking`

Returns the status document alone. `activate` and `deactivate` return the same document, so one
parsing function covers every route that returns it.

```json
{
  "enabled": true,
  "until": "2026-11-02T19:00:00+00:00",
  "changed_at": "2026-11-02T18:00:00+00:00",
  "devices": [
    {"serialnumber": "231511297", "live_until": "2026-11-02T18:10:02+00:00"}
  ]
}
```

| Field | Type | Meaning |
|---|---|---|
| `enabled` | boolean | The switch position. Turns `false` on its own once `until` has passed |
| `until` | timestamp or null | When a timed activation ends. `null` while switched off, or when activated without a duration |
| `changed_at` | timestamp or null | When the switch was last flipped. `null` if it never was |
| `devices` | array or null | One entry per tracker that ever went live. `live_until` (timestamp or null) is when the tracker's current live period ends, `null` if it isn't live. The whole array is `null` when the service was too busy to read it, so keep the previous value |

### `POST /live-tracking/activate`

Switches live tracking on. The body is optional:

- no body, or `{}`: stays on until deactivated;
- `{"minutes": 60}`: switches itself off after that many minutes. Must be a whole number from 1 to
  the service's maximum (600 by default, about one full battery). Anything else gets a `400` and
  changes nothing.

Returns the status document.

### `POST /live-tracking/deactivate`

Switches live tracking off. No body. Returns the status document.

## How live tracking behaves

This shapes the UI, so read it before building the switch.

- **What it is:** normally the tracker reports a position about every 10 minutes. In live mode it
  reports about every 20 seconds. Live mode drains a full battery in about 10 hours.
- **How the service keeps it running:** the tracker's cloud grants live mode 10 minutes at a time.
  While the switch is on, the service renews it shortly before each grant runs out.
- **Switching on is quick.** The service starts live mode within about a second, but `live_until`
  only appears once the tracker's cloud has accepted the request, usually a second or two later.
  The status the activate call returns may not show it yet.
- **Switching off is not immediate.** The tracker's cloud has no "off" command. Deactivating stops
  the renewals, so the tracker stays live until its current grant ends, up to 10 minutes later.
  During that time `enabled` is `false` while `live_until` is still in the future. Show that state,
  for example "Live tracking off, stopping at 18:12", so the switch doesn't look broken.
- **A timed activation can overrun `until`** by up to 10 minutes, for the same reason.
- **The switch belongs to the service.** Other clients can flip it too, so always render it from
  the latest status, not from local state.

## Refresh cadence

The service polls the tracker about every 30 seconds, and live fixes arrive about every 20
seconds. Fetching `/devices` every 30 seconds on the server and pushing the result to browsers is
enough. Don't go much faster than every 10 seconds: each request briefly locks the archive.

After this app flips the switch, fetch `/devices` once immediately and push it to browsers, so the
UI updates without waiting for the next tick.

## Reference implementation

This replaces an earlier version that read DuckDB directly. Adapt the names to the app.

`$lib/server/cat-tracker.js`:

```js
import { env } from '$env/dynamic/private';

const MAX_ACCURACY_METERS = 10;

function trackerApi(path, init = {}) {
	return fetch(new URL(path, env.CAT_TRACKER_API_URL), {
		...init,
		headers: {
			Authorization: `Bearer ${env.CAT_TRACKER_API_TOKEN}`,
			...(init.body ? { 'Content-Type': 'application/json' } : {}),
			...init.headers
		},
		signal: AbortSignal.timeout(10_000)
	});
}

async function readJson(response) {
	const body = await response.json().catch(() => ({}));
	if (!response.ok) {
		const err = new Error(body.error ?? `tracker API answered ${response.status}`);
		err.status = response.status;
		throw err;
	}
	return body;
}

function mapLiveTracking(live) {
	return {
		enabled: live.enabled,
		until: live.until,
		changedAt: live.changed_at,
		// serialnumber -> ISO timestamp, or null when not live. Absent when the service was busy.
		liveUntil: live.devices
			? Object.fromEntries(live.devices.map((d) => [d.serialnumber, d.live_until]))
			: null
	};
}

const mapFix = (fix) => ({
	sampledAt: fix.sampled_at,
	lat: fix.lat,
	lng: fix.lng,
	accuracy: fix.accuracy
});

export async function fetchTracker() {
	const params = new URLSearchParams({
		trail_hours: '6',
		heatmap_days: '7',
		max_accuracy: String(MAX_ACCURACY_METERS)
	});
	const { live_tracking, devices } = await readJson(await trackerApi(`/devices?${params}`));
	return {
		liveTracking: mapLiveTracking(live_tracking),
		devices: devices.map((device) => ({
			serialnumber: device.serialnumber,
			name: device.name,
			battery: device.battery,
			charging: device.charging,
			insideGeofence: device.inside_geofence,
			observedAt: device.observed_at,
			trail: device.trail.map(mapFix),
			current: device.current ? mapFix(device.current) : null,
			heatmap: device.heatmap
		}))
	};
}

export async function setLiveTracking(enabled, minutes) {
	const path = enabled ? '/live-tracking/activate' : '/live-tracking/deactivate';
	const body = enabled && minutes ? JSON.stringify({ minutes }) : undefined;
	return mapLiveTracking(await readJson(await trackerApi(path, { method: 'POST', body })));
}
```

`src/routes/api/live-tracking/+server.js`, the browser-facing switch:

```js
import { error, json } from '@sveltejs/kit';
import { setLiveTracking } from '$lib/server/cat-tracker.js';
import { fetchDataAndNotifyClients } from '$lib/server/cat-tracker-stream.js'; // the SSE module

export async function POST({ request }) {
	const { enabled, minutes } = await request.json();
	try {
		const liveTracking = await setLiveTracking(Boolean(enabled), minutes);
		fetchDataAndNotifyClients(); // push fresh state to every open tab
		return json(liveTracking);
	} catch (err) {
		error(err.status ?? 502, err.message);
	}
}
```

In the SSE module, `fetchDataAndNotifyClients` must be exported at the module's top level, not
created inside the function that starts the interval. It calls `fetchTracker()` and posts
`{ type: 'cat_tracker_changed', payload: { liveTracking, devices } }` to every client. If a fetch
fails (a `503` included), log it and keep the previous data; the next tick tries again.

## Checking it by hand

```bash
curl -H "Authorization: Bearer $CAT_TRACKER_API_TOKEN" "$CAT_TRACKER_API_URL/live-tracking"
curl -H "Authorization: Bearer $CAT_TRACKER_API_TOKEN" "$CAT_TRACKER_API_URL/devices?heatmap_days=0"
curl -H "Authorization: Bearer $CAT_TRACKER_API_TOKEN" -X POST \
     -H "Content-Type: application/json" -d '{"minutes": 30}' \
     "$CAT_TRACKER_API_URL/live-tracking/activate"
```
