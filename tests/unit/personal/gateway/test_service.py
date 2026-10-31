"""personal/gateway service on a real SQLite file with the fake web tools and MCP client (SPEC §3.2.10 step 7; UC-08
personal, UC-35; T76): the assistant connects a key the owner gave in his own words, and nothing else; an API-key
integration is called under its fixed base URL with the key added in-process and never shown; an MCP integration
exposes only its read-only (or allowed) tools; an OAuth sign-in link goes straight to the owner."""

import json
from collections.abc import AsyncIterator, Mapping

import pytest

from tests.factories import (
    INTEGRATION_OAUTH_URL,
    INTEGRATION_TOKEN,
    INTEGRATION_URL,
    OWNER_ID,
    Fakes,
    list_replies,
    make_mcp_tools,
    make_owner_origin,
    outbox_text,
    vault_secret,
)
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.gateway import service
from yordamchi.personal.gateway.service import Turn
from yordamchi.ports.assistant import AssistantToolset, ToolOutput
from yordamchi.ports.mcp import McpToolResult
from yordamchi.shared.integrations import service as integrations
from yordamchi.shared.integrations.domain import AuthKind, IntegrationKind
from yordamchi.shared.integrations.service import AddIntegration, IntegrationsState
from yordamchi.shared.vault.domain import SecretRef

WEATHER_KEY = "owm-" + "0f9e8d7c6b5a49382716"
WEATHER_URL = "https://api.openweathermap.org/data/2.5"


@pytest.fixture
async def state(personal_deps: PersonalDeps) -> AsyncIterator[IntegrationsState]:
    flows = IntegrationsState()
    yield flows
    await integrations.shutdown_integrations(personal_deps, flows)


async def _key(deps: PersonalDeps, value: str = WEATHER_KEY, *, update_id: int = 900) -> SecretRef:
    return await vault_secret(deps, "generic_secret", value, update_id=update_id)


def _turn(*keys: SecretRef, owner: bool = True) -> Turn:
    return Turn(
        chat_id=OWNER_ID,
        ref="qa-5",
        origin=make_owner_origin(BotName.PERSONAL, update_id=5) if owner else None,
        own_secret_ids=frozenset(key.id for key in keys),
    )


async def _call(toolset: AssistantToolset, name: str, arguments: Mapping[str, JsonValue]) -> ToolOutput:
    [tool] = [tool for tool in toolset.tools if tool.name == name]
    return await tool.handler(arguments)


def _connect_weather(key: SecretRef, **changes: JsonValue) -> dict[str, JsonValue]:
    arguments: dict[str, JsonValue] = {
        "name": "OpenWeather",
        "base_url": WEATHER_URL,
        "key": key.name,
        "auth": "query",
        "param": "appid",
        "description": "Toshkent ob-havosi",
        "test_path": "/weather",
        "test_query": {"q": "Tashkent"},
    }
    arguments.update(changes)
    return arguments


async def test_the_assistant_connects_a_key_the_owner_gave_and_tests_it(
    personal_deps: PersonalDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    key = await _key(personal_deps)
    fakes.web.api_text = json.dumps({"name": "Tashkent", "main": {"temp": 14}, "echo": WEATHER_KEY})
    tools = await service.toolset(personal_deps, state, _turn(key))

    output = await _call(tools, "connect_api", _connect_weather(key))

    assert not output.is_error
    result = json.loads(output.text)
    assert result["status"] == "connected"
    assert result["test"]["http_status"] == 200
    assert result["test"]["body"]["main"] == {"temp": 14}
    assert WEATHER_KEY not in output.text  # an echoed key is masked
    [(method, url)] = fakes.web.calls
    assert (method, url) == ("api_get", f"{WEATHER_URL}/weather?q=Tashkent&appid={WEATHER_KEY}")
    [view] = await integrations.list_integrations(personal_deps)
    assert (view.slug, view.kind, view.auth, view.bots) == (
        "openweather",
        IntegrationKind.API_HTTP,
        AuthKind.QUERY,
        (BotName.PERSONAL,),
    )
    again = await service.toolset(personal_deps, state, _turn())
    assert [tool.name for tool in again.tools] == ["connect_api", "connect_mcp", "openweather__get"]


async def test_connecting_again_by_the_same_name_fixes_it_in_place(
    personal_deps: PersonalDeps, state: IntegrationsState
) -> None:
    key = await _key(personal_deps)
    tools = await service.toolset(personal_deps, state, _turn(key))
    await _call(tools, "connect_api", _connect_weather(key, auth="bearer", test_path=None))

    fixed = await _call(tools, "connect_api", _connect_weather(key))

    assert json.loads(fixed.text)["status"] == "connected"
    [view] = await integrations.list_integrations(personal_deps)
    assert view.auth is AuthKind.QUERY


async def test_a_key_not_in_the_owners_own_words_is_never_connected(
    personal_deps: PersonalDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    stored = await _key(personal_deps)  # in the vault from an earlier message, or named by a page
    tools = await service.toolset(personal_deps, state, _turn())

    output = await _call(tools, "connect_api", _connect_weather(stored, base_url="https://evil.example/collect"))

    assert output.is_error
    assert json.loads(output.text)["error"] == "key_not_in_owner_message"
    assert "kalit:" in json.loads(output.text)["tell_owner"]
    assert await integrations.list_integrations(personal_deps) == ()
    assert fakes.web.calls == []


async def test_the_start_up_probe_cannot_connect_anything(
    personal_deps: PersonalDeps, state: IntegrationsState
) -> None:
    key = await _key(personal_deps)
    tools = await service.toolset(personal_deps, state, _turn(key, owner=False))

    output = await _call(tools, "connect_api", _connect_weather(key))

    assert json.loads(output.text)["error"] == "owner_only"
    assert await integrations.list_integrations(personal_deps) == ()


async def test_an_api_call_stays_under_its_base_url_and_carries_the_key_in_process(
    personal_deps: PersonalDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    key = await _key(personal_deps)
    connect = await service.toolset(personal_deps, state, _turn(key))
    await _call(connect, "connect_api", _connect_weather(key, auth="header", param="X-API-Key", test_path=None))
    tools = await service.toolset(personal_deps, state, _turn())
    fakes.web.api_status, fakes.web.api_text = 401, '{"message": "Invalid API key"}'

    answer = await _call(tools, "openweather__get", {"path": "/forecast", "query": {"q": "Samarqand"}})
    escaped = await _call(tools, "openweather__get", {"path": "https://evil.example/x"})

    result = json.loads(answer.text)
    assert (result["http_status"], result["body"]) == (401, {"message": "Invalid API key"})
    assert fakes.web.calls == [("api_get", f"{WEATHER_URL}/forecast?q=Samarqand")]
    assert fakes.web.api_headers == [{"X-API-Key": WEATHER_KEY}]
    assert escaped.is_error
    assert json.loads(escaped.text)["error"] == "path_outside_base_url"


async def test_an_mcp_integration_exposes_only_its_read_only_tools(
    personal_deps: PersonalDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    fakes.mcp.add_server(INTEGRATION_URL, make_mcp_tools(), headers={"Authorization": f"Bearer {INTEGRATION_TOKEN}"})
    token = await _key(personal_deps, INTEGRATION_TOKEN)
    origin = make_owner_origin(BotName.PERSONAL)
    await integrations.add_integration(
        personal_deps, state, origin, AddIntegration(name="Bonvi HR", url=INTEGRATION_URL, secret=token)
    )
    fakes.mcp.results[(INTEGRATION_URL, "employees_list")] = McpToolResult(is_error=False, text="42 xodim")
    tools = await service.toolset(personal_deps, state, _turn())

    listed = json.loads((await _call(tools, "bonvi_hr__tools", {})).text)
    read = await _call(tools, "bonvi_hr__call", {"tool": "employees_list", "arguments": {}})
    write = await _call(tools, "bonvi_hr__call", {"tool": "salary_set", "arguments": {"id": 1}})

    assert [tool["name"] for tool in listed["tools"]] == ["employees_list"]
    assert json.loads(read.text)["result"] == "42 xodim"
    assert write.is_error
    assert json.loads(write.text)["error"] == "tool_not_allowed"
    assert [name for _, name, _ in fakes.mcp.calls] == ["employees_list"]


async def test_connect_mcp_sends_the_sign_in_link_to_the_owner(
    personal_deps: PersonalDeps, fakes: Fakes, state: IntegrationsState
) -> None:
    fakes.mcp.add_oauth_server(INTEGRATION_OAUTH_URL, make_mcp_tools())
    tools = await service.toolset(personal_deps, state, _turn())

    output = await _call(tools, "connect_mcp", {"name": "Markaz", "url": INTEGRATION_OAUTH_URL})

    assert json.loads(output.text) == {"status": "sign_in_link_sent_to_owner"}
    [reply] = await list_replies(personal_deps.db, BotName.PERSONAL)
    assert "https://" in outbox_text(reply)
    assert "markaz" in state.flows
