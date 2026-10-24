"""McpClient: validating and calling a remote MCP integration with vault headers (SPEC §3.2.10; P2).

OAuth from the phone (R-43) is the SDK's `OAuthClientProvider` driven through two callbacks the service supplies:
`on_link(url)` sends the owner the authorization URL in Telegram, and `wait_callback()` waits for him to paste the
final redirect URL (or just the code) back. PKCE, discovery, registration and the token exchange stay in the SDK.

An `OAuthGrant` is what the service seals in its own table: `state` is opaque to every caller (tokens, client
registration, discovered metadata); only the access token and its lifetime are read outside the adapter.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from yordamchi.core.json_value import JsonObject, JsonValue


@dataclass(frozen=True, slots=True)
class McpTool:
    name: str
    description: str
    read_only: bool  # the tool's `readOnlyHint`
    input_schema: Mapping[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class McpToolResult:
    is_error: bool
    text: str
    structured: JsonValue = None


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    """How Yordamchi presents itself to an authorization server. Values come from settings, per call."""

    redirect_uri: str  # http://localhost:<integrations.oauth_redirect_port>/callback
    client_name: str
    client_metadata_url: str | None = None  # `integrations.oauth_client_metadata_url` (CIMD), or None


@dataclass(frozen=True, slots=True)
class OAuthCallback:
    """What the owner pasted back. `state` is None when he pasted only the code: the adapter then uses the state of
    the authorization URL it generated, so the SDK's state check still runs."""

    code: str = field(repr=False)
    state: str | None = field(default=None, repr=False)
    iss: str | None = None


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    state: JsonObject = field(repr=False)  # opaque: sealed by the service, read back only by the adapter
    access_token: str = field(repr=False)
    expires_in: int | None  # seconds from the moment the grant was issued; None = no stated expiry
    issuer: str | None
    client_id: str
    can_refresh: bool
    secret_values: tuple[str, ...] = field(default=(), repr=False)  # every token/secret in `state`, for masking


@dataclass(frozen=True, slots=True)
class OAuthAuthorized:
    tools: list[McpTool]
    grant: OAuthGrant


type LinkHandler = Callable[[str], Awaitable[None]]
type CallbackWaiter = Callable[[], Awaitable[OAuthCallback]]


class McpClient(Protocol):
    async def list_tools(self, url: str, headers: Mapping[str, str], *, timeout: float) -> list[McpTool]:
        """Connect and list the tools. A 401 that advertises OAuth raises `OAuthRequired`; any other 401/403
        raises `AuthFailed`; an unreachable server `ProviderUnavailable`; a slow one `CallTimedOut`."""
        ...

    async def call_tool(
        self,
        url: str,
        headers: Mapping[str, str],
        name: str,
        arguments: Mapping[str, JsonValue],
        *,
        timeout: float,
    ) -> McpToolResult: ...

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
        """Run the authorization-code flow and list the tools with the new token. `request_timeout` bounds each HTTP
        exchange, `timeout` the whole flow (it includes the owner's sign-in). A `YordamchiError` raised by a callback
        (the paste window ran out) propagates unchanged."""
        ...

    async def refresh(self, url: str, grant: OAuthGrant, *, client: OAuthClientConfig, timeout: float) -> OAuthGrant:
        """Exchange the refresh token for a new grant. A refusal (expired or revoked refresh token, no refresh token)
        raises `OAuthReauthRequired`; a server error or an unreachable server is a retryable `ExternalError` and
        leaves the old grant usable."""
        ...
