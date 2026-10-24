"""Transcriber and Synthesizer: speech to text and back (CONVENTIONS §4; SPEC §3.2.11).

Every behaviour value (languages, vocabulary, model, voice, style) comes from the caller's settings snapshot.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    provider: str  # "google" | "openrouter"
    provider_path: str  # e.g. "interactions", "chat_completions", "audio_transcriptions" (fallback)
    model: str
    audio_ms: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    data: bytes = field(repr=False)
    mime: str  # "audio/ogg" (Opus) is what Telegram plays as a voice note
    provider: str
    model: str
    duration_ms: int | None = None


@dataclass(frozen=True, slots=True)
class SpeechKeyCheck:
    """What validating a speech key found (SPEC §3.2.11 step 2). A bad key raises `AuthFailed` instead.

    Money is `Decimal` USD as the provider reports it; `None` means the provider does not say (Google) or the key
    has no limit (OpenRouter `limit: null`).
    """

    provider: str  # "google" | "openrouter"
    available_models: tuple[str, ...]
    missing_models: tuple[str, ...]
    is_free_tier: bool | None = None
    limit_usd: Decimal | None = None
    usage_usd: Decimal | None = None
    limit_remaining_usd: Decimal | None = None

    @property
    def ok(self) -> bool:
        return not self.missing_models


class Transcriber(Protocol):
    async def transcribe(
        self,
        audio: bytes,
        *,
        mime: str,
        languages: Sequence[str],
        vocabulary: Sequence[str],
        model: str,
        timeout: float,
    ) -> Transcript: ...


class Synthesizer(Protocol):
    async def synthesize(
        self, text: str, *, voice: str, style: str, language_code: str, model: str, timeout: float
    ) -> SpeechAudio: ...

    def synthesize_stream(
        self, text: str, *, voice: str, style: str, language_code: str, model: str, idle_timeout: float
    ) -> AsyncIterator[bytes]:
        """Encoded audio chunks as they arrive, so the voice note can follow the text within 12 s (SPEC §3.2.13)."""
        ...
