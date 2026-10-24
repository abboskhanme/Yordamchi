"""adapters/tts_common: sentence segments (S8 fallback), the style text, WAV splitting, and the ffmpeg encode with
a fake ffmpeg: input formats, failures, timeouts, and concurrent segments joined in order."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from tests.integration.stt_harness import make_ogg_opus, make_wav
from tests.integration.tts_harness import fake_ffmpeg, ffmpeg_argv, ffmpeg_stdin
from yordamchi.adapters.tts_common import (
    MP3_INPUT,
    AudioInput,
    OpenedAudio,
    encode_voice,
    opus_argv,
    pcm_input,
    require_ffmpeg,
    sentence_segments,
    split_wav,
    style_text,
    voice_note,
)
from yordamchi.core.errors import CallTimedOut, NotInstalled, ProviderUnavailable, QuotaExhausted


async def _replay(*parts: bytes) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _opened(source: AudioInput, *parts: bytes, closed: list[str] | None = None, name: str = "") -> OpenedAudio:
    async def aclose() -> None:
        if closed is not None:
            closed.append(name)

    return OpenedAudio(source=source, first=parts[0], rest=_replay(*parts[1:]), aclose=aclose)


# --- text ----------------------------------------------------------------------------------------------------------
def test_segments_group_whole_sentences_up_to_the_word_limit() -> None:
    text = "Bir ikki uch. To'rt besh! Olti yetti sakkiz to'qqiz o'n? Tamom."
    assert sentence_segments(text, 5) == ["Bir ikki uch. To'rt besh!", "Olti yetti sakkiz to'qqiz o'n?", "Tamom."]
    assert sentence_segments(text, 0) == [text]
    assert sentence_segments("  bitta\n  gap  ", 3) == ["bitta gap"]
    assert sentence_segments("   ", 5) == []


def test_a_sentence_longer_than_the_limit_stays_whole() -> None:
    assert sentence_segments("Bu juda uzun gap edi. Qisqa.", 2) == ["Bu juda uzun gap edi.", "Qisqa."]


def test_style_text_carries_the_style_and_the_language() -> None:
    assert style_text("iliq, aniq.", "uz-UZ") == "iliq, aniq. Language: uz-UZ"
    assert style_text("", "uz-UZ") == "Language: uz-UZ"
    assert style_text("iliq", " ") == "iliq"


def test_split_wav_returns_raw_pcm_and_its_format() -> None:
    wav = make_wav(100, rate=16_000)
    source, pcm = split_wav(wav)
    assert (source, pcm) == (pcm_input(16_000), wav[44:])
    assert split_wav(b"\x00\x01") == (pcm_input(), b"\x00\x01")


def test_opus_argv_reads_raw_pcm_with_its_rate_and_mp3_without() -> None:
    pcm = opus_argv(Path("/x/ffmpeg"), pcm_input())
    assert pcm[:4] == ["/x/ffmpeg", "-hide_banner", "-loglevel", "error"]
    assert pcm[4:12] == ["-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "pipe:0"]
    assert opus_argv(Path("/x/ffmpeg"), MP3_INPUT)[4:8] == ["-f", "mp3", "-i", "pipe:0"]


def test_require_ffmpeg_needs_an_existing_file(tmp_path: Path) -> None:
    with pytest.raises(NotInstalled):
        require_ffmpeg(None)
    with pytest.raises(NotInstalled):
        require_ffmpeg(tmp_path / "ffmpeg")
    script = fake_ffmpeg(tmp_path / "bin")
    assert require_ffmpeg(script) == script


# --- encode ----------------------------------------------------------------------------------------------------------
async def test_encode_returns_the_note_and_its_duration(tmp_path: Path) -> None:
    script = fake_ffmpeg(tmp_path / "bin", output=make_ogg_opus(2500))
    data, duration_ms = await encode_voice(
        script, pcm_input(), _replay(b"\x00\x00" * 10, b"\x01\x00"), idle_timeout=5, provider="openrouter"
    )
    assert (data, duration_ms) == (make_ogg_opus(2500), 2500)
    assert ffmpeg_stdin(script) == b"\x00\x00" * 10 + b"\x01\x00"


@pytest.mark.parametrize("fail", [True, False])
async def test_an_ffmpeg_failure_or_non_ogg_output_is_a_non_retryable_encoder_failure(
    tmp_path: Path, fail: bool
) -> None:
    script = fake_ffmpeg(tmp_path / "bin", output=b"not an ogg", fail=fail)
    with pytest.raises(ProviderUnavailable) as caught:
        await encode_voice(script, MP3_INPUT, _replay(b"x"), idle_timeout=5, provider="edge")
    assert (caught.value.retryable, caught.value.params["provider"]) == (False, "ffmpeg")


async def test_a_hung_ffmpeg_is_a_timeout(tmp_path: Path) -> None:
    script = fake_ffmpeg(tmp_path / "bin", delay_s=5)
    with pytest.raises(CallTimedOut):
        await encode_voice(script, MP3_INPUT, _replay(b"x"), idle_timeout=0.3, provider="edge")


async def test_an_unrunnable_ffmpeg_is_not_installed(tmp_path: Path) -> None:
    broken = tmp_path / "ffmpeg"
    broken.write_text("not executable", encoding="utf-8")
    with pytest.raises(NotInstalled):
        await encode_voice(broken, MP3_INPUT, _replay(b"x"), idle_timeout=1, provider="edge")


# --- one note --------------------------------------------------------------------------------------------------------
async def test_one_segment_streams_into_ffmpeg_and_releases_the_provider(tmp_path: Path) -> None:
    script = fake_ffmpeg(tmp_path / "bin")
    closed: list[str] = []
    asked: list[str] = []

    async def open_segment(text: str) -> OpenedAudio:
        asked.append(text)
        return _opened(pcm_input(), b"\x01\x00", b"\x02\x00", closed=closed, name=text)

    note = await voice_note(
        ffmpeg=script, text="Salom. Xayr.", segment_words=0, open_segment=open_segment, idle_timeout=5, provider="p"
    )
    assert (asked, closed, note.requests, note.duration_ms) == (["Salom. Xayr."], ["Salom. Xayr."], 1, 1500)
    assert ffmpeg_stdin(script) == b"\x01\x00\x02\x00"
    assert "s16le" in ffmpeg_argv(script)


async def test_segments_run_concurrently_and_are_fed_in_text_order(tmp_path: Path) -> None:
    script = fake_ffmpeg(tmp_path / "bin")
    running = 0
    most = 0

    async def open_segment(text: str) -> OpenedAudio:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.2 if text.startswith("Bir") else 0.05)
        running -= 1
        return _opened(MP3_INPUT, text[:3].encode(), b"|")

    note = await voice_note(
        ffmpeg=script, text="Bir. Ikki. Uch.", segment_words=1, open_segment=open_segment, idle_timeout=5, provider="p"
    )
    assert most == 3
    assert ffmpeg_stdin(script) == b"Bir|Ikk|Uch|"
    assert note.requests == 3


async def test_the_first_failing_segment_is_raised_and_the_others_are_cancelled(tmp_path: Path) -> None:
    script = fake_ffmpeg(tmp_path / "bin")
    cancelled: list[str] = []

    async def open_segment(text: str) -> OpenedAudio:
        if text.startswith("Ikki"):
            raise QuotaExhausted(provider="openrouter")
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(text)
            raise
        return _opened(MP3_INPUT, b"x")

    with pytest.raises(QuotaExhausted):
        await voice_note(
            ffmpeg=script,
            text="Bir. Ikki. Uch.",
            segment_words=1,
            open_segment=open_segment,
            idle_timeout=5,
            provider="p",
        )
    assert sorted(cancelled) == ["Bir.", "Uch."]


async def test_segments_in_different_formats_cannot_be_joined(tmp_path: Path) -> None:
    script = fake_ffmpeg(tmp_path / "bin")

    async def open_segment(text: str) -> OpenedAudio:
        return _opened(MP3_INPUT if text.startswith("Bir") else pcm_input(), b"x")

    with pytest.raises(ProviderUnavailable) as caught:
        await voice_note(
            ffmpeg=script, text="Bir. Ikki.", segment_words=1, open_segment=open_segment, idle_timeout=5, provider="p"
        )
    assert caught.value.retryable is False


async def test_a_stalled_segment_is_a_timeout(tmp_path: Path) -> None:
    script = fake_ffmpeg(tmp_path / "bin")

    async def slow_rest() -> AsyncIterator[bytes]:
        await asyncio.sleep(5)
        yield b"late"

    async def open_segment(text: str) -> OpenedAudio:
        return OpenedAudio(source=MP3_INPUT, first=b"x", rest=slow_rest())

    with pytest.raises(CallTimedOut):
        await voice_note(
            ffmpeg=script, text="Bir. Ikki.", segment_words=1, open_segment=open_segment, idle_timeout=0.2, provider="p"
        )


async def test_empty_text_is_a_caller_bug(tmp_path: Path) -> None:
    async def open_segment(text: str) -> OpenedAudio:
        raise AssertionError(text)

    with pytest.raises(ValueError, match="empty"):
        await voice_note(
            ffmpeg=fake_ffmpeg(tmp_path / "bin"),
            text="  ",
            segment_words=0,
            open_segment=open_segment,
            idle_timeout=1,
            provider="p",
        )
