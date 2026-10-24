"""`McpHttpClient`: remote MCP servers over Streamable HTTP with the `mcp` SDK (2.3.0, pinned; C5 keeps it here).

- `list_tools` / `call_tool` connect with the given headers (a vault token, or an OAuth access token), list or call,
  and disconnect. Only `list_tools` is retried (`core.retry`, transient failures only): a tool call may have effects.
- `authorize` drives the SDK's `OAuthClientProvider` (discovery, registration or CIMD, PKCE, token exchange). Its
  `redirect_handler` hands the authorization URL to the service, which sends it to the owner's phone; its
  `callback_handler` waits for the pasted redirect (R-43). A paste without `state` gets the state of the URL the SDK
  generated, so the SDK's own state check still runs.
- `refresh` restores a sealed grant into a provider and drives the provider's public `async_auth_flow` on a probe
  request that is never sent: the provider refreshes first (its refresh code and its token parsing), and the probe
  comes back carrying the new token. A 5xx or 429 from the token endpoint is not shown to the provider (it would drop
  the tokens) and is raised as retryable instead.
- Every failure becomes an `ExternalError`: 401 with OAuth metadata → `OAuthRequired`, other 401/403 →
  `AuthFailed`, 429 → `RateLimited(retry_after)`, 5xx and network → `ProviderUnavailable(retryable)`, a timeout →
  `CallTimedOut`. A `YordamchiError` raised by one of the service's callbacks passes through unchanged.
- One log line per call: a call id, the operation, the host, the duration and the outcome code. Never a URL path,
  a header, a token or any tool content.

The constructor takes only a transport, for tests (an in-process fixture server, tests/integration/mcp_harness.py).
"""

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, cast
from urllib.parse import parse_qs, urlsplit

import httpx2
from mcp import Client, MCPError
from mcp.client.auth import (
    AuthorizationCodeResult,
    OAuthClientProvider,
    OAuthFlowError,
    OAuthRegistrationError,
    OAuthTokenError,
)
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthMetadata,
    OAuthToken,
    ProtectedResourceMetadata,
)
from pydantic import AnyUrl, ValidationError

from yordamchi.core import retry
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    OAuthClientMetadataNeeded,
    OAuthReauthRequired,
    OAuthRequired,
    ProviderUnavailable,
    RateLimited,
    YordamchiError,
)
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.log import get_log
from yordamchi.ports.mcp import (
    CallbackWaiter,
    LinkHandler,
    McpTool,
    McpToolResult,
    OAuthAuthorized,
    OAuthClientConfig,
    OAuthGrant,
)

log = get_log(__name__)

OPERATION: Final = "mcp"
_FORCED_EXPIRY: Final = 1.0  # a past epoch second: `OAuthContext.is_token_valid` then says "expired" (0 means "none")


@dataclass(slots=True)
class _Memory:
    """The SDK's `TokenStorage` for one call. Persistence is the service's job (it seals the grant we return)."""

    tokens: OAuthToken | None = None
    client_info: OAuthClientInformationFull | None = None
    token_writes: int = 0

    async def get_tokens(self) -> OAuthToken | None:
        return self.tokens

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self.tokens = tokens
        self.token_writes += 1

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        return self.client_info

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        self.client_info = client_info


@dataclass(slots=True)
class _Seen:
    """What the HTTP layer saw, so a JSON-RPC level error can still be told apart by its HTTP status."""

    status: int | None = None
    challenge: str = ""
    retry_after: float | None = None
    sent_auth: bool = False

    async def record(self, response: httpx2.Response) -> None:
        if response.status_code < 400:
            return
        self.status = response.status_code
        self.challenge = response.headers.get("www-authenticate", "")
        self.sent_auth = "authorization" in response.request.headers
        self.retry_after = _seconds(response.headers.get("retry-after"))


def _seconds(value: str | None) -> float | None:
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def _host(url: str) -> str:
    return urlsplit(url).hostname or "mcp"


def _leaf(error: BaseException) -> BaseException:
    """The first real exception inside the task-group wrappers the SDK raises."""
    while isinstance(error, BaseExceptionGroup) and error.exceptions:
        error = error.exceptions[0]
    return error


def _oauth_challenge(seen: _Seen) -> bool:
    challenge = seen.challenge.lower()
    return "resource_metadata" in challenge or (challenge.startswith("bearer") and not seen.sent_auth)


def _translate(error: BaseException, seen: _Seen, *, provider: str, in_flow: bool) -> YordamchiError:  # noqa: PLR0911
    leaf = _leaf(error)
    if isinstance(leaf, YordamchiError):
        return leaf
    if isinstance(leaf, (httpx2.TimeoutException, TimeoutError)):
        return CallTimedOut(operation=OPERATION)
    if isinstance(leaf, OAuthRegistrationError):
        return OAuthClientMetadataNeeded(provider=provider)
    if isinstance(leaf, OAuthFlowError):
        return AuthFailed(provider=provider)
    if seen.status == 401:
        return (
            OAuthRequired(provider=provider)
            if _oauth_challenge(seen) and not in_flow
            else AuthFailed(provider=provider)
        )
    if seen.status == 403:
        return AuthFailed(provider=provider)
    if seen.status == 429:
        return RateLimited(provider=provider, retry_after=seen.retry_after)
    if seen.status is not None and seen.status >= 500:
        return ProviderUnavailable(provider=provider, retryable=True)
    if isinstance(leaf, httpx2.TransportError):
        return ProviderUnavailable(provider=provider, retryable=True)
    if not isinstance(leaf, (MCPError, ValidationError, httpx2.HTTPError, ValueError)):
        log.warning("mcp_error_unmapped", host=provider, error_type=type(leaf).__name__)
    return ProviderUnavailable(provider=provider, retryable=False)


def _tool(tool: Any) -> McpTool:
    annotations = tool.annotations
    schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
    return McpTool(
        name=str(tool.name),
        description=str(tool.description or ""),
        read_only=bool(annotations is not None and annotations.read_only_hint is True),
        input_schema=cast("Mapping[str, JsonValue]", schema),
    )


def _result(result: Any) -> McpToolResult:
    texts = [str(block.text) for block in result.content if getattr(block, "type", None) == "text"]
    return McpToolResult(
        is_error=bool(result.is_error),
        text="\n".join(texts),
        structured=cast("JsonValue", result.structured_content),
    )


def _dump(model: Any) -> JsonObject | None:
    return cast("JsonObject", model.model_dump(mode="json", exclude_none=True)) if model is not None else None


def _state_param(url: str) -> str | None:
    states = parse_qs(urlsplit(url).query).get("state", [])
    return states[0] if len(states) == 1 else None


@dataclass(slots=True)
class _Call:
    """One logged external call."""

    operation: str
    host: str
    call_id: str = field(default_factory=lambda: secrets.token_hex(4))
    started: float = field(default_factory=time.monotonic)

    def done(self, outcome: str) -> None:
        duration_ms = round((time.monotonic() - self.started) * 1000)
        log.info(
            "mcp_call_finished",
            call_id=self.call_id,
            operation=self.operation,
            host=self.host,
            duration_ms=duration_ms,
            outcome=outcome,
        )


class McpHttpClient:
    def __init__(self, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport

    def _http(
        self, *, timeout: float, seen: _Seen, headers: Mapping[str, str] | None = None, auth: httpx2.Auth | None = None
    ) -> httpx2.AsyncClient:
        return httpx2.AsyncClient(
            transport=self._transport,
            headers=dict(headers or {}),
            auth=auth,
            timeout=httpx2.Timeout(timeout),
            event_hooks={"response": [seen.record]},
        )

    async def _session[T](
        self,
        operation: str,
        url: str,
        body: Callable[[Client], Awaitable[T]],
        *,
        timeout: float,
        request_timeout: float | None = None,
        headers: Mapping[str, str] | None = None,
        auth: httpx2.Auth | None = None,
    ) -> T:
        call, seen = _Call(operation, _host(url)), _Seen()
        outcome = "ok"
        try:
            async with asyncio.timeout(timeout):
                http = self._http(timeout=request_timeout or timeout, seen=seen, headers=headers, auth=auth)
                async with (
                    http,
                    Client(
                        streamable_http_client(url, http_client=http), read_timeout_seconds=timeout, cache=None
                    ) as c,
                ):
                    return await body(c)
        except Exception as error:
            translated = _translate(error, seen, provider=call.host, in_flow=auth is not None)
            outcome = translated.code.value
            raise translated from error
        finally:
            call.done(outcome)

    # --- McpClient ----------------------------------------------------------------------------------------------
    async def list_tools(self, url: str, headers: Mapping[str, str], *, timeout: float) -> list[McpTool]:
        async def body(client: Client) -> list[McpTool]:
            tools: list[McpTool] = []
            cursor: str | None = None
            for _ in range(20):  # pagination is bounded: a server that never ends its cursor chain cannot hang us
                page = await client.list_tools(cursor=cursor)
                tools.extend(_tool(tool) for tool in page.tools)
                cursor = page.next_cursor
                if cursor is None:
                    break
            return tools

        return await retry.retrying(
            lambda: self._session("list_tools", url, body, timeout=timeout, headers=headers),
            timeout=timeout,
            operation=OPERATION,
        )

    async def call_tool(
        self,
        url: str,
        headers: Mapping[str, str],
        name: str,
        arguments: Mapping[str, JsonValue],
        *,
        timeout: float,
    ) -> McpToolResult:
        async def body(client: Client) -> McpToolResult:
            return _result(await client.call_tool(name, dict(arguments)))

        return await self._session("call_tool", url, body, timeout=timeout, headers=headers)

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
        storage = _Memory()
        generated: list[str | None] = []

        async def redirect(authorization_url: str) -> None:
            generated.append(_state_param(authorization_url))
            await on_link(authorization_url)

        async def callback() -> AuthorizationCodeResult:
            pasted = await wait_callback()
            state = pasted.state if pasted.state is not None else (generated[-1] if generated else None)
            return AuthorizationCodeResult(code=pasted.code, state=state, iss=pasted.iss)

        provider = OAuthClientProvider(
            server_url=url,
            client_metadata=_metadata(client),
            storage=storage,
            redirect_handler=redirect,
            callback_handler=callback,
            client_metadata_url=client.client_metadata_url,
        )

        async def body(session: Client) -> list[McpTool]:
            page = await session.list_tools()
            return [_tool(tool) for tool in page.tools]

        tools = await self._session(
            "authorize", url, body, timeout=timeout, request_timeout=request_timeout, auth=provider
        )
        return OAuthAuthorized(tools=tools, grant=_grant(provider, storage, provider_host=_host(url)))

    async def refresh(self, url: str, grant: OAuthGrant, *, client: OAuthClientConfig, timeout: float) -> OAuthGrant:
        call, host = _Call("refresh", _host(url)), _host(url)
        outcome = "ok"
        try:
            storage, provider = _restored(url, grant, client, provider_host=host)
            async with asyncio.timeout(timeout):
                await self._drive_refresh(provider, url, timeout=timeout, host=host)
            if storage.token_writes == 0 or storage.tokens is None:
                raise OAuthReauthRequired(provider=host)
            return _grant(provider, storage, provider_host=host)
        except Exception as error:
            translated = _translate_refresh(error, provider=host)
            outcome = translated.code.value
            raise translated from error
        finally:
            call.done(outcome)

    async def _drive_refresh(self, provider: OAuthClientProvider, url: str, *, timeout: float, host: str) -> None:
        probe = httpx2.Request("POST", url)
        flow = provider.async_auth_flow(probe)
        seen = _Seen()
        async with self._http(timeout=timeout, seen=seen) as http:
            try:
                outgoing = await flow.__anext__()
                while outgoing is not probe:
                    response = await http.send(outgoing)
                    if response.status_code == 429:
                        raise RateLimited(provider=host, retry_after=_seconds(response.headers.get("retry-after")))
                    if response.status_code >= 500:
                        raise ProviderUnavailable(provider=host, retryable=True)
                    outgoing = await flow.asend(response)
            finally:
                await flow.aclose()


def _translate_refresh(error: BaseException, *, provider: str) -> YordamchiError:
    leaf = _leaf(error)
    if isinstance(leaf, YordamchiError):
        return leaf
    if isinstance(leaf, (httpx2.TimeoutException, TimeoutError)):
        return CallTimedOut(operation=OPERATION)
    if isinstance(leaf, httpx2.TransportError):
        return ProviderUnavailable(provider=provider, retryable=True)
    if isinstance(leaf, (OAuthTokenError, OAuthFlowError, ValidationError, KeyError, TypeError)):
        return OAuthReauthRequired(provider=provider)
    log.warning("mcp_error_unmapped", host=provider, error_type=type(leaf).__name__)
    return ProviderUnavailable(provider=provider, retryable=False)


def _metadata(client: OAuthClientConfig) -> OAuthClientMetadata:
    return OAuthClientMetadata(
        client_name=client.client_name,
        redirect_uris=[AnyUrl(client.redirect_uri)],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
    )


def _grant(provider: OAuthClientProvider, storage: _Memory, *, provider_host: str) -> OAuthGrant:
    tokens, info = storage.tokens, storage.client_info
    if tokens is None or info is None:
        raise AuthFailed(provider=provider_host)
    context = provider.context
    state: JsonObject = {
        "tokens": _dump(tokens),
        "client_info": _dump(info),
        "metadata": _dump(context.oauth_metadata),
        "resource_metadata": _dump(context.protected_resource_metadata),
    }
    issuer = str(context.oauth_metadata.issuer) if context.oauth_metadata is not None else info.issuer
    return OAuthGrant(
        state=state,
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        issuer=issuer,
        client_id=info.client_id,
        can_refresh=tokens.refresh_token is not None,
        secret_values=tuple(
            value for value in (tokens.access_token, tokens.refresh_token, info.client_secret) if value is not None
        ),
    )


def _restored(
    url: str, grant: OAuthGrant, client: OAuthClientConfig, *, provider_host: str
) -> tuple[_Memory, OAuthClientProvider]:
    """A provider holding the sealed grant, its expiry forced into the past so the flow refreshes first."""
    state = grant.state
    tokens = OAuthToken.model_validate(state["tokens"])
    if not tokens.refresh_token:
        raise OAuthReauthRequired(provider=provider_host)
    storage = _Memory(tokens=tokens, client_info=OAuthClientInformationFull.model_validate(state["client_info"]))

    async def no_interaction(*_args: object) -> Any:
        raise OAuthReauthRequired(provider=provider_host)  # a refresh never asks the owner anything

    provider = OAuthClientProvider(
        server_url=url,
        client_metadata=_metadata(client),
        storage=storage,
        redirect_handler=no_interaction,
        callback_handler=no_interaction,
    )
    metadata, resource = state.get("metadata"), state.get("resource_metadata")
    provider.context.oauth_metadata = OAuthMetadata.model_validate(metadata) if isinstance(metadata, dict) else None
    provider.context.protected_resource_metadata = (
        ProtectedResourceMetadata.model_validate(resource) if isinstance(resource, dict) else None
    )
    provider.context.token_expiry_time = _FORCED_EXPIRY
    return storage, provider
