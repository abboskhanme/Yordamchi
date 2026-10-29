"""`ClaudeAssistant`, the warm pool, on recorded streams (CONVENTIONS §4, §11; SPEC §5.3, §5.4; NFR-SPD-02).

Offline: every client is a `tests/fakes/claude_client.FakeClient` replaying `tests/fixtures/claude/streams/
assistant_{report,routing}.jsonl` (recorded from the real CLI with haiku through this adapter, then sanitised), so the
hooks, the guard and the stream translator run for real. The `live` tests at the end drive `~/.local/bin/claude` and
measure warm-ask latency.
"""

import asyncio
import base64
import json
import stat
import statistics
import sys
import time
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, CLINotFoundError, ProcessError

from tests.fakes.claude_client import FIXTURES, LIVE_CLI, LIVE_SEARCH_PATH, FakeClient, records, scrub_live_env
from tests.fakes.clock import FakeClock
from yordamchi.adapters.claude.assistant import (
    ASSISTANT_ENV,
    ClaudeAssistant,
    context_tokens,
    structured_value,
)
from yordamchi.adapters.claude.options import ALWAYS_DISALLOWED, ENFORCED_ENV
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ConfigInvalid,
    NotInstalled,
    ProviderUnavailable,
    RateLimited,
)
from yordamchi.core.json_value import JsonValue
from yordamchi.ports.assistant import AssistantDelta, AssistantReply, AssistantRequest

TIMEOUT = 5.0
# A probe runs a fake `claude` script written fresh into tmp_path; macOS scans a new executable on its first run
# (XProtect), which took over 5 s while a VM loaded the Mac (2026-10-25). The bound is the harness's, not a latency
# claim: the production probe timeout is a setting.
PROBE_TIMEOUT = 30.0
SEARCH_PATH = "/Users/owner/.local/bin:/usr/bin:/bin"
CLI = Path("/Users/owner/.local/bin/claude")
BASE_ENV = {"HOME": "/Users/owner", "PATH": "/elsewhere", "CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "parent"}
REPORT_SYSTEM = "Fixed report role and contract (test data)."
ROUTING_SYSTEM = "Fixed routing role (test data)."
SCHEMA: dict[str, JsonValue] = {
    "type": "object",
    "properties": {
        "project": {"type": ["string", "null"]},
        "confidence": {"type": "number"},
        "candidates": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["project", "confidence", "candidates"],
    "additionalProperties": False,
}
UZ = "Current instructions from the owner: answer in Uzbek, one sentence, end with the tag [UZ]."
EN = "Current instructions from the owner: answer in English, one sentence, end with the tag [EN]."

type Recording = list[dict[str, Any]]


def _report(prompt: str = "BonviHR qanday?", **overrides: object) -> AssistantRequest:
    values: dict[str, Any] = {
        "kind": "report",
        "system_prompt": REPORT_SYSTEM,
        "prompt": prompt,
        "model": "haiku",
        "settings_version": 1,
        "effort": "low",
        "instructions": UZ,
        "prompt_version": 1,
    }
    values.update(overrides)
    return AssistantRequest(**values)


def _routing(prompt: str = "nomzodlar formasi ishlamayapti", **overrides: object) -> AssistantRequest:
    values: dict[str, Any] = {
        "kind": "routing",
        "system_prompt": ROUTING_SYSTEM,
        "prompt": prompt,
        "model": "haiku",
        "settings_version": 1,
        "output_schema": SCHEMA,
        "instructions": "Current instructions from the owner: Projects: BonviHR, HRMentor.",
    }
    values.update(overrides)
    return AssistantRequest(**values)


# --- recordings ----------------------------------------------------------------------------------------------------
def _turns(name: str) -> list[Recording]:
    """The recording split at each `sent`: one segment per ask."""
    segments: list[Recording] = []
    for record in records(name):
        if record["kind"] == "sent":
            segments.append([])
        if segments:
            segments[-1].append(record)
    return segments


def _asks(name: str, n: int) -> Recording:
    """`n` asks: the recorded turns, cycled (cost totals that drop are a counter restart to the translator)."""
    turns = _turns(name)
    return [record for index in range(n) for record in turns[index % len(turns)]]


def _cut_after_init(name: str) -> Recording:
    """One ask that never ends: the CLI stops after `init` (a hung model call)."""
    first = _turns(name)[0]
    end = next(i for i, r in enumerate(first) if r["kind"] == "raw" and r["data"].get("subtype") == "init")
    return first[: end + 1]


def _with_init_tools(recording: Recording, tools: list[str]) -> Recording:
    out = _copy(recording)
    for record in out:
        if record["kind"] == "raw" and record["data"].get("subtype") == "init":
            record["data"]["tools"] = tools
    return out


def _copy(recording: Recording) -> Recording:
    copied: Recording = json.loads(json.dumps(recording))
    return copied


def _usage_limited(name: str) -> Recording:
    """A real ask whose turn ends in a rejected limit and a 429 result (shapes: synthetic_usage_limited.jsonl)."""
    turn = _copy(_turns(name)[0])
    limit = next(r for r in records("synthetic_usage_limited") if r["data"].get("type") == "rate_limit_event")
    result_at = next(i for i, r in enumerate(turn) if r["kind"] == "raw" and r["data"].get("type") == "result")
    result = turn[result_at]["data"]
    result.update({"is_error": True, "api_error_status": 429, "result": "API Error: 429 You've hit your limit"})
    return [*turn[:result_at], limit, *turn[result_at:]]


def _auth_failed(name: str) -> Recording:
    turn = _copy(_turns(name)[0])
    for record in turn:
        if record["kind"] == "raw" and record["data"].get("type") == "result":
            record["data"].update({"is_error": True, "api_error_status": 401, "result": "Failed to authenticate."})
    return turn


class Factory:
    """Hands each new client the next scripted recording (then `default`), and keeps every client for inspection."""

    def __init__(
        self, default: Recording, *scripted: Recording | BaseException, by_kind: dict[str, Recording] | None = None
    ) -> None:
        self.default = default
        self.scripted: list[Recording | BaseException] = list(scripted)
        self.by_kind = by_kind or {}  # the session cwd's name (the kind) → its recording
        self.clients: list[FakeClient] = []

    def __call__(self, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        kind = Path(str(options.cwd)).name
        item = self.scripted.pop(0) if self.scripted else self.by_kind.get(kind, self.default)
        if isinstance(item, BaseException):
            client = FakeClient(options, connect_error=item, warn=False)
        else:
            client = FakeClient(options, item, warn=False)
        self.clients.append(client)
        return cast("ClaudeSDKClient", client)

    @property
    def options(self) -> list[ClaudeAgentOptions]:
        return [client.options for client in self.clients]


def _assistant(tmp_path: Path, factory: Factory, **overrides: object) -> ClaudeAssistant:
    values: dict[str, Any] = {
        "cli_path": CLI,
        "search_path": SEARCH_PATH,
        "sessions_dir": tmp_path / "sessions",
        "base_env": BASE_ENV,
        "client_factory": factory,
        "clock": FakeClock(),
    }
    values.update(overrides)
    return ClaudeAssistant(**values)


async def _settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0)


async def _until(predicate: Callable[[], bool], rounds: int = 500) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition never held")


def _contexts(client: FakeClient) -> list[str]:
    """The `additionalContext` each UserPromptSubmit hook call returned."""
    found: list[str] = []
    for output in client.hook_outputs:
        specific = output.get("hookSpecificOutput") or {}
        if specific.get("hookEventName") == "UserPromptSubmit":
            found.append(specific.get("additionalContext", ""))
    return found


def _pre_tool_decisions(client: FakeClient) -> list[str | None]:
    return [
        (output.get("hookSpecificOutput") or {}).get("permissionDecision")
        for output in client.hook_outputs
        if (output.get("hookSpecificOutput") or {}).get("hookEventName") == "PreToolUse"
    ]


# --- session options (SPEC §5.3, dev Assistant column) -------------------------------------------------------------
async def test_session_options_follow_the_dev_assistant_column(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_report(), timeout=TIMEOUT)
    options = factory.options[0]

    assert options.cli_path == str(CLI)
    assert options.cwd == str(tmp_path / "sessions" / "report")
    assert (tmp_path / "sessions" / "report").is_dir()
    assert options.setting_sources == []
    assert options.system_prompt == REPORT_SYSTEM  # custom, not the claude_code preset
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.permission_mode == "dontAsk"
    assert options.strict_mcp_config is True
    assert options.mcp_servers == {}
    assert options.model == "haiku"
    assert options.effort == "low"
    assert options.output_format is None
    assert options.include_partial_messages is False
    assert set(ALWAYS_DISALLOWED) <= set(options.disallowed_tools)
    assert options.resume is None
    assert options.session_id
    assert "bare" not in options.extra_args
    env = options.env
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == ASSISTANT_ENV["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == ENFORCED_ENV["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert env["PATH"] == SEARCH_PATH
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SESSION_ID" not in env
    assert not any(name.startswith("ANTHROPIC_") for name in env)
    assert set(options.hooks or {}) == {"PreToolUse", "UserPromptSubmit"}
    await assistant.close(timeout=TIMEOUT)


async def test_routing_options_carry_the_schema(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_routing", 2))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_routing(), timeout=TIMEOUT)
    options = factory.options[0]
    assert options.output_format == {"type": "json_schema", "schema": SCHEMA}
    assert options.cwd == str(tmp_path / "sessions" / "routing")
    assert options.effort is None
    await assistant.close(timeout=TIMEOUT)


async def test_the_chat_kind_is_a_dev_kind_with_no_tools_and_its_own_session(tmp_path: Path) -> None:
    """The dev group's plain answers (`dev/chat`): the report kind's column exactly (no tools, no MCP, no claude.ai
    connectors, no schema), in its own warm client and session directory."""
    factory = Factory(_asks("assistant_report", 2))  # an answer with no tools in `init`, as every chat ask is
    assistant = _assistant(tmp_path, factory)
    request = _report(kind="chat", model="sonnet", prompt="Python'da decorator nima?")
    reply = await assistant.ask(request, timeout=TIMEOUT)
    options = factory.options[0]

    assert reply.text.endswith("[UZ]")
    assert options.cwd == str(tmp_path / "sessions" / "chat")
    assert options.model == "sonnet"
    assert options.tools == []
    assert options.allowed_tools == []
    assert options.mcp_servers == {}
    assert options.strict_mcp_config is True
    assert options.permission_mode == "dontAsk"
    assert options.output_format is None
    assert options.include_partial_messages is False
    assert options.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert set(ALWAYS_DISALLOWED) <= set(options.disallowed_tools)
    await assistant.close(timeout=TIMEOUT)


# --- answers -------------------------------------------------------------------------------------------------------
async def test_a_report_ask_returns_the_text_cost_and_usage(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)

    reply = await assistant.ask(_report(), timeout=TIMEOUT)

    assert reply.provider == "claude"
    assert reply.model == "haiku"
    assert reply.text.endswith("[UZ]")
    assert reply.structured is None
    assert reply.session_id == factory.options[0].session_id
    assert reply.cost_usd_micros == 462  # total_cost_usd 0.00046155 → micro-USD, half-up
    assert reply.usage["output_tokens"] > 0
    assert reply.usage["context_tokens"] > 0
    assert reply.prompt_version == 1
    await assistant.close(timeout=TIMEOUT)


async def test_the_second_ask_is_charged_only_its_own_cost(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    first = await assistant.ask(_report(), timeout=TIMEOUT)
    second = await assistant.ask(_report("HRMentor qanday?", instructions=EN, prompt_version=2), timeout=TIMEOUT)
    assert first.cost_usd_micros + second.cost_usd_micros == 568  # session total 0.00056779
    assert second.session_id == first.session_id
    await assistant.close(timeout=TIMEOUT)


async def test_a_routing_ask_returns_the_structured_value(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_routing", 2))
    assistant = _assistant(tmp_path, factory)

    reply = await assistant.ask(_routing(), timeout=TIMEOUT)

    assert reply.structured == {"project": "BonviHR", "confidence": 0.9, "candidates": ["BonviHR"]}
    assert json.loads(reply.text) == reply.structured
    assert _pre_tool_decisions(factory.clients[0]) == ["allow"]  # StructuredOutput passes the guard explicitly
    await assistant.close(timeout=TIMEOUT)


async def test_the_guard_denies_any_tool_but_structured_output(tmp_path: Path) -> None:
    """Defense in depth: `tools=[]` should leave nothing to call, and a call that appears anyway is denied."""
    recording = _copy(_asks("assistant_routing", 1))
    for record in recording:
        if record["kind"] == "hook" and record["data"]["input"].get("hook_event_name") == "PreToolUse":
            record["data"]["input"]["tool_name"] = "Bash"
    factory = Factory(recording)
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_routing(), timeout=TIMEOUT)
    assert _pre_tool_decisions(factory.clients[0]) == ["deny"]
    await assistant.close(timeout=TIMEOUT)


# --- the per-ask instruction context (SPEC §5.4 revised, S4) --------------------------------------------------------
async def test_every_ask_carries_the_current_instructions_without_a_rotation(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)

    first = await assistant.ask(_report(instructions=UZ, prompt_version=1), timeout=TIMEOUT)
    second = await assistant.ask(_report(instructions=EN, prompt_version=2), timeout=TIMEOUT)

    live = factory.clients[0]
    assert _contexts(live) == [UZ, EN]  # every ask, not only after a change
    assert second.session_id == first.session_id  # a prompt change never rotates
    assert second.prompt_version == 2
    assert live.disconnected is False
    assert factory.options[0].system_prompt == REPORT_SYSTEM  # the frozen part holds no instruction
    await assistant.close(timeout=TIMEOUT)


async def test_no_instructions_means_no_context(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_report(instructions=""), timeout=TIMEOUT)
    assert _contexts(factory.clients[0]) == []
    await assistant.close(timeout=TIMEOUT)


# --- the pool: warm, spare, rotation -------------------------------------------------------------------------------
async def test_warm_starts_the_live_client_and_a_hot_spare(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)

    await assistant.warm(_report().profile, timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    await _settle()

    assert all(client._query is not None for client in factory.clients)  # both connected, none asked
    assert all(client.sent == [] for client in factory.clients)
    reply = await assistant.ask(_report(), timeout=TIMEOUT)
    assert reply.session_id == factory.options[0].session_id
    assert len(factory.clients) == 2  # the ask spawned nothing
    await assistant.close(timeout=TIMEOUT)


async def test_a_client_rotates_after_twenty_asks_and_the_spare_takes_over(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 21))
    assistant = _assistant(tmp_path, factory)

    sessions = [(await assistant.ask(_report(), timeout=TIMEOUT)).session_id for _ in range(21)]

    first, spare = factory.clients[0], factory.clients[1]
    assert set(sessions[:20]) == {first.options.session_id}
    assert sessions[20] == spare.options.session_id
    assert len(first.sent) == 20
    await _until(lambda: first.disconnected)
    await _until(lambda: len(factory.clients) == 3)  # a new spare started at once
    await assistant.close(timeout=TIMEOUT)


async def test_a_custom_ask_limit_is_honoured(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 3))
    assistant = _assistant(tmp_path, factory, max_asks=2)
    sessions = [(await assistant.ask(_report(), timeout=TIMEOUT)).session_id for _ in range(3)]
    assert sessions[0] == sessions[1] != sessions[2]
    await assistant.close(timeout=TIMEOUT)


async def test_a_higher_settings_version_rotates_to_the_fresh_spare(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 4))
    assistant = _assistant(tmp_path, factory)

    first = await assistant.ask(_report(settings_version=3), timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    second = await assistant.ask(_report(settings_version=4), timeout=TIMEOUT)
    third = await assistant.ask(_report(settings_version=4), timeout=TIMEOUT)
    stale = await assistant.ask(_report(settings_version=2), timeout=TIMEOUT)  # an older snapshot never rotates

    assert second.session_id != first.session_id
    assert second.session_id == factory.options[1].session_id  # the spare, already connected
    assert third.session_id == second.session_id == stale.session_id
    await _until(lambda: factory.clients[0].disconnected)
    await assistant.close(timeout=TIMEOUT)


async def test_the_context_threshold_rotates(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory, context_limit_tokens=500)
    first = await assistant.ask(_report(), timeout=TIMEOUT)
    assert first.usage["context_tokens"] >= 500
    await _until(lambda: len(factory.clients) == 2)
    await _settle()
    second = await assistant.ask(_report(), timeout=TIMEOUT)
    assert second.session_id != first.session_id
    await assistant.close(timeout=TIMEOUT)


async def test_a_soft_rotation_keeps_the_warm_client_until_the_spare_is_ready(tmp_path: Path) -> None:
    """The spare's connect hangs, so after the ask limit the live client keeps answering: never a cold ask."""
    factory = Factory(_asks("assistant_report", 3))
    assistant = _assistant(tmp_path, factory, max_asks=1)
    hanging: list[FakeClient] = []

    def slow_spares(options: ClaudeAgentOptions) -> ClaudeSDKClient:
        client = factory(options)
        if len(factory.clients) > 1:
            fake = cast("FakeClient", client)
            fake.connect_hangs = True
            hanging.append(fake)
        return client

    assistant._factory = slow_spares
    sessions = [(await assistant.ask(_report(), timeout=TIMEOUT)).session_id for _ in range(3)]
    assert set(sessions) == {factory.options[0].session_id}
    assert hanging, "a spare was being started"
    await assistant.close(timeout=0.1)


async def test_a_model_change_calls_set_model_instead_of_rotating(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    first = await assistant.ask(_report(model="haiku"), timeout=TIMEOUT)
    second = await assistant.ask(_report(model="sonnet"), timeout=TIMEOUT)
    assert factory.clients[0].models == ["sonnet"]
    assert second.session_id == first.session_id
    assert second.model == "sonnet"
    await assistant.close(timeout=TIMEOUT)


async def test_an_effort_change_rebuilds_the_spare_and_then_rotates(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 3))
    assistant = _assistant(tmp_path, factory)
    first = await assistant.ask(_report(effort="low"), timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    await _settle()
    assert factory.options[1].effort == "low"

    second = await assistant.ask(_report(effort="medium"), timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 3)
    await _settle()

    assert factory.options[2].effort == "medium"  # the spare was rebuilt with the new effort at once
    await _until(lambda: factory.clients[1].disconnected)  # the old-effort spare is closed
    assert second.session_id in {first.session_id, factory.options[2].session_id}
    third = await assistant.ask(_report(effort="medium"), timeout=TIMEOUT)
    assert third.session_id == factory.options[2].session_id
    await assistant.close(timeout=TIMEOUT)


async def test_a_changed_system_prompt_never_reuses_the_frozen_client(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    first = await assistant.ask(_report(), timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    second = await assistant.ask(_report(system_prompt="Fixed report role v2 (test data)."), timeout=TIMEOUT)
    assert second.session_id not in {first.session_id, factory.options[1].session_id}
    assert factory.clients[-2].options.system_prompt == "Fixed report role v2 (test data)."
    await assistant.close(timeout=TIMEOUT)


async def test_reset_drops_the_warm_session_and_promotes_the_spare(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    first = await assistant.ask(_report(), timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    await _settle()
    await assistant.reset("report", conversation=None, timeout=TIMEOUT)
    second = await assistant.ask(_report(), timeout=TIMEOUT)
    assert second.session_id == factory.options[1].session_id != first.session_id
    await _until(lambda: factory.clients[0].disconnected)
    await assistant.reset("settings", conversation=None, timeout=TIMEOUT)  # no pool yet: nothing to drop
    await assistant.close(timeout=TIMEOUT)


async def test_kinds_have_separate_pools(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    report = await assistant.ask(_report(), timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    factory.default = _asks("assistant_routing", 2)
    routing = await assistant.ask(_routing(), timeout=TIMEOUT)
    assert routing.session_id != report.session_id
    cwds = {Path(options.cwd or "").name for options in factory.options}
    assert cwds == {"report", "routing"}
    await assistant.close(timeout=TIMEOUT)


# --- failures ------------------------------------------------------------------------------------------------------
async def test_a_timeout_discards_the_client_and_promotes_the_spare(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2), _cut_after_init("assistant_report"))
    assistant = _assistant(tmp_path, factory)
    await assistant.warm(_report().profile, timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    await _settle()

    with pytest.raises(CallTimedOut) as raised:
        await assistant.ask(_report(), timeout=0.2)
    assert raised.value.retryable is True

    hung = factory.clients[0]
    await _until(lambda: hung.disconnected)
    reply = await assistant.ask(_report(), timeout=TIMEOUT)
    assert reply.session_id == factory.options[1].session_id  # the spare was promoted
    await _until(lambda: len(factory.clients) == 3)  # and replaced
    await assistant.close(timeout=TIMEOUT)


async def test_a_client_that_died_between_asks_is_skipped_for_the_spare(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    first = await assistant.ask(_report(), timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    await _settle()

    factory.clients[0].die(ProcessError("claude exited", exit_code=1))
    await _settle()
    second = await assistant.ask(_report(), timeout=TIMEOUT)

    assert second.session_id == factory.options[1].session_id != first.session_id
    assert second.text
    await assistant.close(timeout=TIMEOUT)


async def test_a_client_that_dies_mid_ask_is_retried_once_on_the_spare(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2), _cut_after_init("assistant_report"))
    assistant = _assistant(tmp_path, factory)
    await assistant.warm(_report().profile, timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    await _settle()

    hung = factory.clients[0]
    async with asyncio.TaskGroup() as tasks:
        ask = tasks.create_task(assistant.ask(_report(), timeout=TIMEOUT))
        await _until(lambda: bool(hung.sent) and hung.replay_done.is_set())
        hung.die(ProcessError("claude exited", exit_code=1))
    reply = ask.result()

    assert reply.session_id == factory.options[1].session_id
    assert len(factory.clients[1].sent) == 1
    await assistant.close(timeout=TIMEOUT)


async def test_a_second_broken_client_is_not_retried_again(tmp_path: Path) -> None:
    cut = _cut_after_init("assistant_report")
    factory = Factory(cut, cut, cut)
    assistant = _assistant(tmp_path, factory)
    await assistant.warm(_report().profile, timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    await _settle()

    async def kill_each_when_hung() -> None:
        for index in (0, 1):
            client = factory.clients[index]

            def hung(c: FakeClient = client) -> bool:
                return bool(c.sent) and c.replay_done.is_set()

            await _until(hung, rounds=5000)
            client.die(ProcessError("claude exited", exit_code=1))

    async def ask() -> ProviderUnavailable | None:
        try:
            await assistant.ask(_report(), timeout=TIMEOUT)
        except ProviderUnavailable as error:
            return error
        return None

    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(kill_each_when_hung())
        outcome = tasks.create_task(ask())
    error = outcome.result()
    assert error is not None
    assert error.retryable is True
    await assistant.close(timeout=TIMEOUT)


async def test_a_usage_limit_maps_to_rate_limited_with_retry_after(tmp_path: Path) -> None:
    clock = FakeClock()
    factory = Factory(_asks("assistant_report", 2), _usage_limited("assistant_report"))
    assistant = _assistant(tmp_path, factory, clock=clock)
    limit = next(r for r in records("synthetic_usage_limited") if r["data"].get("type") == "rate_limit_event")
    resets_at = limit["data"]["rate_limit_info"]["resetsAt"]
    clock.set(clock.now().fromtimestamp(resets_at - 600, tz=clock.now().tzinfo))

    with pytest.raises(RateLimited) as raised:
        await assistant.ask(_report(), timeout=TIMEOUT)

    assert raised.value.retry_after == pytest.approx(timedelta(minutes=10).total_seconds())
    assert factory.clients[0].disconnected is False  # the turn ended; the client stays warm
    await assistant.close(timeout=TIMEOUT)


async def test_an_expired_login_maps_to_auth_failed_and_drops_the_client(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2), _auth_failed("assistant_report"))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(AuthFailed):
        await assistant.ask(_report(), timeout=TIMEOUT)
    await _until(lambda: factory.clients[0].disconnected)
    await assistant.close(timeout=TIMEOUT)


async def test_an_unexpected_tool_list_is_refused_and_not_retried(tmp_path: Path) -> None:
    factory = Factory(_with_init_tools(_asks("assistant_report", 1), ["Bash"]))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(ProviderUnavailable) as raised:
        await assistant.ask(_report(), timeout=TIMEOUT)
    assert raised.value.retryable is False
    await _until(lambda: factory.clients[0].disconnected)
    await assistant.close(timeout=TIMEOUT)


async def test_a_routing_session_may_list_only_structured_output(tmp_path: Path) -> None:
    factory = Factory(_with_init_tools(_asks("assistant_routing", 1), ["StructuredOutput", "WebFetch"]))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(ProviderUnavailable):
        await assistant.ask(_routing(), timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)


async def test_a_missing_cli_maps_to_not_installed(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 1), CLINotFoundError("Claude Code not found"))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(NotInstalled):
        await assistant.ask(_report(), timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)


async def test_a_spare_that_never_connects_times_the_ask_out(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 1))
    assistant = _assistant(tmp_path, factory, connect_timeout_s=0.05)

    def hanging(options: ClaudeAgentOptions) -> ClaudeSDKClient:
        client = factory(options)
        cast("FakeClient", client).connect_hangs = True
        return client

    assistant._factory = hanging
    with pytest.raises(CallTimedOut):
        await assistant.ask(_report(), timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)


async def test_a_reply_without_the_schema_value_is_a_provider_failure(tmp_path: Path) -> None:
    recording = _copy(_asks("assistant_routing", 1))
    for record in recording:
        if record["kind"] == "raw" and record["data"].get("type") == "result":
            record["data"].pop("structured_output", None)
            record["data"]["result"] = "I think it is BonviHR."
    factory = Factory(recording)
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(ProviderUnavailable) as raised:
        await assistant.ask(_routing(), timeout=TIMEOUT)
    assert raised.value.retryable is True
    await assistant.close(timeout=TIMEOUT)


async def test_unsupported_kinds_and_efforts_are_refused(tmp_path: Path) -> None:
    assistant = _assistant(tmp_path, Factory(_asks("assistant_report", 1)))
    with pytest.raises(ConfigInvalid):
        await assistant.ask(_report(kind="qa"), timeout=TIMEOUT)
    with pytest.raises(ConfigInvalid):
        await assistant.ask(_report(effort="turbo"), timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)


async def test_an_image_goes_as_a_base64_block(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG fake")
    factory = Factory(_asks("assistant_routing", 1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_routing(images=(image,)), timeout=TIMEOUT)
    content = factory.clients[0].sent[0]["message"]["content"]
    assert [block["type"] for block in content] == ["text", "image"]
    assert content[1]["source"]["media_type"] == "image/png"
    await assistant.close(timeout=TIMEOUT)


async def test_a_pdf_goes_as_a_base64_document_block_after_the_images(tmp_path: Path) -> None:
    image, pdf = tmp_path / "shot.png", tmp_path / "doc.pdf"
    image.write_bytes(b"\x89PNG fake")
    pdf.write_bytes(b"%PDF-1.4 fake")
    factory = Factory(_asks("assistant_routing", 1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_routing(images=(image,), documents=(pdf,)), timeout=TIMEOUT)
    content = factory.clients[0].sent[0]["message"]["content"]
    assert [block["type"] for block in content] == ["text", "image", "document"]
    assert content[2]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(content[2]["source"]["data"]) == pdf.read_bytes()
    await assistant.close(timeout=TIMEOUT)


async def test_a_document_that_is_no_pdf_is_refused(tmp_path: Path) -> None:
    other = tmp_path / "doc.docx"
    other.write_bytes(b"PK")
    assistant = _assistant(tmp_path, Factory(_asks("assistant_routing", 1)))
    with pytest.raises(ConfigInvalid):
        await assistant.ask(_routing(documents=(other,)), timeout=TIMEOUT)
    await assistant.close(timeout=TIMEOUT)


async def test_stream_yields_text_then_exactly_one_reply(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 1))
    assistant = _assistant(tmp_path, factory)
    items = [item async for item in assistant.stream(_report(), idle_timeout=TIMEOUT)]
    assert isinstance(items[-1], AssistantReply)
    deltas = [item for item in items[:-1] if isinstance(item, AssistantDelta)]
    assert len(deltas) == len(items) - 1
    assert "".join(delta.text for delta in deltas) == items[-1].text
    await assistant.close(timeout=TIMEOUT)


async def test_stream_times_out_on_silence(tmp_path: Path) -> None:
    factory = Factory(_cut_after_init("assistant_report"))
    assistant = _assistant(tmp_path, factory)
    with pytest.raises(CallTimedOut):
        _ = [item async for item in assistant.stream(_report(), idle_timeout=0.2)]
    await _until(lambda: factory.clients[0].disconnected)
    await assistant.close(timeout=TIMEOUT)


async def test_close_disconnects_every_client_and_refuses_later_asks(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 1))
    assistant = _assistant(tmp_path, factory)
    await assistant.ask(_report(), timeout=TIMEOUT)
    await _until(lambda: len(factory.clients) == 2)
    await assistant.close(timeout=TIMEOUT)
    assert all(client.disconnected for client in factory.clients)
    with pytest.raises(ProviderUnavailable):
        await assistant.ask(_report(), timeout=TIMEOUT)


async def test_concurrent_asks_of_one_kind_are_served_in_turn(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    replies = await asyncio.gather(
        assistant.ask(_report(), timeout=TIMEOUT), assistant.ask(_report("HRMentor?"), timeout=TIMEOUT)
    )
    assert replies[0].session_id == replies[1].session_id
    assert len(factory.clients[0].sent) == 2
    await assistant.close(timeout=TIMEOUT)


# --- pure helpers --------------------------------------------------------------------------------------------------
def test_structured_value_prefers_the_structured_output_and_falls_back_to_json_text() -> None:
    value = {"project": "BonviHR", "confidence": 0.9, "candidates": []}
    assert structured_value(value, "ignored", SCHEMA) == value
    fenced = '```json\n{"project": null, "confidence": 0.2, "candidates": ["A", "B"]}\n```'
    assert structured_value(None, fenced, SCHEMA) == {"project": None, "confidence": 0.2, "candidates": ["A", "B"]}


@pytest.mark.parametrize("raw", [None, ["a list"], "text"])
def test_structured_value_refuses_a_value_of_the_wrong_shape(raw: object) -> None:
    with pytest.raises(ProviderUnavailable):
        structured_value(raw, "not json", SCHEMA)


def test_context_tokens_reads_the_last_model_call() -> None:
    result = {
        "usage": {
            "input_tokens": 9,
            "iterations": [
                {"input_tokens": 1, "cache_read_input_tokens": 10, "output_tokens": 1},
                {
                    "input_tokens": 2,
                    "cache_creation_input_tokens": 30,
                    "cache_read_input_tokens": 1000,
                    "output_tokens": 5,
                },
            ],
        }
    }
    assert context_tokens(result) == 1037
    assert context_tokens({"usage": {"input_tokens": 3, "output_tokens": 4}}) == 7
    assert context_tokens({}) == 0


# --- live: the real CLI (haiku). `pytest -m live tests/integration/test_claude_assistant.py` ------------------------
live = pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")
LIVE_ROUTING_SYSTEM = (
    "Route the owner's message to one of their software projects. The application adds system reminders to this "
    "conversation; each turn carries the owner's current instructions and the project table, and the latest one is "
    "authoritative and replaces earlier ones. Answer only through the structured output."
)
LIVE_REPORT_SYSTEM = (
    "Answer the owner's questions about their software projects from the data given in each turn. The application "
    "adds system reminders to this conversation; each turn carries the owner's current instructions, and the latest "
    "one is authoritative and replaces earlier ones."
)
LIVE_TABLE = (
    "Current instructions from the owner: the projects are BonviHR (HR system: hiring, candidates, staff "
    "records) and HRMentor (mentoring app: mentors, matching, sessions)."
)
LIVE_MESSAGES = (
    ("nomzodlar formasi ishlamayapti", "BonviHR"),
    ("mentor matching juda sekin", "HRMentor"),
    ("xodimlar ro'yxatida xato bor", "BonviHR"),
    ("mentorlik sessiyasi jadvali buzildi", "HRMentor"),
    ("ishga olish sahifasini tuzat", "BonviHR"),
    ("mentorlar profili ochilmayapti", "HRMentor"),
)
NFR_SPD_02_S = 4.0


def _report_latency(capsys: pytest.CaptureFixture[str], label: str, samples: Sequence[float]) -> None:
    ordered = sorted(samples)
    p90 = ordered[max(0, int(len(ordered) * 0.9 + 0.5) - 1)]
    with capsys.disabled():
        sys.stderr.write(
            f"\n[latency] {label}: n={len(samples)} min={ordered[0]:.2f}s median={statistics.median(ordered):.2f}s "
            f"p90={p90:.2f}s max={ordered[-1]:.2f}s samples={[round(s, 2) for s in samples]}\n"
        )


@pytest.fixture
def live_assistant(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ClaudeAssistant:
    scrub_live_env(monkeypatch)
    return ClaudeAssistant(cli_path=LIVE_CLI, search_path=LIVE_SEARCH_PATH, sessions_dir=tmp_path / "sessions")


@pytest.mark.live
@live
async def test_live_warm_routing_asks_meet_the_latency_target(
    live_assistant: ClaudeAssistant, capsys: pytest.CaptureFixture[str]
) -> None:
    request = _routing(system_prompt=LIVE_ROUTING_SYSTEM, instructions=LIVE_TABLE)
    try:
        await live_assistant.warm(request.profile, timeout=60)
        samples: list[float] = []
        right = 0
        for prompt, project in LIVE_MESSAGES:
            started = time.monotonic()
            reply = await live_assistant.ask(
                _routing(prompt, system_prompt=LIVE_ROUTING_SYSTEM, instructions=LIVE_TABLE), timeout=30
            )
            samples.append(time.monotonic() - started)
            assert isinstance(reply.structured, dict)
            right += reply.structured.get("project") == project
            assert reply.cost_usd_micros > 0
        _report_latency(capsys, "routing warm (haiku)", samples)
        assert right >= len(LIVE_MESSAGES) - 1
        assert statistics.median(samples) <= NFR_SPD_02_S
    finally:
        await live_assistant.close(timeout=15)


@pytest.mark.live
@live
async def test_live_report_follows_the_current_instructions_on_every_ask(
    live_assistant: ClaudeAssistant, capsys: pytest.CaptureFixture[str]
) -> None:
    base = {"system_prompt": LIVE_REPORT_SYSTEM, "effort": "low"}
    try:
        await live_assistant.warm(_report(**base).profile, timeout=60)
        samples: list[float] = []
        tags: list[bool] = []
        for index, (instructions, tag) in enumerate(((UZ, "[UZ]"), (EN, "[EN]"), (UZ, "[UZ]"), (EN, "[EN]"))):
            started = time.monotonic()
            reply = await live_assistant.ask(
                _report(
                    "BonviHR qanday? Data: last commit 2 h ago, tests green.",
                    instructions=instructions,
                    prompt_version=index + 1,
                    **base,
                ),
                timeout=30,
            )
            samples.append(time.monotonic() - started)
            tags.append(reply.text.strip().endswith(tag))
        _report_latency(capsys, "report warm (haiku, effort low)", samples)
        assert sum(tags) >= 3, tags  # the S4 design: the latest per-turn block wins without a rotation
    finally:
        await live_assistant.close(timeout=15)


@pytest.mark.live
@live
async def test_live_rotation_promotes_a_warm_spare(
    live_assistant: ClaudeAssistant, capsys: pytest.CaptureFixture[str]
) -> None:
    ask = "Reply with exactly: ok"
    try:
        started = time.monotonic()
        first = await live_assistant.ask(_report(ask, instructions="", settings_version=1), timeout=60)
        cold = time.monotonic() - started
        await asyncio.sleep(8)  # let the spare connect
        started = time.monotonic()
        second = await live_assistant.ask(_report(ask, instructions="", settings_version=2), timeout=30)
        rotated = time.monotonic() - started
        _report_latency(capsys, "cold first ask (spawn + answer)", [cold])
        _report_latency(capsys, "ask right after a settings-version rotation", [rotated])
        assert second.session_id != first.session_id
        assert rotated <= NFR_SPD_02_S
    finally:
        await live_assistant.close(timeout=15)


# --- probe (the same CLI checks as the executor, via adapters/claude/cli.py) ---------------------------------------
def fake_cli(tmp_path: Path, auth: str = "auth_status_claude_ai.json", *, slow: bool = False) -> Path:
    """A shell stand-in for `claude --version` / `claude auth status`, printing the recorded probe outputs."""
    probe = FIXTURES / "probe"
    script = tmp_path / "claude"
    body = "exec /bin/sleep 5\n" if slow else ""
    script.write_text(
        f'#!/bin/sh\n{body}case "$1" in --version) cat "{probe / "version.txt"}";; auth) cat "{probe / auth}";; esac\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.parametrize(
    ("auth", "logged_in", "method"),
    [("auth_status_claude_ai.json", True, "claude.ai"), ("auth_status_logged_out.json", False, None)],
)
async def test_probe_reads_version_and_login(tmp_path: Path, auth: str, logged_in: bool, method: str | None) -> None:
    assistant = _assistant(tmp_path, Factory([]), cli_path=fake_cli(tmp_path, auth), base_env=None)
    status = await assistant.probe(timeout=PROBE_TIMEOUT)
    assert status.installed is True
    assert status.version == "2.1.310"
    assert status.logged_in is logged_in
    assert status.auth_method == method


async def test_probe_without_the_binary_reports_not_installed(tmp_path: Path) -> None:
    assistant = _assistant(tmp_path, Factory([]), cli_path=tmp_path / "missing", base_env=None)
    status = await assistant.probe(timeout=PROBE_TIMEOUT)
    assert status.installed is False
    assert status.missing == ("claude",)


async def test_a_hanging_probe_is_bounded(tmp_path: Path) -> None:
    assistant = _assistant(tmp_path, Factory([]), cli_path=fake_cli(tmp_path, slow=True), base_env=None)
    with pytest.raises(CallTimedOut):
        await assistant.probe(timeout=0.3)


async def test_a_stream_abandoned_mid_answer_discards_the_client(tmp_path: Path) -> None:
    factory = Factory(_asks("assistant_report", 2))
    assistant = _assistant(tmp_path, factory)
    stream = assistant.stream(_report(), idle_timeout=TIMEOUT)
    first = await anext(aiter(stream))
    assert isinstance(first, AssistantDelta)
    await stream.aclose()  # type: ignore[attr-defined]  # the port types it as an AsyncIterator
    await _until(lambda: factory.clients[0].disconnected)
    reply = await assistant.ask(_report(), timeout=TIMEOUT)
    assert reply.session_id == factory.options[1].session_id
    await assistant.close(timeout=TIMEOUT)
