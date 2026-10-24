"""`FakeMcpClient`: an in-memory MCP server registry, with and without OAuth.

A static server checks its headers. An OAuth server issues an authorization URL through `on_link`, and `sign_in(url)`
plays the owner's browser: it returns the localhost redirect URL he would paste back. Tokens rotate on every refresh;
`refuse_refresh` makes the authorization server refuse the refresh token. The real adapter runs against an in-process
`mcp` server in tests/integration/test_mcp.py; this fake is for the service tests.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit

from yordamchi.core.errors import (
    AuthFailed,
    OAuthClientMetadataNeeded,
    OAuthReauthRequired,
    OAuthRequired,
    ProviderUnavailable,
)
from yordamchi.core.json_value import JsonValue
from yordamchi.ports.mcp import (
    CallbackWaiter,
    LinkHandler,
    McpClient,
    McpTool,
    McpToolResult,
    OAuthAuthorized,
    OAuthClientConfig,
    OAuthGrant,
)


@dataclass
class FakeOAuthServer:
    tools: list[McpTool]
    expires_in: int | None = 3600
    needs_client_metadata: bool = False
    refuse_refresh: bool = False
    access: set[str] = field(default_factory=set)
    refresh: set[str] = field(default_factory=set)
    codes: dict[str, str] = field(default_factory=dict)  # state → code
    issued: int = 0

    def issue(self, client_id: str) -> OAuthGrant:
        self.issued += 1
        access, refresh = f"at-{self.issued:04d}-fixture", f"rt-{self.issued:04d}-fixture"
        self.access.add(access)
        self.refresh.add(refresh)
        return OAuthGrant(
            state={"access": access, "refresh": refresh, "client_id": client_id},
            access_token=access,
            expires_in=self.expires_in,
            issuer="https://auth.fixture.test",
            client_id=client_id,
            can_refresh=True,
            secret_values=(access, refresh),
        )


class FakeMcpClient:
    def __init__(self) -> None:
        self.servers: dict[str, list[McpTool]] = {}
        self.required_headers: dict[str, Mapping[str, str]] = {}
        self.oauth_servers: dict[str, FakeOAuthServer] = {}
        self.results: dict[tuple[str, str], McpToolResult] = {}
        self.calls: list[tuple[str, str, Mapping[str, JsonValue]]] = []
        self.list_calls: list[tuple[str, dict[str, str]]] = []
        self.refreshes: list[str] = []
        self.unreachable: set[str] = set()

    def add_server(self, url: str, tools: list[McpTool], *, headers: Mapping[str, str] | None = None) -> None:
        self.servers[url] = tools
        if headers is not None:
            self.required_headers[url] = headers

    def add_oauth_server(
        self, url: str, tools: list[McpTool], *, expires_in: int | None = 3600, needs_client_metadata: bool = False
    ) -> FakeOAuthServer:
        server = FakeOAuthServer(tools=tools, expires_in=expires_in, needs_client_metadata=needs_client_metadata)
        self.oauth_servers[url] = server
        return server

    def sign_in(self, authorization_url: str, *, redirect_uri: str = "http://localhost:33418/callback") -> str:
        """The owner's browser: approve and land on the redirect URI, which the phone cannot open."""
        state = parse_qs(urlsplit(authorization_url).query)["state"][0]
        for server in self.oauth_servers.values():
            if state in server.codes:
                return f"{redirect_uri}?{urlencode({'code': server.codes[state], 'state': state})}"
        msg = "no OAuth flow issued this URL"
        raise AssertionError(msg)

    def _check(self, url: str, headers: Mapping[str, str]) -> list[McpTool]:
        if url in self.unreachable:
            raise ProviderUnavailable(provider=url, retryable=True)
        oauth = self.oauth_servers.get(url)
        if oauth is not None:
            bearer = headers.get("Authorization", "")
            if not bearer:
                raise OAuthRequired(provider=url)
            if bearer.removeprefix("Bearer ") not in oauth.access:
                raise AuthFailed(provider=url)
            return oauth.tools
        if url not in self.servers:
            raise ProviderUnavailable(provider=url, retryable=False)
        required = self.required_headers.get(url)
        if required is not None and any(headers.get(key) != value for key, value in required.items()):
            raise AuthFailed(provider=url)
        return self.servers[url]

    async def list_tools(self, url: str, headers: Mapping[str, str], *, timeout: float) -> list[McpTool]:
        self.list_calls.append((url, dict(headers)))
        return list(self._check(url, headers))

    async def call_tool(
        self,
        url: str,
        headers: Mapping[str, str],
        name: str,
        arguments: Mapping[str, JsonValue],
        *,
        timeout: float,
    ) -> McpToolResult:
        self._check(url, headers)
        self.calls.append((url, name, dict(arguments)))
        return self.results.get((url, name), McpToolResult(is_error=False, text="ok"))

    async def authorize(
        self,
        url: str,
        *,
        client: OAuthClientConfig,
        on_link: LinkHandler,
        wait_callback: CallbackWaiter,
        request_timeout: float,
        timeout: float,
    ) -> OAuthAuthorized:
        server = self.oauth_servers.get(url)
        if server is None or url in self.unreachable:
            raise ProviderUnavailable(provider=url, retryable=True)
        if server.needs_client_metadata and client.client_metadata_url is None:
            raise OAuthClientMetadataNeeded(provider=url)
        server.issued += 1
        state, code = f"state-{server.issued:04d}-0123456789abcdef", f"code-{server.issued:04d}-fixture"
        server.codes[state] = code
        query = urlencode({"response_type": "code", "client_id": "fake-client", "redirect_uri": client.redirect_uri})
        await on_link(f"{url.rsplit('/', 1)[0]}/authorize?{query}&state={state}")
        pasted = await wait_callback()
        if (pasted.state or state) != state or pasted.code != code:
            raise AuthFailed(provider=url)
        del server.codes[state]
        return OAuthAuthorized(tools=list(server.tools), grant=server.issue("fake-client"))

    async def refresh(self, url: str, grant: OAuthGrant, *, client: OAuthClientConfig, timeout: float) -> OAuthGrant:
        self.refreshes.append(url)
        server = self.oauth_servers.get(url)
        if server is None or url in self.unreachable:
            raise ProviderUnavailable(provider=url, retryable=True)
        refresh = grant.state.get("refresh")
        if server.refuse_refresh or refresh not in server.refresh:
            raise OAuthReauthRequired(provider=url)
        server.refresh.discard(str(refresh))
        server.access.discard(grant.access_token)
        return server.issue(grant.client_id)


_: McpClient = FakeMcpClient()
