"""`GeminiTranscriber`: speech to text through the Gemini Interactions API (UC-05; SPEC §3.2 step 5, §3.2.11).

Request shape copied from OvozYoz `engines/segment.py` (google-genai 2.33.0, STACK):
- a transcription model (`gemini-3.5-transcribe`) gets `generation_config.transcription_config` with
  `language_codes`, `custom_vocabulary` (≤ 1,000 terms) and `mode` (the `stt.mode` setting, per call);
- any other Gemini model gets the instruction, the hints in the prompt (first 200 terms) and minimal thinking,
  falling back to `low` or none when the model rejects the level;
- `store: False` always: voice notes are not kept by the provider.

A Live model (`gemini-3.5-transcribe-live`, the default since 2026-10-25) goes through `live_stt` instead: the note is
converted to 16 kHz PCM by ffmpeg and sent over one Live session (`provider_path = "live"`).

Telegram voice notes are OGG/Opus, which the API takes as `audio/ogg`, so the bytes go in as they are.

Failure handling (SPEC §3.2.11 step 6): 400 `API_KEY_INVALID`, 401 and 403 → `AuthFailed`; 429 / `RESOURCE_EXHAUSTED`
→ `RateLimited(retry_after)` from `RetryInfo` or `Retry-After`, or `QuotaExhausted` when a per-day or zero quota is
the cause; timeouts → `CallTimedOut`; 5xx and network errors → retryable `ProviderUnavailable`; anything else from
the API → non-retryable `ProviderUnavailable`. Only `core.retry` retries; the SDK's own retries are switched off.
"""

import asyncio
import base64
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

import httpx
from google import genai
from google.genai import errors as genai_errors
from google.genai._gaos.lib import compat_errors as interaction_errors  # the Interactions API error classes

from yordamchi.adapters.gemini import live_stt
from yordamchi.adapters.speech_common import (
    STT_INSTRUCTION,
    STT_OPERATION,
    TRANSCRIBE_VOCABULARY_LIMIT,
    ApiKey,
    audio_duration_ms,
    clean_transcript,
    duration_s,
    key_kind,
    prompt_hint,
    resolve_key,
    retry_after_s,
    unique_terms,
)
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ExternalError,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
)
from yordamchi.core.log import get_log
from yordamchi.core.retry import retrying
from yordamchi.ports.speech import SpeechKeyCheck, Transcript

log = get_log(__name__)

PROVIDER: Final = "google"
KEY_KIND: Final = "google_api_key"
PATH_INTERACTIONS: Final = "interactions"
KEY_CHECK_OPERATION: Final = "speech_key_check"
_THINKING_FIRST: Final = "minimal"
_THINKING_FALLBACK: Final = "low"

type ClientFactory = Callable[[str], genai.Client]

_VENDOR_ERRORS: Final = (genai_errors.APIError, interaction_errors.APIError, httpx.HTTPError)


def default_client(api_key: str) -> genai.Client:
    return genai.Client(api_key=api_key, vertexai=False)


def clean_model_id(model: str) -> str:
    return model.strip().removeprefix("models/")


def is_transcribe_model(model: str) -> bool:
    """Dedicated speech-to-text models accept a `transcription_config` (OvozYoz `models.py`)."""
    return "transcribe" in clean_model_id(model).lower()


def normalize_mime(mime: str, audio: bytes) -> str:
    base = mime.split(";", 1)[0].strip().lower()
    if base in {"audio/x-wav", "audio/wave", "audio/vnd.wave"}:
        return "audio/wav"
    if base:
        return base
    return "audio/ogg" if audio.startswith(b"OggS") else "audio/wav"


def build_request(
    audio: bytes,
    *,
    mime: str,
    languages: Sequence[str],
    vocabulary: Sequence[str],
    model: str,
    mode: str,
    thinking: str | None = _THINKING_FIRST,
) -> dict[str, Any]:
    """The `interactions.create` keyword arguments (OvozYoz `SegmentEngine.build_request`). `mode` applies to
    transcription models only."""
    part = {"type": "audio", "data": base64.b64encode(audio).decode("ascii"), "mime_type": normalize_mime(mime, audio)}
    model_id = clean_model_id(model)
    if is_transcribe_model(model_id):
        config: dict[str, Any] = {"mode": mode}
        codes = [code.strip() for code in languages if code.strip()]
        if codes:
            config["language_codes"] = codes
        terms = unique_terms(vocabulary, limit=TRANSCRIBE_VOCABULARY_LIMIT)
        if terms:
            config["custom_vocabulary"] = terms
        return {
            "model": model_id,
            "input": [part],
            "generation_config": {"transcription_config": config},
            "store": False,
        }
    request: dict[str, Any] = {
        "model": model_id,
        "input": [{"type": "text", "text": prompt_hint(languages, vocabulary)}, part],
        "system_instruction": STT_INSTRUCTION,
        "store": False,
    }
    if thinking:
        request["generation_config"] = {"thinking_level": thinking}
    return request


# --- error translation ---------------------------------------------------------------------------------------------
def _status_and_error(exc: BaseException) -> tuple[int | None, Mapping[str, Any]]:
    """HTTP status and the Google `error` object from either SDK error hierarchy."""
    raw_status: object
    body: object
    if isinstance(exc, genai_errors.APIError):
        raw_status, body = exc.code, exc.details
    else:
        raw_status, body = getattr(exc, "status_code", None), getattr(exc, "body", None)
    status = raw_status if isinstance(raw_status, int) else None
    if isinstance(body, list) and len(body) == 1:
        body = body[0]
    if isinstance(body, dict):
        inner = body.get("error")
        return status, inner if isinstance(inner, dict) else body
    return status, {}


def _details(error: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    details = error.get("details")
    return [item for item in details if isinstance(item, dict)] if isinstance(details, list) else []


def _headers(exc: BaseException) -> Mapping[str, str]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    return headers if isinstance(headers, Mapping) else {}


def _quota_or_rate(error: Mapping[str, Any], headers: Mapping[str, str]) -> ExternalError:
    """429 `RESOURCE_EXHAUSTED` is either a short rate window (wait) or a used-up quota (waiting does not help)."""
    retry_after: float | None = None
    exhausted = False
    for detail in _details(error):
        kind = str(detail.get("@type", ""))
        if kind.endswith("RetryInfo"):
            retry_after = duration_s(detail.get("retryDelay"))
        elif kind.endswith("QuotaFailure"):
            violations = detail.get("violations")
            for violation in violations if isinstance(violations, list) else []:
                if not isinstance(violation, dict):
                    continue
                quota_id = str(violation.get("quotaId", ""))
                if "PerDay" in quota_id or str(violation.get("quotaValue", "")) == "0":
                    exhausted = True
    if exhausted:
        return QuotaExhausted(provider=PROVIDER)
    if retry_after is None:
        retry_after = retry_after_s(headers.get("retry-after"), date=headers.get("date"))
    return RateLimited(provider=PROVIDER, retry_after=retry_after)


def status_of(exc: BaseException) -> int | None:
    return _status_and_error(exc)[0]


def is_thinking_rejection(exc: BaseException) -> bool:
    status, error = _status_and_error(exc)
    return status == 400 and "thinking" in str(error.get("message", "")).lower()


def translate_error(  # noqa: PLR0911  # one mapping table, read top to bottom
    exc: BaseException, *, operation: str = STT_OPERATION
) -> ExternalError:
    """Map a google-genai or httpx exception to the `core.errors` hierarchy (CONVENTIONS §7)."""
    if isinstance(exc, (httpx.TimeoutException, interaction_errors.APITimeoutError)):
        return CallTimedOut(operation=operation)
    if isinstance(exc, (httpx.HTTPError, interaction_errors.APIConnectionError)):
        return ProviderUnavailable(provider=PROVIDER)
    if isinstance(exc, interaction_errors.APIResponseValidationError):
        return ProviderUnavailable(provider=PROVIDER, retryable=False)
    status, error = _status_and_error(exc)
    message = str(error.get("message", "")).lower()
    state = str(error.get("status", ""))
    reasons = {str(detail.get("reason", "")) for detail in _details(error)}
    if (
        status in {401, 403}
        or state in {"UNAUTHENTICATED", "PERMISSION_DENIED"}
        or reasons & {"API_KEY_INVALID", "API_KEY_EXPIRED", "API_KEY_SERVICE_BLOCKED"}
        or "api key not valid" in message
        or "api key expired" in message
    ):
        return AuthFailed(provider=PROVIDER)
    if status == 429 or state == "RESOURCE_EXHAUSTED":
        return _quota_or_rate(error, _headers(exc))
    if status in {408, 504} or state == "DEADLINE_EXCEEDED":
        return CallTimedOut(operation=operation)
    if status is not None and status >= 500:
        return ProviderUnavailable(provider=PROVIDER)
    return ProviderUnavailable(provider=PROVIDER, retryable=False)


# --- the adapter ---------------------------------------------------------------------------------------------------
class GeminiTranscriber:
    """`Transcriber` on a direct Google key. `api_key` is the key or a callable returning the vault's current one.

    `ffmpeg` is the static binary from the installer (`DATA/bin/ffmpeg`); only a Live model needs it (PCM input), and
    `None` there is `NotInstalled`, which falls back to the next provider.
    """

    def __init__(
        self, *, api_key: ApiKey, client_factory: ClientFactory = default_client, ffmpeg: Path | None = None
    ) -> None:
        self._api_key = api_key
        self._client_factory = client_factory
        self._ffmpeg = ffmpeg
        self._client: tuple[str, genai.Client] | None = None
        self._client_lock = asyncio.Lock()
        # Per model: the thinking level it accepted last (None: send none). A general model that rejects
        # "minimal" is not asked again on every voice note.
        self._thinking: dict[str, str | None] = {}

    def _new_client(self, key: str) -> genai.Client:
        client = self._client_factory(key)
        # google-genai 2.33.0 retries Interactions calls on its own (3 retries by default, and `attempts=0` in
        # HttpRetryOptions still becomes one retry). core.retry is the only retry layer (CONVENTIONS §7).
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
        del fallback_model  # the Interactions API has one path; there is nothing to fall back to
        started = time.monotonic()
        key = await resolve_key(self._api_key, provider=PROVIDER)
        model_id = clean_model_id(model)
        live = live_stt.is_live_model(model_id)
        path = live_stt.PATH_LIVE if live else PATH_INTERACTIONS
        attempts = 0

        async def attempt() -> str:
            nonlocal attempts
            attempts += 1
            client = await self._client_for(key)
            return await self._create(
                client,
                audio,
                mime=mime,
                languages=languages,
                vocabulary=vocabulary,
                model=model_id,
                mode=mode,
                timeout=timeout,
            )

        try:
            if live:
                pcm = await live_stt.to_pcm(audio, ffmpeg=self._ffmpeg, timeout=timeout)

                async def live_attempt() -> str:
                    nonlocal attempts
                    attempts += 1
                    client = await self._client_for(key)
                    return await live_stt.transcribe_pcm(
                        client, pcm, model=model_id, languages=languages, vocabulary=vocabulary, mode=mode
                    )

                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise CallTimedOut(operation=STT_OPERATION)
                text = await retrying(live_attempt, timeout=remaining, operation=STT_OPERATION)
            else:
                text = await retrying(attempt, timeout=timeout, operation=STT_OPERATION)
        except ExternalError as error:
            log.warning(
                "stt_call_failed",
                provider=PROVIDER,
                provider_path=path,
                model=model_id,
                code=error.code.value,
                retryable=error.retryable,
                attempts=attempts,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            raise
        latency_ms = int((time.monotonic() - started) * 1000)
        return Transcript(
            text=text,
            provider=PROVIDER,
            provider_path=path,
            model=model,
            audio_ms=audio_duration_ms(audio, mime),
            latency_ms=latency_ms,
        )

    async def _create(
        self,
        client: genai.Client,
        audio: bytes,
        *,
        mime: str,
        languages: Sequence[str],
        vocabulary: Sequence[str],
        model: str,
        mode: str,
        timeout: float,
    ) -> str:
        """One logical attempt. A rejected thinking level is retried at once with the fallback (not a failure)."""
        interactions: Any = client.aio.interactions
        while True:
            thinking = self._thinking.get(model, _THINKING_FIRST)
            request = build_request(
                audio, mime=mime, languages=languages, vocabulary=vocabulary, model=model, mode=mode, thinking=thinking
            )
            started = time.monotonic()
            try:
                response = await interactions.create(**request, timeout=timeout)
            except _VENDOR_ERRORS as exc:
                if thinking and not is_transcribe_model(model) and is_thinking_rejection(exc):
                    message = str(_status_and_error(exc)[1].get("message", "")).lower()
                    fallback = _THINKING_FALLBACK if thinking != _THINKING_FALLBACK and "low" in message else None
                    self._thinking[model] = fallback
                    log.info("stt_thinking_level_rejected", provider=PROVIDER, model=model, fallback=fallback)
                    continue
                error = translate_error(exc)
                log.info(
                    "stt_attempt_failed",
                    provider=PROVIDER,
                    model=model,
                    status=status_of(exc),
                    code=error.code.value,
                    retryable=error.retryable,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise error from exc
            return self._text_of(response, model=model, started=started, audio_bytes=len(audio))

    @staticmethod
    def _text_of(response: Any, *, model: str, started: float, audio_bytes: int) -> str:
        state = getattr(response, "status", None)
        if state == "failed":
            log.info("stt_attempt_failed", provider=PROVIDER, model=model, status=200, code="interaction_failed")
            raise ProviderUnavailable(provider=PROVIDER)
        text = clean_transcript(getattr(response, "output_text", None))
        request_id = getattr(response, "id", None)
        log.info(
            "stt_call_finished",
            provider=PROVIDER,
            provider_path=PATH_INTERACTIONS,
            model=model,
            request_id=request_id if isinstance(request_id, str) else None,
            audio_bytes=audio_bytes,
            chars=len(text),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return text

    async def validate_key(self, key: str, *, models: Sequence[str], timeout: float) -> SpeechKeyCheck:
        """SPEC §3.2.11 step 2: `models.get` for each model on a candidate key, within `timeout` in total.

        A key that is not a Google key is refused before anything is sent, so another vendor's secret never
        reaches Google. 404 → the model is listed as missing; every other failure raises its mapped error.
        """
        candidate = key.strip()
        if key_kind(candidate) != KEY_KIND:
            raise AuthFailed(provider=PROVIDER)
        started = time.monotonic()
        deadline = started + timeout
        client = self._new_client(candidate)
        available: list[str] = []
        missing: list[str] = []
        try:
            for model in models:
                model_id = clean_model_id(model)

                async def probe(model_id: str = model_id) -> bool:
                    try:
                        await client.aio.models.get(model=model_id)
                    except _VENDOR_ERRORS as exc:
                        if status_of(exc) == 404:
                            return False
                        raise translate_error(exc, operation=KEY_CHECK_OPERATION) from exc
                    return True

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CallTimedOut(operation=KEY_CHECK_OPERATION)
                found = await retrying(probe, timeout=remaining, operation=KEY_CHECK_OPERATION)
                (available if found else missing).append(model)
        finally:
            await client.aio.aclose()
        log.info(
            "speech_key_checked",
            provider=PROVIDER,
            available=len(available),
            missing=len(missing),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return SpeechKeyCheck(provider=PROVIDER, available_models=tuple(available), missing_models=tuple(missing))
