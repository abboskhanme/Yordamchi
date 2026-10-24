"""Assistant: a warm question-answer session with an allowlisted toolset (CONVENTIONS §4; SPEC §5.3).

Dev kinds `routing`, `report` and `settings`, and the personal `qa`. The production adapter keeps one live client and
one hot spare per kind and rotates them (after 20 asks, on a context-size threshold, or when `settings_version`
grows). The Gemini/OpenRouter fallback answers are labelled "zaxira model" by the caller.
"""

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from yordamchi.core.json_value import JsonValue
from yordamchi.ports.agent import RuntimeStatus

type AssistantKind = Literal["routing", "report", "settings", "qa"]


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


@dataclass(frozen=True, slots=True)
class AssistantDelta:
    """A streamed text fragment (personal answers stream into `sendMessageDraft`)."""

    text: str


class Assistant(Protocol):
    async def probe(self, *, timeout: float) -> RuntimeStatus: ...

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply: ...

    def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        """Deltas, then exactly one final `AssistantReply`."""
        ...

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        """Drop the warm session ("yangi suhbat", idle rotation)."""
        ...
