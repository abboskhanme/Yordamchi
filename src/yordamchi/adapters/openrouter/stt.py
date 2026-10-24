"""`OpenRouterTranscriber`: speech to text on an OpenRouter key (UC-05; SPEC §3.2.11 step 5). Plain HTTP via aiohttp.

OpenRouter's `/audio/transcriptions` takes no vocabulary and only one ISO-639-1 language, so the main path is
`POST /chat/completions` with a Gemini model (`stt.openrouter_model`, default `google/gemini-3.8-flash`):
- `system` = the transcription instruction; `user` = the request with "Expected languages: …" and "Spell these terms
  exactly: <≤ 200 terms>", plus `input_audio{data: base64 WAV 16 kHz mono, format: "wav"}`;
- `reasoning: {effort: "minimal", exclude: true}`, dropped for a model that rejects it;
- a 400/404/415/422 on that path (the model or route cannot take the audio) falls back to `/audio/transcriptions`
  with the call's `fallback_model` (`stt.openrouter_fallback_model`, default `google/gemini-3.5-transcribe`) and no
  hints; `Transcript.provider_path` says which path answered;
- `usage.cost` (USD, a JSON number) becomes `Transcript.cost_usd_micros`, and the token counts its `units`.

Telegram voice is OGG/Opus; OpenRouter's documented `ogg` input is Vorbis and support varies by provider, so ffmpeg
(`DATA/bin/ffmpeg`, through `core.proc`) converts it to WAV first, as SPEC §3.2.11 says.

Failure mapping (openrouter.ai/docs/api-reference/errors, …/limits; checked 2026-10-24): 401 → `AuthFailed`; 403 →
`AuthFailed`, unless it is a moderation flag; 402 → `QuotaExhausted`, or `RateLimited(Retry-After)` when the
in-flight budget is the cause; 429 → `RateLimited(Retry-After)`, `QuotaExhausted` for the free-model daily cap;
408 and client timeouts → `CallTimedOut`; 5xx and network errors → retryable `ProviderUnavailable`. A 200 whose body
is an `error` object, or whose choice finished with `error`, is mapped by its `error.code`.
"""

import asyncio
import base64
import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final
from urllib.parse import quote

import aiohttp

from yordamchi.adapters.speech_common import (
    STT_INSTRUCTION,
    STT_OPERATION,
    ApiKey,
    clean_transcript,
    fix_wav_header,
    is_wav,
    key_kind,
    prompt_hint,
    resolve_key,
    retry_after_s,
    wav_duration_ms,
)
from yordamchi.core import proc
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    NotInstalled,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
)
from yordamchi.core.log import get_log
from yordamchi.core.money import usd_to_micros
from yordamchi.core.retry import retrying
from yordamchi.ports.speech import SpeechKeyCheck, Transcript

log = get_log(__name__)

PROVIDER: Final = "openrouter"
KEY_KIND: Final = "openrouter_api_key"
BASE_URL: Final = "https://openrouter.ai/api/v1"
PATH_CHAT: Final = "chat_completions"
PATH_TRANSCRIPTIONS: Final = "audio_transcriptions"
KEY_CHECK_OPERATION: Final = "speech_key_check"
FALLBACK_STATUSES: Final = frozenset({400, 404, 415, 422})
FFMPEG: Final = "ffmpeg"
WAV_SAMPLE_RATE: Final = 16_000
# ffmpeg needs no inherited environment; nothing of the bot's reaches it.
FFMPEG_ENV: Final[Mapping[str, str]] = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
_REASONING: Final = {"effort": "minimal", "exclude": True}
# OpenRouter `usage` counters → `Transcript.units` names (chat completions, then /audio/transcriptions).
_UNIT_NAMES: Final = {
    "prompt_tokens": "input_tokens",
    "completion_tokens": "output_tokens",
    "input_tokens": "input_tokens",
    "output_tokens": "output_tokens",
}


class _UseTranscriptionsEndpoint(Exception):
    """The chat path refused the request in a way the hint-less endpoint may accept."""


def ffmpeg_argv(ffmpeg: Path) -> list[str]:
    """OGG/Opus (or anything ffmpeg reads) on stdin → 16 kHz mono 16-bit WAV on stdout."""
    return [
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-map_metadata",
        "-1",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(WAV_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
    ]


def chat_payload(
    wav_b64: str, *, languages: Sequence[str], vocabulary: Sequence[str], model: str, reasoning: bool
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": STT_INSTRUCTION},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_hint(languages, vocabulary)},
                    {"type": "input_audio", "input_audio": {"data": wav_b64, "format": "wav"}},
                ],
            },
        ],
        "temperature": 0,
        "stream": False,
    }
    if reasoning:
        payload["reasoning"] = dict(_REASONING)
    return payload


def transcriptions_payload(wav_b64: str, *, model: str) -> dict[str, Any]:
    return {"model": model, "input_audio": {"data": wav_b64, "format": "wav"}}


# --- responses and errors ------------------------------------------------------------------------------------------
def _error_object(body: object) -> Mapping[str, Any]:
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict):
            return error
    return {}


def _error_code(error: Mapping[str, Any]) -> int | None:
    code = error.get("code")
    if isinstance(code, bool):
        return None
    if isinstance(code, int):
        return code
    if isinstance(code, str) and code.isdigit():
        return int(code)
    return None


def translate_status(  # noqa: PLR0911  # one mapping table, read top to bottom
    status: int, body: object, headers: Mapping[str, str], *, operation: str = STT_OPERATION
) -> ExternalError:
    """Map an OpenRouter error status (or a 200 body's `error.code`) to the `core.errors` hierarchy."""
    error = _error_object(body)
    message = str(error.get("message", "")).lower()
    metadata = error.get("metadata")
    meta: Mapping[str, Any] = metadata if isinstance(metadata, dict) else {}
    retry_after = retry_after_s(headers.get("retry-after"), date=headers.get("date"))
    if status == 401:
        return AuthFailed(provider=PROVIDER)
    if status == 403:
        if "flagged_input" in meta or "reasons" in meta:
            return ProviderUnavailable(provider=PROVIDER, retryable=False)  # moderation, not the key
        return AuthFailed(provider=PROVIDER)
    if status == 402:
        if meta.get("limit_source") == "openrouter_in_flight_budget" and retry_after is not None:
            return RateLimited(provider=PROVIDER, retry_after=retry_after)
        return QuotaExhausted(provider=PROVIDER)
    if status == 408:
        return CallTimedOut(operation=operation)
    if status == 429:
        if "per-day" in message or "per day" in message:
            return QuotaExhausted(provider=PROVIDER)
        return RateLimited(provider=PROVIDER, retry_after=retry_after)
    if status >= 500:
        return ProviderUnavailable(provider=PROVIDER)
    return ProviderUnavailable(provider=PROVIDER, retryable=False)


def _content_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def chat_text(body: object, headers: Mapping[str, str]) -> str:
    """The transcript from a 200 chat completion, or the error that 200 is carrying."""
    error = _error_object(body)
    if error:
        raise translate_status(_error_code(error) or 502, body, headers)
    choices = body.get("choices") if isinstance(body, dict) else None
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ProviderUnavailable(provider=PROVIDER, retryable=False)
    choice = choices[0]
    if choice.get("finish_reason") == "error":
        inner = choice.get("error")
        wrapped = {"error": inner} if isinstance(inner, dict) else {}
        raise translate_status(_error_code(_error_object(wrapped)) or 502, wrapped, headers)
    message = choice.get("message")
    return _content_text(message.get("content") if isinstance(message, dict) else None)


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


@dataclass(frozen=True, slots=True)
class _Answer:
    """One successful exchange: the cleaned transcript and what the response said it cost."""

    text: str = field(repr=False)
    cost_usd_micros: int | None = None
    units: Mapping[str, int] = field(default_factory=dict)


def usage_of(body: object) -> tuple[int | None, dict[str, int]]:
    """`usage.cost` of a 200 body in micro-USD (None when absent or unreadable), and its token counts."""
    usage = body.get("usage") if isinstance(body, dict) else None
    if not isinstance(usage, dict):
        return None, {}
    cost = _decimal(usage.get("cost"))
    micros = usd_to_micros(cost) if cost is not None and cost.is_finite() and cost >= 0 else None
    units = {
        name: count
        for raw, name in _UNIT_NAMES.items()
        if isinstance(count := usage.get(raw), int) and not isinstance(count, bool) and count >= 0
    }
    return micros, units


class OpenRouterTranscriber:
    """`Transcriber` on an OpenRouter key. `api_key` is the key or a callable returning the vault's current one.

    `ffmpeg` is the static binary from the installer (`DATA/bin/ffmpeg`); `None` means it is missing, which only
    matters for non-WAV input. `session` lets the caller share one aiohttp session; otherwise the adapter opens its
    own on first use and `aclose()` closes it.
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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        key: str,
        timeout: float,
        operation: str,
        payload: Mapping[str, Any] | None = None,
    ) -> tuple[int, dict[str, str], object]:
        """One HTTP exchange: status, lower-cased headers and the parsed JSON body (`None` if it is not JSON)."""
        session = await self._http()
        try:
            async with session.request(
                method,
                f"{self._base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as response:
                raw = await response.read()
                status = response.status
                headers = {name.lower(): value for name, value in response.headers.items()}
        except TimeoutError as exc:
            raise CallTimedOut(operation=operation) from exc
        except aiohttp.ClientError as exc:
            raise ProviderUnavailable(provider=PROVIDER) from exc
        try:
            body: object = json.loads(raw) if raw else None
        except ValueError:
            body = None
        return status, headers, body

    async def _to_wav(self, audio: bytes, *, timeout: float) -> bytes:
        if is_wav(audio):
            return fix_wav_header(audio)
        if self._ffmpeg is None or not self._ffmpeg.is_file():
            raise NotInstalled(binary=FFMPEG)
        try:
            result = await proc.run(ffmpeg_argv(self._ffmpeg), env=FFMPEG_ENV, timeout=timeout, stdin=audio)
        except TimeoutError as exc:
            raise CallTimedOut(operation=STT_OPERATION) from exc
        except OSError as exc:
            raise NotInstalled(binary=FFMPEG) from exc
        if result.returncode != 0 or not is_wav(result.stdout):
            log.warning("stt_audio_convert_failed", returncode=result.returncode, duration_ms=result.duration_ms)
            raise ProviderUnavailable(provider=FFMPEG, retryable=False)
        return fix_wav_header(result.stdout)

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
        del mime  # the bytes decide: WAV goes as it is, anything else through ffmpeg
        del mode  # Gemini's transcription_config mode; neither OpenRouter path takes one
        started = time.monotonic()
        deadline = started + timeout
        key = await resolve_key(self._api_key, provider=PROVIDER)
        path = PATH_CHAT
        used_model = model
        attempts = 0
        try:
            wav = await self._to_wav(audio, timeout=timeout)
            wav_b64 = base64.b64encode(wav).decode("ascii")

            async def chat_attempt() -> _Answer:
                nonlocal attempts
                attempts += 1
                return await self._chat(
                    key, wav_b64, languages=languages, vocabulary=vocabulary, model=model, timeout=timeout
                )

            async def transcriptions_attempt() -> _Answer:
                nonlocal attempts
                attempts += 1
                return await self._transcriptions(key, wav_b64, model=fallback_model, timeout=timeout)

            try:
                answer = await retrying(
                    chat_attempt, timeout=_remaining(deadline, STT_OPERATION), operation=STT_OPERATION
                )
            except _UseTranscriptionsEndpoint:
                path, used_model = PATH_TRANSCRIPTIONS, fallback_model
                answer = await retrying(
                    transcriptions_attempt, timeout=_remaining(deadline, STT_OPERATION), operation=STT_OPERATION
                )
        except ExternalError as error:
            log.warning(
                "stt_call_failed",
                provider=PROVIDER,
                provider_path=path,
                model=used_model,
                code=error.code.value,
                retryable=error.retryable,
                attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        return Transcript(
            text=answer.text,
            provider=PROVIDER,
            provider_path=path,
            model=used_model,
            audio_ms=wav_duration_ms(wav),
            latency_ms=int((time.monotonic() - started) * 1000),
            cost_usd_micros=answer.cost_usd_micros,
            units=answer.units,
        )

    async def _chat(
        self,
        key: str,
        wav_b64: str,
        *,
        languages: Sequence[str],
        vocabulary: Sequence[str],
        model: str,
        timeout: float,
    ) -> _Answer:
        while True:
            reasoning = self._reasoning.get(model, True)
            payload = chat_payload(
                wav_b64, languages=languages, vocabulary=vocabulary, model=model, reasoning=reasoning
            )
            started = time.monotonic()
            status, headers, body = await self._request(
                "POST", "/chat/completions", key=key, timeout=timeout, operation=STT_OPERATION, payload=payload
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            if status != 200:
                message = str(_error_object(body).get("message", "")).lower()
                if status == 400 and reasoning and ("reasoning" in message or "thinking" in message):
                    self._reasoning[model] = False
                    log.info("stt_reasoning_rejected", provider=PROVIDER, model=model)
                    continue
                self._log_attempt_failed(PATH_CHAT, model, status, body, headers, duration_ms)
                if status in FALLBACK_STATUSES:
                    raise _UseTranscriptionsEndpoint
                raise translate_status(status, body, headers)
            text = clean_transcript(chat_text(body, headers))
            self._log_finished(PATH_CHAT, model, body, headers, duration_ms, len(text))
            cost, units = usage_of(body)
            return _Answer(text=text, cost_usd_micros=cost, units=units)

    async def _transcriptions(self, key: str, wav_b64: str, *, model: str, timeout: float) -> _Answer:
        started = time.monotonic()
        status, headers, body = await self._request(
            "POST",
            "/audio/transcriptions",
            key=key,
            timeout=timeout,
            operation=STT_OPERATION,
            payload=transcriptions_payload(wav_b64, model=model),
        )
        duration_ms = int((time.monotonic() - started) * 1000)
        if status != 200:
            self._log_attempt_failed(PATH_TRANSCRIPTIONS, model, status, body, headers, duration_ms)
            raise translate_status(status, body, headers)
        error = _error_object(body)
        if error:
            raise translate_status(_error_code(error) or 502, body, headers)
        raw_text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(raw_text, str):
            raise ProviderUnavailable(provider=PROVIDER, retryable=False)
        text = clean_transcript(raw_text)
        self._log_finished(PATH_TRANSCRIPTIONS, model, body, headers, duration_ms, len(text))
        cost, units = usage_of(body)
        return _Answer(text=text, cost_usd_micros=cost, units=units)

    @staticmethod
    def _log_attempt_failed(
        path: str, model: str, status: int, body: object, headers: Mapping[str, str], duration_ms: int
    ) -> None:
        error = translate_status(status, body, headers)
        log.info(
            "stt_attempt_failed",
            provider=PROVIDER,
            provider_path=path,
            model=model,
            status=status,
            code=error.code.value,
            retryable=error.retryable,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _log_finished(
        path: str, model: str, body: object, headers: Mapping[str, str], duration_ms: int, chars: int
    ) -> None:
        request_id = body.get("id") if isinstance(body, dict) else None
        log.info(
            "stt_call_finished",
            provider=PROVIDER,
            provider_path=path,
            model=model,
            request_id=request_id if isinstance(request_id, str) else headers.get("x-generation-id"),
            chars=chars,
            duration_ms=duration_ms,
        )

    async def validate_key(self, key: str, *, models: Sequence[str], timeout: float) -> SpeechKeyCheck:
        """SPEC §3.2.11 step 2: `GET /key` (limit, usage, free tier), then each model's endpoints, within `timeout`.

        A key that is not an OpenRouter key is refused before anything is sent. A model with no endpoints, or a
        404, is listed as missing; every other failure raises its mapped error.
        """
        candidate = key.strip()
        if key_kind(candidate) != KEY_KIND:
            raise AuthFailed(provider=PROVIDER)
        started = time.monotonic()
        deadline = started + timeout

        async def key_info() -> Mapping[str, Any]:
            status, headers, body = await self._request(
                "GET", "/key", key=candidate, timeout=timeout, operation=KEY_CHECK_OPERATION
            )
            if status != 200:
                raise translate_status(status, body, headers, operation=KEY_CHECK_OPERATION)
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, dict):
                raise ProviderUnavailable(provider=PROVIDER, retryable=False)
            return data

        data = await retrying(
            key_info, timeout=_remaining(deadline, KEY_CHECK_OPERATION), operation=KEY_CHECK_OPERATION
        )
        available: list[str] = []
        missing: list[str] = []
        for model in models:
            if "/" not in model.strip("/"):
                missing.append(model)
                continue

            async def probe(model: str = model) -> bool:
                status, headers, body = await self._request(
                    "GET",
                    f"/models/{quote(model.strip('/'), safe='/:')}/endpoints",
                    key=candidate,
                    timeout=timeout,
                    operation=KEY_CHECK_OPERATION,
                )
                if status == 404:
                    return False
                if status != 200:
                    raise translate_status(status, body, headers, operation=KEY_CHECK_OPERATION)
                info = body.get("data") if isinstance(body, dict) else None
                endpoints = info.get("endpoints") if isinstance(info, dict) else None
                return isinstance(endpoints, list) and bool(endpoints)

            found = await retrying(
                probe, timeout=_remaining(deadline, KEY_CHECK_OPERATION), operation=KEY_CHECK_OPERATION
            )
            (available if found else missing).append(model)
        is_free = data.get("is_free_tier")
        log.info(
            "speech_key_checked",
            provider=PROVIDER,
            available=len(available),
            missing=len(missing),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return SpeechKeyCheck(
            provider=PROVIDER,
            available_models=tuple(available),
            missing_models=tuple(missing),
            is_free_tier=is_free if isinstance(is_free, bool) else None,
            limit_usd=_decimal(data.get("limit")),
            usage_usd=_decimal(data.get("usage")),
            limit_remaining_usd=_decimal(data.get("limit_remaining")),
        )


def _remaining(deadline: float, operation: str) -> float:
    """Time left of the call's own `timeout` (A4); each `core.retry` budget gets only that."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CallTimedOut(operation=operation)
    return remaining
