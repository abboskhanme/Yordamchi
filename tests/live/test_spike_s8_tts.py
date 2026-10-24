"""Spike S8 (SPEC §11, T71): can streaming TTS piped into the Opus encoder deliver the voice note within 12 s of the
text (SPEC §3.2.13 step 6)? Measured per provider on a ~60-word Uzbek summary (`voice.max_words` 70 ≈ 20–30 s of
speech), once as one request and once cut into sentence segments synthesized in parallel (`tts.segment_words`, the
S8 fallback). The note is ready when `synthesize` returns; `sendVoice` adds one upload on top.

Opt in with `pytest -m live -s tests/live/test_spike_s8_tts.py`; OpenRouter and Google need
`YORDAMCHI_LIVE_OPENROUTER_API_KEY` / `YORDAMCHI_LIVE_GOOGLE_API_KEY`, Edge needs no key; all need the real ffmpeg
(`DATA/bin/ffmpeg`, or `YORDAMCHI_TEST_FFMPEG`). If whole notes miss the target and segments meet it, set
`tts.segment_words` (≈ 20) and record the decision in docs/SPIKES.md.
"""

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from tests.integration.stt_harness import live_key
from tests.integration.tts_harness import real_ffmpeg
from yordamchi.adapters.edge_tts.tts import EdgeSynthesizer
from yordamchi.adapters.gemini.tts import GeminiSynthesizer
from yordamchi.adapters.openrouter.tts import OpenRouterSynthesizer
from yordamchi.ports.speech import Synthesizer

pytestmark = pytest.mark.live

TARGET_S = 12.0  # SPEC §3.2.13 step 6: the voice note ≤ 12 s after the text
SEGMENT_WORDS = 20
SUMMARY = (
    "Bugun Toshkentda havo quyoshli bo'ladi, kunduzi o'n sakkiz darajagacha isiydi. Kechqurun shamol kuchayadi, "
    "shuning uchun issiqroq kiyining. Ertaga ertalab yomg'ir boshlanadi va tushgacha davom etadi, soyabonni unutmang. "
    "Yo'llarda tirbandlik odatdagidan ko'proq bo'lishi mumkin, uydan o'n besh daqiqa oldin chiqing. Hafta oxirida "
    "havo yana ochiladi va sayr uchun qulay bo'ladi."
)
STYLE = "tabiiy o'zbek talaffuzi, iliq, aniq"


def _ffmpeg() -> Path:
    ffmpeg = real_ffmpeg()
    if ffmpeg is None:
        pytest.skip("no ffmpeg at DATA/bin/ffmpeg (or YORDAMCHI_TEST_FFMPEG)")
    return ffmpeg


def _openrouter(ffmpeg: Path) -> tuple[Synthesizer, str, str]:
    return (
        OpenRouterSynthesizer(api_key=live_key("YORDAMCHI_LIVE_OPENROUTER_API_KEY"), ffmpeg=ffmpeg),
        "google/gemini-3.8-flash-tts",
        "Kore",
    )


def _google(ffmpeg: Path) -> tuple[Synthesizer, str, str]:
    return (
        GeminiSynthesizer(api_key=live_key("YORDAMCHI_LIVE_GOOGLE_API_KEY"), ffmpeg=ffmpeg),
        "gemini-3.8-flash-tts",
        "Kore",
    )


def _edge(ffmpeg: Path) -> tuple[Synthesizer, str, str]:
    return EdgeSynthesizer(ffmpeg=ffmpeg), "edge-tts", "uz-UZ-MadinaNeural"


@pytest.mark.parametrize("segment_words", [0, SEGMENT_WORDS], ids=["whole", "segments"])
@pytest.mark.parametrize("build", [_openrouter, _google, _edge], ids=["openrouter", "google", "edge"])
async def test_voice_note_is_ready_within_12_s(
    build: Callable[[Path], tuple[Synthesizer, str, str]], segment_words: int
) -> None:
    synthesizer, model, voice = build(_ffmpeg())
    started = time.monotonic()
    try:
        audio = await synthesizer.synthesize(
            SUMMARY,
            voice=voice,
            style=STYLE,
            language_code="uz-UZ",
            model=model,
            segment_words=segment_words,
            timeout=30,
        )
    finally:
        close = getattr(synthesizer, "aclose", None)
        if close is not None:
            await close()
    elapsed = time.monotonic() - started
    print(  # noqa: T201  # the spike's measurement, read with -s
        f"S8 {audio.provider} segments={segment_words}: ready in {elapsed:.2f} s, "
        f"{(audio.duration_ms or 0) / 1000:.1f} s of speech, {len(audio.data)} bytes"
    )
    assert audio.data.startswith(b"OggS")
    assert (audio.duration_ms or 0) > 10_000  # ~60 words is well over 10 s of speech
    assert elapsed <= TARGET_S
