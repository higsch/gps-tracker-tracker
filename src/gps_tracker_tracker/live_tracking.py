"""Motion-gated live tracking.

Left alone, the tracker reports a fix roughly every ten minutes while it sits
still and in bursts of one every ~20s while it moves. The Fressnapf app's
"Live-Tracking" button is a single call --

    PUT /devices/{serial}/enable_live_tracking?devicetoken=...

-- which puts the device into a 10-minute live mode of a fix every ~20s. There
is no disable call; the mode simply expires. Live mode drains the battery in
about ten hours, so it must not be left on all day. The rule here is:

* when a new fix lands, compare it with the fixes just before it; if it has
  moved more than `Config.live_motion_metres` from any of them, request live
  tracking;
* while a live-tracking window is still running, do nothing;
* when it is about to run out, request again *only* if the newest fixes still
  show motion. A pet that has stopped moving therefore falls back to the normal
  cadence within ten minutes, all by itself.

The route was taken from the app's bundle; the upstream client does not know it.
"""

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from fressnapftracker import ApiClient
from fressnapftracker.exceptions import FressnapfTrackerError

log = logging.getLogger(__name__)

# The server grants ten minutes per request. Re-arm two minutes early so a poll
# interval of up to ~90s, plus the request itself, never leaves a gap.
LIVE_TRACKING_DURATION = timedelta(minutes=10)
RENEW_AFTER = timedelta(minutes=8)

_EARTH_RADIUS_M = 6_371_000.0


@dataclass(frozen=True, slots=True)
class Fix:
    """One GPS fix as far as motion detection cares."""

    sampled_at: datetime
    lat: float
    lng: float
    accuracy: int | None = None


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(math.sqrt(a))


def reference_fixes(fixes: list[Fix], *, window: timedelta) -> list[Fix]:
    """Which earlier fixes the newest one is compared against.

    Always the one immediately before it -- in normal mode that is the only
    recent one there is, and a displaced fix after a quiet spell is exactly
    "starting to move". Plus everything inside `window`, so that in live mode a
    pet pausing for a moment mid-walk is still compared with where it was a
    couple of minutes ago rather than only with the fix 20s earlier.
    """
    if len(fixes) < 2:
        return []
    newest = fixes[0]
    references = [fixes[1]]
    for fix in fixes[2:]:
        if newest.sampled_at - fix.sampled_at <= window:
            references.append(fix)
    return references


def motion_reason(fixes: list[Fix], *, threshold_m: float, window: timedelta) -> str | None:
    """Describe the motion in the newest fix, or None if it is standing still.

    `fixes` is newest first. A displacement only counts if it exceeds both the
    configured threshold and the accuracy radius of either fix involved, so a
    cell-tower fallback fix a few hundred metres off does not read as a walk.
    """
    if not fixes:
        return None
    newest = fixes[0]
    best: tuple[float, Fix] | None = None
    for reference in reference_fixes(fixes, window=window):
        distance = haversine_m(newest.lat, newest.lng, reference.lat, reference.lng)
        if distance <= max(threshold_m, newest.accuracy or 0, reference.accuracy or 0):
            continue
        if best is None or distance > best[0]:
            best = (distance, reference)
    if best is None:
        return None
    distance, reference = best
    elapsed = int((newest.sampled_at - reference.sampled_at).total_seconds())
    return f"moved {distance:.0f}m in {elapsed}s"


def live_tracking_due(last_requested_at: datetime | None, *, now: datetime) -> bool:
    """Whether a request now would start or extend live mode, rather than be a no-op."""
    return last_requested_at is None or now - last_requested_at >= RENEW_AFTER


class LiveTrackingClient(ApiClient):
    """The upstream device client plus the one call it is missing."""

    async def enable_live_tracking(self) -> str:
        """Ask the server to put the device into live mode. Returns its message."""
        # Same base URL, headers, `devicetoken` query and error mapping as
        # get_tracker(); the route is the only thing upstream does not know.
        result = await self._device_request("PUT", "/enable_live_tracking")
        if isinstance(result, dict) and result.get("success") is False:
            raise FressnapfTrackerError(result.get("message") or "live tracking refused")
        message = result.get("message") if isinstance(result, dict) else None
        return message or "live tracking enabled"
