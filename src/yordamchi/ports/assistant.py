"""Assistant: a warm question-answer session with an allowlisted toolset (CONVENTIONS §4; SPEC §5.3).

Dev kinds `routing`, `report`, `settings` and `chat` (the dev group's general answers), and the personal `qa`. The
production adapter keeps one live client and one hot spare per kind and rotates them (after 20 asks, on a
context-size threshold, or when `settings_version` grows). The Gemini/OpenRouter fallback answers are labelled
"zaxira model" by the caller.

Prompts (SPEC §5.4, revised after spike S4): `system_prompt` is frozen per warm client, so it holds only the fixed
parts. The owner-editable block (persona, tone, language, limits) travels with **every** ask in `instructions`; a
changed `prompt_version` therefore applies on the next ask without a rotation.

Structured answers (routing): a request with `output_schema` gets `AssistantReply.structured`, the JSON value the
model produced for that schema (the CLI validates it); `text` is its JSON text. An adapter that cannot produce a value
of the schema's top-level type raises an `ExternalError` instead of returning `structured=None`.

**The personal `qa` kind** (SPEC §3.2.13, §5.3 personal column; T72). One conversation is one warm session:
`conversation` is its key, and the Claude adapter uses it as the CLI session id, so a restart (or an effort change)
resumes the same conversation (`resume=True` once it has answered). Its tools are the CLI's `WebSearch` and `WebFetch`
plus in-process `toolsets` (`yordamchi_bridge`, `yordamchi_media`, `yordamchi_int`): each tool is a name, a
model-facing description (rendered from `prompts/**` by the caller), a JSON schema and an async handler the adapter
calls with the model's arguments. A tool's result is **data** for the model, never an instruction. The toolset
*signature* (names, descriptions, schemas) is a launch option; the handlers are taken from every request, so they can
close over this turn's settings snapshot.
"""

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from yordamchi.core.json_value import JsonValue
from yordamchi.ports.agent import RuntimeStatus

type AssistantKind = Literal["routing", "report", "settings", "chat", "qa"]

TOOL_NAME: Final = re.compile(r"[a-z][a-z0-9_]{0,47}")
SERVER_NAME: Final = re.compile(r"yordamchi_[a-z][a-z0-9_]{0,30}")


# --- in-process tools (the personal `qa` kind) ---------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ToolOutput:
    """What a tool hands back to the model: text it reads as data. `is_error` marks a failure it may explain."""

    text: str = field(repr=False)
    is_error: bool = False


type ToolHandler = Callable[[Mapping[str, JsonValue]], Awaitable[ToolOutput]]


async def _no_handler(arguments: Mapping[str, JsonValue]) -> ToolOutput:
    del arguments
    return ToolOutput(text="tool_unavailable", is_error=True)


@dataclass(frozen=True, slots=True)
class AssistantTool:
    """One in-process tool. `handler` never raises for an expected failure; it returns `ToolOutput(is_error=True)`.
    The adapter bounds every call by `timeout_s` and turns a timeout or an error into an error result."""

    name: str
    description: str = field(repr=False)  # model-facing text, rendered from prompts/** (never a literal, A8)
    input_schema: Mapping[str, JsonValue] = field(repr=False)  # a JSON schema object
    timeout_s: float = 10.0
    read_only: bool = True
    handler: ToolHandler = field(default=_no_handler, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not TOOL_NAME.fullmatch(self.name):
            msg = f"tool name {self.name!r} must match {TOOL_NAME.pattern}"
            raise ValueError(msg)
        if self.timeout_s <= 0:
            msg = "a tool needs a positive timeout"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class AssistantToolset:
    """The tools of one in-process MCP server. Equality is the signature: handlers are not compared."""

    server: str
    tools: tuple[AssistantTool, ...]

    def __post_init__(self) -> None:
        if not SERVER_NAME.fullmatch(self.server):
            msg = f"server name {self.server!r} must match {SERVER_NAME.pattern}"
            raise ValueError(msg)
        names = [tool.name for tool in self.tools]
        if not names or len(set(names)) != len(names):
            msg = f"toolset {self.server} needs at least one tool and unique tool names"
            raise ValueError(msg)

    def qualified(self, tool: AssistantTool) -> str:
        """The name the model and the `init` tool list use: `mcp__<server>__<tool>`."""
        return f"mcp__{self.server}__{tool.name}"

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(self.qualified(tool) for tool in self.tools)


@dataclass(frozen=True, slots=True)
class AssistantProfile:
    """What a warm client is launched with: the part of a request that is fixed per kind and settings snapshot."""

    kind: AssistantKind
    system_prompt: str  # rendered from prompts/<bot>/<kind>_system.md; never a literal (A8)
    model: str
    settings_version: int
    effort: str | None = None
    output_schema: Mapping[str, JsonValue] | None = None
    # `qa` only (see the module docstring); the dev kinds leave these at their defaults.
    conversation: str | None = None
    resume: bool = False  # the conversation already has turns: a new client resumes it
    toolsets: tuple[AssistantToolset, ...] = ()
    max_web_searches: int | None = None  # the CLI's per-session WebSearch cap (STACK §4.8)


@dataclass(frozen=True, slots=True)
class AssistantRequest:
    kind: AssistantKind
    system_prompt: str  # rendered from prompts/<bot>/<kind>_system.md; never a literal (A8)
    prompt: str
    model: str
    settings_version: int  # the snapshot version; a higher one rotates the warm client
    effort: str | None = None
    images: tuple[Path, ...] = ()
    documents: tuple[Path, ...] = ()  # `qa`: PDF files, sent as `document` blocks after the images (UC-37)
    output_schema: Mapping[str, JsonValue] | None = None  # structured output (routing)
    conversation: str | None = None  # the personal conversation key; None = a stateless ask
    instructions: str = ""  # the current owner-editable block, sent with every ask as context (SPEC §5.4)
    prompt_version: int = 0  # the version of `instructions`; tracked for audit, never a rotation (S4)
    resume: bool = False  # `qa`: the conversation already has turns (a fresh process resumes it)
    toolsets: tuple[AssistantToolset, ...] = ()  # `qa`: in-process tools; handlers from this request are used
    max_web_searches: int | None = None  # `qa`: `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION`

    @property
    def profile(self) -> AssistantProfile:
        return AssistantProfile(
            kind=self.kind,
            system_prompt=self.system_prompt,
            model=self.model,
            settings_version=self.settings_version,
            effort=self.effort,
            output_schema=self.output_schema,
            conversation=self.conversation,
            resume=self.resume,
            toolsets=self.toolsets,
            max_web_searches=self.max_web_searches,
        )


@dataclass(frozen=True, slots=True)
class AssistantReply:
    text: str
    provider: str  # "claude" | "gemini" | "openrouter"
    model: str
    duration_ms: int
    structured: JsonValue = None
    session_id: str | None = None
    cost_usd_micros: int = 0
    web_searches: int = 0
    usage: Mapping[str, int] = field(default_factory=dict)
    prompt_version: int = 0  # the request's `prompt_version`, echoed for `prompt_version_seen`
    tool_calls: tuple[str, ...] = ()  # `qa`: the tools the model called in this ask, in order (names only)


@dataclass(frozen=True, slots=True)
class AssistantDelta:
    """A streamed text fragment (personal answers stream into `sendMessageDraft`)."""

    text: str


class Assistant(Protocol):
    async def probe(self, *, timeout: float) -> RuntimeStatus: ...

    async def warm(self, profile: AssistantProfile, *, timeout: float) -> None:
        """Start the live client and the hot spare for `profile` (startup, settings change), so the first ask is
        warm. Waits for the live client at most `timeout`; the spare keeps starting in the background."""
        ...

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply: ...

    def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        """Deltas, then exactly one final `AssistantReply`."""
        ...

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        """Drop the warm session ("yangi suhbat", idle rotation)."""
        ...
