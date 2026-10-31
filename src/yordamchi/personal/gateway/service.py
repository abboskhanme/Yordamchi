"""The personal integrations gateway `yordamchi_int` (SPEC §3.2.10 step 7; UC-08 personal, UC-35; T76).

`toolset(deps, ...)` is built for every answer, from the registry as it is now, so an integration added, removed or
failing shows in the next answer (the toolset signature changes, and the adapter opens a client with the new tools).
The rules are `domain.py`'s; this module carries them out:
- `connect_api` → `integrations.add_api_key`, then an optional test GET; `connect_mcp` → `integrations.add_integration`
  (an OAuth sign-in link goes straight to the owner, as the dev bot sends it).
- `<slug>__get` → `integrations.api_credentials` + `integrations.domain.api_url` + `WebTools.api_get`.
- `<slug>__tools` / `<slug>__call` → `McpClient.list_tools` / `call_tool` with `integrations.call_headers`.

A handler never raises: every failure is an error result the model may explain. Logs carry slugs, statuses, sizes and
codes, never a URL, a query, a key or a response body.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import YordamchiError
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.log import get_log
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.gateway import domain
from yordamchi.personal.gateway.domain import ConnectApi, ConnectMcp
from yordamchi.ports.assistant import AssistantTool, AssistantToolset, ToolOutput
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.integrations import domain as integrations_domain
from yordamchi.shared.integrations import service as integrations
from yordamchi.shared.integrations.domain import IntegrationKind, IntegrationView
from yordamchi.shared.integrations.service import AddApiKey, AddIntegration, IntegrationsState
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault.domain import SecretRef

log = get_log(__name__)

PROMPT_CONNECT_API: Final = "personal/gateway_tool_connect_api"
PROMPT_CONNECT_MCP: Final = "personal/gateway_tool_connect_mcp"
PROMPT_GET: Final = "personal/gateway_tool_get"
PROMPT_TOOLS: Final = "personal/gateway_tool_tools"
PROMPT_CALL: Final = "personal/gateway_tool_call"
TEST_BODY_CHARS: Final = 600
KIB: Final = 1024
CONNECT_MARGIN_S: Final = 10.0


@dataclass(frozen=True, slots=True)
class Turn:
    """Who this answer is for. `origin` is None for the start-up probe and the warm-up (connect tools then refuse);
    `own_secret_ids` are the keys that came in the owner's own words of this burst."""

    chat_id: int
    ref: str
    origin: OwnerOrigin | None = None
    own_secret_ids: frozenset[int] = frozenset()


def _output(payload: JsonObject, *, error: bool = False, secrets: Sequence[str] = ()) -> ToolOutput:
    text = domain.mask_secret(json.dumps(payload, ensure_ascii=False, sort_keys=True), secrets)
    return ToolOutput(text=redact.scrubbed(text), is_error=error)


def _failure(error: YordamchiError) -> ToolOutput:
    """An error result; `tell_owner` is the owner's Uzbek line for it, when the catalog has one."""
    payload: JsonObject = {"status": "failed", "error": error.code.value}
    if error.code in S.ERRORS:
        payload["tell_owner"] = str(S.ERRORS[error.code](**error.params))
    return _output(payload, error=True)


async def _own_keys(deps: PersonalDeps, turn: Turn) -> tuple[SecretRef, ...]:
    if not turn.own_secret_ids:
        return ()
    return tuple(ref for ref in await vault.list_secrets(deps) if ref.id in turn.own_secret_ids)


# --- connect --------------------------------------------------------------------------------------------------------
async def _connect_api(deps: PersonalDeps, turn: Turn, request: ConnectApi, snap: Snapshot) -> ToolOutput:
    if turn.origin is None:
        return _output({"status": "refused", "error": "owner_only"}, error=True)
    key = domain.usable_key(request.key, await _own_keys(deps, turn))
    if key is None:
        hint = str(S.PERSONAL_INTEGRATION_HELP())
        return _output({"status": "refused", "error": "key_not_in_owner_message", "tell_owner": hint}, error=True)
    add = AddApiKey(
        name=request.name,
        base_url=request.base_url,
        description=request.description,
        secret=key,
        header_name=request.param if request.auth == "header" else None,
        query_param=request.param if request.auth == "query" else None,
        replace=True,
    )
    try:
        said = await integrations.add_api_key(deps, turn.origin, add)
    except YordamchiError as error:
        log.info("gateway_connect_refused", kind="api", error_code=error.code.value)
        return _failure(error)
    payload: JsonObject = {"status": "connected", "said": str(said)}
    if request.test_path is not None:
        view = integrations_domain.find(await integrations.list_integrations(deps), request.name)
        if view is not None:
            payload["test"] = await _get(deps, view, request.test_path, request.test_query, snap, test=True)
    log.info("gateway_connected", kind="api", tested=request.test_path is not None)
    return _output(payload)


async def _connect_mcp(
    deps: PersonalDeps, state: IntegrationsState, turn: Turn, request: ConnectMcp, snap: Snapshot
) -> ToolOutput:
    if turn.origin is None:
        return _output({"status": "refused", "error": "owner_only"}, error=True)
    key: SecretRef | None = None
    if request.key is not None:
        key = domain.usable_key(request.key, await _own_keys(deps, turn))
        if key is None:
            hint = str(S.PERSONAL_INTEGRATION_HELP())
            return _output({"status": "refused", "error": "key_not_in_owner_message", "tell_owner": hint}, error=True)
    add = AddIntegration(name=request.name, url=request.url, secret=key, header_name=request.header)
    try:
        said = await integrations.add_integration(deps, state, turn.origin, add)
    except YordamchiError as error:
        log.info("gateway_connect_refused", kind="mcp", error_code=error.code.value)
        return _failure(error)
    if (
        integrations_domain.slugify(request.name) in state.flows
    ):  # the sign-in link goes to the owner exactly as the bot wrote it
        await outbox.enqueue_reply(
            deps,
            chat_id=turn.chat_id,
            text=said,
            kind=Kind.NOTICE,
            ref=f"{turn.ref}-oauth",
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
        log.info("gateway_oauth_link_sent")
        return _output({"status": "sign_in_link_sent_to_owner"})
    log.info("gateway_connected", kind="mcp", tested=True)
    return _output({"status": "connected", "said": str(said)})


# --- calls ----------------------------------------------------------------------------------------------------------
async def _get(
    deps: PersonalDeps,
    view: IntegrationView,
    path: str,
    query: Mapping[str, str],
    snap: Snapshot,
    *,
    test: bool = False,
) -> JsonObject:
    """One GET of an API-key integration, as the result object (an `error` key when it did not happen)."""
    try:
        credentials = await integrations.api_credentials(deps, view.id)
    except YordamchiError as error:
        return {"error": error.code.value}
    url = integrations_domain.api_url(view.url, path, query, credentials)
    if url is None:
        return {"error": "path_outside_base_url"}
    secrets = (*credentials.headers.values(), *credentials.query.values())
    try:
        answer = await deps.web.api_get(
            url,
            headers=credentials.headers,
            max_bytes=snap[defaults.INTEGRATIONS_RESPONSE_MAX_KB] * KIB,
            timeout=snap[defaults.TIMEOUTS_INTEGRATION_CALL_S],
        )
    except YordamchiError as error:
        log.info("gateway_call_failed", slug=view.slug, error_code=error.code.value)
        return {"error": error.code.value}
    limit = TEST_BODY_CHARS if test else snap[defaults.INTEGRATIONS_RESULT_MAX_CHARS]
    body, cut = domain.cut(domain.mask_secret(answer.text, secrets), limit)
    log.info("gateway_called", slug=view.slug, status=answer.status, size=len(answer.text), test=test)
    return {
        "http_status": answer.status,
        "content_type": answer.mime,
        "body": domain.body_json(body) if not cut else body,
        "truncated": cut or answer.truncated,
    }


async def _mcp_tools(deps: PersonalDeps, view: IntegrationView, snap: Snapshot) -> ToolOutput:
    try:
        headers = await integrations.call_headers(deps, view.id)
        tools = await deps.mcp.list_tools(view.url, headers, timeout=snap[defaults.TIMEOUTS_INTEGRATION_CALL_S])
    except YordamchiError as error:
        log.info("gateway_call_failed", slug=view.slug, error_code=error.code.value)
        return _failure(error)
    return _output({"tools": domain.tools_json(tools, domain.allowed_tools(view))})


async def _mcp_call(
    deps: PersonalDeps, view: IntegrationView, tool: str, arguments: JsonObject, snap: Snapshot
) -> ToolOutput:
    if tool not in domain.allowed_tools(view):
        return _output({"status": "refused", "error": "tool_not_allowed"}, error=True)
    try:
        headers = await integrations.call_headers(deps, view.id)
        result = await deps.mcp.call_tool(
            view.url, headers, tool, arguments, timeout=snap[defaults.TIMEOUTS_INTEGRATION_CALL_S]
        )
    except YordamchiError as error:
        log.info("gateway_call_failed", slug=view.slug, error_code=error.code.value)
        return _failure(error)
    text, cut = domain.cut(result.text, snap[defaults.INTEGRATIONS_RESULT_MAX_CHARS])
    log.info("gateway_called", slug=view.slug, status="error" if result.is_error else "ok", size=len(result.text))
    payload: JsonObject = {"result": text, "truncated": cut}
    if result.structured is not None and not cut:
        payload["structured"] = result.structured
    return _output(payload, error=result.is_error, secrets=tuple(headers.values()))


# --- the toolset ----------------------------------------------------------------------------------------------------
def _integration_tools(deps: PersonalDeps, view: IntegrationView, timeout_s: float) -> list[AssistantTool]:
    async def get(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        path, query = arguments.get("path"), domain.query_of(arguments.get("query"))
        if not isinstance(path, str) or query is None:
            return _output({"status": "refused", "error": "bad_arguments"}, error=True)
        result = await _get(deps, view, path, query, await settings.snapshot(deps))
        return _output(result, error="error" in result)

    async def list_tools(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        del arguments
        return await _mcp_tools(deps, view, await settings.snapshot(deps))

    async def call(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        parsed = domain.call_of(arguments)
        if parsed is None:
            return _output({"status": "refused", "error": "bad_arguments"}, error=True)
        return await _mcp_call(deps, view, parsed[0], parsed[1], await settings.snapshot(deps))

    slots = {"name": view.display_name, "url": view.url, "description": view.description or "-"}
    if view.kind is IntegrationKind.API_HTTP:
        return [
            AssistantTool(
                name=domain.tool_name(view.slug, domain.GET_SUFFIX),
                description=prompts.render(PROMPT_GET, **slots),
                input_schema=domain.GET_SCHEMA,
                timeout_s=timeout_s,
                handler=get,
            )
        ]
    tools = ", ".join(sorted(domain.allowed_tools(view))) or "-"
    return [
        AssistantTool(
            name=domain.tool_name(view.slug, domain.TOOLS_SUFFIX),
            description=prompts.render(PROMPT_TOOLS, name=view.display_name, tools=tools),
            input_schema=domain.TOOLS_SCHEMA,
            timeout_s=timeout_s,
            handler=list_tools,
        ),
        AssistantTool(
            name=domain.tool_name(view.slug, domain.CALL_SUFFIX),
            description=prompts.render(PROMPT_CALL, name=view.display_name, tools=tools),
            input_schema=domain.CALL_SCHEMA,
            timeout_s=timeout_s,
            handler=call,
        ),
    ]


async def toolset(deps: PersonalDeps, state: IntegrationsState, turn: Turn) -> AssistantToolset:
    """The `yordamchi_int` tools of one answer: the two connect tools, then each offered integration's own."""
    snap = await settings.snapshot(deps)
    call_s = snap[defaults.TIMEOUTS_INTEGRATION_CALL_S]
    connect_s = max(call_s, snap[defaults.TIMEOUTS_INTEGRATION_VALIDATE_S]) + call_s + CONNECT_MARGIN_S

    async def connect_api(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        request = domain.connect_api_of(arguments)
        if isinstance(request, str):
            return _output({"status": "refused", "error": request}, error=True)
        return await _connect_api(deps, turn, request, await settings.snapshot(deps))

    async def connect_mcp(arguments: Mapping[str, JsonValue]) -> ToolOutput:
        request = domain.connect_mcp_of(arguments)
        if isinstance(request, str):
            return _output({"status": "refused", "error": request}, error=True)
        return await _connect_mcp(deps, state, turn, request, await settings.snapshot(deps))

    tools = [
        AssistantTool(
            name=domain.CONNECT_API,
            description=prompts.render(PROMPT_CONNECT_API),
            input_schema=domain.CONNECT_API_SCHEMA,
            timeout_s=connect_s,
            read_only=False,
            handler=connect_api,
        ),
        AssistantTool(
            name=domain.CONNECT_MCP,
            description=prompts.render(PROMPT_CONNECT_MCP),
            input_schema=domain.CONNECT_MCP_SCHEMA,
            timeout_s=connect_s,
            read_only=False,
            handler=connect_mcp,
        ),
    ]
    for view in domain.offered(await integrations.list_integrations(deps)):
        tools.extend(_integration_tools(deps, view, call_s))
    return AssistantToolset(server=domain.SERVER, tools=tuple(tools))
