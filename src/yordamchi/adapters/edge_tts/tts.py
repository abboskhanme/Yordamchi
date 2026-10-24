"""`EdgeSynthesizer`: the free TTS fallback through Microsoft Edge's read-aloud voices (edge-tts 7.3.1; R-40; SPEC
§3.2.11 step 3). Third under `speech.tts_provider = "auto"`, after OpenRouter and Google; it needs no key.

`edge_tts.Communicate(text, voice, connect_timeout=, receive_timeout=)` streams `{"type": "audio", "data": …}`
chunks of MP3 (`audio-24khz-48kbitrate-mono-mp3`) over a websocket; they are piped into ffmpeg as they arrive. The
voice comes per call (`tts.edge_voice` = `uz-UZ-MadinaNeural`, or `tts.edge_male_voice` for a male voice). Edge has
no style, language or model parameter: those arguments are ignored.

Failures (edge-tts `exceptions.py` and its aiohttp transport): a bad voice name (`ValueError`) or `NoAudioReceived` →
non-retryable `ProviderUnavailable`; `WebSocketError`, `UnexpectedResponse`, `UnknownResponse`, `SkewAdjustmentError`
and network errors → retryable `ProviderUnavailable`; an HTTP 429 at the handshake → `RateLimited`; timeouts →
`CallTimedOut`. aiohttp is confined to `adapters.openrouter` (C5), so its errors are recognised by their module and
`status`, not imported. Anything else is not ours to translate and propagates unchanged.
"""

import asyncio
import math
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from pathlib import Path
from typing import Final, Protocol

import edge_tts
from edge_tts import exceptions as edge_errors

from yordamchi.adapters.tts_common import (
    MP3_INPUT,
    TTS_OPERATION,
    VOICE_MIME,
    EncodedVoice,
    OpenedAudio,
    require_ffmpeg,
    stream_once,
    voice_note,
)
from yordamchi.core.errors import CallTimedOut, ExternalError, ProviderUnavailable, RateLimited
from yordamchi.core.log import get_log
from yordamchi.core.retry import retrying
from yordamchi.ports.speech import SpeechAudio

log = get_log(__name__)

PROVIDER: Final = "edge"
_FATAL: Final = (edge_errors.NoAudioReceived, ValueError)
_TRANSIENT: Final = (
    edge_errors.WebSocketError,
    edge_errors.UnexpectedResponse,
    edge_errors.UnknownResponse,
    edge_errors.SkewAdjustmentError,
    OSError,
)


class Communicator(Protocol):
    """What the adapter uses of `edge_tts.Communicate`."""

    def stream(self) -> AsyncIterator[Mapping[str, object]]: ...


type CommunicateFactory = Callable[[str, str, int, int], Communicator]


def default_communicate(text: str, voice: str, connect_timeout_s: int, receive_timeout_s: int) -> Communicator:
    communicate: Communicator = edge_tts.Communicate(
        text, voice, connect_timeout=connect_timeout_s, receive_timeout=receive_timeout_s
    )
    return communicate


def _transport_error(exc: Exception) -> ExternalError | None:
    """An aiohttp error (by module and `status`), translated; None for anything else."""
    if type(exc).__module__.split(".", 1)[0] != "aiohttp":
        return None
    status = getattr(exc, "status", None)
    if status == 429:
        return RateLimited(provider=PROVIDER, retry_after=None)
    if isinstance(status, int) and 400 <= status < 500 and status not in {403, 408}:
        return ProviderUnavailable(provider=PROVIDER, retryable=False)
    return ProviderUnavailable(provider=PROVIDER)  # 403 (clock skew, DRM token), 5xx, a dropped connection


def translate_error(exc: Exception) -> ExternalError | None:
    """Map an edge-tts or transport exception to the `core.errors` hierarchy; None when it is neither."""
    if isinstance(exc, ExternalError):
        return exc
    if isinstance(exc, TimeoutError):
        return CallTimedOut(operation=TTS_OPERATION)
    if isinstance(exc, _FATAL):
        return ProviderUnavailable(provider=PROVIDER, retryable=False)
    if isinstance(exc, _TRANSIENT):
        return ProviderUnavailable(provider=PROVIDER)
    return _transport_error(exc)


def _seconds(timeout: float) -> int:
    return max(1, math.ceil(timeout))


class EdgeSynthesizer:
    """`Synthesizer` on Edge's free voices. `ffmpeg` is `DATA/bin/ffmpeg` (`None` or missing → `NotInstalled`
    before any request); `communicate` builds the edge-tts client (tests pass a scripted one)."""

    def __init__(self, *, ffmpeg: Path | None, communicate: CommunicateFactory = default_communicate) -> None:
        self._ffmpeg = ffmpeg
        self._communicate = communicate

    async def aclose(self) -> None:
        """Nothing stays open between calls; here for the same shutdown wiring as the other adapters."""

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
        del style, language_code  # Edge has neither; the voice name carries the language
        try:
            async with asyncio.timeout(timeout):
                encoded = await self._note(text, voice=voice, segment_words=segment_words, idle_timeout=timeout)
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
        del style, language_code, model
        return stream_once(self._note(text, voice=voice, segment_words=segment_words, idle_timeout=idle_timeout))

    async def _note(self, text: str, *, voice: str, segment_words: int, idle_timeout: float) -> EncodedVoice:
        ffmpeg = require_ffmpeg(self._ffmpeg)
        started = time.monotonic()
        attempts = 0

        async def open_segment(segment: str) -> OpenedAudio:
            async def attempt() -> OpenedAudio:
                nonlocal attempts
                attempts += 1
                return await self._open(segment, voice=voice, idle_timeout=idle_timeout)

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
                code=error.code.value,
                retryable=error.retryable,
                attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        log.info(
            "tts_call_finished",
            provider=PROVIDER,
            requests=encoded.requests,
            attempts=attempts,
            chars=len(text),
            audio_ms=encoded.duration_ms,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return encoded

    async def _open(self, text: str, *, voice: str, idle_timeout: float) -> OpenedAudio:
        started = time.monotonic()
        seconds = _seconds(idle_timeout)
        try:
            communicator = self._communicate(text, voice, seconds, seconds)
        except Exception as exc:
            error = translate_error(exc)
            if error is None:
                raise
            raise error from exc
        audio = _audio_of(communicator)
        opened = False
        try:
            try:
                first = await anext(audio)
            except StopAsyncIteration:
                raise ProviderUnavailable(provider=PROVIDER, retryable=False) from None
            opened = True
        except ExternalError as error:
            log.info(
                "tts_attempt_failed",
                provider=PROVIDER,
                code=error.code.value,
                retryable=error.retryable,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        finally:
            if not opened:
                await audio.aclose()
        log.info("tts_audio_started", provider=PROVIDER, first_byte_ms=int((time.monotonic() - started) * 1000))
        return OpenedAudio(source=MP3_INPUT, first=first, rest=audio, aclose=audio.aclose)


async def _audio_of(communicator: Communicator) -> AsyncGenerator[bytes]:
    """The MP3 bytes of the stream (boundary metadata skipped), every failure translated."""
    try:
        async for chunk in communicator.stream():
            if chunk.get("type") == "audio":
                data = chunk.get("data")
                if isinstance(data, bytes) and data:
                    yield data
    except Exception as exc:
        error = translate_error(exc)
        if error is None:
            raise
        raise error from exc
