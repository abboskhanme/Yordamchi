"""What the three TTS adapters share (`openrouter/tts.py`, `gemini/tts.py`, `edge_tts/tts.py`): the OGG/Opus encode
through the static ffmpeg, sentence segments (the S8 fallback) and the request-side style text.

No vendor SDK is imported here; each adapter keeps its own (C5).

The encode (SPEC §3.2.13 step 6): the provider's audio goes into `ffmpeg -c:a libopus -b:a 32k -ar 48000 -ac 1
-application voip -f ogg` on stdin as it arrives (`core.proc.run_piped`), so the note is ready right after the
provider's last chunk; Telegram needs the whole file for `sendVoice` anyway. Gemini (direct and through OpenRouter)
speaks raw 16-bit little-endian PCM at 24 kHz mono; Edge speaks MP3 (`audio-24khz-48kbitrate-mono-mp3`).
"""

import asyncio
import re
import struct
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from yordamchi.adapters.speech_common import is_wav, ogg_opus_duration_ms
from yordamchi.core import proc
from yordamchi.core.errors import CallTimedOut, ExternalError, NotInstalled, ProviderUnavailable
from yordamchi.core.log import get_log

log = get_log(__name__)

TTS_OPERATION: Final = "tts"
FFMPEG: Final = "ffmpeg"
VOICE_MIME: Final = "audio/ogg"
# ffmpeg needs no inherited environment; nothing of the bot's reaches it.
FFMPEG_ENV: Final[Mapping[str, str]] = {"PATH": "/usr/bin:/bin", "LC_ALL": "C"}
OPUS_BITRATE: Final = "32k"
OPUS_RATE: Final = 48_000
GEMINI_PCM_RATE: Final = 24_000  # ai.google.dev speech-generation: 24 kHz mono 16-bit PCM; OpenRouter passes it on
_SENTENCE_END: Final = re.compile(r"(?<=[.!?…])\s+")


@dataclass(frozen=True, slots=True)
class AudioInput:
    """How a provider's audio reaches ffmpeg's stdin: a demuxer (`-f`), and the rate and channels of raw PCM."""

    container: str  # "s16le" | "mp3" | "wav"
    sample_rate: int | None = None
    channels: int = 1


def pcm_input(sample_rate: int = GEMINI_PCM_RATE, channels: int = 1) -> AudioInput:
    return AudioInput("s16le", sample_rate, channels)


MP3_INPUT: Final = AudioInput("mp3")


def opus_argv(ffmpeg: Path, source: AudioInput) -> list[str]:
    """The provider's audio on stdin → a Telegram voice note (OGG/Opus, 48 kHz mono, 32 kb/s, voip) on stdout."""
    argv = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-f", source.container]
    if source.sample_rate is not None:
        argv += ["-ar", str(source.sample_rate), "-ac", str(source.channels)]
    return [
        *argv,
        "-i",
        "pipe:0",
        "-map_metadata",
        "-1",
        "-vn",
        "-c:a",
        "libopus",
        "-b:a",
        OPUS_BITRATE,
        "-ar",
        str(OPUS_RATE),
        "-ac",
        "1",
        "-application",
        "voip",
        "-f",
        "ogg",
        "pipe:1",
    ]


def require_ffmpeg(ffmpeg: Path | None) -> Path:
    """The binary, or `NotInstalled` before anything is sent to a provider (no money spent on audio nobody hears)."""
    if ffmpeg is None or not ffmpeg.is_file():
        raise NotInstalled(binary=FFMPEG)
    return ffmpeg


# --- request text --------------------------------------------------------------------------------------------------
def style_text(style: str, language_code: str) -> str:
    """The delivery instruction: the owner's style (`voice.style`, accent included) plus the language to speak. It is
    sent as the provider's instruction field, never as text to read: Gemini 3.8 TTS reads its input verbatim."""
    parts = [style.strip().rstrip(".")]
    code = language_code.strip()
    if code:
        parts.append(f"Language: {code}")
    return ". ".join(part for part in parts if part)


def sentence_segments(text: str, max_words: int) -> list[str]:
    """`text` cut at sentence ends into pieces of at most `max_words` words, in order. A sentence longer than that
    stays whole; `max_words` ≤ 0 keeps the text as one piece. Empty text → []."""
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    if max_words <= 0:
        return [cleaned]
    segments: list[str] = []
    current: list[str] = []
    words = 0
    for sentence in _SENTENCE_END.split(cleaned):
        count = len(sentence.split())
        if current and words + count > max_words:
            segments.append(" ".join(current))
            current, words = [], 0
        current.append(sentence)
        words += count
    if current:
        segments.append(" ".join(current))
    return segments


# --- provider audio ------------------------------------------------------------------------------------------------
async def _nothing() -> None:
    return None


@dataclass(frozen=True, slots=True)
class OpenedAudio:
    """A provider answered one segment: its format, the first audio bytes, and the rest as they arrive. `aclose`
    releases the provider's stream (an HTTP body, a websocket); it is always awaited."""

    source: AudioInput
    first: bytes = field(repr=False)
    rest: AsyncIterator[bytes]
    aclose: Callable[[], Awaitable[None]] = _nothing

    async def chunks(self) -> AsyncIterator[bytes]:
        yield self.first
        async for chunk in self.rest:
            yield chunk


type OpenSegment = Callable[[str], Awaitable[OpenedAudio]]


def split_wav(data: bytes) -> tuple[AudioInput, bytes]:
    """A WAV (header included) as raw PCM plus its format, so pieces can be joined without a header in the middle."""
    if not is_wav(data):
        return pcm_input(), data
    offset = 12
    rate, channels = GEMINI_PCM_RATE, 1
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (size,) = struct.unpack_from("<I", data, offset + 4)
        payload = offset + 8
        if chunk_id == b"fmt " and payload + 16 <= len(data):
            channels, rate = struct.unpack_from("<HI", data, payload + 2)
        elif chunk_id == b"data":
            return pcm_input(rate, channels), data[payload:]
        offset = payload + size + (size & 1)
    return pcm_input(rate, channels), b""


async def _whole(open_segment: OpenSegment, text: str, idle_timeout: float) -> tuple[AudioInput, bytes]:
    opened = await open_segment(text)
    try:
        parts = [opened.first]
        iterator = aiter(opened.rest)
        while True:
            async with asyncio.timeout(idle_timeout):
                try:
                    parts.append(await anext(iterator))
                except StopAsyncIteration:
                    break
    except TimeoutError as exc:
        raise CallTimedOut(operation=TTS_OPERATION) from exc
    finally:
        await opened.aclose()
    return opened.source, b"".join(parts)


def _first_external(group: BaseExceptionGroup[BaseException]) -> ExternalError | None:
    for error in group.exceptions:
        if isinstance(error, ExternalError):
            return error
        if isinstance(error, BaseExceptionGroup) and (inner := _first_external(error)) is not None:
            return inner
    return None


async def _concurrently(
    open_segment: OpenSegment, segments: Sequence[str], idle_timeout: float, provider: str
) -> tuple[AudioInput, list[bytes]]:
    """Every segment at once (the S8 fallback); the first provider failure cancels the others and is raised."""
    try:
        async with asyncio.TaskGroup() as group:
            jobs = [group.create_task(_whole(open_segment, text, idle_timeout)) for text in segments]
    except BaseExceptionGroup as failed:
        first = _first_external(failed)
        if first is None:
            raise
        raise first from None
    results = [job.result() for job in jobs]
    sources = {source for source, _ in results}
    if len(sources) != 1:  # one provider and model per note; a mix cannot be joined
        raise ProviderUnavailable(provider=provider, retryable=False)
    return results[0][0], [audio for _, audio in results]


async def _replay(parts: Sequence[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


# --- the voice note ------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class EncodedVoice:
    data: bytes = field(repr=False)
    duration_ms: int
    requests: int  # provider requests made: one per segment


async def encode_voice(
    ffmpeg: Path, source: AudioInput, chunks: AsyncIterable[bytes], *, idle_timeout: float, provider: str
) -> tuple[bytes, int]:
    """Pipe `chunks` through ffmpeg → (OGG/Opus bytes, duration in ms). A provider error inside `chunks` propagates
    as it is; ffmpeg's own failures map to `CallTimedOut`, `NotInstalled` or a non-retryable `ProviderUnavailable`."""
    try:
        result = await proc.run_piped(
            opus_argv(ffmpeg, source), env=FFMPEG_ENV, chunks=chunks, idle_timeout=idle_timeout
        )
    except TimeoutError as exc:
        raise CallTimedOut(operation=TTS_OPERATION) from exc
    except OSError as exc:
        raise NotInstalled(binary=FFMPEG) from exc
    duration_ms = ogg_opus_duration_ms(result.stdout)
    if result.returncode != 0 or duration_ms <= 0:
        log.warning(
            "tts_encode_failed",
            provider=provider,
            returncode=result.returncode,
            output_bytes=len(result.stdout),
            duration_ms=result.duration_ms,
        )
        raise ProviderUnavailable(provider=FFMPEG, retryable=False)
    return result.stdout, duration_ms


async def voice_note(
    *,
    ffmpeg: Path,
    text: str,
    segment_words: int,
    open_segment: OpenSegment,
    idle_timeout: float,
    provider: str,
) -> EncodedVoice:
    """One note from `text`. One segment streams straight into ffmpeg; several are fetched concurrently and fed in
    order. `open_segment` makes the provider request (with the adapter's retries) and returns once audio flows."""
    segments = sentence_segments(text, segment_words)
    if not segments:
        msg = "nothing to say: the text is empty"
        raise ValueError(msg)
    if len(segments) == 1:
        opened = await open_segment(segments[0])
        try:
            data, duration_ms = await encode_voice(
                ffmpeg, opened.source, opened.chunks(), idle_timeout=idle_timeout, provider=provider
            )
        finally:
            await opened.aclose()
        return EncodedVoice(data=data, duration_ms=duration_ms, requests=1)
    source, parts = await _concurrently(open_segment, segments, idle_timeout, provider)
    data, duration_ms = await encode_voice(ffmpeg, source, _replay(parts), idle_timeout=idle_timeout, provider=provider)
    return EncodedVoice(data=data, duration_ms=duration_ms, requests=len(segments))


async def stream_once(note: Awaitable[EncodedVoice]) -> AsyncIterator[bytes]:
    """`Synthesizer.synthesize_stream`: the finished note's bytes as one chunk."""
    encoded = await note
    yield encoded.data
