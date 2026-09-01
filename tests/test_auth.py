"""The email sign-in flow, driven against recorded API responses."""

import httpx
import pytest

from gps_tracker_tracker.auth import (
    LoginError,
    ShopLoginError,
    request_magic_link,
)

SHOP_TOKEN_URL = "https://api.os.fressnapf.com/authorizationserver/oauth/token"
CUSTOMER_URL = "https://api.os.fressnapf.com/rest/v2/FressnapfDE/users/pet%40example.com"
MAGIC_LINK_URL = "https://user.iot-pet-tracking.cloud/api/app/v1/magic_link_auth"

# What the cloud actually answers when it sends a link: 202, and a user object
# carrying only id and email. Recorded 2026-09-01.
SLIM_MAGIC_LINK_RESPONSE = {
    "user": {"id": 438414, "email": "pet@example.com"},
    "user_token": {"access_token": "tok-abc", "token_valid": False},
}


def make_client(handler):
    """An httpx client whose requests are served by `handler`."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def happy_path(magic_link_response=SLIM_MAGIC_LINK_RESPONSE, magic_link_status=202):
    """Route the three sign-in requests, letting the last one be overridden."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == SHOP_TOKEN_URL:
            return httpx.Response(200, json={"access_token": "shop-token"})
        if url == CUSTOMER_URL:
            return httpx.Response(200, json={"customerId": "cust-1"})
        if url == MAGIC_LINK_URL:
            return httpx.Response(magic_link_status, json=magic_link_response)
        raise AssertionError(f"unexpected request to {url}")

    return handler


@pytest.mark.asyncio
async def test_slim_magic_link_response_is_accepted():
    """The 202 body omits most of the user; the flow must still proceed."""
    async with make_client(happy_path()) as client:
        pending = await request_magic_link(client, "pet@example.com", "pw")

    assert pending.user_id == 438414
    assert pending.access_token == "tok-abc"
    assert pending.email == "pet@example.com"
    assert pending.token_valid is False
    # Not in the response at all -- it has to come from the shop lookup.
    assert pending.customer_id == "cust-1"


@pytest.mark.asyncio
async def test_already_valid_token_skips_the_email():
    async with make_client(
        happy_path(
            magic_link_response={
                "user": {"id": 1, "email": "pet@example.com"},
                "user_token": {"access_token": "tok", "token_valid": True},
            },
            magic_link_status=200,
        )
    ) as client:
        pending = await request_magic_link(client, "pet@example.com", "pw")

    assert pending.token_valid is True


@pytest.mark.asyncio
async def test_wrong_password_is_reported_as_a_shop_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == SHOP_TOKEN_URL, "must not proceed past the shop login"
        return httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Bad credentials"}
        )

    async with make_client(handler) as client:
        with pytest.raises(ShopLoginError, match="Bad credentials"):
            await request_magic_link(client, "pet@example.com", "wrong")


@pytest.mark.asyncio
async def test_unreachable_host_names_the_host():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with make_client(handler) as client:
        with pytest.raises(LoginError, match="could not reach api.os.fressnapf.com") as caught:
            await request_magic_link(client, "pet@example.com", "pw")

    # A transport failure is not a credentials problem, and must not claim to be.
    assert not isinstance(caught.value, ShopLoginError)


@pytest.mark.asyncio
async def test_html_error_page_is_not_mistaken_for_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, html="<h1>Service Unavailable</h1>")

    async with make_client(handler) as client:
        with pytest.raises(LoginError, match="non-JSON body"):
            await request_magic_link(client, "pet@example.com", "pw")


@pytest.mark.asyncio
async def test_session_without_a_token_is_rejected():
    async with make_client(
        happy_path(magic_link_response={"user": {"id": 1}, "user_token": {}})
    ) as client:
        with pytest.raises(LoginError, match="no usable session"):
            await request_magic_link(client, "pet@example.com", "pw")
