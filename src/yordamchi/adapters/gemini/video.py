"""`GeminiMediaAnalyzer`: video and YouTube understanding through the Gemini Interactions API (UC-31; SPEC §3.2.14).

Request shape (ai.google.dev/gemini-api/docs/video-understanding and …/file-input-methods, checked 2026-10-24;
google-genai 2.33.0, `VideoContentParam`):
- `input` = a `video` part, then the `text` prompt. A YouTube link goes as `{"type": "video", "uri": <watch URL>}`
  (public videos only; preview; free of charge for now); a file goes inline as `{"type": "video", "data": <base64>,
  "mime_type": …}`. Inline payloads may be up to 100 MB, so Telegram's ≤ 20 MB videos never need the Files API (no
  upload, no polling, nothing left on Google's side). `INLINE_MAX_BYTES` guards the adapter on its own anyway;
- `system_instruction` = the caller's instruction (what to produce; the video is data), `store: False`;
- the default static processing (1 frame per second, ~100 tokens per second of video at the default resolution);
- `generation_config.thinking_level = "low"` for latency (UC-31: result within 60 s), dropped for a model that rejects
  the level, and not asked again for that model.

Failure mapping: the Google error model as `gemini/stt.translate_error` reads it (401/403/`API_KEY_INVALID` →
`AuthFailed`; 429 → `RateLimited(retry_after)` or `QuotaExhausted` for a per-day or zero quota; 408/504 and client
timeouts → `CallTimedOut`; 5xx and network errors → retryable `ProviderUnavailable`), plus two video cases: a 400
about a YouTube link (private, removed, age-gated, not fetchable) → `WebContentUnavailable("youtube.com")`, and 413 →
`WebContentTooLarge`. Only `core.retry` retries; the SDK's own retries are switched off.
"""

import asyncio
import base64
import mimetypes
import time
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
    is_thinking_rejection,
    status_of,
    translate_error,
)
from yordamchi.adapters.speech_common import ApiKey, resolve_key
from yordamchi.core.errors import ExternalError, ProviderUnavailable, WebContentTooLarge, WebContentUnavailable
from yordamchi.core.log import get_log
from yordamchi.core.retry import retrying
from yordamchi.ports.media import MediaAnalysis, VideoSource

log = get_log(__name__)

OPERATION: Final = "video"
YOUTUBE_SITE: Final = "youtube.com"
MIB: Final = 1024 * 1024
# Inline data may be 100 MB per request; base64 adds a third, so the raw file stays well under it.
INLINE_MAX_BYTES: Final = 70 * MIB
THINKING_LEVEL: Final = "low"
DEFAULT_MIME: Final = "video/mp4"
# Telegram and macOS names → the names the Interactions API lists (`VideoContentMimeType`).
_MIME_ALIASES: Final = {
    "video/quicktime": "video/mov",
    "video/x-msvideo": "video/avi",
    "video/x-ms-wmv": "video/wmv",
}
_URL_REFUSALS: Final = ("url", "youtube", "private", "unavailable", "not accessible", "cannot fetch", "fetch")
_VENDOR_ERRORS: Final = (genai_errors.APIError, interaction_errors.APIError, httpx.HTTPError)


def video_mime(source: VideoSource) -> str:
    """The file's type in the API's spelling: the declared one, else a guess from the file name, else MP4."""
    declared = (source.mime or "").split(";", 1)[0].strip().lower()
    if not declared.startswith("video/") and source.path is not None:
        declared = (mimetypes.guess_type(source.path.name)[0] or "").lower()
    if not declared.startswith("video/"):
        return DEFAULT_MIME
    return _MIME_ALIASES.get(declared, declared)


def video_part(source: VideoSource, data: bytes | None) -> dict[str, Any]:
    if source.url is not None:
        return {"type": "video", "uri": source.url}
    if data is None:
        msg = "a file source needs its bytes"
        raise ValueError(msg)
    return {"type": "video", "data": base64.b64encode(data).decode("ascii"), "mime_type": video_mime(source)}


def build_request(
    source: VideoSource, data: bytes | None, *, instruction: str, prompt: str, model: str, thinking: str | None
) -> dict[str, Any]:
    """The `interactions.create` keyword arguments for one reading of the video."""
    request: dict[str, Any] = {
        "model": clean_model_id(model),
        "input": [video_part(source, data), {"type": "text", "text": prompt}],
        "system_instruction": instruction,
        "store": False,
    }
    if thinking:
        request["generation_config"] = {"thinking_level": thinking}
    return request


def _message(exc: BaseException) -> str:
    body: object = exc.details if isinstance(exc, genai_errors.APIError) else getattr(exc, "body", None)
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    error = body.get("error") if isinstance(body, dict) else None
    return str(error.get("message", "")).lower() if isinstance(error, dict) else ""


def translate_video_error(exc: BaseException, *, from_url: bool) -> ExternalError:
    """`gemini/stt.translate_error` plus what only a video request can hit."""
    status = status_of(exc)
    if status == 413:
        return WebContentTooLarge(limit_mb=INLINE_MAX_BYTES // MIB)
    message = _message(exc)
    # A private, removed or age-gated video; a 403 that names the key stays an auth failure below.
    if (
        from_url
        and status in {400, 403, 404}
        and any(word in message for word in _URL_REFUSALS)
        and "api key" not in message
    ):
        return WebContentUnavailable(site=YOUTUBE_SITE)
    return translate_error(exc, operation=OPERATION)


def usage_units(response: Any) -> dict[str, int]:
    usage = getattr(response, "usage", None)
    units: dict[str, int] = {}
    for name, attribute in (
        ("input_tokens", "total_input_tokens"),
        ("output_tokens", "total_output_tokens"),
        ("thought_tokens", "total_thought_tokens"),
    ):
        value = getattr(usage, attribute, None)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            units[name] = value
    return units


class GeminiMediaAnalyzer:
    """`MediaAnalyzer` on a direct Google key. `api_key` is the key or a callable returning the vault's current one."""

    def __init__(self, *, api_key: ApiKey, client_factory: ClientFactory = default_client) -> None:
        self._api_key = api_key
        self._client_factory = client_factory
        self._client: tuple[str, genai.Client] | None = None
        self._client_lock = asyncio.Lock()
        self._thinking: dict[str, str | None] = {}  # per model: the level it accepted last (None: send none)

    def _new_client(self, key: str) -> genai.Client:
        client = self._client_factory(key)
        interactions: Any = client.aio.interactions
        interactions.sdk_configuration.retry_config = None  # core.retry is the only retry layer (CONVENTIONS §7)
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

    async def analyze_video(
        self, source: VideoSource, *, instruction: str, prompt: str, model: str, timeout: float
    ) -> MediaAnalysis:
        started = time.monotonic()
        model_id = clean_model_id(model)
        kind = "url" if source.url is not None else "file"
        attempts = 0
        try:
            key = await resolve_key(self._api_key, provider=PROVIDER)
            data = await self._file_bytes(source)

            async def attempt() -> MediaAnalysis:
                nonlocal attempts
                attempts += 1
                client = await self._client_for(key)
                return await self._create(
                    client, source, data, instruction=instruction, prompt=prompt, model=model_id, timeout=timeout
                )

            analysis = await retrying(attempt, timeout=timeout, operation=OPERATION)
        except ExternalError as error:
            log.warning(
                "video_call_failed",
                provider=PROVIDER,
                model=model_id,
                source=kind,
                code=error.code.value,
                retryable=error.retryable,
                attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        return MediaAnalysis(
            text=analysis.text,
            provider=PROVIDER,
            model=model_id,
            latency_ms=int((time.monotonic() - started) * 1000),
            units=analysis.units,
        )

    @staticmethod
    async def _file_bytes(source: VideoSource) -> bytes | None:
        if source.path is None:
            return None
        path = source.path
        try:
            size = (await asyncio.to_thread(path.stat)).st_size
            if size > INLINE_MAX_BYTES:
                raise WebContentTooLarge(limit_mb=INLINE_MAX_BYTES // MIB)
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:  # the caller's file vanished: nothing a provider can do
            raise ProviderUnavailable(provider=PROVIDER, retryable=False) from exc

    async def _create(
        self,
        client: genai.Client,
        source: VideoSource,
        data: bytes | None,
        *,
        instruction: str,
        prompt: str,
        model: str,
        timeout: float,
    ) -> MediaAnalysis:
        """One logical attempt. A rejected thinking level is retried at once without it (not a failure)."""
        interactions: Any = client.aio.interactions
        while True:
            thinking = self._thinking.get(model, THINKING_LEVEL)
            request = build_request(
                source, data, instruction=instruction, prompt=prompt, model=model, thinking=thinking
            )
            started = time.monotonic()
            try:
                response = await interactions.create(**request, timeout=timeout)
            except _VENDOR_ERRORS as exc:
                if thinking and is_thinking_rejection(exc):
                    self._thinking[model] = None
                    log.info("video_thinking_level_rejected", provider=PROVIDER, model=model)
                    continue
                error = translate_video_error(exc, from_url=source.url is not None)
                log.info(
                    "video_attempt_failed",
                    provider=PROVIDER,
                    model=model,
                    status=status_of(exc),
                    code=error.code.value,
                    retryable=error.retryable,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise error from exc
            return self._answer(response, model=model, started=started, data_bytes=len(data or b""))

    @staticmethod
    def _answer(response: Any, *, model: str, started: float, data_bytes: int) -> MediaAnalysis:
        if getattr(response, "status", None) == "failed":
            log.info("video_attempt_failed", provider=PROVIDER, model=model, status=200, code="interaction_failed")
            raise ProviderUnavailable(provider=PROVIDER)
        raw = getattr(response, "output_text", None)
        text = raw.strip() if isinstance(raw, str) else ""
        request_id = getattr(response, "id", None)
        units = usage_units(response)
        log.info(
            "video_call_finished",
            provider=PROVIDER,
            model=model,
            request_id=request_id if isinstance(request_id, str) else None,
            video_bytes=data_bytes,
            chars=len(text),
            input_tokens=units.get("input_tokens"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return MediaAnalysis(text=text, provider=PROVIDER, model=model, units=units)
