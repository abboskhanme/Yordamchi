"""The OGG/Opus encode on the real static ffmpeg (`DATA/bin/ffmpeg`, or `YORDAMCHI_TEST_FFMPEG`): the argv the TTS
adapters use must give a voice note Telegram plays, with the duration read back from its last page. Skipped when
the binary is absent (CI, a Mac before the installer ran)."""

import math
import struct
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.integration.tts_harness import PCM_RATE, real_ffmpeg
from yordamchi.adapters.tts_common import encode_voice, pcm_input

FFMPEG = real_ffmpeg()
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="no ffmpeg at DATA/bin/ffmpeg (or YORDAMCHI_TEST_FFMPEG)")


def tone(ms: int, *, hz: float = 440.0) -> bytes:
    samples = PCM_RATE * ms // 1000
    return b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / PCM_RATE))) for i in range(samples))


async def chunked(data: bytes, size: int = 4096) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def binary() -> Path:
    assert FFMPEG is not None
    return FFMPEG


async def test_pcm_becomes_a_mono_48_khz_ogg_opus_note_of_the_same_length() -> None:
    data, duration_ms = await encode_voice(
        binary(), pcm_input(), chunked(tone(2000)), idle_timeout=10, provider="openrouter"
    )
    assert data.startswith(b"OggS")
    head = data.find(b"OpusHead")
    assert head > 0
    channels, rate = data[head + 9], struct.unpack_from("<I", data, head + 12)[0]
    assert (channels, rate) == (1, 48_000)  # mono, resampled to 48 kHz before the encoder (`-ar 48000`)
    assert abs(duration_ms - 2000) <= 100


async def test_pieces_fed_one_after_another_make_one_note() -> None:
    pieces = [tone(700, hz=330.0), tone(800, hz=550.0)]

    async def both() -> AsyncIterator[bytes]:
        for piece in pieces:
            async for chunk in chunked(piece):
                yield chunk

    _, duration_ms = await encode_voice(binary(), pcm_input(), both(), idle_timeout=10, provider="google")
    assert abs(duration_ms - 1500) <= 100
