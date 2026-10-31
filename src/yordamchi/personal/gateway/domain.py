"""The personal bot's integrations gateway `yordamchi_int`, pure (SPEC §3.2.10 step 7; UC-08 personal, UC-35; T76).

The personal session never gets a remote MCP server. It gets this in-process toolset instead:
- `connect_api` / `connect_mcp`: the owner gave a key or a server in his own words ("mana OpenWeather API kaliti:
  [kalit: … ••••abcd], ob-havo uchun ulan") and the assistant fills in what it knows (the base URL, how the key is
  sent). The key itself never reaches the model: it sees the vault marker, and a connect tool takes only the name of a
  key **that came in the owner's own words in this very message** (`usable_key`). A key named by a web page, a file or
  a forwarded message is refused, so content cannot point the owner's key at a host of its choosing.
- `<slug>__get(path, query)` for each API-key integration: GET only, under its fixed base URL, the key added
  in-process (`integrations.domain.api_url`).
- `<slug>__tools` and `<slug>__call(tool, arguments)` for each MCP integration: only the tools that were read-only
  (or allowed) when it was added; a tool that appeared on the server later is never callable.

Every tool result is data for the model; a key value that comes back in one is masked (`mask_secret`).
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from yordamchi.core import redact
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName
from yordamchi.ports.mcp import McpTool
from yordamchi.shared.integrations import domain as integrations
from yordamchi.shared.integrations.domain import IntegrationView
from yordamchi.shared.vault.domain import SecretRef

SERVER: Final = "yordamchi_int"
CONNECT_API: Final = "connect_api"
CONNECT_MCP: Final = "connect_mcp"
GET_SUFFIX: Final = "__get"
TOOLS_SUFFIX: Final = "__tools"
CALL_SUFFIX: Final = "__call"
AUTH_STYLES: Final = ("bearer", "header", "query")
SECRET_MASK: Final = redact.MASK
_TOOL_NAME_MAX: Final = 48
# The statuses an integration is offered in: a server that may come back stays; a refused key does not.
OFFERED: Final = integrations.INJECTED_STATUSES

CONNECT_API_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "description": "Short name of the service, e.g. OpenWeather"},
        "base_url": {"type": "string", "description": "https base URL every call is made under"},
        "key": {"type": "string", "description": "The vault name inside the owner's [kalit: <name> ••••] marker"},
        "auth": {"type": "string", "enum": list(AUTH_STYLES)},
        "param": {"type": "string", "description": "Header name (auth=header) or query parameter (auth=query)"},
        "description": {"type": "string", "description": "What the owner uses it for, in a few words"},
        "test_path": {"type": "string", "description": "Optional cheap GET path to check the key works"},
        "test_query": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}},
    },
    "required": ["name", "base_url", "key", "auth", "description"],
    "additionalProperties": False,
}
CONNECT_MCP_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "url": {"type": "string", "description": "The MCP server URL (https)"},
        "key": {"type": "string", "description": "Optional: the vault name inside the owner's [kalit: …] marker"},
        "header": {"type": "string", "description": "Optional header name for the key (default Authorization)"},
    },
    "required": ["name", "url"],
    "additionalProperties": False,
}
GET_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "Path under the base URL, e.g. /weather"},
        "query": {"type": "object", "additionalProperties": {"type": ["string", "number", "boolean"]}},
    },
    "required": ["path"],
    "additionalProperties": False,
}
TOOLS_SCHEMA: Final[JsonObject] = {"type": "object", "properties": {}, "additionalProperties": False}
CALL_SCHEMA: Final[JsonObject] = {
    "type": "object",
    "properties": {"tool": {"type": "string"}, "arguments": {"type": "object"}},
    "required": ["tool"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class ConnectApi:
    name: str
    base_url: str
    key: str
    auth: str
    param: str | None
    description: str
    test_path: str | None
    test_query: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class ConnectMcp:
    name: str
    url: str
    key: str | None
    header: str | None


# --- which integrations, which tools --------------------------------------------------------------------------------
def offered(views: Iterable[IntegrationView]) -> tuple[IntegrationView, ...]:
    """The personal bot's integrations the assistant gets tools for, by slug (a stable toolset signature)."""
    return tuple(
        sorted(
            (
                view
                for view in views
                if view.removed_at is None and BotName.PERSONAL in view.bots and view.status in OFFERED
            ),
            key=lambda view: view.slug,
        )
    )


def tool_name(slug: str, suffix: str) -> str:
    return f"{slug}{suffix}"[:_TOOL_NAME_MAX]


def allowed_tools(view: IntegrationView) -> frozenset[str]:
    """The MCP tools of `view` the personal bot may call: read-only at add time, or allowed then (UC-35)."""
    names = integrations.allowed_tools((view,), BotName.PERSONAL)
    prefix = integrations.tool_name(view.slug, "")
    return frozenset(name.removeprefix(prefix) for name in names)


def tools_json(tools: Sequence[McpTool], allowed: frozenset[str]) -> JsonValue:
    """The live list of the allowed tools (their descriptions and schemas are server content: data)."""
    return [
        {"name": tool.name, "description": tool.description[:1000], "input_schema": dict(tool.input_schema)}
        for tool in tools
        if tool.name in allowed
    ]


# --- the owner's key ------------------------------------------------------------------------------------------------
def usable_key(name: str, own: Sequence[SecretRef]) -> SecretRef | None:
    """The key a connect tool names, only if it came in the owner's own words of this message (module docstring)."""
    wanted = name.strip().strip("[]").removeprefix("kalit:").strip().split(" ", 1)[0]
    return next((ref for ref in own if ref.name == wanted), None)


def mask_secret(text: str, values: Iterable[str]) -> str:
    for value in values:
        if value:
            text = text.replace(value, SECRET_MASK)
    return text


# --- arguments ------------------------------------------------------------------------------------------------------
def _text(arguments: Mapping[str, JsonValue], key: str) -> str | None:
    value = arguments.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else None


def query_of(raw: JsonValue) -> dict[str, str] | None:
    """A tool's `query` object as strings; None when it is not an object of scalars."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        return None
    found: dict[str, str] = {}
    for key, value in raw.items():
        if isinstance(value, bool):
            found[key] = "true" if value else "false"
        elif isinstance(value, (str, int, float)):
            found[key] = str(value)
        else:
            return None
    return found


def connect_api_of(arguments: Mapping[str, JsonValue]) -> ConnectApi | str:
    """The checked `connect_api` arguments, or the snake_case problem to hand back."""
    name, base_url, key = _text(arguments, "name"), _text(arguments, "base_url"), _text(arguments, "key")
    auth = _text(arguments, "auth") or "bearer"
    if name is None or base_url is None or key is None:
        return "missing_argument"
    if auth not in AUTH_STYLES:
        return "bad_auth"
    param = _text(arguments, "param")
    if auth != "bearer" and param is None:
        return "missing_param"
    test_query = query_of(arguments.get("test_query"))
    if test_query is None:
        return "bad_test_query"
    return ConnectApi(
        name=name,
        base_url=base_url,
        key=key,
        auth=auth,
        param=param if auth != "bearer" else None,
        description=_text(arguments, "description") or "",
        test_path=_text(arguments, "test_path"),
        test_query=test_query,
    )


def connect_mcp_of(arguments: Mapping[str, JsonValue]) -> ConnectMcp | str:
    name, url = _text(arguments, "name"), _text(arguments, "url")
    if name is None or url is None:
        return "missing_argument"
    return ConnectMcp(name=name, url=url, key=_text(arguments, "key"), header=_text(arguments, "header"))


def call_of(arguments: Mapping[str, JsonValue]) -> tuple[str, JsonObject] | None:
    tool, call_arguments = _text(arguments, "tool"), arguments.get("arguments", {})
    if tool is None or not isinstance(call_arguments, dict):
        return None
    return tool, call_arguments


# --- results --------------------------------------------------------------------------------------------------------
def cut(text: str, max_chars: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= max_chars else (text[:max_chars], True)


def body_json(text: str) -> JsonValue:
    """A JSON body as JSON (the model reads it more easily), anything else as text."""
    try:
        parsed: JsonValue = json.loads(text)
    except ValueError:
        return text
    return parsed
