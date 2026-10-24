"""AgentRuntime: one project session of an executor (SPEC §5.1). Claude (P1) and Codex (P4) implement it.

Every type here is executor-neutral. The adapter maps vendor messages onto the events below and calls
`SessionSpec.guard` before every tool call (fail closed: an error or a timeout is a deny).
"""

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, Protocol

from yordamchi.core.json_value import JsonValue

MAX_INPUT_IMAGES: Final = 5

type SessionKind = Literal["coding", "investigation", "smoke"]
type Effort = Literal["low", "medium", "high", "xhigh", "max"]
type PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk"]
type SettingSource = Literal["user", "project", "local"]
type DeliveryMode = Literal["new_turn", "next"]
type ProgressKind = Literal["tool_start", "tool_end", "text", "todo"]
type FailureCode = Literal["process_died", "auth_failed", "session_not_found", "cli_missing", "protocol"]
type ContextProvider = Callable[[], str]


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    """`probe()`: is the executor installed and logged in the right way (`authMethod: "claude.ai"`)."""

    installed: bool
    version: str | None
    logged_in: bool
    auth_method: str | None
    missing: tuple[str, ...] = ()


# --- the gate hook -------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_name: str
    tool_input: Mapping[str, JsonValue]
    tool_use_id: str
    agent_id: str | None  # set inside a subagent
    session_id: str
    cwd: str | None = None  # the executor's working directory at this call (a Bash `cd` persists across calls)


@dataclass(frozen=True, slots=True)
class GuardVerdict:
    """`allow`, or a deny whose `message` the agent reads (rendered from `prompts/dev/gate_*.md`)."""

    allow: bool
    message: str = ""
    reason_code: str = ""


type GuardFn = Callable[[ToolCall], Awaitable[GuardVerdict]]


# --- session -------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class McpHttpServer:
    """A remote MCP server. Its auth header comes from `headers_helper` at connect time, never from argv."""

    url: str
    headers_helper: str


@dataclass(frozen=True, slots=True)
class SessionSpec:
    """Everything needed to open (or resume) a session; frozen, built per session open (SPEC §5.3)."""

    kind: SessionKind
    cwd: Path
    model: str
    guard: GuardFn
    effort: Effort | None = None
    system_append: str = ""
    context_provider: ContextProvider | None = None  # returns a precomputed string in < 50 ms
    setting_sources: tuple[SettingSource, ...] = ()
    tools: tuple[str, ...] | None = None  # None = the executor's preset default
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()  # forbidden-only rules (SPEC §5.2 second layer)
    permission_mode: PermissionMode = "default"
    mcp_servers: Mapping[str, McpHttpServer] = field(default_factory=dict)
    sdk_servers: tuple[str, ...] = ()  # in-process tool sets, by id
    strict_mcp: bool = False
    env: Mapping[str, str] = field(default_factory=dict)  # additions only; the base is core.proc.clean_env()
    expected_tools: frozenset[str] | None = None  # init tool list must equal this (personal bot, NFR-SEC-08)
    max_turns: int | None = None
    extra_args: Mapping[str, str | None] = field(default_factory=dict)
    hook_timeout_s: float = 15.0  # `gate.hook_timeout_s`: a guard call slower than this is a deny (SPEC §3.2.3)
    question_hold_s: float = 1800.0  # `questions.hold_process_min`: how long a question callback is held (§3.2.2)
    question_parked_message: str = ""  # `prompts/dev/question_parked.md`: the deny text once the hold elapses


@dataclass(frozen=True, slots=True)
class SessionHandle:
    session_id: str
    executor: str
    kind: SessionKind


@dataclass(frozen=True, slots=True)
class AgentInput:
    """Text plus at most five image paths; other files are referenced by path in the text."""

    text: str
    images: tuple[Path, ...] = ()

    def __post_init__(self) -> None:
        if len(self.images) > MAX_INPUT_IMAGES:
            msg = f"an agent input carries at most {MAX_INPUT_IMAGES} images"
            raise ValueError(msg)


# --- events --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class SessionReady:
    session_id: str
    tools: tuple[str, ...]
    mcp_servers: tuple[str, ...]
    model: str
    cli_version: str


@dataclass(frozen=True, slots=True)
class Progress:
    kind: ProgressKind
    tool_name: str | None
    summary: str
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class QuestionAsked:
    """`AskUserQuestion`: 1–4 questions, each `{question, header, options[{label, description}], multiSelect}`."""

    question_id: str
    questions: tuple[Mapping[str, JsonValue], ...]


@dataclass(frozen=True, slots=True)
class ToolDenied:
    tool_name: str
    reason_code: str


@dataclass(frozen=True, slots=True)
class UsageLimited:
    resets_at: datetime | None
    limit_type: str


@dataclass(frozen=True, slots=True)
class UsageWindow:
    """The plan's usage window as the executor reports it on every turn (`status`: allowed|allowed_warning|rejected;
    `utilization` 0.0–1.0). `UsageLimited` follows when the status is `rejected`."""

    limit_type: str
    status: str
    utilization: float | None
    resets_at: datetime | None


@dataclass(frozen=True, slots=True)
class ModelUsage:
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd_micros: int = 0


@dataclass(frozen=True, slots=True)
class TurnResult:
    """The executor's final message of a turn; `turns.domain.parse_result` reads `Natija`, tests and handoff.

    Emitted once per turn, when the executor reports it finished: a result followed by more work (a background
    subagent, a queued `next` input) is held and folded into the final one, so cost and counts cover the whole turn.
    """

    text: str
    subtype: str
    is_error: bool
    stop_reason: str | None
    cost_usd_micros: int
    model_usage: Mapping[str, ModelUsage]
    num_turns: int
    duration_ms: int
    permission_denials: tuple[str, ...] = ()
    interrupted: bool = False  # the turn was stopped (`interrupt()`); a stopped turn, not a failure
    background: bool = False  # a turn the executor started on its own after background work; no send owns it


@dataclass(frozen=True, slots=True)
class RuntimeFailed:
    code: FailureCode
    retryable: bool


@dataclass(frozen=True, slots=True)
class SessionClosed:
    reason: str


type AgentEvent = (
    SessionReady
    | Progress
    | QuestionAsked
    | ToolDenied
    | UsageLimited
    | UsageWindow
    | TurnResult
    | RuntimeFailed
    | SessionClosed
)


class AgentRuntime(Protocol):
    async def probe(self, *, timeout: float) -> RuntimeStatus: ...

    async def open(self, spec: SessionSpec, *, resume_id: str | None, timeout: float) -> SessionHandle:
        """Start a session, or resume `resume_id`. A missing transcript emits `RuntimeFailed(session_not_found)`."""
        ...

    async def send(self, h: SessionHandle, msg: AgentInput, *, mode: DeliveryMode, timeout: float) -> None:
        """`new_turn` starts a turn; `next` steers the running one (merged once running tools finish)."""
        ...

    async def answer(self, h: SessionHandle, question_id: str, answers: Mapping[str, str], *, timeout: float) -> bool:
        """Answer a pending `QuestionAsked`. False when its callback is gone; the caller then sends a message."""
        ...

    async def interrupt(self, h: SessionHandle, *, timeout: float) -> None:
        """Stop the running turn; kills the process group if it is still busy after `timeout`."""
        ...

    async def set_model(self, h: SessionHandle, model: str, *, timeout: float) -> None: ...

    async def close(self, h: SessionHandle, *, timeout: float) -> None: ...

    def events(self, h: SessionHandle, *, idle_timeout: float) -> AsyncIterator[AgentEvent]:
        """The session's events in order; ends after `SessionClosed`."""
        ...
