"""`GeminiSynthesizer`: text to a voice note on a direct Google key (UC-30 voice; SPEC §3.2.11, §3.2.13). Second in
the TTS order under `auto`, used only when a Google key is in the vault (owner decision 2026-10-24).

Request shape (ai.google.dev/gemini-api/docs/speech-generation, google-genai 2.33.0 `interactions.create`; checked
2026-10-24):
- `model` = `tts.google_model` (default `gemini-3.8-flash-tts`), `response_format={"type": "audio"}`,
  `generation_config.speech_config=[{"voice": <voice.name>}]`, `store: False`;
- the text goes in verbatim as a text part; the style and the language go in its `speech_metadata` annotation
  (`style`), because Gemini 3.8 TTS reads the text as a transcript and the models detect the language themselves;
- `stream=True`: `step.delta` events carry base64 audio, headerless 16-bit PCM at 24 kHz mono (`audio/l16`), which is
  piped into ffmpeg as it arrives. A delta that says `audio/wav` has its header stripped.

Failures: the same translation as `gemini/stt.py` (`translate_error`): 401/403 and invalid keys → `AuthFailed`;
429 / `RESOURCE_EXHAUSTED` → `RateLimited` or `QuotaExhausted`; timeouts → `CallTimedOut`; 5xx and network errors →
retryable `ProviderUnavailable`. An `error` event inside the stream, or a stream that ends without audio, is a
retryable `ProviderUnavailable`. Only `core.retry` retries, and only before the first audio byte.
"""

import asyncio
import base64
import binascii
import time
from collections.abc import AsyncGenerator, AsyncIterator
from pathlib import Path
from typing import Any, Final

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai._gaos.lib import compat_errors as interaction_errors  # the Interactions API error classes

from yordamchi.adapters.gemini.stt import (
    PROVIDER,
    ClientFactory,
    clean_model_id,
    default_client,
    status_of,
    translate_error,
)
from yordamchi.adapters.speech_common import ApiKey, resolve_key
from yordamchi.adapters.tts_common import (
    GEMINI_PCM_RATE,
    MP3_INPUT,
    TTS_OPERATION,
    VOICE_MIME,
    AudioInput,
    EncodedVoice,
    OpenedAudio,
    pcm_input,
    require_ffmpeg,
    split_wav,
    stream_once,
    style_text,
    voice_note,
)
from yordamchi.core.errors import CallTimedOut, ExternalError, ProviderUnavailable
from yordamchi.core.log import get_log
from yordamchi.core.retry import retrying
from yordamchi.ports.speech import SpeechAudio

log = get_log(__name__)

_VENDOR_ERRORS: Final = (genai_errors.APIError, interaction_errors.APIError, httpx.HTTPError)
_PCM_TYPES: Final = frozenset({"audio/l16", "audio/pcm"})
_WAV_TYPES: Final = frozenset({"audio/wav", "audio/x-wav", "audio/wave"})
_MP3_TYPES: Final = frozenset({"audio/mp3", "audio/mpeg"})


def build_request(text: str, *, voice: str, style: str, language_code: str, model: str) -> dict[str, Any]:
    """The `interactions.create` keyword arguments for one streamed segment."""
    part: dict[str, Any] = {"type": "text", "text": text}
    instruction = style_text(style, language_code)
    if instruction:
        part["annotations"] = [{"type": "speech_metadata", "style": instruction}]
    return {
        "model": clean_model_id(model),
        "input": [part],
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
        "store": False,
        "stream": True,
    }


def _audio_delta(event: Any) -> Any:
    """The audio delta of a `step.delta` event, else None."""
    if getattr(event, "event_type", None) != "step.delta":
        return None
    delta = getattr(event, "delta", None)
    return delta if getattr(delta, "type", None) == "audio" else None


def _decoded(delta: Any) -> bytes:
    data = getattr(delta, "data", None)
    if not isinstance(data, str) or not data:
        return b""
    try:
        return base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ProviderUnavailable(provider=PROVIDER, retryable=False) from exc


def _source_of(delta: Any, audio: bytes) -> tuple[AudioInput, bytes]:
    """The encoder's input format from the first audio delta (and that delta's bytes, a WAV header removed)."""
    mime = str(getattr(delta, "mime_type", None) or "audio/l16").split(";", 1)[0].strip().lower()
    rate = getattr(delta, "sample_rate", None)
    channels = getattr(delta, "channels", None)
    if mime in _PCM_TYPES:
        sample_rate = rate if isinstance(rate, int) and rate > 0 else GEMINI_PCM_RATE
        return pcm_input(sample_rate, channels if isinstance(channels, int) and channels > 0 else 1), audio
    if mime in _WAV_TYPES:
        return split_wav(audio)
    if mime in _MP3_TYPES:
        return MP3_INPUT, audio
    raise ProviderUnavailable(provider=PROVIDER, retryable=False)


class GeminiSynthesizer:
    """`Synthesizer` on a direct Google key. `api_key` is the key or a callable returning the vault's current one;
    `ffmpeg` is `DATA/bin/ffmpeg` (`None` or missing → `NotInstalled` before any request)."""

    def __init__(self, *, api_key: ApiKey, ffmpeg: Path | None, client_factory: ClientFactory = default_client) -> None:
        self._api_key = api_key
        self._ffmpeg = ffmpeg
        self._client_factory = client_factory
        self._client: tuple[str, genai.Client] | None = None
        self._client_lock = asyncio.Lock()

    def _new_client(self, key: str) -> genai.Client:
        client = self._client_factory(key)
        # As in gemini/stt.py: the SDK retries Interactions calls on its own; core.retry is the only retry layer.
        interactions: Any = client.aio.interactions
        interactions.sdk_configuration.retry_config = None
        return client

    async def _client_for(self, key: str) -> genai.Client:
        async with self._client_lock:
            if self._client is not None and self._client[0] == key:
                return self._client[1]
            old = self._client
            client = self._new_client(key)
            self._client = (key, client)
        if old is not None:
            await old[1].aio.aclose()
        return client

    async def aclose(self) -> None:
        async with self._client_lock:
            old, self._client = self._client, None
        if old is not None:
            await old[1].aio.aclose()

    # --- the port --------------------------------------------------------------------------------------------------
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
        try:
            async with asyncio.timeout(timeout):
                encoded = await self._note(
                    text,
                    voice=voice,
                    style=style,
                    language_code=language_code,
                    model=model,
                    segment_words=segment_words,
                    idle_timeout=timeout,
                )
        except TimeoutError as exc:
            raise CallTimedOut(operation=TTS_OPERATION) from exc
        return SpeechAudio(
            data=encoded.data,
            mime=VOICE_MIME,
            provider=PROVIDER,
            model=model,
            duration_ms=encoded.duration_ms,
            units={"characters": len(text), "requests": encoded.requests},
        )

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
        return stream_once(
            self._note(
                text,
                voice=voice,
                style=style,
                language_code=language_code,
                model=model,
                segment_words=segment_words,
                idle_timeout=idle_timeout,
            )
        )

    # --- one note --------------------------------------------------------------------------------------------------
    async def _note(
        self,
        text: str,
        *,
        voice: str,
        style: str,
        language_code: str,
        model: str,
        segment_words: int,
        idle_timeout: float,
    ) -> EncodedVoice:
        ffmpeg = require_ffmpeg(self._ffmpeg)
        started = time.monotonic()
        key = await resolve_key(self._api_key, provider=PROVIDER)
        model_id = clean_model_id(model)
        attempts = 0

        async def open_segment(segment: str) -> OpenedAudio:
            async def attempt() -> OpenedAudio:
                nonlocal attempts
                attempts += 1
                client = await self._client_for(key)
                request = build_request(segment, voice=voice, style=style, language_code=language_code, model=model)
                return await self._open(client, request, model=model_id, idle_timeout=idle_timeout)

            return await retrying(attempt, timeout=idle_timeout, operation=TTS_OPERATION)

        try:
            encoded = await voice_note(
                ffmpeg=ffmpeg,
                text=text,
                segment_words=segment_words,
                open_segment=open_segment,
                idle_timeout=idle_timeout,
                provider=PROVIDER,
            )
        except ExternalError as error:
            log.warning(
                "tts_call_failed",
                provider=PROVIDER,
                model=model_id,
                code=error.code.value,
                retryable=error.retryable,
                attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        log.info(
            "tts_call_finished",
            provider=PROVIDER,
            model=model_id,
            requests=encoded.requests,
            attempts=attempts,
            chars=len(text),
            audio_ms=encoded.duration_ms,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return encoded

    async def _open(
        self, client: genai.Client, request: dict[str, Any], *, model: str, idle_timeout: float
    ) -> OpenedAudio:
        interactions: Any = client.aio.interactions
        started = time.monotonic()
        try:
            stream = await interactions.create(**request, timeout=idle_timeout)
        except _VENDOR_ERRORS as exc:
            error = translate_error(exc, operation=TTS_OPERATION)
            log.info(
                "tts_attempt_failed",
                provider=PROVIDER,
                model=model,
                status=status_of(exc),
                code=error.code.value,
                retryable=error.retryable,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise error from exc
        events = _audio_events(stream)
        opened = False
        try:
            delta, first = await _first_audio(events)
            source, first = _source_of(delta, first)
            opened = True
        finally:
            if not opened:
                await _close(events, stream)
        log.info(
            "tts_audio_started",
            provider=PROVIDER,
            model=model,
            first_byte_ms=int((time.monotonic() - started) * 1000),
        )

        async def rest() -> AsyncGenerator[bytes]:
            async for delta in events:
                audio = _decoded(delta)
                if audio:
                    yield audio

        remaining = rest()

        async def aclose() -> None:
            await remaining.aclose()
            await _close(events, stream)

        return OpenedAudio(source=source, first=first, rest=remaining, aclose=aclose)


async def _audio_events(stream: Any) -> AsyncGenerator[Any]:
    """The stream's audio deltas; an `error` event or a failed interaction is a provider failure."""
    try:
        async for event in stream:
            kind = getattr(event, "event_type", None)
            if kind == "error":
                raise ProviderUnavailable(provider=PROVIDER)
            if kind == "interaction.status_update" and getattr(event, "status", None) == "failed":
                raise ProviderUnavailable(provider=PROVIDER)
            delta = _audio_delta(event)
            if delta is not None:
                yield delta
    except _VENDOR_ERRORS as exc:
        raise translate_error(exc, operation=TTS_OPERATION) from exc


async def _first_audio(events: AsyncGenerator[Any]) -> tuple[Any, bytes]:
    async for delta in events:
        audio = _decoded(delta)
        if audio:
            return delta, audio
    raise ProviderUnavailable(provider=PROVIDER)  # the stream ended without audio


async def _close(events: AsyncGenerator[Any], stream: Any) -> None:
    await events.aclose()
    close = getattr(stream, "close", None)
    if close is not None:
        try:
            await close()
        except _VENDOR_ERRORS:
            log.info("tts_stream_close_failed", provider=PROVIDER)
