"""The raw `claude` stream-json → executor-neutral `AgentEvent`s (SPEC §5.1; docs/SPIKES.md N1–N5, N12).

Pure: no process, no SDK, no clock, so every case replays from `tests/fixtures/claude/streams/`. The executor feeds it
each raw message (read before the SDK parser, which drops `command_lifecycle`) and tells it what it sent.

**Turn completion.** A `result` ends one CLI turn, not necessarily a Yordamchi turn: a background subagent or a queued
`next` input makes the CLI start another turn, with its own result, right after (S1, S3). So results are held and
folded into one `TurnResult`, which is delivered when the turn is really over:
- primary signal: the CLI's `session_state_changed` frames (`CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1`). The state
  stays `running` while a background agent or a queued input still owes a turn, and turns `idle` when none does;
- fallback, for a CLI that sends no state frames: the task ledger (`task_started.is_backgrounded`,
  `task_updated.patch`, `background_tasks_changed`, `task_notification`) and the `command_lifecycle` queue. A result
  is held while a background agent is outstanding or one of our inputs is still queued;
- an interrupted result (`error_during_execution` / `aborted_tools`) while stopping is delivered at once, marked
  `interrupted` (N5). An empty `aborted_tools` result is never delivered as a normal result (S1 `now`): it is folded
  into the next one, or, if nothing follows, delivered as `interrupted`;
- a turn the CLI starts on its own after Yordamchi's turn was delivered (a background shell finished) is delivered
  with `background=True`: no send owns it.

`total_cost_usd` and `modelUsage` on a result are session totals of this CLI process; each `TurnResult` carries the
delta, in integer micro-USD (CONVENTIONS §3). `system/init` repeats on every turn (N3): `SessionReady` is emitted
once, later inits only update the capabilities and model.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final

from yordamchi.core.money import usd_float_to_micros
from yordamchi.ports.agent import (
    AgentEvent,
    ModelUsage,
    Progress,
    RuntimeFailed,
    SessionReady,
    TurnResult,
    UsageLimited,
    UsageWindow,
)

AGENT_TASK_TYPES: Final = frozenset({"local_agent", "local_workflow"})
TERMINAL_TASK_STATUSES: Final = frozenset({"completed", "failed", "stopped", "killed"})
# Tools the harness may add however narrow the tool list is (STACK C2, revised after S5).
HARNESS_TOOLS: Final = frozenset({"EndConversation", "ToolSearch", "WaitForMcpServers"})
TODO_TOOLS: Final = frozenset({"TodoWrite"})
CAP_CANCEL_QUEUED: Final = "interrupt_cancel_queued_v1"
CAP_LIFECYCLE: Final = "msg_lifecycle_v1"
SUMMARY_MAX_CHARS: Final = 200
TEXT_MAX_CHARS: Final = 2000
INTERRUPTED_SUBTYPE: Final = "error_during_execution"
ABORTED_TOOLS: Final = "aborted_tools"
AUTH_STATUSES: Final = frozenset({401, 403})
USAGE_LIMIT_STATUS: Final = 429

_SUMMARY_KEYS: Final = {
    "Bash": "command",
    "Read": "file_path",
    "Edit": "file_path",
    "Write": "file_path",
    "NotebookEdit": "notebook_path",
    "Glob": "pattern",
    "Grep": "pattern",
    "WebFetch": "url",
    "WebSearch": "query",
    "Agent": "description",
    "Task": "description",
}


def tools_match(expected: frozenset[str], reported: Sequence[str]) -> bool:
    """The reported tools hold every expected one and nothing else but harness tools (STACK C2, revised after S5)."""
    actual = frozenset(reported)
    return expected <= actual <= expected | HARNESS_TOOLS


MCP_CONNECTED: Final = "connected"


def servers_match(expected: frozenset[str], reported: Any) -> bool:
    """`init.mcp_servers` names exactly the expected servers, each `connected` (S5: check names and status too)."""
    entries = [entry for entry in reported if isinstance(entry, Mapping)] if isinstance(reported, list) else []
    names = [str(entry.get("name")) for entry in entries]
    connected = {str(entry.get("name")) for entry in entries if entry.get("status") == MCP_CONNECTED}
    return len(names) == len(set(names)) and set(names) == expected == connected


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tool_summary(name: str, tool_input: Any) -> str:
    """One line for the progress message: the command, path, pattern or URL the call is about."""
    if not isinstance(tool_input, Mapping):
        return ""
    value = tool_input.get(_SUMMARY_KEYS.get(name, ""))
    if not isinstance(value, str):
        return ""
    first = value.strip().splitlines()[0] if value.strip() else ""
    return _clip(first, SUMMARY_MAX_CHARS)


def todo_summary(tool_input: Any) -> str:
    """`2/5 · Running the tests`: done count, total, and the item in progress."""
    todos = tool_input.get("todos") if isinstance(tool_input, Mapping) else None
    if not isinstance(todos, list):
        return ""
    items = [item for item in todos if isinstance(item, Mapping)]
    done = sum(1 for item in items if item.get("status") == "completed")
    current = next((item for item in items if item.get("status") == "in_progress"), None)
    label = ""
    if current is not None:
        text = current.get("activeForm") or current.get("content")
        label = f" · {_clip(str(text), SUMMARY_MAX_CHARS)}" if text else ""
    return f"{done}/{len(items)}{label}"


def epoch_to_utc(value: Any) -> datetime | None:
    """`resetsAt` is a Unix timestamp in seconds."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) and not isinstance(value, bool) else 0


def _usd_micros(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    return usd_float_to_micros(float(value))


def _model_usage(raw: Any) -> dict[str, ModelUsage]:
    usage: dict[str, ModelUsage] = {}
    if not isinstance(raw, Mapping):
        return usage
    for model, entry in raw.items():
        if isinstance(entry, Mapping):
            usage[str(model)] = ModelUsage(
                input_tokens=_int(entry.get("inputTokens")),
                output_tokens=_int(entry.get("outputTokens")),
                cache_read_tokens=_int(entry.get("cacheReadInputTokens")),
                cache_write_tokens=_int(entry.get("cacheCreationInputTokens")),
                cost_usd_micros=_usd_micros(entry.get("costUSD")),
            )
    return usage


def _minus(now: ModelUsage, base: ModelUsage | None) -> ModelUsage:
    """Per-turn usage from session totals; a total below its base means the counters restarted."""
    if base is None or now.input_tokens < base.input_tokens or now.cost_usd_micros < base.cost_usd_micros:
        return now
    return ModelUsage(
        input_tokens=now.input_tokens - base.input_tokens,
        output_tokens=max(0, now.output_tokens - base.output_tokens),
        cache_read_tokens=max(0, now.cache_read_tokens - base.cache_read_tokens),
        cache_write_tokens=max(0, now.cache_write_tokens - base.cache_write_tokens),
        cost_usd_micros=now.cost_usd_micros - base.cost_usd_micros,
    )


def _plus(a: ModelUsage | None, b: ModelUsage) -> ModelUsage:
    if a is None:
        return b
    return ModelUsage(
        input_tokens=a.input_tokens + b.input_tokens,
        output_tokens=a.output_tokens + b.output_tokens,
        cache_read_tokens=a.cache_read_tokens + b.cache_read_tokens,
        cache_write_tokens=a.cache_write_tokens + b.cache_write_tokens,
        cost_usd_micros=a.cost_usd_micros + b.cost_usd_micros,
    )


@dataclass(slots=True)
class _Task:
    task_type: str
    backgrounded: bool


@dataclass(slots=True)
class _Held:
    """The CLI results of one Yordamchi turn that are not delivered yet."""

    cost_usd_micros: int = 0
    model_usage: dict[str, ModelUsage] = field(default_factory=dict)
    num_turns: int = 0
    duration_ms: int = 0
    denials: list[str] = field(default_factory=list)
    text: str = ""
    subtype: str = "success"
    is_error: bool = False
    stop_reason: str | None = None
    aborted: bool = False
    interrupted: bool = False


@dataclass(slots=True)
class _Limits:
    resets_at: datetime | None = None
    limit_type: str = "unknown"
    reported_this_turn: bool = False


class StreamTranslator:
    """One per session. `feed()` each raw message in order; call `sent()` / `stop_requested()` as the executor acts."""

    def __init__(
        self,
        *,
        session_id: str,
        expected_tools: frozenset[str] | None = None,
        expected_servers: frozenset[str] | None = None,
    ) -> None:
        self.session_id = session_id
        self.expected_tools = expected_tools
        self.expected_servers = expected_servers  # the personal session's in-process MCP servers (NFR-SEC-08)
        self.ready = False
        self.refused = False
        self.capabilities: frozenset[str] = frozenset()
        self.model: str | None = None
        self.cli_version: str | None = None
        self.state_events = False
        self.session_state: str | None = None
        self._tasks: dict[str, _Task] = {}
        self._queued: set[str] = set()
        self._tool_names: dict[str, str] = {}
        self._owned = False
        self._turn_open = False
        self._stopping = False
        self._held: _Held | None = None
        self._cost_base = 0
        self._usage_base: dict[str, ModelUsage] = {}
        self._limits = _Limits()

    # --- what the executor tells it ---------------------------------------------------------------------------
    def sent(self, command_uuid: str) -> None:
        """A user message went out: it opens (or joins) a turn that Yordamchi owns, and ends any stop."""
        self._owned = True
        self._turn_open = True
        self._stopping = False
        if CAP_LIFECYCLE in self.capabilities:
            # The CLI confirms with `command_lifecycle queued`; counting it now closes the window before that frame.
            self._queued.add(command_uuid)

    def stop_requested(self) -> bool:
        """`interrupt()` was called. Returns whether a turn is running (only then is the guard closed)."""
        if self._turn_open:
            self._stopping = True
        return self._turn_open

    def cancelled(self, command_uuids: Sequence[str]) -> None:
        """Inputs the interrupt receipt reports as cancelled (`cancel_queued`)."""
        self._queued.difference_update(command_uuids)

    # --- what the guard and the executor read -----------------------------------------------------------------
    @property
    def stopping(self) -> bool:
        return self._stopping

    @property
    def turn_open(self) -> bool:
        return self._turn_open

    @property
    def queued_inputs(self) -> int:
        return len(self._queued)

    @property
    def supports_cancel_queued(self) -> bool:
        return CAP_CANCEL_QUEUED in self.capabilities

    def blocked_reason(self) -> str | None:
        """Why every tool call must be denied right now, if it must (the guard asks before each call)."""
        if self._stopping:
            return "stopping"
        if self.refused:
            return "tools_mismatch"
        if self.expected_tools is not None and not self.ready:
            return "tools_unverified"
        return None

    def background_task_ids(self) -> tuple[str, ...]:
        """Background tasks still running (shells and agents): `interrupt()` stops them too."""
        return tuple(task_id for task_id, task in self._tasks.items() if task.backgrounded)

    def background_agents(self) -> int:
        return sum(1 for task in self._tasks.values() if task.task_type in AGENT_TASK_TYPES)

    # --- the stream ----------------------------------------------------------------------------------------
    def feed(self, msg: Mapping[str, Any]) -> list[AgentEvent]:
        kind = msg.get("type")
        if kind == "system":
            return self._on_system(msg)
        if kind == "assistant":
            self._open_turn()
            return self._on_assistant(msg)
        if kind == "user":
            return self._on_user(msg)
        if kind == "result":
            return self._on_result(msg)
        if kind == "rate_limit_event":
            return self._on_rate_limit(msg)
        if kind == "command_lifecycle":
            self._on_lifecycle(msg)
        elif kind == "conversation_reset":
            self._cost_base = 0
            self._usage_base = {}
        return []

    def _open_turn(self) -> None:
        """Activity while no turn is open: the CLI started one on its own (after background work)."""
        if not self._turn_open:
            self._turn_open = True

    # --- system frames ----------------------------------------------------------------------------------------
    def _on_system(self, msg: Mapping[str, Any]) -> list[AgentEvent]:
        subtype = msg.get("subtype")
        if subtype == "init":
            self._open_turn()
            return self._on_init(msg)
        if subtype == "session_state_changed":
            return self._on_state(msg.get("state"))
        if subtype in {"task_started", "task_updated", "task_notification", "background_tasks_changed"}:
            self._on_task(subtype, msg)
        return []

    def _on_init(self, msg: Mapping[str, Any]) -> list[AgentEvent]:
        raw_caps = msg.get("capabilities")
        if isinstance(raw_caps, list):
            self.capabilities = frozenset(str(item) for item in raw_caps)
        model = msg.get("model")
        self.model = str(model) if model else self.model
        version = msg.get("claude_code_version")
        self.cli_version = str(version) if version else self.cli_version
        if self.ready:
            return []  # N3: a later init is an update, never a new session
        self.ready = True
        tools = tuple(str(tool) for tool in msg.get("tools") or ())
        if self.expected_tools is not None and not tools_match(self.expected_tools, tools):
            self.refused = True
            return [RuntimeFailed(code="protocol", retryable=False)]
        if self.expected_servers is not None and not servers_match(self.expected_servers, msg.get("mcp_servers")):
            self.refused = True
            return [RuntimeFailed(code="protocol", retryable=False)]
        servers = tuple(
            str(server.get("name")) for server in msg.get("mcp_servers") or () if isinstance(server, Mapping)
        )
        session_id = msg.get("session_id")
        return [
            SessionReady(
                session_id=str(session_id) if session_id else self.session_id,
                tools=tools,
                mcp_servers=servers,
                model=self.model or "",
                cli_version=self.cli_version or "",
            )
        ]

    def _on_state(self, state: Any) -> list[AgentEvent]:
        self.state_events = True
        self.session_state = str(state) if state is not None else None
        if self.session_state == "running":
            self._open_turn()
        elif self.session_state == "idle" and self._held is not None:
            return self._flush()
        return []

    def _on_background_snapshot(self, msg: Mapping[str, Any]) -> None:
        """`background_tasks_changed` lists the live background set: add what it lists, drop background tasks it
        no longer lists (a subagent registered in the foreground shows up here once it is backgrounded)."""
        listed: dict[str, str] = {}
        for entry in msg.get("tasks") or ():
            if isinstance(entry, Mapping) and entry.get("task_id"):
                listed[str(entry["task_id"])] = str(entry.get("task_type") or "")
        for known_id, task in list(self._tasks.items()):
            if task.backgrounded and known_id not in listed:
                del self._tasks[known_id]
        for listed_id, task_type in listed.items():
            known = self._tasks.get(listed_id)
            if known is None:
                self._tasks[listed_id] = _Task(task_type=task_type, backgrounded=True)
            else:
                known.backgrounded = True

    def _on_task(self, subtype: str, msg: Mapping[str, Any]) -> None:
        if subtype == "background_tasks_changed":
            self._on_background_snapshot(msg)
            return
        raw_id = msg.get("task_id")
        if not raw_id:
            return
        key = str(raw_id)
        if subtype == "task_started":
            self._tasks[key] = _Task(
                task_type=str(msg.get("task_type") or ""), backgrounded=msg.get("is_backgrounded") is True
            )
        elif subtype == "task_notification":
            self._tasks.pop(key, None)
        else:  # task_updated
            patch = msg.get("patch")
            if not isinstance(patch, Mapping):
                return
            if patch.get("status") in TERMINAL_TASK_STATUSES:
                self._tasks.pop(key, None)
            elif patch.get("is_backgrounded") is True:
                known = self._tasks.get(key)
                if known is None:
                    self._tasks[key] = _Task(task_type="", backgrounded=True)
                else:
                    known.backgrounded = True

    def _on_lifecycle(self, msg: Mapping[str, Any]) -> None:
        command = msg.get("command_uuid")
        if not command:
            return
        if msg.get("state") == "queued":
            self._queued.add(str(command))
        else:  # started, completed, cancelled
            self._queued.discard(str(command))

    # --- conversation -----------------------------------------------------------------------------------------
    def _on_assistant(self, msg: Mapping[str, Any]) -> list[AgentEvent]:
        message = msg.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            return []
        main_thread = msg.get("parent_tool_use_id") is None
        events: list[AgentEvent] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            kind = block.get("type")
            if kind == "text" and main_thread:
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    events.append(Progress(kind="text", tool_name=None, summary=_clip(text, TEXT_MAX_CHARS)))
            elif kind == "tool_use":
                name = str(block.get("name") or "")
                tool_use_id = block.get("id")
                if tool_use_id:
                    self._tool_names[str(tool_use_id)] = name
                tool_input = block.get("input")
                if name in TODO_TOOLS:
                    events.append(Progress(kind="todo", tool_name=name, summary=todo_summary(tool_input)))
                else:
                    events.append(Progress(kind="tool_start", tool_name=name, summary=tool_summary(name, tool_input)))
        return events

    def _on_user(self, msg: Mapping[str, Any]) -> list[AgentEvent]:
        message = msg.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            return []  # a plain string: a local command's output or our own echo
        events: list[AgentEvent] = []
        for block in content:
            if isinstance(block, Mapping) and block.get("type") == "tool_result":
                name = self._tool_names.get(str(block.get("tool_use_id")))
                events.append(
                    Progress(kind="tool_end", tool_name=name, summary="", is_error=block.get("is_error") is True)
                )
        return events

    def _on_rate_limit(self, msg: Mapping[str, Any]) -> list[AgentEvent]:
        info = msg.get("rate_limit_info")
        if not isinstance(info, Mapping):
            return []
        status = str(info.get("status") or "unknown")
        limit_type = str(info.get("rateLimitType") or "unknown")
        resets_at = epoch_to_utc(info.get("resetsAt"))
        self._limits.resets_at = resets_at
        self._limits.limit_type = limit_type
        events: list[AgentEvent] = []
        windows = info.get("unifiedWindows")
        if isinstance(windows, Mapping) and windows:
            for name, window in windows.items():
                if isinstance(window, Mapping):
                    events.append(
                        UsageWindow(
                            limit_type=str(name),
                            status=str(window.get("status") or status),
                            utilization=_fraction(window.get("utilization")),
                            resets_at=epoch_to_utc(window.get("resetsAt")),
                        )
                    )
        else:
            events.append(
                UsageWindow(
                    limit_type=limit_type,
                    status=status,
                    utilization=_fraction(info.get("utilization")),
                    resets_at=resets_at,
                )
            )
        if status == "rejected":
            events.extend(self._usage_limited())
        return events

    def _usage_limited(self) -> list[AgentEvent]:
        if self._limits.reported_this_turn:
            return []
        self._limits.reported_this_turn = True
        return [UsageLimited(resets_at=self._limits.resets_at, limit_type=self._limits.limit_type)]

    # --- results ----------------------------------------------------------------------------------------------
    def _on_result(self, msg: Mapping[str, Any]) -> list[AgentEvent]:
        self._open_turn()
        held = self._held if self._held is not None else _Held()
        self._held = held
        total = _usd_micros(msg.get("total_cost_usd"))
        held.cost_usd_micros += total - self._cost_base if total >= self._cost_base else total
        self._cost_base = total
        totals = _model_usage(msg.get("modelUsage"))
        for model, usage in totals.items():
            held.model_usage[model] = _plus(held.model_usage.get(model), _minus(usage, self._usage_base.get(model)))
        self._usage_base = {**self._usage_base, **totals}
        held.num_turns += _int(msg.get("num_turns"))
        held.duration_ms += _int(msg.get("duration_ms"))
        for denial in msg.get("permission_denials") or ():
            if isinstance(denial, Mapping) and denial.get("tool_name"):
                held.denials.append(str(denial["tool_name"]))
        text = msg.get("result")
        if isinstance(text, str) and text.strip():
            held.text = text
        held.subtype = str(msg.get("subtype") or "success")
        held.is_error = msg.get("is_error") is True
        stop_reason = msg.get("stop_reason")
        held.stop_reason = str(stop_reason) if stop_reason else None
        held.aborted = msg.get("terminal_reason") == ABORTED_TOOLS
        held.interrupted = held.aborted and (held.subtype == INTERRUPTED_SUBTYPE or self._stopping)

        events: list[AgentEvent] = []
        status = msg.get("api_error_status")
        if status == USAGE_LIMIT_STATUS:
            events.extend(self._usage_limited())
        elif status in AUTH_STATUSES:
            events.append(RuntimeFailed(code="auth_failed", retryable=False))
        if self._ready_to_deliver(held):
            events.extend(self._flush())
        return events

    def _ready_to_deliver(self, held: _Held) -> bool:
        if held.interrupted and self._stopping:
            return True
        if self.state_events:
            return self.session_state == "idle"  # "idle" may come just before the result
        return self.background_agents() == 0 and not self._queued

    def _flush(self) -> list[AgentEvent]:
        held = self._held
        if held is None:
            return []
        result = TurnResult(
            text=held.text,
            subtype=held.subtype,
            is_error=held.is_error,
            stop_reason=held.stop_reason,
            cost_usd_micros=held.cost_usd_micros,
            model_usage=dict(held.model_usage),
            num_turns=held.num_turns,
            duration_ms=held.duration_ms,
            permission_denials=tuple(held.denials),
            # An empty aborted result with nothing after it is a stopped turn, never an empty success (S1); so is
            # any turn that ends while a stop is under way (a held turn whose background agent was stopped).
            interrupted=held.interrupted or held.aborted or self._stopping,
            background=not self._owned,
        )
        self._held = None
        self._owned = False
        self._turn_open = False
        self._stopping = False
        self._limits.reported_this_turn = False
        self._tool_names.clear()
        return [result]


def _fraction(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)
