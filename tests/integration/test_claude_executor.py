"""`ClaudeAgentRuntime` on recorded streams (CONVENTIONS §4, §11; SPEC §5.1–5.3; docs/SPIKES.md N1–N12).

Offline, nothing spawns a `claude`: the stream translator replays `tests/fixtures/claude/streams/*.jsonl` directly,
and the runtime runs on `tests/fakes/claude_client.FakeClient`, which replays the same recordings (hooks and
`can_use_tool` included). The `live` tests at the end drive the real `~/.local/bin/claude` with haiku.
"""

import asyncio
import os
import signal
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIJSONDecodeError,
    CLINotFoundError,
    ProcessError,
    ResultError,
)
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from claude_agent_sdk.types import PermissionResultAllow

from tests.fakes.claude_client import (
    FAKE_CLI_PID,
    FIXTURES,
    LIVE_CLI,
    LIVE_SEARCH_PATH,
    FakeClient,
    records,
    scrub_live_env,
)
from yordamchi.adapters.claude import control, proctree
from yordamchi.adapters.claude.executor import ClaudeAgentRuntime
from yordamchi.adapters.claude.guard import HOOK_MARGIN_S
from yordamchi.adapters.claude.options import (
    ALWAYS_DISALLOWED,
    Launch,
    build_options,
    default_cli_path,
    session_env,
)
from yordamchi.adapters.claude.stream import StreamTranslator, todo_summary, tool_summary, tools_match
from yordamchi.core import proc
from yordamchi.core.errors import CallTimedOut, ConfigInvalid, ProviderUnavailable
from yordamchi.ports.agent import (
    AgentEvent,
    AgentInput,
    GuardVerdict,
    McpHttpServer,
    Progress,
    QuestionAsked,
    RuntimeFailed,
    SessionClosed,
    SessionHandle,
    SessionReady,
    SessionSpec,
    ToolCall,
    ToolDenied,
    TurnResult,
    UsageLimited,
    UsageWindow,
)

TIMEOUT = 5.0
# A probe runs a fake `claude` script written fresh into tmp_path; macOS scans a new executable on its first run
# (XProtect), which took over 5 s while a VM loaded the Mac (2026-10-25). The bound is the harness's, not a latency
# claim: the production probe timeout is a setting.
PROBE_TIMEOUT = 30.0
SEARCH_PATH = "/opt/yordamchi/shims:/Users/owner/.local/bin:/usr/bin:/bin"
LEAKY_BASE = {
    "HOME": "/Users/owner",
    "PATH": "/somewhere/else",
    "CLAUDECODE": "1",
    "CLAUDE_CODE_SESSION_ID": "parent-session",
    "CLAUDE_CODE_MESSAGING_TOKEN": "parent-token",
    "CLAUDE_PID": "123",
    "CLAUDE_EFFORT": "max",
    "YORDAMCHI_ENV_FILE": "/secret/dev.env",
    "LANG": "en_US.UTF-8",
}


async def _allow(call: ToolCall) -> GuardVerdict:
    del call
    return GuardVerdict(allow=True)


def _spec(tmp_path: Path, **overrides: object) -> SessionSpec:
    values: dict[str, Any] = {
        "kind": "coding",
        "cwd": tmp_path,
        "model": "haiku",
        "guard": _allow,
        "setting_sources": ("user", "project", "local"),
        "permission_mode": "bypassPermissions",
    }
    values.update(overrides)
    return SessionSpec(**values)


# --- the stream translator on recorded streams ------------------------------------------------------------------
def _translate(
    name: str, *, strip_state: bool = False, expected_tools: frozenset[str] | None = None
) -> tuple[StreamTranslator, list[AgentEvent]]:
    """Replays a recording: `sent` → `sent()`, `receipt` → `stop_requested()` + `cancelled()`, `raw` → `feed()`.
    `strip_state` drops `session_state_changed`, as from a CLI that sends none (the fallback ledger)."""
    translator = StreamTranslator(session_id="fixture-session", expected_tools=expected_tools)
    events: list[AgentEvent] = []
    for record in records(name):
        kind, data = record["kind"], record["data"]
        if kind == "sent":
            translator.sent(data["uuid"])
        elif kind == "receipt":
            translator.stop_requested()
            translator.cancelled(data.get("cancelled", []))
        elif kind == "raw":
            if strip_state and data.get("subtype") == "session_state_changed":
                continue
            events.extend(translator.feed(data))
    return translator, events


def _results(events: list[AgentEvent]) -> list[TurnResult]:
    return [event for event in events if isinstance(event, TurnResult)]


def _of[T](events: list[AgentEvent], kind: type[T]) -> list[T]:
    return [event for event in events if isinstance(event, kind)]


def test_session_ready_is_emitted_once_although_init_repeats_every_turn() -> None:
    translator, events = _translate("simple")

    ready = _of(events, SessionReady)
    assert len(ready) == 1  # two inits in the recording (N3)
    assert ready[0].tools == ("Bash",)
    assert ready[0].model == "claude-haiku-5-5"
    assert ready[0].cli_version == "2.1.310"
    assert isinstance(events[0], SessionReady)
    assert "msg_lifecycle_v1" in translator.capabilities
    assert translator.supports_cancel_queued


def test_each_turn_result_carries_its_own_cost_in_micro_usd() -> None:
    _, events = _translate("simple")

    first, second = _results(events)
    assert (first.text, second.text) == ("ok", "again")
    # total_cost_usd is a session total (0.00032884, then 0.00045686): the result carries the delta.
    assert first.cost_usd_micros == 329
    assert second.cost_usd_micros == 457 - 329
    assert first.model_usage["claude-haiku-5-5"].cache_read_tokens == 1624
    assert second.model_usage["claude-haiku-5-5"].cache_read_tokens == 2092
    assert (first.interrupted, first.background) == (False, False)
    assert all(isinstance(result.cost_usd_micros, int) for result in (first, second))


def test_rate_limit_events_surface_every_window_with_utilization() -> None:
    _, events = _translate("simple")

    windows = _of(events, UsageWindow)
    assert {(w.limit_type, w.status, w.utilization) for w in windows} == {
        ("five_hour", "allowed", 0.55),
        ("seven_day", "allowed", 0.68),
    }
    assert windows[0].resets_at == datetime.fromtimestamp(1792830600, tz=UTC)
    assert _of(events, UsageLimited) == []


def test_tool_calls_become_progress_with_errors_marked() -> None:
    _, events = _translate("tools")

    progress = _of(events, Progress)
    starts = [(p.tool_name, p.summary) for p in progress if p.kind == "tool_start"]
    assert ("Bash", "echo hi > hi.txt && cat hi.txt") in starts
    assert ("Bash", "cat no-such-file.txt") in starts
    ends = [(p.tool_name, p.is_error) for p in progress if p.kind == "tool_end"]
    assert ("Bash", True) in ends
    assert ("Bash", False) in ends
    assert [p for p in progress if p.kind == "text"]
    assert len(_results(events)) == 1


@pytest.mark.parametrize("strip_state", [False, True], ids=["state_events", "fallback_ledger"])
def test_a_merged_next_follow_up_yields_one_result(strip_state: bool) -> None:
    translator, events = _translate("next_merge", strip_state=strip_state)

    results = _results(events)
    assert [r.text for r in results] == ["PINEAPPLE"]
    assert translator.queued_inputs == 0


def test_an_interrupted_turn_is_a_stopped_turn_not_a_failure() -> None:
    translator, events = _translate("interrupt")

    interrupted, after = _results(events)
    assert interrupted.interrupted is True
    assert (interrupted.subtype, interrupted.is_error, interrupted.text) == ("error_during_execution", True, "")
    assert _of(events, RuntimeFailed) == []
    assert after.text == "after"
    assert after.interrupted is False
    assert translator.queued_inputs == 0  # the receipt cancelled the queued follow-up
    assert translator.blocked_reason() is None  # the guard opens again after the stopped turn


def test_the_guard_is_closed_from_the_stop_until_the_stopped_result() -> None:
    translator = StreamTranslator(session_id="s")
    stops: list[str | None] = []
    for record in records("interrupt"):
        if record["kind"] == "sent":
            translator.sent(record["data"]["uuid"])
        elif record["kind"] == "receipt":
            assert translator.stop_requested() is True
            stops.append(translator.blocked_reason())
        elif record["kind"] == "raw" and translator.feed(record["data"]) and record["data"].get("type") == "result":
            stops.append(translator.blocked_reason())
            break
    assert stops == ["stopping", None]


def test_stopping_with_no_turn_running_keeps_the_guard_open() -> None:
    translator = StreamTranslator(session_id="s")
    assert translator.stop_requested() is False
    assert translator.blocked_reason() is None


def test_hook_denials_are_cross_checked_from_the_result() -> None:
    _, events = _translate("hook_deny")

    (result,) = _results(events)
    assert result.permission_denials == ("Bash",)
    assert any(p.kind == "tool_end" and p.is_error for p in _of(events, Progress))


@pytest.mark.parametrize("strip_state", [False, True], ids=["state_events", "fallback_ledger"])
def test_a_background_subagent_holds_the_turn_until_its_follow_up_result(strip_state: bool) -> None:
    _, events = _translate("background_agent", strip_state=strip_state)

    (result,) = _results(events)  # "launched." is held and folded into the CLI-started second turn
    assert result.text == "sub-done"
    assert result.num_turns == 3
    assert result.cost_usd_micros == 1937  # both turns: the second session total
    assert result.background is False
    texts = [p.summary for p in _of(events, Progress) if p.kind == "text"]
    assert not any("The command output was" in text for text in texts), "subagent text stays out of progress"
    assert any(p.tool_name == "Bash" and p.kind == "tool_start" for p in _of(events, Progress))


@pytest.mark.parametrize("strip_state", [False, True], ids=["state_events", "fallback_ledger"])
def test_a_background_shell_turn_is_delivered_separately_as_background(strip_state: bool) -> None:
    translator, events = _translate("background_bash", strip_state=strip_state)

    first, late = _results(events)
    assert (first.text, first.background) == ("started", False)
    assert late.background is True
    assert "finished" in late.text
    assert translator.background_task_ids() == ()


@pytest.mark.parametrize("strip_state", [False, True], ids=["state_events", "fallback_ledger"])
def test_an_empty_aborted_tools_result_is_never_delivered(strip_state: bool) -> None:
    _, events = _translate("now_redirect", strip_state=strip_state)

    results = _results(events)
    assert all(result.text for result in results)
    assert results[0].text == "REDIRECTED"
    assert results[0].interrupted is False
    assert results[-1].background is True  # the backgrounded shell finished later and woke the CLI


def test_backgrounding_is_tracked_from_task_updated_and_the_snapshot() -> None:
    translator = StreamTranslator(session_id="s")
    for record in records("now_redirect"):
        if record["kind"] != "raw":
            continue
        translator.feed(record["data"])
        if record["data"].get("subtype") == "task_updated":
            break
    assert translator.background_task_ids() == ("b591hiocd",)


def test_an_empty_aborted_result_with_nothing_after_it_is_a_stopped_turn() -> None:
    translator = StreamTranslator(session_id="s")
    translator.sent("u1")
    translator.feed({"type": "system", "subtype": "session_state_changed", "state": "running"})
    translator.feed({"type": "result", "subtype": "success", "terminal_reason": "aborted_tools", "result": ""})
    (result,) = translator.feed({"type": "system", "subtype": "session_state_changed", "state": "idle"})

    assert isinstance(result, TurnResult)
    assert result.interrupted is True


def test_an_idle_that_arrives_before_the_result_still_ends_the_turn() -> None:
    translator = StreamTranslator(session_id="s")
    translator.sent("u1")
    translator.feed({"type": "system", "subtype": "session_state_changed", "state": "running"})
    assert translator.feed({"type": "system", "subtype": "session_state_changed", "state": "idle"}) == []
    events = translator.feed({"type": "result", "subtype": "success", "result": "ok", "total_cost_usd": 0.001})

    assert [type(event) for event in events] == [TurnResult]


def test_restarted_counters_count_from_zero() -> None:
    translator = StreamTranslator(session_id="s")
    translator.feed({"type": "result", "result": "a", "total_cost_usd": 0.5})
    (second,) = translator.feed({"type": "result", "result": "b", "total_cost_usd": 0.2})
    assert isinstance(second, TurnResult)
    assert second.cost_usd_micros == 200_000


def test_a_model_change_updates_the_session_without_a_second_ready() -> None:
    translator, events = _translate("set_model")

    assert len(_of(events, SessionReady)) == 1
    assert len(_results(events)) == 2
    assert translator.model == "claude-sonnet-5-5"


def test_a_rejected_rate_limit_is_usage_limited_once_per_turn() -> None:
    _, events = _translate("synthetic_usage_limited")

    limited = _of(events, UsageLimited)
    assert limited == [UsageLimited(resets_at=datetime.fromtimestamp(1792830600, tz=UTC), limit_type="five_hour")]
    assert any(w.status == "rejected" and w.utilization == 1.0 for w in _of(events, UsageWindow))
    (result,) = _results(events)
    assert result.is_error is True


def test_an_expired_login_is_auth_failed() -> None:
    _, events = _translate("synthetic_auth_failed")

    assert RuntimeFailed(code="auth_failed", retryable=False) in events


def test_todos_and_a_conversation_reset() -> None:
    _, events = _translate("synthetic_todo")

    todo = [p for p in _of(events, Progress) if p.kind == "todo"]
    assert todo == [Progress(kind="todo", tool_name="TodoWrite", summary="1/3 · Running the tests")]
    assert _of(events, SessionReady)[0].mcp_servers == ("github",)
    (result,) = _results(events)
    assert result.cost_usd_micros == 12_500
    assert result.model_usage["claude-opus-5-5"].cache_write_tokens == 400


def test_a_tool_list_that_differs_from_the_allowlist_refuses_the_session() -> None:
    translator = StreamTranslator(session_id="s", expected_tools=frozenset({"Read"}))
    assert translator.blocked_reason() == "tools_unverified"
    events: list[AgentEvent] = []
    for message in [r["data"] for r in records("simple") if r["kind"] == "raw"][:6]:
        events.extend(translator.feed(message))

    assert RuntimeFailed(code="protocol", retryable=False) in events
    assert _of(events, SessionReady) == []
    assert translator.blocked_reason() == "tools_mismatch"


def test_the_allowlist_tolerates_harness_tools_only() -> None:
    assert tools_match(frozenset({"WebSearch"}), ["WebSearch", "ToolSearch"])
    assert not tools_match(frozenset({"WebSearch"}), ["WebSearch", "Bash"])
    assert not tools_match(frozenset({"WebSearch", "WebFetch"}), ["WebSearch"])
    translator, events = _translate("simple", expected_tools=frozenset({"Bash"}))
    assert _of(events, SessionReady)
    assert translator.blocked_reason() is None


def test_summaries_never_fail_on_odd_input() -> None:
    assert tool_summary("Bash", None) == ""
    assert tool_summary("Bash", {"command": "  \n"}) == ""
    assert tool_summary("Bash", {"command": "make test\nmake lint"}) == "make test"
    assert tool_summary("Unknown", {"x": 1}) == ""
    assert len(tool_summary("Bash", {"command": "x" * 500})) == 200
    assert todo_summary({"todos": "nope"}) == ""
    assert todo_summary({"todos": [{"status": "completed"}]}) == "1/1"


def test_unknown_messages_are_ignored() -> None:
    translator = StreamTranslator(session_id="s")
    assert translator.feed({"type": "stream_event"}) == []
    assert translator.feed({"type": "system", "subtype": "thinking_tokens"}) == []
    assert (
        translator.feed({"type": "user", "message": {"content": "<local-command-stdout>x</local-command-stdout>"}})
        == []
    )
    assert translator.feed({"type": "rate_limit_event", "rate_limit_info": "bad"}) == []


# --- options (SPEC §5.3) ----------------------------------------------------------------------------------------
def _options(tmp_path: Path, spec: SessionSpec, *, resume: bool = False) -> ClaudeAgentOptions:
    env = session_env(spec, search_path=SEARCH_PATH, githooks_dir=Path("/rel/githooks"), base=LEAKY_BASE)
    return build_options(
        spec,
        cli_path=default_cli_path(Path("/Users/owner")),
        env=env,
        launch=Launch(session_id="sid-1", resume=resume),
        hooks={},
        can_use_tool=_allow,
    )


def test_the_session_spawns_the_installed_cli_with_a_clean_environment(tmp_path: Path) -> None:
    spec = _spec(tmp_path, env={"YORDAMCHI_SESSION_TOKEN": "tok", "ENABLE_CLAUDEAI_MCP_SERVERS": "true"})
    options = _options(tmp_path, spec)

    assert options.cli_path == "/Users/owner/.local/bin/claude"
    env = options.env
    assert env["PATH"] == SEARCH_PATH  # shims first
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"  # enforced over the spec (NFR-SEC-07)
    assert env["CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"] == "1"
    assert (env["GIT_CONFIG_COUNT"], env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == (
        "1",
        "core.hooksPath",
        "/rel/githooks",
    )
    assert (env["GIT_TERMINAL_PROMPT"], env["GH_PROMPT_DISABLED"]) == ("0", "1")
    assert env["YORDAMCHI_SESSION_TOKEN"] == "tok"
    for leaked in (
        "CLAUDECODE",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
    ):
        assert leaked not in env
    assert "YORDAMCHI_ENV_FILE" not in env
    assert env["LANG"] == "en_US.UTF-8"


def test_a_credential_variable_in_the_spec_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="credential"):
        _options(tmp_path, _spec(tmp_path, env={"ANTHROPIC_BASE_URL": "http://proxy"}))


@pytest.mark.parametrize("flag", ["bare", "dangerously-skip-permissions"])
def test_refused_cli_flags_never_reach_argv(tmp_path: Path, flag: str) -> None:
    with pytest.raises(ConfigInvalid):
        _options(tmp_path, _spec(tmp_path, extra_args={flag: None}))


@pytest.mark.parametrize("kind", ["coding", "investigation", "smoke"])
def test_argv_is_explicit_about_settings_and_disallows_peer_messaging(tmp_path: Path, kind: str) -> None:
    sources = () if kind == "smoke" else ("user", "project", "local")
    spec = _spec(tmp_path, kind=kind, setting_sources=sources, disallowed_tools=("Bash(launchctl * uz.yordamchi*)",))
    argv = SubprocessCLITransport(prompt="", options=_options(tmp_path, spec))._build_command()

    assert argv[0] == "/Users/owner/.local/bin/claude"
    assert f"--setting-sources={','.join(sources)}" in argv  # never the SDK's version-dependent default
    disallowed = argv[argv.index("--disallowedTools") + 1].split(",")
    assert disallowed == ["Bash(launchctl * uz.yordamchi*)", *ALWAYS_DISALLOWED]
    assert "--bare" not in argv
    assert "--session-id=sid-1" in argv
    assert not any(part.startswith("--resume") for part in argv)


def test_a_resume_passes_the_id_and_no_new_one(tmp_path: Path) -> None:
    options = _options(tmp_path, _spec(tmp_path), resume=True)
    assert (options.resume, options.session_id) == ("sid-1", None)


def test_the_system_prompt_keeps_the_preset_and_appends_only_fixed_parts(tmp_path: Path) -> None:
    options = _options(tmp_path, _spec(tmp_path, system_append="FIXED", effort="xhigh", max_turns=40))
    assert options.system_prompt == {"type": "preset", "preset": "claude_code", "append": "FIXED"}
    assert (options.effort, options.max_turns) == ("xhigh", 40)
    plain = _options(tmp_path, _spec(tmp_path))
    assert plain.system_prompt == {"type": "preset", "preset": "claude_code"}


def test_http_mcp_servers_carry_a_headers_helper_never_a_token(tmp_path: Path) -> None:
    servers = {"linear": McpHttpServer(url="https://mcp.example/linear", headers_helper="/rel/bin/mcp-header linear")}
    options = _options(tmp_path, _spec(tmp_path, mcp_servers=servers))
    assert options.mcp_servers == {
        "linear": {"type": "http", "url": "https://mcp.example/linear", "headersHelper": "/rel/bin/mcp-header linear"}
    }
    with pytest.raises(ConfigInvalid):
        _options(tmp_path, _spec(tmp_path, sdk_servers=("nope",)))


# --- the private-API seam (control.py) --------------------------------------------------------------------------
def _client(*, control_api: bool = True, private_api: bool = True) -> FakeClient:
    return FakeClient(ClaudeAgentOptions(), control_api=control_api, private_api=private_api)


def _sdk(client: FakeClient) -> ClaudeSDKClient:
    return cast("ClaudeSDKClient", client)


async def test_interrupt_returns_the_receipt_through_the_private_control_request() -> None:
    client = _client()
    await control.connect(_sdk(client))
    client.receipt = {"still_queued": [], "cancelled": ["u-2"]}

    receipt = await control.interrupt(_sdk(client), cancel_queued=True, timeout=1)

    assert receipt == control.InterruptReceipt(still_queued=(), cancelled=("u-2",), has_receipt=True)
    assert client._query.control_requests == [{"subtype": "interrupt", "cancel_queued": True}]
    await client.disconnect()


async def test_interrupt_falls_back_to_the_public_call_when_the_private_one_is_gone() -> None:
    client = _client(control_api=False)
    await control.connect(_sdk(client))

    receipt = await control.interrupt(_sdk(client), cancel_queued=True, timeout=1)

    assert receipt.has_receipt is False
    assert client.public_interrupts == 1
    await client.disconnect()


async def test_a_refused_control_request_is_a_named_error() -> None:
    client = _client()
    await control.connect(_sdk(client))
    client.control_error = Exception("Control request timeout: interrupt")
    with pytest.raises(control.ControlFailed):
        await control.interrupt(_sdk(client), cancel_queued=False, timeout=1)
    with pytest.raises(control.ControlFailed):
        await control.set_model(_sdk(client), "sonnet")
    await client.disconnect()


async def test_the_raw_stream_needs_the_private_query() -> None:
    client = _client(private_api=False)
    await control.connect(_sdk(client))
    with pytest.raises(control.PrivateApiMissing):
        async for _ in control.raw_messages(_sdk(client)):
            pass


async def test_an_untyped_stream_error_becomes_stream_failed() -> None:
    client = _client()
    await control.connect(_sdk(client))
    client.die(Exception("Unknown error"))
    with pytest.raises(control.StreamFailed):
        async for _ in control.raw_messages(_sdk(client)):
            pass


def test_the_cli_pid_comes_from_the_initialize_response() -> None:
    client = _client()
    assert control.cli_pid(_sdk(client)) is None
    client._query = type("Q", (), {"_initialization_result": {"pid": 4242}})()
    assert control.cli_pid(_sdk(client)) == 4242


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (CLINotFoundError("Claude Code not found"), ("cli_missing", False)),
        (
            ResultError("Claude Code returned an error result: No conversation found with session ID: x"),
            ("session_not_found", False),
        ),
        (ResultError("auth", data={"api_error_status": 401}), ("auth_failed", False)),
        (CLIJSONDecodeError("{", ValueError("bad")), ("protocol", False)),
        (control.PrivateApiMissing("x"), ("protocol", False)),
        (ProcessError("Command failed", exit_code=-9), ("process_died", True)),
        (OSError("pipe"), ("process_died", True)),
    ],
)
def test_failures_map_to_runtime_failed_codes(error: BaseException, expected: tuple[str, bool]) -> None:
    assert control.failure(error) == expected


# --- the interrupt backstop's kill list (proctree.py) ----------------------------------------------------------
BOT_PGID = 500
REAL_LIST_PROCESSES = proctree.list_processes  # the autouse fixture below replaces both for every other test
REAL_SEND = proctree.send
ROWS = [
    proctree.ProcRow(pid=500, ppid=1, pgid=500),  # the bot
    proctree.ProcRow(pid=600, ppid=500, pgid=500),  # the CLI, in the bot's group (no setsid in the SDK)
    proctree.ProcRow(pid=700, ppid=600, pgid=700),  # a Bash tool's shell, leading its own group
    proctree.ProcRow(pid=701, ppid=700, pgid=700),  # its python
    proctree.ProcRow(pid=800, ppid=600, pgid=500),  # an MCP stdio server left in the bot's group
    proctree.ProcRow(pid=900, ppid=600, pgid=950),  # joined a foreign group: killed by pid only
    proctree.ProcRow(pid=950, ppid=1, pgid=950),  # that foreign group's leader, not ours
]


def test_the_kill_plan_never_targets_the_bots_own_group() -> None:
    plan = proctree.kill_plan(ROWS, root=600, own_pgid=BOT_PGID, own_pid=500)

    assert plan.groups == (700,)
    assert BOT_PGID not in plan.groups
    assert set(plan.pids) == {600, 800, 900}
    assert 500 not in plan.pids


def test_ps_output_parses_and_a_missing_root_plans_only_the_root() -> None:
    rows = proctree.parse_ps("  600   500   500\nPID PPID PGID\n  700 600 700\n")
    assert rows == [proctree.ProcRow(600, 500, 500), proctree.ProcRow(700, 600, 700)]
    assert proctree.kill_plan([], root=600, own_pgid=BOT_PGID, own_pid=500) == proctree.KillPlan((), (600,))


def test_send_skips_the_own_group_even_if_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, int, int]] = []
    monkeypatch.setattr("yordamchi.adapters.claude.proctree.os.killpg", lambda g, s: calls.append(("killpg", g, s)))
    monkeypatch.setattr("yordamchi.adapters.claude.proctree.os.kill", lambda p, s: calls.append(("kill", p, s)))

    REAL_SEND(proctree.KillPlan(groups=(BOT_PGID, 700), pids=(600,)), signal.SIGTERM, own_pgid=BOT_PGID)

    assert calls == [("killpg", 700, signal.SIGTERM), ("kill", 600, signal.SIGTERM)]


async def test_the_backstop_kills_a_real_tool_group_and_the_caller_survives() -> None:
    env = proc.clean_env(path="/usr/bin:/bin")
    child = asyncio.get_running_loop().create_task(proc.run(["/bin/sleep", "30"], env=env, timeout=20))
    try:
        sleeper: proctree.ProcRow | None = None
        rows: list[proctree.ProcRow] = []
        previous: set[proctree.ProcRow] = set()
        for _ in range(200):
            rows = await REAL_LIST_PROCESSES(env=env, timeout=2)
            # Our own group leaders in two snapshots in a row: the sleeper (each `ps` is a new pid).
            leaders = {r for r in proctree.descendants(rows, os.getpid()) if r.pgid == r.pid != os.getpgrp()}
            sleeper = next(iter(leaders & previous), None)
            if sleeper is not None:
                break
            previous = leaders
            await asyncio.sleep(0.01)
        assert sleeper is not None, "the child never showed up in ps"
        plan = proctree.kill_plan(rows, root=os.getpid(), own_pgid=os.getpgrp(), own_pid=os.getpid())
        assert os.getpgrp() not in plan.groups
        assert os.getpid() not in plan.pids
        REAL_SEND(proctree.KillPlan(groups=(sleeper.pgid,), pids=()), signal.SIGTERM, own_pgid=os.getpgrp())
        result = await asyncio.wait_for(child, 5)
        assert result.returncode == -signal.SIGTERM
    finally:
        child.cancel()


# --- the runtime on the replaying client -----------------------------------------------------------------------
@pytest.fixture(autouse=True)
def no_real_signals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[proctree.KillPlan, signal.Signals]]:
    """No test may signal a real process: the backstop's targets are recorded instead."""
    sent: list[tuple[proctree.KillPlan, signal.Signals]] = []

    def record(plan: proctree.KillPlan, sig: signal.Signals, *, own_pgid: int) -> int:
        assert own_pgid not in plan.groups
        sent.append((plan, sig))
        return len(plan.groups) + len(plan.pids)

    async def no_ps(*, env: Mapping[str, str], timeout: float) -> list[proctree.ProcRow]:
        del env, timeout
        return []

    monkeypatch.setattr(proctree, "send", record)
    monkeypatch.setattr(proctree, "list_processes", no_ps)
    return sent


class Harness:
    def __init__(self, recording: str | None = None, **client_kwargs: object) -> None:
        self.recording = records(recording) if recording else []
        self.client_kwargs: dict[str, Any] = dict(client_kwargs)
        self.clients: list[FakeClient] = []
        self.runtime = ClaudeAgentRuntime(
            cli_path=Path("/Users/owner/.local/bin/claude"),
            search_path=SEARCH_PATH,
            base_env=LEAKY_BASE,
            client_factory=self._make,
            kill_grace_s=0.05,
        )

    def _make(self, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        client = FakeClient(options, self.recording, **self.client_kwargs)
        self.clients.append(client)
        return cast("ClaudeSDKClient", client)

    @property
    def client(self) -> FakeClient:
        return self.clients[-1]

    async def until(
        self, h: SessionHandle, stop: Callable[[AgentEvent], bool], *, idle: float = TIMEOUT
    ) -> list[AgentEvent]:
        seen: list[AgentEvent] = []
        async for event in self.runtime.events(h, idle_timeout=idle):
            seen.append(event)
            if stop(event):
                break
        return seen


def _is(kind: type) -> Callable[[AgentEvent], bool]:
    return lambda event: isinstance(event, kind)


def _tool_start(event: AgentEvent) -> bool:
    return isinstance(event, Progress) and event.kind == "tool_start"


async def test_a_turn_runs_through_the_runtime_from_ready_to_result(tmp_path: Path) -> None:
    harness = Harness("simple")
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)

    await harness.runtime.send(h, AgentInput(text="Reply with exactly the word: ok"), mode="new_turn", timeout=TIMEOUT)
    events = await harness.until(h, _is(TurnResult))

    assert isinstance(events[0], SessionReady)
    assert isinstance(events[-1], TurnResult)
    assert events[-1].text == "ok"
    sent = harness.client.sent[0]
    assert sent["priority"] == "later"  # new_turn: a separate turn, never "now"
    assert sent["uuid"]
    assert sent["message"] == {"role": "user", "content": "Reply with exactly the word: ok"}
    await harness.runtime.close(h, timeout=TIMEOUT)
    assert isinstance((await harness.until(h, _is(SessionClosed)))[-1], SessionClosed)
    assert harness.client.disconnected


async def test_a_follow_up_goes_out_with_priority_next_and_merges(tmp_path: Path) -> None:
    harness = Harness("next_merge")
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    await harness.runtime.send(h, AgentInput(text="run the sleep"), mode="new_turn", timeout=TIMEOUT)
    await harness.until(h, _tool_start)

    await harness.runtime.send(h, AgentInput(text="answer PINEAPPLE"), mode="next", timeout=TIMEOUT)
    events = await harness.until(h, _is(TurnResult))

    assert [m["priority"] for m in harness.client.sent] == ["later", "next"]
    assert [e.text for e in _results(events)] == ["PINEAPPLE"]
    await harness.runtime.close(h, timeout=TIMEOUT)


async def test_interrupt_cancels_the_queue_waits_for_the_stopped_result_and_reopens(tmp_path: Path) -> None:
    harness = Harness("interrupt")
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    await harness.runtime.send(h, AgentInput(text="long command"), mode="new_turn", timeout=TIMEOUT)
    await harness.until(h, _tool_start)
    await harness.runtime.send(h, AgentInput(text="Say QUEUED-RAN."), mode="next", timeout=TIMEOUT)

    await harness.runtime.interrupt(h, timeout=2.0)
    events = await harness.until(h, _is(TurnResult))

    assert harness.client._query.control_requests == [{"subtype": "interrupt", "cancel_queued": True}]
    (stopped,) = _results(events)
    assert stopped.interrupted is True
    assert _of(events, RuntimeFailed) == []
    await harness.runtime.send(h, AgentInput(text="after"), mode="new_turn", timeout=TIMEOUT)
    after = await harness.until(h, _is(TurnResult))
    assert _results(after)[0].text == "after"
    await harness.runtime.close(h, timeout=TIMEOUT)


async def test_interrupt_stops_background_tasks_too(tmp_path: Path) -> None:
    harness = Harness("background_bash")
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    await harness.runtime.send(h, AgentInput(text="start it"), mode="new_turn", timeout=TIMEOUT)
    first = await harness.until(h, _is(TurnResult))
    assert _results(first)[0].text == "started"

    await harness.runtime.interrupt(h, timeout=1.0)

    assert harness.client.stopped_tasks == ["b43ajs9vy"]
    await harness.runtime.close(h, timeout=TIMEOUT)


async def test_interrupt_without_a_result_runs_the_backstop_and_the_session_dies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_real_signals: list[tuple[proctree.KillPlan, signal.Signals]],
) -> None:
    tree = [
        proctree.ProcRow(pid=FAKE_CLI_PID, ppid=os.getpid(), pgid=os.getpgrp()),
        proctree.ProcRow(pid=FAKE_CLI_PID + 1, ppid=FAKE_CLI_PID, pgid=FAKE_CLI_PID + 1),
    ]

    async def ps(*, env: Mapping[str, str], timeout: float) -> list[proctree.ProcRow]:
        del env, timeout
        return tree

    monkeypatch.setattr(proctree, "list_processes", ps)
    wedged = [r for r in records("interrupt") if r["kind"] != "receipt"][:12]  # up to the tool call, no result
    harness = Harness()
    harness.recording = wedged
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    await harness.runtime.send(h, AgentInput(text="long command"), mode="new_turn", timeout=TIMEOUT)
    await harness.until(h, _tool_start)

    await harness.runtime.interrupt(h, timeout=0.2)

    assert [sig for _, sig in no_real_signals] == [signal.SIGTERM, signal.SIGKILL]
    plan = no_real_signals[0][0]
    assert plan.groups == (FAKE_CLI_PID + 1,)
    assert plan.pids == (FAKE_CLI_PID,)
    harness.client.die(ProcessError("Command failed", exit_code=-15))
    events = await harness.until(h, _is(SessionClosed))
    assert RuntimeFailed(code="process_died", retryable=True) in events
    assert events[-1] == SessionClosed(reason="process_died")


async def test_a_missing_transcript_fails_the_resume(tmp_path: Path) -> None:
    error = ResultError("Claude Code returned an error result: No conversation found with session ID: gone")
    harness = Harness(connect_error=error)

    h = await harness.runtime.open(_spec(tmp_path), resume_id="gone", timeout=TIMEOUT)

    assert h.session_id == "gone"
    assert harness.client.options.resume == "gone"
    events = await harness.until(h, _is(SessionClosed))
    assert events == [RuntimeFailed(code="session_not_found", retryable=False), SessionClosed("session_not_found")]
    with pytest.raises(ProviderUnavailable):
        await harness.runtime.send(h, AgentInput(text="x"), mode="new_turn", timeout=TIMEOUT)


async def test_a_missing_cli_is_cli_missing(tmp_path: Path) -> None:
    harness = Harness(connect_error=CLINotFoundError("Claude Code not found at: /x"))
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    assert (await harness.until(h, _is(SessionClosed)))[0] == RuntimeFailed(code="cli_missing", retryable=False)


async def test_a_connect_that_hangs_is_bounded(tmp_path: Path) -> None:
    harness = Harness(connect_hangs=True)
    with pytest.raises(CallTimedOut):
        await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=0.05)
    assert harness.client.disconnected


async def test_a_dying_process_is_process_died_then_closed(tmp_path: Path) -> None:
    harness = Harness()
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    harness.client.die(ProcessError("Command failed", exit_code=1))

    events = await harness.until(h, _is(SessionClosed))

    assert events == [RuntimeFailed(code="process_died", retryable=True), SessionClosed(reason="process_died")]


async def test_a_vanished_private_stream_is_a_protocol_failure(tmp_path: Path) -> None:
    harness = Harness(private_api=False)
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    events = await harness.until(h, _is(SessionClosed))
    assert events[0] == RuntimeFailed(code="protocol", retryable=False)


async def test_a_wrong_tool_list_refuses_and_closes_the_session(tmp_path: Path) -> None:
    harness = Harness("simple")
    h = await harness.runtime.open(
        _spec(tmp_path, expected_tools=frozenset({"WebSearch"})), resume_id=None, timeout=TIMEOUT
    )
    await harness.runtime.send(h, AgentInput(text="x"), mode="new_turn", timeout=TIMEOUT)

    events = await harness.until(h, _is(SessionClosed))

    assert RuntimeFailed(code="protocol", retryable=False) in events
    assert events[-1] == SessionClosed(reason="tools_mismatch")
    for _ in range(100):
        if harness.client.disconnected:
            break
        await asyncio.sleep(0.01)
    assert harness.client.disconnected


async def test_a_question_round_trips_through_answer(tmp_path: Path) -> None:
    harness = Harness("question")
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    await harness.runtime.send(h, AgentInput(text="ask me"), mode="new_turn", timeout=TIMEOUT)
    asked = (await harness.until(h, _is(QuestionAsked)))[-1]
    assert isinstance(asked, QuestionAsked)
    assert asked.question_id == "toolu_014LZpSQNFaBDfVtgmu2vmbF"
    assert asked.questions[0]["question"] == "Which color do you prefer?"

    answered = await harness.runtime.answer(h, asked.question_id, {"Which color do you prefer?": "Blue"}, timeout=1)
    events = await harness.until(h, _is(TurnResult))

    assert answered is True
    (result,) = harness.client.permission_results
    assert isinstance(result, PermissionResultAllow)
    assert result.updated_input is not None
    assert result.updated_input["answers"] == {"Which color do you prefer?": "Blue"}
    assert result.updated_input["questions"][0]["header"] == "Color"
    assert _results(events)[0].text == "Blue"
    assert await harness.runtime.answer(h, asked.question_id, {"x": "y"}, timeout=1) is False
    await harness.runtime.close(h, timeout=TIMEOUT)


async def test_the_hook_emits_tool_denied_the_moment_it_denies(tmp_path: Path) -> None:
    async def deny_marker(call: ToolCall) -> GuardVerdict:
        if "7731" in str(call.tool_input.get("command")):
            return GuardVerdict(allow=False, message="BLOCKED: gate", reason_code="forbidden")
        return GuardVerdict(allow=True)

    harness = Harness("hook_deny")
    h = await harness.runtime.open(_spec(tmp_path, guard=deny_marker), resume_id=None, timeout=TIMEOUT)
    await harness.runtime.send(h, AgentInput(text="x"), mode="new_turn", timeout=TIMEOUT)
    events = await harness.until(h, _is(TurnResult))

    assert ToolDenied(tool_name="Bash", reason_code="forbidden") in events
    denies = [o for o in harness.client.hook_outputs if o.get("hookSpecificOutput", {}).get("permissionDecision")]
    assert denies[0]["hookSpecificOutput"]["permissionDecisionReason"] == "BLOCKED: gate"
    await harness.runtime.close(h, timeout=TIMEOUT)


async def test_the_session_hooks_carry_the_guard_and_the_turn_context(tmp_path: Path) -> None:
    harness = Harness()
    spec = _spec(tmp_path, hook_timeout_s=7.0, context_provider=lambda: "Current instructions from the owner: x")
    h = await harness.runtime.open(spec, resume_id=None, timeout=TIMEOUT)

    hooks = harness.client.options.hooks or {}
    assert hooks["PreToolUse"][0].timeout == 7.0 + HOOK_MARGIN_S
    assert hooks["PreToolUse"][0].matcher is None
    assert hooks["UserPromptSubmit"][0].timeout == 10
    assert harness.client.options.can_use_tool is not None
    await harness.runtime.close(h, timeout=TIMEOUT)


async def test_set_model_close_and_errors(tmp_path: Path) -> None:
    harness = Harness()
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)

    await harness.runtime.set_model(h, "sonnet", timeout=TIMEOUT)
    assert harness.client.models == ["sonnet"]
    harness.client.control_error = Exception("refused")
    with pytest.raises(ProviderUnavailable):
        await harness.runtime.set_model(h, "opus", timeout=TIMEOUT)

    await harness.runtime.close(h, timeout=TIMEOUT)
    assert (await harness.until(h, _is(SessionClosed))) == [SessionClosed(reason="closed")]
    with pytest.raises(ProviderUnavailable):
        await harness.runtime.send(h, AgentInput(text="x"), mode="new_turn", timeout=TIMEOUT)
    await harness.runtime.close(h, timeout=TIMEOUT)  # twice is fine


async def test_events_time_out_when_the_session_is_silent(tmp_path: Path) -> None:
    harness = Harness()
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)
    with pytest.raises(CallTimedOut):
        await harness.until(h, _is(SessionClosed), idle=0.05)
    await harness.runtime.close(h, timeout=TIMEOUT)


async def test_images_go_out_as_base64_blocks(tmp_path: Path) -> None:
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG fake")
    harness = Harness()
    h = await harness.runtime.open(_spec(tmp_path), resume_id=None, timeout=TIMEOUT)

    await harness.runtime.send(h, AgentInput(text="look", images=(image,)), mode="new_turn", timeout=TIMEOUT)

    content = harness.client.sent[0]["message"]["content"]
    assert content[0] == {"type": "text", "text": "look"}
    assert content[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBORyBmYWtl"}
    with pytest.raises(ConfigInvalid):
        await harness.runtime.send(
            h, AgentInput(text="x", images=(tmp_path / "a.bmp",)), mode="new_turn", timeout=TIMEOUT
        )
    await harness.runtime.close(h, timeout=TIMEOUT)


# --- probe ----------------------------------------------------------------------------------------------------
def _fake_cli(tmp_path: Path, auth: str, *, slow: bool = False) -> Path:
    script = tmp_path / "claude"
    probe = FIXTURES / "probe"
    body = "exec /bin/sleep 5\n" if slow else ""
    script.write_text(
        f'#!/bin/sh\n{body}case "$1" in --version) cat "{probe / "version.txt"}";; auth) cat "{probe / auth}";; esac\n',
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


@pytest.mark.parametrize(
    ("auth", "logged_in", "method", "missing"),
    [
        ("auth_status_claude_ai.json", True, "claude.ai", ()),
        ("auth_status_api_key.json", True, "api_key", ()),
        ("auth_status_logged_out.json", False, None, ("login",)),
    ],
)
async def test_probe_reads_version_and_login(
    tmp_path: Path, auth: str, logged_in: bool, method: str | None, missing: tuple[str, ...]
) -> None:
    runtime = ClaudeAgentRuntime(cli_path=_fake_cli(tmp_path, auth), search_path="/usr/bin:/bin")

    status = await runtime.probe(timeout=PROBE_TIMEOUT)

    assert (status.installed, status.version) == (True, "2.1.310")
    assert (status.logged_in, status.auth_method, status.missing) == (logged_in, method, missing)


async def test_probe_without_the_binary_reports_not_installed(tmp_path: Path) -> None:
    runtime = ClaudeAgentRuntime(cli_path=tmp_path / "missing", search_path="/usr/bin:/bin")
    status = await runtime.probe(timeout=PROBE_TIMEOUT)
    assert (status.installed, status.missing) == (False, ("claude",))


async def test_a_hanging_probe_is_bounded(tmp_path: Path) -> None:
    runtime = ClaudeAgentRuntime(
        cli_path=_fake_cli(tmp_path, "auth_status_claude_ai.json", slow=True), search_path="/usr/bin:/bin"
    )
    with pytest.raises(CallTimedOut):
        await runtime.probe(timeout=0.3)


# --- live: the real CLI (haiku). `pytest -m live tests/integration/test_claude_executor.py` --------------------
live = pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")
PY_SLEEP = "python3 -c 'import time; time.sleep({n}); print(\"{tag}\")'"


@pytest.fixture
def live_runtime(monkeypatch: pytest.MonkeyPatch) -> ClaudeAgentRuntime:
    scrub_live_env(monkeypatch)
    return ClaudeAgentRuntime(cli_path=LIVE_CLI, search_path=LIVE_SEARCH_PATH)


def _live_spec(tmp_path: Path, **overrides: object) -> SessionSpec:
    return _spec(tmp_path, kind="smoke", setting_sources=(), tools=("Bash",), **overrides)


async def _live_until(
    runtime: ClaudeAgentRuntime, h: SessionHandle, stop: Callable[[AgentEvent], bool], *, idle: float = 120
) -> list[AgentEvent]:
    seen: list[AgentEvent] = []
    async for event in runtime.events(h, idle_timeout=idle):
        seen.append(event)
        if stop(event):
            break
    return seen


@pytest.mark.live
@live
async def test_live_a_next_follow_up_merges_into_one_result(live_runtime: ClaudeAgentRuntime, tmp_path: Path) -> None:
    h = await live_runtime.open(_live_spec(tmp_path), resume_id=None, timeout=60)
    try:
        command = PY_SLEEP.format(n=6, tag="first-done")
        await live_runtime.send(
            h,
            AgentInput(text=f"Run exactly this bash command: `{command}`, then report it."),
            mode="new_turn",
            timeout=10,
        )
        await _live_until(live_runtime, h, _tool_start)
        await live_runtime.send(
            h,
            AgentInput(text="Change: once the command finishes, your whole final answer must be the word PINEAPPLE."),
            mode="next",
            timeout=10,
        )
        events = await _live_until(live_runtime, h, _is(TurnResult))
        (result,) = _results(events)
        assert "PINEAPPLE" in result.text
        assert result.cost_usd_micros > 0
        # nothing else is owed: no second result arrives
        with pytest.raises(CallTimedOut):
            await _live_until(live_runtime, h, _is(TurnResult), idle=4)
    finally:
        await live_runtime.close(h, timeout=15)


@pytest.mark.live
@live
async def test_live_interrupt_stops_bash_without_the_backstop(
    live_runtime: ClaudeAgentRuntime, tmp_path: Path, no_real_signals: list[Any]
) -> None:
    h = await live_runtime.open(_live_spec(tmp_path), resume_id=None, timeout=60)
    try:
        command = PY_SLEEP.format(n=30, tag="late")
        await live_runtime.send(
            h,
            AgentInput(text=f"Run exactly this bash command in the foreground: `{command}`"),
            mode="new_turn",
            timeout=10,
        )
        await _live_until(live_runtime, h, _tool_start)
        await asyncio.sleep(1.5)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await live_runtime.interrupt(h, timeout=2.0)
        assert loop.time() - started < 2.0
        events = await _live_until(live_runtime, h, _is(TurnResult), idle=10)
        assert _results(events)[0].interrupted is True
        assert no_real_signals == [], "the CLI stopped the command; the backstop never ran"
        await live_runtime.send(h, AgentInput(text="Reply with exactly the word: after"), mode="new_turn", timeout=10)
        after = await _live_until(live_runtime, h, _is(TurnResult))
        assert _results(after)[0].interrupted is False
    finally:
        await live_runtime.close(h, timeout=15)


@pytest.mark.live
@live
async def test_live_a_resumed_session_keeps_its_id(live_runtime: ClaudeAgentRuntime, tmp_path: Path) -> None:
    h = await live_runtime.open(_live_spec(tmp_path), resume_id=None, timeout=60)
    await live_runtime.send(h, AgentInput(text="Remember the code word ZEBRA. Reply: ok"), mode="new_turn", timeout=10)
    await _live_until(live_runtime, h, _is(TurnResult))
    await live_runtime.close(h, timeout=15)

    again = await live_runtime.open(_live_spec(tmp_path), resume_id=h.session_id, timeout=60)
    try:
        await live_runtime.send(
            again, AgentInput(text="What was the code word? One word."), mode="new_turn", timeout=10
        )
        events = await _live_until(live_runtime, again, _is(TurnResult))
        assert _of(events, SessionReady)[0].session_id == h.session_id
        assert "ZEBRA" in _results(events)[0].text.upper()
    finally:
        await live_runtime.close(again, timeout=15)
