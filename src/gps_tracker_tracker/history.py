"""Backfilling the track from the API's position history.

Besides the current fix, the device API serves the last day of fixes:

    GET /devices/{serial}/positions?devicetoken=...&hours_ago=N&sample=false

with N capped at 24 by the server (larger values are a 400). Unsampled, the
list holds every fix the tracker reported -- one every ~20s in live mode, more
than a poller on a 30s interval can catch one at a time. So every poll also
pulls that history and inserts whatever the archive does not have yet; the
`(serialnumber, sampled_at)` key makes the insert idempotent.

How far back to ask is sized from the newest fix already stored: normally that
is minutes old and one hour is plenty, after an outage it stretches to the full
day the API allows, so the gap heals on the first poll that comes back.

The route was taken from the app's bundle, see docs/api.md.
"""

import math
from datetime import datetime

# The server rejects anything further back.
MAX_HISTORY_HOURS = 24


def backfill_hours(latest_fix_at: datetime | None, *, now: datetime) -> int:
    """How many hours of history to request, given the newest fix already stored."""
    if latest_fix_at is None:
        return MAX_HISTORY_HOURS
    hours = math.ceil((now - latest_fix_at).total_seconds() / 3600)
    return min(MAX_HISTORY_HOURS, max(1, hours))
