"""Text to speech on a Gemini Live model (`gemini-3.8-live`; owner request 2026-11-01).

`gemini-3.8-flash-tts` (Interactions API) has a daily request cap on the owner's AI Studio key: once it is spent,
every note fell back to Edge. The Live models have no daily request cap (the STT decision of 2026-10-25), so
`GeminiSynthesizer` sends a model whose id has a `live` part (`live_stt.is_live_model`) here.

Request shape (verified live on 2026-11-01, spike `live_tts_spike.py`):
- `client.aio.live.connect(model, config)` with `response_modalities: ["AUDIO"]`, the prebuilt voice in
  `speech_config.voice_config` (the same names as Gemini TTS: Kore, Charon, Puck, ...), and a system instruction that
  makes the model read instead of answer (`prompts/personal/voice_live_system.md`, injected by the bot's wiring since
  adapters never import prompts, C3) plus the delivery style and language (`tts_common.style_text`);
- one `send_client_content` turn with the text and `turn_complete=True`;
- `server_content.model_turn.parts[].inline_data` carries raw 16-bit PCM, 24 kHz mono (`audio/pcm;rate=24000`), until
  `turn_complete`. The audio arrives at about real time (9 s of audio in ~9 s after the first byte), which is why the
  caller's timeout grows with the text (`timeouts.tts_per_word_s`). `output_audio_transcription` came back empty, so
  nothing relies on it.

A Live model can answer the text instead of reading it. The guard is the length of the audio: a note far shorter or
longer than the text needs (`duration_bounds_ms`) is a non-retryable `ProviderUnavailable`, and the caller's fallback
model takes over. Failures map as on the STT Live path (`live_stt.translate_live_error`).
"""

import contextlib
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, Final

from yordamchi.adapters.gemini.live_stt import LIVE_ERRORS, PATH_LIVE, PROVIDER, close_of, translate_live_error
from yordamchi.adapters.tts_common import GEMINI_PCM_RATE, TTS_OPERATION, OpenedAudio, pcm_input, style_text
from yordamchi.core.errors import ExternalError, ProviderUnavailable
from yordamchi.core.log import get_log

log = get_log(__name__)

# The duration guard. Uzbek speech on `gemini-3.8-live` ran at ~0.53 s per word (17 words in 9.0 s, 2026-11-01);
# Russian and English words are shorter. A note under 30 % of the estimate skipped words; one over 300 % (plus a
# fixed slack for the pauses around a short text) said something else.
SECONDS_PER_WORD: Final = 0.5
MIN_RATIO: Final = 0.3
MAX_RATIO: Final = 3.0
SLACK_MS: Final = 2_000
_PCM_BYTES_PER_SAMPLE: Final = 2
_RATE_IN_MIME: Final = re.compile(r"rate=(\d+)")


def duration_bounds_ms(text: str) -> tuple[int, int, int]:
    """(expected, shortest, longest) audio in ms for reading `text` aloud."""
    expected = int(len(text.split()) * SECONDS_PER_WORD * 1000)
    return expected, int(expected * MIN_RATIO), int(expected * MAX_RATIO) + SLACK_MS


def live_instruction(instruction: str, *, style: str, language_code: str) -> str:
    """The session's system instruction: the fixed read-aloud prompt, then how to speak (never words to say)."""
    delivery = style_text(style, language_code)
    parts = [instruction.strip(), f"How to speak (never words to say): {delivery}." if delivery else ""]
    return "\n\n".join(part for part in parts if part)


def live_config(*, voice: str, instruction: str) -> dict[str, Any]:
    """The `LiveConnectConfig` of one read-aloud session."""
    config: dict[str, Any] = {
        "response_modalities": ["AUDIO"],
        "speech_config": {"voice_config": {"prebuilt_voice_config": {"voice_name": voice}}},
    }
    if instruction:
        config["system_instruction"] = instruction
    return config


def _rate_of(mime: str | None) -> int:
    match = _RATE_IN_MIME.search(mime or "")
    return int(match.group(1)) if match else GEMINI_PCM_RATE


def _audio_of(message: Any) -> tuple[bytes, str | None]:
    """The PCM bytes (and their mime type) in one server message."""
    content = getattr(message, "server_content", None)
    turn = getattr(content, "model_turn", None) if content is not None else None
    chunks: list[bytes] = []
    mime: str | None = None
    for part in getattr(turn, "parts", None) or ():
        blob = getattr(part, "inline_data", None)
        data = getattr(blob, "data", None)
        if isinstance(data, bytes) and data:
            chunks.append(data)
            mime = mime or getattr(blob, "mime_type", None)
    return b"".join(chunks), mime


def _done(message: Any) -> bool:
    content = getattr(message, "server_content", None)
    return bool(content is not None and content.turn_complete)


class _Guard:
    """Counts the PCM of one segment and refuses audio that cannot be a reading of its text."""

    def __init__(self, text: str, *, model: str) -> None:
        self.expected_ms, self.min_ms, self.max_ms = duration_bounds_ms(text)
        self.model = model
        self.bytes = 0
        self.rate = GEMINI_PCM_RATE

    @property
    def audio_ms(self) -> int:
        return self.bytes * 1000 // (self.rate * _PCM_BYTES_PER_SAMPLE)

    def add(self, chunk: bytes) -> None:
        self.bytes += len(chunk)
        if self.audio_ms > self.max_ms:  # stop at once: it is not reading, and it may go on for a while
            self._refuse()

    def finish(self) -> None:
        if self.audio_ms < self.min_ms:
            self._refuse()

    def _refuse(self) -> None:
        log.warning(
            "tts_live_duration_rejected",
            provider=PROVIDER,
            provider_path=PATH_LIVE,
            model=self.model,
            audio_ms=self.audio_ms,
            expected_ms=self.expected_ms,
            min_ms=self.min_ms,
            max_ms=self.max_ms,
        )
        raise ProviderUnavailable(provider=PROVIDER, retryable=False)


async def open_live(
    client: Any, text: str, *, model: str, voice: str, instruction: str, idle_timeout: float
) -> OpenedAudio:
    """One Live session for one segment; returns once the first audio arrived (as `OpenedAudio`). Failures before that
    raise the mapped `ExternalError` (retryable ones are retried by the caller); the rest raise from `rest`."""
    del idle_timeout  # every wait of the caller is bounded by it; a Live session has no request-level timeout
    started = time.monotonic()
    stack = contextlib.AsyncExitStack()
    guard = _Guard(text, model=model)
    try:
        session = await stack.enter_async_context(
            client.aio.live.connect(model=model, config=live_config(voice=voice, instruction=instruction))
        )
        await session.send_client_content(
            turns={"role": "user", "parts": [{"text": text}]},
            turn_complete=True,
        )
        messages: Any = session.receive()
        stack.push_async_callback(_close_messages, messages)
        first, mime = await _first_audio(messages)
    except BaseException as exc:
        await stack.aclose()
        if isinstance(exc, LIVE_ERRORS):
            raise _failed(exc, model=model, started=started) from exc
        raise
    guard.rate = _rate_of(mime)
    guard.add(first)
    log.info(
        "tts_audio_started",
        provider=PROVIDER,
        provider_path=PATH_LIVE,
        model=model,
        first_byte_ms=int((time.monotonic() - started) * 1000),
    )

    async def rest() -> AsyncGenerator[bytes]:
        try:
            async for message in messages:
                audio, _ = _audio_of(message)
                if audio:
                    guard.add(audio)
                    yield audio
                if _done(message):
                    guard.finish()
                    return
        except LIVE_ERRORS as exc:
            raise _failed(exc, model=model, started=started) from exc
        raise ProviderUnavailable(provider=PROVIDER)  # the session ended before the turn did

    remaining = rest()

    async def aclose() -> None:
        try:
            await remaining.aclose()
        finally:
            await stack.aclose()

    return OpenedAudio(source=pcm_input(guard.rate), first=first, rest=remaining, aclose=aclose)


async def _close_messages(messages: Any) -> None:
    close = getattr(messages, "aclose", None)
    if close is not None:
        await close()


async def _first_audio(messages: AsyncIterator[Any]) -> tuple[bytes, str | None]:
    async for message in messages:
        audio, mime = _audio_of(message)
        if audio:
            return audio, mime
        if _done(message):
            break
    raise ProviderUnavailable(provider=PROVIDER)  # the turn ended without audio


def _failed(exc: BaseException, *, model: str, started: float) -> ExternalError:
    error = translate_live_error(exc, operation=TTS_OPERATION)
    code, _ = close_of(exc)
    log.info(
        "tts_attempt_failed",
        provider=PROVIDER,
        provider_path=PATH_LIVE,
        model=model,
        status=code,
        code=error.code.value,
        retryable=error.retryable,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return error
