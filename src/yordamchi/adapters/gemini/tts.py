"""`GeminiSynthesizer`: text to a voice note on a direct Google key (UC-30 voice; SPEC §3.2.11, §3.2.13). Second in
the TTS order under `auto`, used only when a Google key is in the vault (owner decision 2026-10-24).

Request shape (ai.google.dev/gemini-api/docs/speech-generation, google-genai 2.33.0 `interactions.create`; checked
2026-10-24):
- `model` = a model that is not a Live one (`gemini-3.8-flash-tts`, the fallback model), `response_format={"type":
  "audio"}`, `generation_config.speech_config=[{"voice": <voice.name>}]`, `store: False`;
- the text goes in verbatim as a text part; the style and the language go in its `speech_metadata` annotation
  (`style`), because Gemini 3.8 TTS reads the text as a transcript and the models detect the language themselves;
- `stream=True`: `step.delta` events carry base64 audio, headerless 16-bit PCM at 24 kHz mono (`audio/l16`), which is
  piped into ffmpeg as it arrives. A delta that says `audio/wav` has its header stripped.

A Live model (`gemini-3.8-live`, the default since 2026-11-01: no daily request cap) goes through `live_tts` instead,
one Live session per segment (`provider_path = "live"`). Its PCM takes the same encode path.

The call's `fallback_model` (`tts.google_fallback_model`, default `gemini-3.8-flash-tts`) gets one try when the model
fails for any reason but a bad key or a missing or failing ffmpeg, with its own `timeout`; then the caller's provider
chain (Edge) takes over as before.

Failures: the same translation as `gemini/stt.py` (`translate_error`): 401/403 and invalid keys → `AuthFailed`;
429 / `RESOURCE_EXHAUSTED` → `RateLimited` or `QuotaExhausted`; timeouts → `CallTimedOut`; 5xx and network errors →
retryable `ProviderUnavailable`. An `error` event inside the stream, or a stream that ends without audio, is a
retryable `ProviderUnavailable`. Only `core.retry` retries, and only before the first audio byte.
"""

import asyncio
import base64
import binascii
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable
from pathlib import Path
from typing import Any, Final

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai._gaos.lib import compat_errors as interaction_errors  # the Interactions API error classes

from yordamchi.adapters.gemini import live_tts
from yordamchi.adapters.gemini.live_stt import PATH_LIVE, is_live_model
from yordamchi.adapters.gemini.stt import (
    PATH_INTERACTIONS,
    PROVIDER,
    ClientFactory,
    clean_model_id,
    default_client,
    status_of,
    translate_error,
)
from yordamchi.adapters.speech_common import ApiKey, resolve_key
from yordamchi.adapters.tts_common import (
    FFMPEG,
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
from yordamchi.core.errors import AuthFailed, CallTimedOut, ExternalError, NotInstalled, ProviderUnavailable
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


def falls_back(error: ExternalError, *, model: str, fallback_model: str) -> bool:
    """Whether `fallback_model` gets a try after `model` failed with `error`: not after a bad key (the same key fails
    again), a missing ffmpeg or a failed encode (every model's audio goes through it), nor to the same model."""
    fallback = fallback_model.strip()
    if not fallback or clean_model_id(fallback) == clean_model_id(model):
        return False
    if isinstance(error, (AuthFailed, NotInstalled)):
        return False
    return error.params.get("provider") != FFMPEG


class GeminiSynthesizer:
    """`Synthesizer` on a direct Google key. `api_key` is the key or a callable returning the vault's current one;
    `ffmpeg` is `DATA/bin/ffmpeg` (`None` or missing → `NotInstalled` before any request). `live_instruction` is the
    read-aloud system prompt of a Live model (`prompts/personal/voice_live_system.md`, from the bot's wiring)."""

    def __init__(
        self,
        *,
        api_key: ApiKey,
        ffmpeg: Path | None,
        client_factory: ClientFactory = default_client,
        live_instruction: str = "",
    ) -> None:
        self._api_key = api_key
        self._ffmpeg = ffmpeg
        self._client_factory = client_factory
        self._live_instruction = live_instruction
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
        fallback_model: str = "",
    ) -> SpeechAudio:
        encoded, used = await self._spoken(
            text,
            voice=voice,
            style=style,
            language_code=language_code,
            model=model,
            fallback_model=fallback_model,
            segment_words=segment_words,
            idle_timeout=timeout,
            budget=timeout,
        )
        return SpeechAudio(
            data=encoded.data,
            mime=VOICE_MIME,
            provider=PROVIDER,
            model=used,
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
        fallback_model: str = "",
    ) -> AsyncIterator[bytes]:
        return stream_once(
            self._note_of(
                self._spoken(
                    text,
                    voice=voice,
                    style=style,
                    language_code=language_code,
                    model=model,
                    fallback_model=fallback_model,
                    segment_words=segment_words,
                    idle_timeout=idle_timeout,
                    budget=None,
                )
            )
        )

    @staticmethod
    async def _note_of(spoken: Awaitable[tuple[EncodedVoice, str]]) -> EncodedVoice:
        encoded, _ = await spoken
        return encoded

    # --- the model, then the fallback model --------------------------------------------------------------------------
    async def _spoken(
        self,
        text: str,
        *,
        voice: str,
        style: str,
        language_code: str,
        model: str,
        fallback_model: str,
        segment_words: int,
        idle_timeout: float,
        budget: float | None,
    ) -> tuple[EncodedVoice, str]:
        """The note and the model that made it. Each model gets the whole `budget` (None: no total bound)."""

        async def with_model(name: str) -> EncodedVoice:
            note = self._note(
                text,
                voice=voice,
                style=style,
                language_code=language_code,
                model=name,
                segment_words=segment_words,
                idle_timeout=idle_timeout,
            )
            if budget is None:
                return await note
            try:
                async with asyncio.timeout(budget):
                    return await note
            except TimeoutError as exc:
                raise CallTimedOut(operation=TTS_OPERATION) from exc

        try:
            return await with_model(model), model
        except ExternalError as error:
            if not falls_back(error, model=model, fallback_model=fallback_model):
                raise
            log.warning(
                "tts_model_fell_back",
                provider=PROVIDER,
                model=clean_model_id(model),
                fallback_model=clean_model_id(fallback_model),
                code=error.code.value,
            )
        return await with_model(fallback_model.strip()), fallback_model.strip()

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
        live = is_live_model(model_id)
        path = PATH_LIVE if live else PATH_INTERACTIONS
        instruction = live_tts.live_instruction(self._live_instruction, style=style, language_code=language_code)
        attempts = 0

        async def open_segment(segment: str) -> OpenedAudio:
            async def attempt() -> OpenedAudio:
                nonlocal attempts
                attempts += 1
                client = await self._client_for(key)
                if live:
                    return await live_tts.open_live(
                        client, segment, model=model_id, voice=voice, instruction=instruction, idle_timeout=idle_timeout
                    )
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
                provider_path=path,
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
            provider_path=path,
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
                provider_path=PATH_INTERACTIONS,
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
            provider_path=PATH_INTERACTIONS,
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
