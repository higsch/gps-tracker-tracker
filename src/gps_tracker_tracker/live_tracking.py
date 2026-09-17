"""Keeping the tracker in live mode.

Left alone, the tracker reports a fix roughly every ten minutes while it sits
still and in bursts of one every ~20s while it moves. The Fressnapf app's
"Live-Tracking" button is a single call --

    PUT /devices/{serial}/enable_live_tracking?devicetoken=...

-- which puts the device into a 10-minute live mode of a fix every ~20s. There
is no disable call; the mode simply expires. While `Config.live_tracking` is on
the poller keeps that mode running throughout a daily window of UTC clock times
(`Config.live_from` to `Config.live_until`, 15:00-22:00 by default): it requests
live mode on the first poll inside the window and again shortly before every
ten-minute grant runs out. Outside the window it stops renewing, so live mode
dies on its own within ten minutes. Live mode drains a full battery in about ten
hours, which is what the window is for.

The route was taken from the app's bundle; the upstream client does not know it.
"""

import logging
from datetime import UTC, datetime, time, timedelta
from typing import Any

import fressnapftracker.fressnapftracker as upstream
from fressnapftracker import ApiClient
from fressnapftracker.exceptions import FressnapfTrackerError

log = logging.getLogger(__name__)

# The server grants ten minutes per request. Re-arm two minutes early so a poll
# interval of up to ~90s, plus the request itself, never leaves a gap.
LIVE_TRACKING_DURATION = timedelta(minutes=10)
RENEW_AFTER = timedelta(minutes=8)


def in_live_hours(now: datetime, *, start: time | None, end: time | None) -> bool:
    """Whether `now` falls inside the daily [start, end) window of UTC clock times.

    No bound on either side means always. A window with end before start
    wraps past midnight, e.g. 22:00-03:00.
    """
    if start is None or end is None:
        return True
    clock = now.astimezone(UTC).time()
    if start < end:
        return start <= clock < end
    return clock >= start or clock < end


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
    """The upstream device client plus the calls it is missing."""

    async def get_positions(self, *, hours_ago: int, sample: bool = False) -> list[dict[str, Any]]:
        """The fixes of the last `hours_ago` hours (at most 24), oldest first.

        Each is `{"lat": "53.45", "lng": "9.94", "h_pos_error": 2, "created_at": ...}`
        -- coordinates as strings, accuracy in metres. `sample=True` asks the
        server to thin the list to roughly one fix a minute; the poller never
        does, it wants every fix. See history.py.
        """
        # Built by hand rather than through _device_request so that the extra
        # query parameters travel alongside devicetoken. API_BASE_URL is read at
        # call time on purpose: the tests point it at a local server.
        url = f"{upstream.API_BASE_URL}/devices/{self._serial_number}/positions"
        params = {
            "devicetoken": self._device_token,
            "hours_ago": hours_ago,
            "sample": "true" if sample else "false",
        }
        result = await self._request("GET", url, self._get_device_headers(), params=params)
        if isinstance(result, dict):
            # {"error": ...} for a bad hours_ago or token; anything else is unexpected.
            self._handle_device_error(result)
            raise FressnapfTrackerError(f"unexpected positions response: {result!r}")
        return result

    async def enable_live_tracking(self) -> str:
        """Ask the server to put the device into live mode. Returns its message."""
        # Same base URL, headers, `devicetoken` query and error mapping as
        # get_tracker(); the route is the only thing upstream does not know.
        result = await self._device_request("PUT", "/enable_live_tracking")
        if isinstance(result, dict) and result.get("success") is False:
            raise FressnapfTrackerError(result.get("message") or "live tracking refused")
        message = result.get("message") if isinstance(result, dict) else None
        return message or "live tracking enabled"
