"""Keeping the tracker in live mode.

Left alone, the tracker reports a fix roughly every ten minutes while it sits
still and in bursts of one every ~20s while it moves. The Fressnapf app's
"Live-Tracking" button is a single call --

    PUT /devices/{serial}/enable_live_tracking?devicetoken=...

-- which puts the device into a 10-minute live mode of a fix every ~20s. There
is no disable call; the mode simply expires. While `Config.live_tracking` is on
the poller keeps that mode running continuously: it requests live mode on the
first poll and again shortly before every window runs out. Live mode drains a
full battery in about ten hours, so switch the feature off when that matters.

The route was taken from the app's bundle; the upstream client does not know it.
"""

import logging
from datetime import datetime, timedelta

from fressnapftracker import ApiClient
from fressnapftracker.exceptions import FressnapfTrackerError

log = logging.getLogger(__name__)

# The server grants ten minutes per request. Re-arm two minutes early so a poll
# interval of up to ~90s, plus the request itself, never leaves a gap.
LIVE_TRACKING_DURATION = timedelta(minutes=10)
RENEW_AFTER = timedelta(minutes=8)


def live_tracking_due(last_requested_at: datetime | None, *, now: datetime) -> bool:
    """Whether a request now would start or extend live mode, rather than be a no-op."""
    return last_requested_at is None or now - last_requested_at >= RENEW_AFTER


def live_tracking_reason(last_requested_at: datetime | None, *, now: datetime) -> str | None:
    """Why to request live mode now, or None while the current window still has time.

    "start" when no window is running any more (or none was ever requested),
    "renew" when the running one is about to expire. The distinction is only
    for the log; both send the same request.
    """
    if not live_tracking_due(last_requested_at, now=now):
        return None
    if last_requested_at is None or now - last_requested_at >= LIVE_TRACKING_DURATION:
        return "start"
    return "renew"


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
