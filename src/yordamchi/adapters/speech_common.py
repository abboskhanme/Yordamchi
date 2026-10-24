"""What the two STT adapters share (`gemini/stt.py`, `openrouter/stt.py`): key handling, the transcription
instruction for prompt-based models, transcript cleaning, audio durations and retry-delay parsing.

No vendor SDK is imported here; each adapter keeps its own (C5).

The instruction text follows OvozYoz `engines/segment.py` `_SYSTEM` plus the UC-05 mixed-language rules. It is a
request detail of the prompt-based STT path, not a persona prompt; C3 keeps adapters away from `prompts/**`.
"""

import math
import struct
from collections.abc import Awaitable, Callable, Sequence
from email.utils import parsedate_to_datetime
from typing import Final

from yordamchi.core.errors import AuthFailed
from yordamchi.core.redact import find_secrets

# The vault is the source of a key, and it may change by chat at any time, so an adapter can take a callable that
# returns the current key; a plain string is enough for tests and one-off checks.
type ApiKey = str | Callable[[], Awaitable[str]]

STT_OPERATION: Final = "stt"
# Gemini `custom_vocabulary` takes up to 1,000 terms; prompt-based paths get the first 200 (SPEC §3.2 step 5).
TRANSCRIBE_VOCABULARY_LIMIT: Final = 1000
PROMPT_VOCABULARY_LIMIT: Final = 200

STT_INSTRUCTION: Final = (
    "Transcribe the audio exactly as spoken, word for word. "
    "The speaker mixes Uzbek, Russian and English, often within one sentence. "
    "Write every part in the language it was spoken, using its standard orthography: "
    "Uzbek in Latin script (oʻ, gʻ, sh, ch), Russian in Cyrillic, English as spoken. "
    "Never translate any word or phrase into another language. Add punctuation. "
    "Do not summarize, correct grammar, or add commentary. "
    "Output only the transcript. If there is no intelligible speech, output nothing."
)
STT_REQUEST_TEXT: Final = "Transcribe this audio."

_NO_SPEECH: Final = frozenset({"", "silence", "no speech", "nutq yo'q", "no audio", "inaudible", "empty", "none"})
_OPUS_GRANULE_RATE: Final = 48_000  # Opus granule positions always count 48 kHz samples (RFC 7845 §4)


# --- keys ----------------------------------------------------------------------------------------------------------
def key_kind(key: str) -> str | None:
    """The `core.redact` pattern kind of `key` when the whole string is exactly one key, else `None`."""
    text = key.strip()
    hits = find_secrets(text)
    if len(hits) == 1 and hits[0].start == 0 and hits[0].end == len(text):
        return hits[0].kind
    return None


async def resolve_key(api_key: ApiKey, *, provider: str) -> str:
    """The current key; an empty one is an auth failure, not a request without credentials."""
    key = api_key if isinstance(api_key, str) else await api_key()
    key = key.strip()
    if not key:
        raise AuthFailed(provider=provider)
    return key


# --- request text --------------------------------------------------------------------------------------------------
def unique_terms(vocabulary: Sequence[str], *, limit: int) -> list[str]:
    """Stripped, de-duplicated terms in their given order, at most `limit`."""
    seen: set[str] = set()
    terms: list[str] = []
    for raw in vocabulary:
        term = raw.strip()
        if term and term not in seen:
            seen.add(term)
            terms.append(term)
            if len(terms) == limit:
                break
    return terms


def prompt_hint(languages: Sequence[str], vocabulary: Sequence[str]) -> str:
    """The user-turn text for prompt-based models: the request plus the language and vocabulary hints."""
    hint = STT_REQUEST_TEXT
    codes = [code.strip() for code in languages if code.strip()]
    if codes:
        hint += f" Expected languages: {', '.join(codes)}."
    terms = unique_terms(vocabulary, limit=PROMPT_VOCABULARY_LIMIT)
    if terms:
        hint += f" Spell these terms exactly: {', '.join(terms)}."
    return hint


def clean_transcript(text: str | None) -> str:
    """Strip wrapping quotes and no-speech markers an LLM sometimes adds (OvozYoz `clean_model_transcript`)."""
    cleaned = (text or "").strip()
    # Only a pair that wraps the whole text: `«a» va «b»` and `"a" or "b"` are quoted words, not a wrapper.
    if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in "\"'" and cleaned[0] not in cleaned[1:-1]:
        cleaned = cleaned[1:-1].strip()
    if cleaned.startswith("«") and cleaned.endswith("»") and not set("«»") & set(cleaned[1:-1]):
        cleaned = cleaned[1:-1].strip()
    if cleaned.lower().strip("[]()<>*. ") in _NO_SPEECH:
        return ""
    return cleaned


# --- audio ---------------------------------------------------------------------------------------------------------
def _riff_chunks(data: bytes) -> list[tuple[bytes, int, int]]:
    """(id, payload offset, declared size) of each top-level chunk of a RIFF/WAVE file."""
    chunks: list[tuple[bytes, int, int]] = []
    offset = 12
    while offset + 8 <= len(data):
        chunk_id = data[offset : offset + 4]
        (size,) = struct.unpack_from("<I", data, offset + 4)
        chunks.append((chunk_id, offset + 8, size))
        if chunk_id == b"data":
            break  # a streamed WAV declares a bogus size; data runs to the end
        offset += 8 + size + (size & 1)
    return chunks


def is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def fix_wav_header(data: bytes) -> bytes:
    """Rewrite the RIFF and `data` sizes from the real length. ffmpeg writing to a pipe cannot seek back, so it
    leaves placeholder sizes that some decoders reject."""
    if not is_wav(data):
        return data
    for chunk_id, payload, _ in _riff_chunks(data):
        if chunk_id == b"data":
            fixed = bytearray(data)
            struct.pack_into("<I", fixed, 4, len(data) - 8)
            struct.pack_into("<I", fixed, payload - 4, len(data) - payload)
            return bytes(fixed)
    return data


def wav_duration_ms(data: bytes) -> int:
    if not is_wav(data):
        return 0
    byte_rate = 0
    for chunk_id, payload, size in _riff_chunks(data):
        if chunk_id == b"fmt " and size >= 16 and payload + 16 <= len(data):
            (byte_rate,) = struct.unpack_from("<I", data, payload + 8)
        elif chunk_id == b"data" and byte_rate > 0:
            available = min(size, len(data) - payload)
            return available * 1000 // byte_rate
    return 0


def ogg_opus_duration_ms(data: bytes) -> int:
    """Duration from the last Ogg page's granule position minus the Opus pre-skip; 0 if this is not Ogg Opus."""
    if not data.startswith(b"OggS"):
        return 0
    head = data.find(b"OpusHead")
    if head < 0 or head + 12 > len(data):
        return 0
    pre_skip: int = struct.unpack_from("<H", data, head + 10)[0]
    end = len(data)
    while (page := data.rfind(b"OggS", 0, end)) >= 0:
        if page + 14 <= len(data) and data[page + 4] == 0:
            granule: int = struct.unpack_from("<q", data, page + 6)[0]
            if granule >= 0:
                return max(0, granule - pre_skip) * 1000 // _OPUS_GRANULE_RATE
        end = page
    return 0


def audio_duration_ms(data: bytes, mime: str) -> int:
    """Best-effort duration for `Transcript.audio_ms`; 0 when the container is not one we read."""
    if is_wav(data):
        return wav_duration_ms(data)
    if data.startswith(b"OggS") or "ogg" in mime or "opus" in mime:
        return ogg_opus_duration_ms(data)
    return 0


# --- retry delays --------------------------------------------------------------------------------------------------
def retry_after_s(value: str | None, *, date: str | None = None) -> float | None:
    """A `Retry-After` header in seconds: delta-seconds, or an HTTP date measured against the response `Date`
    (no local clock involved). `None` when absent or unreadable."""
    if not value:
        return None
    text = value.strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        return max(0.0, seconds) if math.isfinite(seconds) else None
    if not date:
        return None
    try:
        delta = (parsedate_to_datetime(text) - parsedate_to_datetime(date)).total_seconds()
    except (TypeError, ValueError):
        return None
    return max(0.0, delta)


def duration_s(value: object) -> float | None:
    """A protobuf JSON duration such as Google's `RetryInfo.retryDelay` (`"17s"`, `"1.5s"`) in seconds."""
    if not isinstance(value, str) or not value.endswith("s"):
        return None
    try:
        seconds = float(value[:-1])
    except ValueError:
        return None
    return max(0.0, seconds) if math.isfinite(seconds) else None
