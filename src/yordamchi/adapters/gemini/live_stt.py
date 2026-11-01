"""Speech to text on a Gemini Live model (`gemini-3.5-transcribe-live`; owner decision 2026-10-25).

The owner picked a Live model for STT: on his AI Studio key the Live models have no daily request cap, and
`gemini-3.5-transcribe-live` is Google's newest dedicated transcription model. `GeminiTranscriber` sends a model whose
id has a `live` part here instead of the Interactions API.

Request shape (ai.google.dev "Live transcription", verified live on 2026-10-25):
- `client.aio.live.connect(model, config)` with `response_modalities: ["TEXT"]` and `input_audio_transcription` =
  `language_codes`, `custom_vocabulary` (≤ 1,000 terms) and `mode` (`stt.mode`);
- the server's voice activity detection is off: one `activity_start`, the whole note as raw 16-bit PCM, 16 kHz, mono
  (ffmpeg from Telegram's OGG/Opus), one `activity_end`. One note is then one utterance, so the server answers with
  one final `input_transcription` and `generation_complete` instead of a part per pause, and the call ends there
  instead of on the server's idle close a minute later;
- interim hypotheses (`interim_input_transcription`) are live captions and are ignored.

A Live session reports a failure as a WebSocket close: code 1008 with the reason ("API key not valid", "… is not
found …", a quota message) or 1011 for an internal error. `translate_live_error` maps them onto `core.errors` like the
HTTP errors of the Interactions path. Nothing is stored by the provider beyond the session.
"""

import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from google.genai import errors as genai_errors
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from yordamchi.adapters.speech_common import STT_OPERATION, TRANSCRIBE_VOCABULARY_LIMIT, clean_transcript, unique_terms
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

log = get_log(__name__)

PROVIDER: Final = "google"
PATH_LIVE: Final = "live"
FFMPEG: Final = "ffmpeg"
PCM_RATE: Final = 16_000
PCM_MIME: Final = f"audio/pcm;rate={PCM_RATE}"
# 1 s of 16-bit mono PCM per message: the Live API takes realtime input in chunks, and a voice note is sent whole.
PCM_CHUNK_BYTES: Final = PCM_RATE * 2
# ffmpeg needs no inherited environment; nothing of the bot's reaches it.
FFMPEG_ENV: Final[Mapping[str, str]] = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
# WebSocket close codes the Live API uses (RFC 6455 §7.4.1): 1008 policy violation carries auth, model and quota
# refusals in its reason; 1011 is an internal error.
WS_POLICY_VIOLATION: Final = 1008
WS_INTERNAL_ERROR: Final = 1011
WS_ABNORMAL: Final = 1006

_LIVE_PART: Final = re.compile(r"(?:^|[-_.])live(?:$|[-_.])")
_QUOTA_SPENT: Final = ("per day", "per_day", "perday", "daily", "limit: 0")


def is_live_model(model: str) -> bool:
    """A model served only on the Live API (`bidiGenerateContent`): its id has a `live` part."""
    return _LIVE_PART.search(model.strip().removeprefix("models/").lower()) is not None


def pcm_argv(ffmpeg: Path) -> list[str]:
    """OGG/Opus (or anything ffmpeg reads) on stdin → raw 16-bit little-endian PCM, 16 kHz, mono on stdout."""
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
        str(PCM_RATE),
        "-f",
        "s16le",
        "pipe:1",
    ]


def ffmpeg_binary(ffmpeg: Path | None) -> Path:
    """The installer's ffmpeg, or `NotInstalled` (one stat; the caller's provider fallback takes over)."""
    if ffmpeg is None or not ffmpeg.is_file():
        raise NotInstalled(binary=FFMPEG)
    return ffmpeg


async def to_pcm(audio: bytes, *, ffmpeg: Path | None, timeout: float) -> bytes:
    binary = ffmpeg_binary(ffmpeg)
    try:
        result = await proc.run(pcm_argv(binary), env=FFMPEG_ENV, timeout=timeout, stdin=audio)
    except TimeoutError as exc:
        raise CallTimedOut(operation=STT_OPERATION) from exc
    except OSError as exc:
        raise NotInstalled(binary=FFMPEG) from exc
    if result.returncode != 0 or not result.stdout:
        log.warning("stt_audio_convert_failed", returncode=result.returncode, duration_ms=result.duration_ms)
        raise ProviderUnavailable(provider=FFMPEG, retryable=False)
    return result.stdout


def live_config(*, languages: Sequence[str], vocabulary: Sequence[str], mode: str) -> dict[str, Any]:
    """The `LiveConnectConfig` of one transcription session."""
    transcription: dict[str, Any] = {}
    codes = [code.strip() for code in languages if code.strip()]
    if codes:
        transcription["language_codes"] = codes
    terms = unique_terms(vocabulary, limit=TRANSCRIBE_VOCABULARY_LIMIT)
    if terms:
        transcription["custom_vocabulary"] = terms
    if mode.strip():
        transcription["mode"] = mode.strip().upper()
    return {
        "response_modalities": ["TEXT"],
        "input_audio_transcription": transcription,
        "realtime_input_config": {"automatic_activity_detection": {"disabled": True}},
    }


# --- error translation ---------------------------------------------------------------------------------------------
def close_of(exc: BaseException) -> tuple[int | None, str]:
    """The close code and reason of a Live session failure (the SDK re-raises a close as `APIError(code, reason)`)."""
    if isinstance(exc, genai_errors.APIError):
        details = exc.details
        reason = details if isinstance(details, str) else str(exc.message or "")
        return exc.code, reason
    if isinstance(exc, ConnectionClosed):
        frame = exc.rcvd
        return (frame.code, frame.reason) if frame is not None else (WS_ABNORMAL, "")
    return None, ""


def translate_live_error(  # noqa: PLR0911  # one mapping table, read top down
    exc: BaseException, *, operation: str = STT_OPERATION
) -> ExternalError:
    """A Live session failure as a `core.errors` error; `operation` names the call in a timeout (`stt` or `tts`)."""
    if isinstance(exc, TimeoutError):
        return CallTimedOut(operation=operation)
    if isinstance(exc, (InvalidHandshake, OSError)):
        return ProviderUnavailable(provider=PROVIDER)
    code, reason = close_of(exc)
    text = reason.lower()
    if code in {401, 403} or "api key" in text or "permission" in text or "unauthenticated" in text:
        return AuthFailed(provider=PROVIDER)
    if code == 429 or "quota" in text or "resource_exhausted" in text or "resource exhausted" in text:
        # Waiting does not help a per-day or zero quota (the Interactions path reads the same from `QuotaFailure`).
        if any(mark in text for mark in _QUOTA_SPENT):
            return QuotaExhausted(provider=PROVIDER)
        return RateLimited(provider=PROVIDER, retry_after=None)
    if "deadline" in text:
        return CallTimedOut(operation=operation)
    if code in {WS_INTERNAL_ERROR, WS_ABNORMAL} or (code is not None and 500 <= code < 600):
        return ProviderUnavailable(provider=PROVIDER)
    return ProviderUnavailable(provider=PROVIDER, retryable=False)


LIVE_ERRORS: Final = (genai_errors.APIError, ConnectionClosed, InvalidHandshake, OSError)


# --- one session ---------------------------------------------------------------------------------------------------
async def transcribe_pcm(
    client: Any, pcm: bytes, *, model: str, languages: Sequence[str], vocabulary: Sequence[str], mode: str
) -> str:
    """One Live session: the note as one utterance, the final transcript back. Raises the mapped `ExternalError`;
    the caller bounds it with a timeout."""
    started = time.monotonic()
    config = live_config(languages=languages, vocabulary=vocabulary, mode=mode)
    parts: list[str] = []
    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            await session.send_realtime_input(activity_start={})
            for offset in range(0, len(pcm), PCM_CHUNK_BYTES):
                chunk = pcm[offset : offset + PCM_CHUNK_BYTES]
                await session.send_realtime_input(audio={"data": chunk, "mime_type": PCM_MIME})
            await session.send_realtime_input(activity_end={})
            async for message in session.receive():
                content = message.server_content
                if content is None:
                    continue
                final = content.input_transcription
                if final is not None and final.text:
                    parts.append(final.text.strip())
                if content.generation_complete or content.turn_complete:
                    break
    except LIVE_ERRORS as exc:
        error = translate_live_error(exc)
        code, _ = close_of(exc)
        log.info(
            "stt_attempt_failed",
            provider=PROVIDER,
            provider_path=PATH_LIVE,
            model=model,
            status=code,
            code=error.code.value,
            retryable=error.retryable,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        raise error from exc
    text = clean_transcript(" ".join(part for part in parts if part))
    log.info(
        "stt_call_finished",
        provider=PROVIDER,
        provider_path=PATH_LIVE,
        model=model,
        audio_bytes=len(pcm),
        chars=len(text),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return text
