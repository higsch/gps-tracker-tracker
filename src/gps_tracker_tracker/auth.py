"""Email magic-link sign-in and storage of the resulting device tokens.

The Fressnapf app dropped phone-number authentication, so email is the only way
in. `login` runs the three-step flow once; everything it produces that polling
needs is the (serialnumber, device token) pair, and those are long-lived -- the
poller never re-authenticates.
"""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx
from fressnapftracker import AuthClient
from fressnapftracker.exceptions import (
    FressnapfTrackerAuthenticationError,
    FressnapfTrackerError,
)
from fressnapftracker.fressnapftracker import (
    AUTH_BASE_URL,
    CLOUD_AUTH_TOKEN,
    LIB_VERSION,
    SHOP_API_BASE_URL,
    SHOP_CLIENT_ID,
    SHOP_CLIENT_SECRET,
    SHOP_SITE,
)

log = logging.getLogger(__name__)

CREDENTIALS_VERSION = 1
_MAGIC_LINK_POLL_INTERVAL = 3.0
_MAGIC_LINK_TIMEOUT = 300.0

# What the Android app reports about itself. Only used to shape the sign-in
# request the way the tracker cloud expects.
_APP_VERSION = "2.9.5_2"
_APP_PLATFORM_VERSION = 34
_USER_AGENT = f"fressnapftracker/{LIB_VERSION}"


class LoginError(RuntimeError):
    """Sign-in failed."""


class ShopLoginError(LoginError):
    """The Fressnapf shop rejected the credentials, before any email was sent."""


class MagicLinkTimeoutError(LoginError):
    """The sign-in link was not opened in time."""


class NoCredentialsError(RuntimeError):
    """No device tokens on disk yet."""


@dataclass(frozen=True, slots=True)
class DeviceCredential:
    """A tracker and the token that authorises reading it."""

    serialnumber: str
    token: str


def redact(secret: str) -> str:
    """Render a token safe to log."""
    return f"…{secret[-4:]}" if len(secret) > 4 else "…"


def save_credentials(path: Path, devices: list[DeviceCredential]) -> None:
    """Write device tokens atomically with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)

    document = {
        "version": CREDENTIALS_VERSION,
        "saved_at": datetime.now(UTC).isoformat(),
        "devices": [{"serialnumber": d.serialnumber, "token": d.token} for d in devices],
    }

    # Write-then-rename so an interrupted save cannot truncate working credentials.
    tmp_path = path.with_name(path.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    os.replace(tmp_path, path)
    os.chmod(path, 0o600)
    log.debug("wrote %d device credential(s) to %s", len(devices), path)


def load_credentials(path: Path) -> list[DeviceCredential]:
    """Read device tokens from disk.

    Raises:
        NoCredentialsError: the file is missing, malformed, or lists no devices.

    """
    if not path.is_file():
        raise NoCredentialsError(f"no credentials at {path} -- run `gtt login` first")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise NoCredentialsError(f"could not read {path}: {exc}") from exc

    devices = [
        DeviceCredential(serialnumber=str(entry["serialnumber"]), token=str(entry["token"]))
        for entry in document.get("devices", [])
        if entry.get("serialnumber") and entry.get("token")
    ]
    if not devices:
        raise NoCredentialsError(f"{path} lists no usable devices -- run `gtt login` again")
    return devices


@dataclass(frozen=True, slots=True)
class PendingLogin:
    """What the tracker cloud hands back when a sign-in link is requested."""

    user_id: int
    access_token: str
    customer_id: str
    email: str
    token_valid: bool


async def _json_request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str],
    data: Mapping[str, str] | None = None,
    json_data: Mapping[str, Any] | None = None,
) -> tuple[int, Any]:
    """Make one request and decode its JSON, naming the host in any failure."""
    host = urlsplit(url).netloc
    try:
        response = await client.request(method, url, headers=headers, data=data, json=json_data)
    except httpx.HTTPError as exc:
        raise LoginError(f"could not reach {host}: {type(exc).__name__}: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        content_type = response.headers.get("content-type", "an unknown type")
        raise LoginError(
            f"{host} returned HTTP {response.status_code} with a non-JSON body ({content_type})"
        ) from exc
    log.debug("%s %s -> HTTP %d", method, url, response.status_code)
    return response.status_code, payload


def _shop_error(payload: Any) -> str | None:
    """Extract the shop's error description, if the payload carries one."""
    if isinstance(payload, dict) and "error" in payload:
        return str(payload.get("error_description") or payload["error"])
    return None


async def _get_shop_access_token(client: httpx.AsyncClient, email: str, password: str) -> str:
    """Trade shop credentials for an OAuth token. Nothing is emailed at this point."""
    status, payload = await _json_request(
        client,
        "POST",
        f"{SHOP_API_BASE_URL}/authorizationserver/oauth/token",
        headers={
            "accept": "application/json",
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
        },
        data={
            "grant_type": "password",
            "username": email,
            "password": password,
            "client_id": SHOP_CLIENT_ID,
            "client_secret": SHOP_CLIENT_SECRET,
        },
    )

    if (error := _shop_error(payload)) is not None or status >= 400:
        raise ShopLoginError(
            "Fressnapf rejected the sign-in before any email was sent. This is almost always a "
            f"wrong shop email or password. Server said: HTTP {status}, {error or 'no detail'}"
        )

    token = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(token, str):
        raise LoginError("the Fressnapf shop accepted the login but returned no access token")
    return token


async def _get_customer_id(client: httpx.AsyncClient, email: str, shop_token: str) -> str:
    """Look up the shop customer ID, which the tracker cloud wants echoed back."""
    status, payload = await _json_request(
        client,
        "GET",
        f"{SHOP_API_BASE_URL}/rest/v2/{SHOP_SITE}/users/{quote(email, safe='')}",
        headers={
            "accept": "application/json",
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {shop_token}",
        },
    )

    if (error := _shop_error(payload)) is not None or status >= 400:
        raise ShopLoginError(
            f"Fressnapf would not return the customer record for {email}: "
            f"HTTP {status}, {error or 'no detail'}"
        )

    customer_id = payload.get("customerId") if isinstance(payload, dict) else None
    if not isinstance(customer_id, str):
        raise LoginError("the Fressnapf shop returned a customer record without a customer ID")
    return customer_id


async def request_magic_link(
    client: httpx.AsyncClient,
    email: str,
    password: str,
    *,
    locale: str = "de",
) -> PendingLogin:
    """Ask the tracker cloud to email a sign-in link.

    This exists instead of `AuthClient.request_magic_link` because upstream
    validates the response against the *full* user model. When a link is
    actually sent the cloud answers `202 Accepted` with only `user.id` and
    `user.email`, so upstream's parse fails and -- worse -- it reports the
    failure as an authentication error, which reads as a wrong password.
    Verified against fressnapftracker 0.3.0 on 2026-09-01.
    """
    shop_token = await _get_shop_access_token(client, email, password)
    customer_id = await _get_customer_id(client, email, shop_token)

    status, payload = await _json_request(
        client,
        "POST",
        f"{AUTH_BASE_URL}/magic_link_auth",
        headers={
            "accept": "application/json",
            "User-Agent": _USER_AGENT,
            "Content-Type": "application/json",
            "Authorization": f"Token token={CLOUD_AUTH_TOKEN}",
        },
        json_data={
            "user": {
                "email": email,
                "locale": locale,
                "tracker_service": "fressnapf",
                "user_token": {
                    "push_token": "",
                    "app_version": _APP_VERSION,
                    "app_platform": "android",
                    "platform_version": _APP_PLATFORM_VERSION,
                    "phone_name": _USER_AGENT,
                },
                "additional_parameters": {
                    "acceptedPrivacyPolicy": True,
                    "region": "DE",
                    "fressnapfId": customer_id,
                    "acceptedNewsletter": False,
                    "onlineShopRatingHasBeenShowed": False,
                    "onlineShopRatingPopupLastShowedDate": None,
                },
            }
        },
    )

    if (error := _shop_error(payload)) is not None or status >= 400:
        raise LoginError(
            f"the tracker cloud refused to send a sign-in link: HTTP {status}, "
            f"{error or 'no detail'}"
        )

    user = payload.get("user") if isinstance(payload, dict) else None
    user_token = payload.get("user_token") if isinstance(payload, dict) else None
    if not isinstance(user, dict) or not isinstance(user_token, dict):
        raise LoginError(f"unexpected sign-in response from the tracker cloud: {payload!r}")

    user_id = user.get("id")
    access_token = user_token.get("access_token")
    if not isinstance(user_id, int) or not isinstance(access_token, str):
        raise LoginError(f"the tracker cloud returned no usable session: {payload!r}")

    return PendingLogin(
        user_id=user_id,
        access_token=access_token,
        customer_id=customer_id,
        email=str(user.get("email") or email),
        token_valid=bool(user_token.get("token_valid")),
    )


async def login(
    email: str,
    password: str,
    *,
    locale: str = "de",
    request_timeout: int = 10,
    on_link_sent: Callable[[str], None] | None = None,
    poll_interval: float = _MAGIC_LINK_POLL_INTERVAL,
    timeout: float = _MAGIC_LINK_TIMEOUT,
) -> list[DeviceCredential]:
    """Run the email magic-link flow and return the account's tracker credentials."""
    # AuthClient builds its own httpx client when not given one, and that one
    # uses httpx's 5s default -- shorter than request_timeout, which upstream
    # only applies as an outer wait_for. Passing a client makes the configured
    # timeout the one that actually governs.
    async with httpx.AsyncClient(timeout=httpx.Timeout(request_timeout)) as client:
        pending = await request_magic_link(client, email, password, locale=locale)

        async with AuthClient(client=client, request_timeout=request_timeout) as auth:
            if not pending.token_valid:
                if on_link_sent is not None:
                    on_link_sent(pending.email)
                await _await_magic_link(
                    auth, pending.access_token, poll_interval=poll_interval, timeout=timeout
                )

            try:
                await auth.complete_magic_link(
                    pending.user_id, pending.access_token, pending.customer_id
                )
                devices = await auth.get_devices(pending.user_id, pending.access_token)
            except FressnapfTrackerAuthenticationError as exc:
                raise LoginError(
                    f"the sign-in link was opened but the session was rejected: {exc}"
                ) from exc
            except FressnapfTrackerError as exc:
                raise LoginError(f"could not finish sign-in: {exc}") from exc

    if not devices:
        raise LoginError(
            "sign-in succeeded but the account has no trackers -- add one in the "
            "Fressnapf app first"
        )
    return [DeviceCredential(serialnumber=d.serialnumber, token=d.token) for d in devices]


async def _await_magic_link(
    auth: AuthClient,
    access_token: str,
    *,
    poll_interval: float,
    timeout: float,
) -> None:
    """Poll until the emailed link is opened. Upstream checks once per call."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            if await auth.check_magic_link_was_clicked(access_token):
                return
        except FressnapfTrackerError as exc:
            raise LoginError(f"could not check the sign-in link: {exc}") from exc
        if time.monotonic() >= deadline:
            raise MagicLinkTimeoutError(
                f"the sign-in link was not opened within {int(timeout)}s -- run `gtt login` again"
            )
        await asyncio.sleep(poll_interval)
