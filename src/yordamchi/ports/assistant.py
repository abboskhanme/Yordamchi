"""Assistant: a warm question-answer session with an allowlisted toolset (CONVENTIONS §4; SPEC §5.3).

Dev kinds `routing`, `report` and `settings`, and the personal `qa`. The production adapter keeps one live client and
one hot spare per kind and rotates them (after 20 asks, on a context-size threshold, or when `settings_version`
grows). The Gemini/OpenRouter fallback answers are labelled "zaxira model" by the caller.

Prompts (SPEC §5.4, revised after spike S4): `system_prompt` is frozen per warm client, so it holds only the fixed
parts. The owner-editable block (persona, tone, language, limits) travels with **every** ask in `instructions`; a
changed `prompt_version` therefore applies on the next ask without a rotation.

Structured answers (routing): a request with `output_schema` gets `AssistantReply.structured`, the JSON value the
model produced for that schema (the CLI validates it); `text` is its JSON text. An adapter that cannot produce a value
of the schema's top-level type raises an `ExternalError` instead of returning `structured=None`.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from yordamchi.core.json_value import JsonValue
from yordamchi.ports.agent import RuntimeStatus

type AssistantKind = Literal["routing", "report", "settings", "qa"]


@dataclass(frozen=True, slots=True)
class AssistantProfile:
    """What a warm client is launched with: the part of a request that is fixed per kind and settings snapshot."""

    kind: AssistantKind
    system_prompt: str  # rendered from prompts/<bot>/<kind>_system.md; never a literal (A8)
    model: str
    settings_version: int
    effort: str | None = None
    output_schema: Mapping[str, JsonValue] | None = None


@dataclass(frozen=True, slots=True)
class AssistantRequest:
    kind: AssistantKind
    system_prompt: str  # rendered from prompts/<bot>/<kind>_system.md; never a literal (A8)
    prompt: str
    model: str
    settings_version: int  # the snapshot version; a higher one rotates the warm client
    effort: str | None = None
    images: tuple[Path, ...] = ()
    output_schema: Mapping[str, JsonValue] | None = None  # structured output (routing)
    conversation: str | None = None  # the personal conversation key; None = a stateless ask
    instructions: str = ""  # the current owner-editable block, sent with every ask as context (SPEC §5.4)
    prompt_version: int = 0  # the version of `instructions`; tracked for audit, never a rotation (S4)

    @property
    def profile(self) -> AssistantProfile:
        return AssistantProfile(
            kind=self.kind,
            system_prompt=self.system_prompt,
            model=self.model,
            settings_version=self.settings_version,
            effort=self.effort,
            output_schema=self.output_schema,
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
