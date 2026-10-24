"""`McpHttpClient` against in-process fixture MCP servers (tests/integration/mcp_harness.py), offline: listing and
calling, the error mapping (401 with and without OAuth, 403, 429, 5xx, unreachable, slow), the OAuth paste-back flow
end to end, and refresh (rotation, refusal, a transient token-endpoint failure). Nothing opens a network socket."""

import logging
from collections.abc import AsyncIterator

import httpx2
import pytest

from tests.integration.mcp_harness import (
    OAUTH_URL,
    REDIRECT_URI,
    STATIC_TOKEN,
    STATIC_URL,
    McpFixtures,
    mcp_fixtures,
)
from yordamchi.adapters.mcp.client import McpHttpClient
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    OAuthPasteTimedOut,
    OAuthReauthRequired,
    OAuthRequired,
    ProviderUnavailable,
    RateLimited,
)
from yordamchi.ports.mcp import McpClient, OAuthAuthorized, OAuthCallback, OAuthClientConfig
from yordamchi.shared.integrations import domain

BEARER = {"Authorization": f"Bearer {STATIC_TOKEN}"}
CLIENT = OAuthClientConfig(redirect_uri=REDIRECT_URI, client_name="Yordamchi")


@pytest.fixture
async def fixtures() -> AsyncIterator[McpFixtures]:
    async with mcp_fixtures() as running:
        yield running


@pytest.fixture
def client(fixtures: McpFixtures) -> McpHttpClient:
    return McpHttpClient(transport=fixtures.transport)


class Owner:
    """The owner on his phone: opens the link the bot sent, approves, and pastes back what the browser shows."""

    def __init__(self, fixtures: McpFixtures, *, paste: str = "url") -> None:
        self.fixtures, self.paste = fixtures, paste
        self.links: list[str] = []

    async def on_link(self, url: str) -> None:
        self.links.append(url)

    async def wait_callback(self) -> OAuthCallback:
        redirect = await self.fixtures.sign_in(self.links[-1])
        callback = domain.parse_callback(redirect)
        assert callback is not None
        if self.paste == "code":
            return OAuthCallback(code=callback.code)
        if self.paste == "wrong_state":
            return OAuthCallback(code=callback.code, state="not-the-state")
        return callback


async def authorize(client: McpHttpClient, owner: Owner) -> OAuthAuthorized:
    return await client.authorize(
        OAUTH_URL,
        client=CLIENT,
        on_link=owner.on_link,
        wait_callback=owner.wait_callback,
        request_timeout=10,
        timeout=30,
    )


# --- static servers -------------------------------------------------------------------------------------------------
async def test_listing_marks_the_read_only_tools(client: McpHttpClient) -> None:
    tools = {tool.name: tool for tool in await client.list_tools(STATIC_URL, BEARER, timeout=10)}

    assert tools["read_status"].read_only
    assert not tools["write_note"].read_only
    assert tools["write_note"].input_schema["type"] == "object"


async def test_a_tool_call_returns_its_text(client: McpHttpClient) -> None:
    result = await client.call_tool(STATIC_URL, BEARER, "read_status", {}, timeout=10)

    assert not result.is_error
    assert result.text == "barcha xizmatlar ishlayapti"


async def test_a_wrong_token_is_auth_failed_not_oauth(client: McpHttpClient) -> None:
    with pytest.raises(AuthFailed):
        await client.list_tools(STATIC_URL, {"Authorization": "Bearer wrong"}, timeout=10)


async def test_an_oauth_server_without_a_token_asks_for_oauth(client: McpHttpClient) -> None:
    with pytest.raises(OAuthRequired):
        await client.list_tools(OAUTH_URL, {}, timeout=10)


async def test_an_unknown_host_is_unreachable(client: McpHttpClient) -> None:
    with pytest.raises(ProviderUnavailable) as raised:
        await client.list_tools("https://nowhere.fixture.test/mcp", {}, timeout=5)
    assert raised.value.retryable


async def test_a_403_is_auth_failed(client: McpHttpClient, fixtures: McpFixtures) -> None:
    fixtures.transport.fault = lambda request: httpx2.Response(403, request=request)
    with pytest.raises(AuthFailed):
        await client.list_tools(STATIC_URL, BEARER, timeout=5)


async def test_a_429_is_rate_limited_with_its_retry_after(client: McpHttpClient, fixtures: McpFixtures) -> None:
    fixtures.transport.fault = lambda request: httpx2.Response(429, headers={"Retry-After": "0"}, request=request)
    with pytest.raises(RateLimited) as raised:
        await client.list_tools(STATIC_URL, BEARER, timeout=5)
    assert raised.value.retry_after == 0.0
    assert len(fixtures.transport.requests) >= 3  # retried: a 429 is transient


async def test_a_5xx_is_retried_then_unreachable(client: McpHttpClient, fixtures: McpFixtures) -> None:
    fixtures.transport.fault = lambda request: httpx2.Response(503, request=request)
    with pytest.raises(ProviderUnavailable) as raised:
        await client.list_tools(STATIC_URL, BEARER, timeout=5)
    assert raised.value.retryable
    assert len(fixtures.transport.requests) >= 2


async def test_a_tool_call_is_never_retried(client: McpHttpClient, fixtures: McpFixtures) -> None:
    def fail_calls(request: httpx2.Request) -> httpx2.Response | None:
        return httpx2.Response(503, request=request) if b"tools/call" in request.content else None

    fixtures.transport.fault = fail_calls
    with pytest.raises(ProviderUnavailable):
        await client.call_tool(STATIC_URL, BEARER, "write_note", {"text": "x"}, timeout=5)
    assert sum(b"tools/call" in request.content for request in fixtures.transport.requests) == 1


async def test_a_slow_server_times_out_within_the_bound(client: McpHttpClient, fixtures: McpFixtures) -> None:
    fixtures.transport.delay_s = 1.0
    with pytest.raises(CallTimedOut):
        await client.list_tools(STATIC_URL, BEARER, timeout=0.3)


async def test_each_call_logs_id_host_duration_and_outcome_but_no_secret(
    client: McpHttpClient, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO):
        await client.list_tools(STATIC_URL, BEARER, timeout=10)
    finished = [record for record in caplog.records if record.getMessage() == "mcp_call_finished"]
    assert finished
    fields = vars(finished[-1])
    rendered = repr(fields)
    assert STATIC_TOKEN not in rendered
    assert "/mcp" not in rendered  # the host only, never a path or query


# --- OAuth from the phone (R-43) ------------------------------------------------------------------------------------
async def test_the_paste_back_flow_ends_with_a_working_token(client: McpHttpClient, fixtures: McpFixtures) -> None:
    owner = Owner(fixtures)

    authorized = await authorize(client, owner)

    assert len(owner.links) == 1
    assert "code_challenge=" in owner.links[0]  # PKCE comes from the SDK
    assert {tool.name for tool in authorized.tools} == {"read_status", "write_note"}
    grant = authorized.grant
    assert grant.can_refresh
    assert grant.expires_in == 3600
    assert grant.access_token in grant.secret_values
    headers = {"Authorization": f"Bearer {grant.access_token}"}
    assert {tool.name for tool in await client.list_tools(OAUTH_URL, headers, timeout=10)} >= {"read_status"}


async def test_pasting_only_the_code_works(client: McpHttpClient, fixtures: McpFixtures) -> None:
    authorized = await authorize(client, Owner(fixtures, paste="code"))

    assert authorized.grant.access_token.startswith("at-")


async def test_a_paste_with_a_foreign_state_is_refused(client: McpHttpClient, fixtures: McpFixtures) -> None:
    with pytest.raises(AuthFailed):
        await authorize(client, Owner(fixtures, paste="wrong_state"))


async def test_a_callback_error_of_ours_passes_through(client: McpHttpClient, fixtures: McpFixtures) -> None:
    async def times_out() -> OAuthCallback:
        raise OAuthPasteTimedOut(name="Bonvi HR", minutes=15)

    with pytest.raises(OAuthPasteTimedOut):
        await client.authorize(
            OAUTH_URL,
            client=CLIENT,
            on_link=Owner(fixtures).on_link,
            wait_callback=times_out,
            request_timeout=10,
            timeout=30,
        )


async def test_refresh_rotates_the_tokens(client: McpHttpClient, fixtures: McpFixtures) -> None:
    grant = (await authorize(client, Owner(fixtures))).grant

    fresh = await client.refresh(OAUTH_URL, grant, client=CLIENT, timeout=10)

    assert fresh.access_token != grant.access_token
    assert fixtures.authorization.refreshes == 1
    await client.list_tools(OAUTH_URL, {"Authorization": f"Bearer {fresh.access_token}"}, timeout=10)
    again = await client.refresh(OAUTH_URL, fresh, client=CLIENT, timeout=10)  # the rotated refresh token works
    assert again.access_token != fresh.access_token


async def test_a_refused_refresh_needs_reauth(client: McpHttpClient, fixtures: McpFixtures) -> None:
    grant = (await authorize(client, Owner(fixtures))).grant
    fixtures.authorization.revoke_refresh_tokens()

    with pytest.raises(OAuthReauthRequired):
        await client.refresh(OAUTH_URL, grant, client=CLIENT, timeout=10)


async def test_a_token_endpoint_outage_is_transient_and_keeps_the_grant(
    client: McpHttpClient, fixtures: McpFixtures
) -> None:
    grant = (await authorize(client, Owner(fixtures))).grant
    fixtures.transport.fault = lambda request: (
        httpx2.Response(503, request=request) if request.url.path == "/token" else None
    )

    with pytest.raises(ProviderUnavailable) as raised:
        await client.refresh(OAUTH_URL, grant, client=CLIENT, timeout=10)
    assert raised.value.retryable

    fixtures.transport.fault = None
    fresh = await client.refresh(OAUTH_URL, grant, client=CLIENT, timeout=10)  # the old refresh token still works
    assert fresh.access_token != grant.access_token


async def test_the_adapter_satisfies_the_port(client: McpHttpClient) -> None:
    port: McpClient = client
    assert await port.list_tools(STATIC_URL, BEARER, timeout=10)
