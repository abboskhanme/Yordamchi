"""Transcriber and Synthesizer: speech to text and back (CONVENTIONS §4; SPEC §3.2.11).

Every behaviour value (languages, vocabulary, model, voice, style, segment size) comes from the caller's settings
snapshot.
"""

from collections.abc import AsyncIterator, Mapping, Sequence
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
    # What the call cost as the provider reported it (OpenRouter `usage.cost`), converted once by the adapter
    # (`core.money`); None when the provider does not say (Google). `shared/voice` records it via `shared/cost`.
    cost_usd_micros: int | None = None
    units: Mapping[str, int] = field(default_factory=dict)  # the provider's counters: input_tokens, output_tokens


@dataclass(frozen=True, slots=True)
class SpeechAudio:
    """One finished voice note. The adapters always return OGG/Opus (`audio/ogg`), what Telegram plays as a voice
    note; `duration_ms` is read from the encoded stream (`sendVoice` takes it in whole seconds)."""

    data: bytes = field(repr=False)
    mime: str  # "audio/ogg" (Opus)
    provider: str  # "openrouter" | "google" | "edge"
    model: str
    duration_ms: int | None = None
    # As on `Transcript`: what the provider said the call cost (None: it does not say), and its own counters
    # (`characters`, `requests`). `shared/voice` records them via `shared/cost`.
    cost_usd_micros: int | None = None
    units: Mapping[str, int] = field(default_factory=dict)


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
        mode: str,
        fallback_model: str,
        timeout: float,
    ) -> Transcript:
        """`mode` is Gemini's `transcription_config.mode` (`stt.mode`). `fallback_model` is the model of a provider's
        hint-less second path (OpenRouter `/audio/transcriptions`, `stt.openrouter_fallback_model`). A provider without
        that feature ignores the value."""
        ...

    async def validate_key(self, key: str, *, models: Sequence[str], timeout: float) -> SpeechKeyCheck:
        """SPEC §3.2.11 step 2 on a candidate `key` (not necessarily the one the adapter uses): which `models` it can
        use, its tier and its limits, within `timeout` in total. A rejected key raises `AuthFailed`, and so does a key
        of another vendor, before anything is sent."""
        ...


class Synthesizer(Protocol):
    """Text to one OGG/Opus voice note (SPEC §3.2.13 step 6, S8).

    The provider's audio is piped into the encoder as it arrives, so the note is ready right after the provider's
    last chunk. `segment_words` is the S8 fallback: 0 sends the text in one request; above 0 the adapter cuts it at
    sentence ends into pieces of at most that many words, asks for them concurrently, and encodes them in order into
    one note. `voice`, `style`, `language_code` and `model` come from the caller's settings snapshot; a provider
    without a feature (Edge has no style or model) ignores the value. No ffmpeg → `NotInstalled`, before any request.
    """

    async def synthesize(
        self,
        text: str,
        *,
        voice: str,
        style: str,
        language_code: str,
        model: str,
        segment_words: int,
        timeout: float,
    ) -> SpeechAudio:
        """The whole note within `timeout`, retries included."""
        ...

    def synthesize_stream(
        self,
        text: str,
        *,
        voice: str,
        style: str,
        language_code: str,
        model: str,
        segment_words: int,
        idle_timeout: float,
    ) -> AsyncIterator[bytes]:
        """The note's OGG/Opus bytes, in order, once the encoder has them. No wait for the provider or the encoder
        lasts longer than `idle_timeout`; there is no total bound. `sendVoice` needs the whole file, so callers that
        send use `synthesize`."""
        ...
