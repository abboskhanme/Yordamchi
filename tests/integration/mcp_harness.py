"""In-process fixture MCP servers for the `mcp` adapter (UC-08, UC-35; CONVENTIONS §4 "fixture MCP server").

Two servers built with the SDK's own server API (`MCPServer`), reached through an httpx2 transport that routes by
host to their ASGI apps, so nothing opens a network socket:

- a **static** server behind a fixed bearer token (a 401 without OAuth metadata when it is wrong);
- an **OAuth** server that is its own authorization server (the SDK's auth routes: metadata, `/register`,
  `/authorize`, `/token`), issuing rotating access and refresh tokens.

Both have one read-only tool (`read_status`, `readOnlyHint`) and one write tool (`write_note`). `sign_in(url)` plays
the owner's phone browser: it follows the authorization URL to the localhost redirect, which the phone cannot open,
and returns that URL, i.e. exactly what he pastes back into Telegram (R-43).
"""

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

import httpx2
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    IdentityAssertionParams,
    RefreshToken,
    construct_redirect_uri,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from mcp.types import ToolAnnotations
from pydantic import AnyHttpUrl
from starlette.types import ASGIApp, Receive, Scope, Send

from yordamchi.core import aio

STATIC_HOST = "mcp.fixture.test"
OAUTH_HOST = "oauth.fixture.test"
STATIC_URL = f"https://{STATIC_HOST}/mcp"
OAUTH_URL = f"https://{OAUTH_HOST}/mcp"
STATIC_TOKEN = "fixture-static-token-7Hq2Lw9Xz"
REDIRECT_URI = "http://localhost:33418/callback"


@dataclass
class FixtureAuthorizationServer:
    """The SDK's `OAuthAuthorizationServerProvider`, in memory. Every refresh rotates both tokens."""

    access_ttl_s: int = 3600
    clients: dict[str, OAuthClientInformationFull] = field(default_factory=dict)
    codes: dict[str, AuthorizationCode] = field(default_factory=dict)
    access: dict[str, AccessToken] = field(default_factory=dict)
    refresh: dict[str, RefreshToken] = field(default_factory=dict)
    refreshes: int = 0

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        assert client_info.client_id is not None
        self.clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        assert client.client_id is not None
        code = secrets.token_urlsafe(24)
        self.codes[code] = AuthorizationCode(
            code=code,
            scopes=params.scopes or [],
            expires_at=time.time() + 300,  # noqa: TID251  # the SDK server checks wall-clock epoch seconds
            client_id=client.client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
        )
        return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        return self.codes.get(authorization_code)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        del self.codes[authorization_code.code]
        return self._issue(authorization_code.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        return self.refresh.get(refresh_token)

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        del self.refresh[refresh_token.token]
        self.refreshes += 1
        return self._issue(refresh_token.client_id, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        return self.access.get(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        self.access.pop(token.token, None)
        self.refresh.pop(token.token, None)

    async def exchange_identity_assertion(
        self, client: OAuthClientInformationFull, params: IdentityAssertionParams
    ) -> OAuthToken:
        raise NotImplementedError  # SEP-990 enterprise grants are not part of the fixture

    def revoke_refresh_tokens(self) -> None:
        """The authorization server forgot every refresh token (expired, or the owner revoked the app)."""
        self.refresh.clear()

    def _issue(self, client_id: str, scopes: list[str], resource: str | None) -> OAuthToken:
        access, refresh = f"at-{secrets.token_urlsafe(18)}", f"rt-{secrets.token_urlsafe(18)}"
        expires = int(time.time()) + self.access_ttl_s  # noqa: TID251  # the SDK server's own clock
        self.access[access] = AccessToken(
            token=access, client_id=client_id, scopes=scopes, expires_at=expires, resource=resource
        )
        self.refresh[refresh] = RefreshToken(token=refresh, client_id=client_id, scopes=scopes, resource=resource)
        return OAuthToken(
            access_token=access,
            token_type="Bearer",
            expires_in=self.access_ttl_s,
            refresh_token=refresh,
            scope=" ".join(scopes) or None,
        )


def _tools(server: MCPServer) -> None:
    @server.tool(annotations=ToolAnnotations(read_only_hint=True), description="Read the system status.")
    def read_status() -> str:
        return "barcha xizmatlar ishlayapti"

    @server.tool(description="Write a note.")
    def write_note(text: str) -> str:
        return f"saved {len(text)}"


class StaticBearer:
    """ASGI middleware: only `Authorization: Bearer <token>` passes; anything else is a bare 401 (no OAuth)."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self.app, self.token = app, token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope["headers"])
            if headers.get(b"authorization") != f"Bearer {self.token}".encode():
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await self.app(scope, receive, send)


type Fault = Callable[[httpx2.Request], httpx2.Response | None]


class Router(httpx2.AsyncBaseTransport):
    """Routes each request by host to an in-process ASGI app; an unknown host is a connection error. `fault` may
    answer a request first (a 503 from the token endpoint, a 429, a slow server)."""

    def __init__(self, apps: dict[str, ASGIApp]) -> None:
        self.apps = {host: httpx2.ASGITransport(app=app) for host, app in apps.items()}
        self.fault: Fault | None = None
        self.delay_s = 0.0
        self.requests: list[httpx2.Request] = []

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fault is not None and (answer := self.fault(request)) is not None:
            return answer
        transport = self.apps.get(request.url.host)
        if transport is None:
            raise httpx2.ConnectError("unknown host", request=request)
        return await transport.handle_async_request(request)


@dataclass
class McpFixtures:
    transport: Router
    authorization: FixtureAuthorizationServer

    async def sign_in(self, authorization_url: str) -> str:
        """The owner approves on his phone; the server redirects to localhost, which does not load there."""
        async with httpx2.AsyncClient(transport=self.transport) as browser:
            response = await browser.get(authorization_url)
        assert response.status_code in {302, 303, 307}, response.status_code
        location = response.headers["location"]
        assert location.startswith(REDIRECT_URI)
        return location


@asynccontextmanager
async def mcp_fixtures(
    *, static_host: str = STATIC_HOST, static_token: str = STATIC_TOKEN, access_ttl_s: int = 3600
) -> AsyncIterator[McpFixtures]:
    static = MCPServer("fixture-static", log_level="WARNING")
    _tools(static)
    authorization = FixtureAuthorizationServer(access_ttl_s=access_ttl_s)
    oauth = MCPServer(
        "fixture-oauth",
        log_level="WARNING",
        auth_server_provider=authorization,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(f"https://{OAUTH_HOST}"),
            resource_server_url=AnyHttpUrl(OAUTH_URL),
            validate_token_resource=False,
            client_registration_options=ClientRegistrationOptions(enabled=True),
        ),
    )
    _tools(oauth)
    security = TransportSecuritySettings(enable_dns_rebinding_protection=False)  # hosts are fixture names
    apps: dict[str, ASGIApp] = {
        static_host: StaticBearer(
            static.streamable_http_app(json_response=True, stateless_http=True, transport_security=security),
            static_token,
        ),
        OAUTH_HOST: oauth.streamable_http_app(json_response=True, stateless_http=True, transport_security=security),
    }
    fixtures = McpFixtures(transport=Router(apps), authorization=authorization)
    ready, stop = asyncio.Event(), asyncio.Event()

    async def host() -> None:
        # The session managers hold anyio task groups, which must be left by the task that entered them; a pytest
        # fixture's teardown runs in another task, so one task owns them for the whole test.
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(static.session_manager.run())
            await stack.enter_async_context(oauth.session_manager.run())
            ready.set()
            await stop.wait()

    task = aio.spawn("mcp_fixtures", host())
    try:
        await asyncio.wait_for(ready.wait(), timeout=10)
        yield fixtures
    finally:
        stop.set()
        await task
