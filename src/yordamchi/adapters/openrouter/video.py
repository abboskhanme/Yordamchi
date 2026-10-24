"""`OpenRouterMediaAnalyzer`: video and YouTube understanding on an OpenRouter key (UC-31; SPEC §3.2.11 step 5).

Plain HTTP via aiohttp, `POST /api/v1/chat/completions` (openrouter.ai/docs/guides/overview/multimodal/videos and
…/guides/routing/provider-selection, checked 2026-10-24):
- `system` = the caller's instruction; `user` = the prompt text plus a `video_url` part, `{"type": "video_url",
  "video_url": {"url": …}}`. The URL is either a YouTube watch URL or a base64 data URL of the file
  (`data:video/mp4;base64,…`); OpenRouter documents `video/mp4`, `video/mpeg`, `video/mov` and `video/webm`;
- YouTube links work only on Gemini's AI Studio route (Vertex takes no video URLs), so a URL request pins
  `provider: {"order": ["google-ai-studio"], "allow_fallbacks": false}`; a data URL may go to any Gemini endpoint;
- `reasoning: {effort: "low", exclude: true}` for latency (UC-31: within 60 s), dropped for a model that rejects it;
- `usage.cost` (USD) becomes `MediaAnalysis.cost_usd_micros`, the token counts its `units`.

Failure mapping: `openrouter/stt.translate_status` (401/403 → `AuthFailed`, moderation 403 aside; 402 →
`QuotaExhausted`, or `RateLimited` for the in-flight budget; 429 → `RateLimited(Retry-After)`, `QuotaExhausted` for the
free daily cap; 408 and client timeouts → `CallTimedOut`; 5xx and network errors → retryable `ProviderUnavailable`),
a 200 that carries an `error` mapped by its code, plus a 400/404 about a YouTube link that cannot be fetched →
`WebContentUnavailable("youtube.com")` and 413 → `WebContentTooLarge`.
"""

import asyncio
import base64
import json
import mimetypes
import time
from collections.abc import Mapping
from typing import Any, Final

import aiohttp

from yordamchi.adapters.openrouter.stt import BASE_URL, PROVIDER, chat_text, translate_status, usage_of
from yordamchi.adapters.speech_common import ApiKey, resolve_key
from yordamchi.core.errors import (
    CallTimedOut,
    ExternalError,
    ProviderUnavailable,
    WebContentTooLarge,
    WebContentUnavailable,
)
from yordamchi.core.log import get_log
from yordamchi.core.retry import retrying
from yordamchi.ports.media import MediaAnalysis, VideoSource

log = get_log(__name__)

OPERATION: Final = "video"
YOUTUBE_SITE: Final = "youtube.com"
YOUTUBE_ROUTE: Final = "google-ai-studio"
MIB: Final = 1024 * 1024
# OpenRouter states no request cap; a data URL of a Telegram video (≤ 20 MB) is ~27 MB. This guards the adapter alone.
DATA_URL_MAX_BYTES: Final = 50 * MIB
DEFAULT_MIME: Final = "video/mp4"
_MIME_ALIASES: Final = {"video/quicktime": "video/mov", "video/mpg": "video/mpeg"}
_REASONING: Final = {"effort": "low", "exclude": True}
_URL_REFUSALS: Final = ("youtube", "video url", "fetch", "private", "unavailable", "not accessible")


def video_mime(source: VideoSource) -> str:
    declared = (source.mime or "").split(";", 1)[0].strip().lower()
    if not declared.startswith("video/") and source.path is not None:
        declared = (mimetypes.guess_type(source.path.name)[0] or "").lower()
    if not declared.startswith("video/"):
        return DEFAULT_MIME
    return _MIME_ALIASES.get(declared, declared)


def video_url(source: VideoSource, data: bytes | None) -> str:
    if source.url is not None:
        return source.url
    if data is None:
        msg = "a file source needs its bytes"
        raise ValueError(msg)
    return f"data:{video_mime(source)};base64,{base64.b64encode(data).decode('ascii')}"


def chat_payload(
    source: VideoSource, data: bytes | None, *, instruction: str, prompt: str, model: str, reasoning: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": instruction},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "video_url", "video_url": {"url": video_url(source, data)}},
                ],
            },
        ],
        "stream": False,
    }
    if source.url is not None:
        payload["provider"] = {"order": [YOUTUBE_ROUTE], "allow_fallbacks": False}
    if reasoning:
        payload["reasoning"] = dict(_REASONING)
    return payload


def _message(body: object) -> str:
    error = body.get("error") if isinstance(body, dict) else None
    return str(error.get("message", "")).lower() if isinstance(error, dict) else ""


def translate_video_status(status: int, body: object, headers: Mapping[str, str], *, from_url: bool) -> ExternalError:
    if status == 413:
        return WebContentTooLarge(limit_mb=DATA_URL_MAX_BYTES // MIB)
    if from_url and status in {400, 404, 422} and any(word in _message(body) for word in _URL_REFUSALS):
        return WebContentUnavailable(site=YOUTUBE_SITE)
    return translate_status(status, body, headers, operation=OPERATION)


class OpenRouterMediaAnalyzer:
    """`MediaAnalyzer` on an OpenRouter key. `api_key` is the key or a callable returning the vault's current one.

    `session` lets the caller share one aiohttp session; otherwise the adapter opens its own on first use and
    `aclose()` closes it.
    """

    def __init__(
        self, *, api_key: ApiKey, base_url: str = BASE_URL, session: aiohttp.ClientSession | None = None
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._session_lock = asyncio.Lock()
        self._reasoning: dict[str, bool] = {}  # per model: False once it rejected `reasoning`

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

    async def analyze_video(
        self, source: VideoSource, *, instruction: str, prompt: str, model: str, timeout: float
    ) -> MediaAnalysis:
        started = time.monotonic()
        kind = "url" if source.url is not None else "file"
        attempts = 0
        try:
            key = await resolve_key(self._api_key, provider=PROVIDER)
            data = await self._file_bytes(source)

            async def attempt() -> MediaAnalysis:
                nonlocal attempts
                attempts += 1
                return await self._chat(
                    key, source, data, instruction=instruction, prompt=prompt, model=model, timeout=timeout
                )

            analysis = await retrying(attempt, timeout=timeout, operation=OPERATION)
        except ExternalError as error:
            log.warning(
                "video_call_failed",
                provider=PROVIDER,
                model=model,
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
            model=model,
            cost_usd_micros=analysis.cost_usd_micros,
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
            if size > DATA_URL_MAX_BYTES:
                raise WebContentTooLarge(limit_mb=DATA_URL_MAX_BYTES // MIB)
            return await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise ProviderUnavailable(provider=PROVIDER, retryable=False) from exc

    async def _request(
        self, payload: Mapping[str, Any], *, key: str, timeout: float
    ) -> tuple[int, dict[str, str], object]:
        """One HTTP exchange: status, lower-cased headers and the parsed JSON body (`None` if it is not JSON)."""
        session = await self._http()
        try:
            async with session.post(
                f"{self._base_url}/chat/completions",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                raw = await response.read()
                status = response.status
                headers = {name.lower(): value for name, value in response.headers.items()}
        except TimeoutError as exc:
            raise CallTimedOut(operation=OPERATION) from exc
        except aiohttp.ClientError as exc:
            raise ProviderUnavailable(provider=PROVIDER) from exc
        try:
            body: object = json.loads(raw) if raw else None
        except ValueError:
            body = None
        return status, headers, body

    async def _chat(
        self,
        key: str,
        source: VideoSource,
        data: bytes | None,
        *,
        instruction: str,
        prompt: str,
        model: str,
        timeout: float,
    ) -> MediaAnalysis:
        from_url = source.url is not None
        while True:
            reasoning = self._reasoning.get(model, True)
            payload = chat_payload(
                source, data, instruction=instruction, prompt=prompt, model=model, reasoning=reasoning
            )
            started = time.monotonic()
            status, headers, body = await self._request(payload, key=key, timeout=timeout)
            duration_ms = int((time.monotonic() - started) * 1000)
            if status != 200:
                message = _message(body)
                if status == 400 and reasoning and ("reasoning" in message or "thinking" in message):
                    self._reasoning[model] = False
                    log.info("video_reasoning_rejected", provider=PROVIDER, model=model)
                    continue
                error = translate_video_status(status, body, headers, from_url=from_url)
                log.info(
                    "video_attempt_failed",
                    provider=PROVIDER,
                    model=model,
                    status=status,
                    code=error.code.value,
                    retryable=error.retryable,
                    duration_ms=duration_ms,
                )
                raise error
            try:
                text = chat_text(body, headers).strip()
            except CallTimedOut as exc:  # a 200 carrying a 408: name this operation, not STT's
                raise CallTimedOut(operation=OPERATION) from exc
            cost, units = usage_of(body)
            request_id = body.get("id") if isinstance(body, dict) else None
            log.info(
                "video_call_finished",
                provider=PROVIDER,
                model=model,
                request_id=request_id if isinstance(request_id, str) else headers.get("x-generation-id"),
                video_bytes=len(data or b""),
                chars=len(text),
                input_tokens=units.get("input_tokens"),
                duration_ms=duration_ms,
            )
            return MediaAnalysis(text=text, provider=PROVIDER, model=model, cost_usd_micros=cost or 0, units=units)
