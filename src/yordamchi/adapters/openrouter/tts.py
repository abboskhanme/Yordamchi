"""`OpenRouterSynthesizer`: text to a voice note on an OpenRouter key (UC-30 voice; SPEC §3.2.11 step 5, §3.2.13).
The first TTS provider under `speech.tts_provider = "auto"` (owner decision 2026-10-24). Plain HTTP via aiohttp.

`POST /api/v1/audio/speech` (openrouter.ai/docs/api/api-reference/tts/create-speech and
…/guides/overview/multimodal/tts, checked 2026-10-24):
- body `{model, input, voice, response_format, instructions}`; `model` is `tts.openrouter_model` (default
  `google/gemini-3.8-flash-tts`), `instructions` carries the style and the language (Gemini 3.8 TTS reads `input`
  verbatim, so directions never go into the text);
- `response_format` is `mp3` or `pcm`, but **Gemini TTS returns only `pcm`** (16-bit little-endian; a request for
  `mp3` is a 400), so the adapter asks for `pcm`. Gemini's PCM is 24 kHz mono. The `Content-Type` header says which
  format came back (`audio/pcm`, `audio/mpeg`), and the encoder follows it;
- the 200 body is the raw audio, read as it arrives and piped into ffmpeg → OGG/Opus; there is no JSON and so no
  `usage.cost` (the price is per character: `units.characters` is recorded for T51); `X-Generation-Id` is logged.

Failure mapping as `openrouter/stt.py` (`translate_status`): 401/403 → `AuthFailed`; 402 → `QuotaExhausted`, or
`RateLimited` for the in-flight budget; 429 → `RateLimited(Retry-After)` or `QuotaExhausted` for a daily cap; 408 and
timeouts → `CallTimedOut`; 5xx (502, 503, 524, 529) and network errors → retryable `ProviderUnavailable`; other 4xx
(400 format or voice, 404, 413) → non-retryable `ProviderUnavailable`. Retries happen only before the first audio byte.
"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Final

import aiohttp

from yordamchi.adapters.openrouter.stt import BASE_URL, PROVIDER, translate_status
from yordamchi.adapters.speech_common import ApiKey, resolve_key
from yordamchi.adapters.tts_common import (
    MP3_INPUT,
    TTS_OPERATION,
    VOICE_MIME,
    AudioInput,
    EncodedVoice,
    OpenedAudio,
    pcm_input,
    require_ffmpeg,
    stream_once,
    style_text,
    voice_note,
)
from yordamchi.core.errors import CallTimedOut, ExternalError, ProviderUnavailable
from yordamchi.core.log import get_log
from yordamchi.core.retry import retrying
from yordamchi.ports.speech import SpeechAudio

log = get_log(__name__)

SPEECH_PATH: Final = "/audio/speech"
RESPONSE_FORMAT: Final = "pcm"
CHUNK_BYTES: Final = 16 * 1024


def speech_payload(text: str, *, voice: str, style: str, language_code: str, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"model": model, "input": text, "voice": voice, "response_format": RESPONSE_FORMAT}
    instructions = style_text(style, language_code)
    if instructions:
        payload["instructions"] = instructions
    return payload


def audio_input_of(content_type: str) -> AudioInput:
    """The encoder's input format from the 200's `Content-Type`; a type we cannot decode is a provider failure."""
    base = content_type.split(";", 1)[0].strip().lower()
    if base in {"audio/pcm", "audio/l16", "application/octet-stream", ""}:
        return pcm_input()
    if base in {"audio/mpeg", "audio/mp3"}:
        return MP3_INPUT
    if base in {"audio/wav", "audio/x-wav", "audio/wave"}:
        return AudioInput("wav")
    raise ProviderUnavailable(provider=PROVIDER, retryable=False)


class OpenRouterSynthesizer:
    """`Synthesizer` on an OpenRouter key. `api_key` is the key or a callable returning the vault's current one.

    `ffmpeg` is the static binary from the installer (`DATA/bin/ffmpeg`); `None` or missing → `NotInstalled` before
    any request. `session` lets the caller share one aiohttp session; otherwise the adapter opens its own on first use
    and `aclose()` closes it.
    """

    def __init__(
        self,
        *,
        api_key: ApiKey,
        ffmpeg: Path | None,
        base_url: str = BASE_URL,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._api_key = api_key
        self._ffmpeg = ffmpeg
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._session_lock = asyncio.Lock()

    async def _http(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
                self._owns_session = True
            return self._session

    async def aclose(self) -> None:
        async with self._session_lock:
            session, self._session = self._session, None
        if session is not None and self._owns_session and not session.closed:
            await session.close()

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
        attempts = 0

        async def open_segment(segment: str) -> OpenedAudio:
            async def attempt() -> OpenedAudio:
                nonlocal attempts
                attempts += 1
                payload = speech_payload(segment, voice=voice, style=style, language_code=language_code, model=model)
                return await self._open(key, payload, model=model, idle_timeout=idle_timeout)

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
                model=model,
                code=error.code.value,
                retryable=error.retryable,
                attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        log.info(
            "tts_call_finished",
            provider=PROVIDER,
            model=model,
            requests=encoded.requests,
            attempts=attempts,
            chars=len(text),
            audio_ms=encoded.duration_ms,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return encoded

    async def _open(self, key: str, payload: Mapping[str, Any], *, model: str, idle_timeout: float) -> OpenedAudio:
        """One request, returned once the first audio bytes are in; the rest of the body streams from there."""
        session = await self._http()
        started = time.monotonic()
        try:
            response = await session.post(
                f"{self._base_url}{SPEECH_PATH}",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=idle_timeout, sock_read=idle_timeout),
            )
        except TimeoutError as exc:
            raise CallTimedOut(operation=TTS_OPERATION) from exc
        except aiohttp.ClientError as exc:
            raise ProviderUnavailable(provider=PROVIDER) from exc
        opened = False
        try:
            headers = {name.lower(): value for name, value in response.headers.items()}
            content_type = headers.get("content-type", "")
            if response.status != 200 or content_type.lower().startswith("application/json"):
                body = await _json_body(response)
                status = response.status if response.status != 200 else _error_status(body)
                error = translate_status(status, body, headers, operation=TTS_OPERATION)
                log.info(
                    "tts_attempt_failed",
                    provider=PROVIDER,
                    model=model,
                    status=response.status,
                    code=error.code.value,
                    retryable=error.retryable,
                    request_id=headers.get("x-generation-id"),
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise error
            source = audio_input_of(content_type)
            rest = _body_chunks(response)
            first = await _first_chunk(rest)
            opened = True
        finally:
            if not opened:
                response.close()
        log.info(
            "tts_audio_started",
            provider=PROVIDER,
            model=model,
            request_id=headers.get("x-generation-id"),
            first_byte_ms=int((time.monotonic() - started) * 1000),
        )

        async def aclose() -> None:
            await rest.aclose()
            response.close()

        return OpenedAudio(source=source, first=first, rest=rest, aclose=aclose)


async def _json_body(response: aiohttp.ClientResponse) -> object:
    try:
        raw = await response.read()
    except TimeoutError as exc:
        raise CallTimedOut(operation=TTS_OPERATION) from exc
    except aiohttp.ClientError as exc:
        raise ProviderUnavailable(provider=PROVIDER) from exc
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _error_status(body: object) -> int:
    """A 200 that carries a JSON error instead of audio: its `error.code`, else a 502."""
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)
    return 502


async def _body_chunks(response: aiohttp.ClientResponse) -> AsyncGenerator[bytes]:
    """The audio body as it arrives; a broken or stalled body is a provider failure."""
    try:
        async for chunk in response.content.iter_chunked(CHUNK_BYTES):
            if chunk:
                yield chunk
    except TimeoutError as exc:
        raise CallTimedOut(operation=TTS_OPERATION) from exc
    except aiohttp.ClientError as exc:
        raise ProviderUnavailable(provider=PROVIDER) from exc


async def _first_chunk(rest: AsyncGenerator[bytes]) -> bytes:
    try:
        return await anext(rest)
    except StopAsyncIteration:
        raise ProviderUnavailable(provider=PROVIDER) from None  # a 200 without audio
