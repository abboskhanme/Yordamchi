"""`adapters/speech_common`: the pure helpers both STT adapters share."""

import pytest

from tests.integration.stt_harness import GOOGLE_KEY, OPENROUTER_KEY, make_ogg_opus, make_wav, streamed_wav
from yordamchi.adapters import speech_common as sc
from yordamchi.core.errors import AuthFailed


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  Salom, dunyo.  ", "Salom, dunyo."),
        ('"Salom"', "Salom"),
        ("«Привет, BonviHR»", "Привет, BonviHR"),
        ("[silence]", ""),
        ("(no speech)", ""),
        ("Nutq yo'q.", ""),
        (None, ""),
        ("«a» va «b»", "«a» va «b»"),  # quoted words, not a wrapper (OvozYoz stripped these)
        ('"a" yoki "b"', '"a" yoki "b"'),
        ("'BonviHR'ga o'tamiz'", "'BonviHR'ga o'tamiz'"),
    ],
)
def test_clean_transcript_strips_wrappers_and_no_speech_markers(raw: str | None, expected: str) -> None:
    assert sc.clean_transcript(raw) == expected


def test_unique_terms_keeps_order_drops_blanks_and_caps() -> None:
    assert sc.unique_terms([" BonviHR", "HRMentor", "BonviHR", "", "  ", "X"], limit=2) == ["BonviHR", "HRMentor"]


def test_prompt_hint_names_languages_and_first_200_terms() -> None:
    hint = sc.prompt_hint(["uz-UZ", " ", "ru-RU"], [f"t{index}" for index in range(250)])
    assert hint.startswith("Transcribe this audio. Expected languages: uz-UZ, ru-RU. Spell these terms exactly: t0, ")
    assert hint.endswith("t199.")
    assert sc.prompt_hint([], []) == "Transcribe this audio."


def test_instruction_carries_the_uc05_language_rules() -> None:
    text = sc.STT_INSTRUCTION
    assert "Never translate" in text
    assert "Uzbek in Latin script (oʻ, gʻ, sh, ch)" in text
    assert "Russian in Cyrillic" in text


@pytest.mark.parametrize(
    ("value", "date", "expected"),
    [
        ("30", None, 30.0),
        ("1.5", None, 1.5),
        ("-4", None, 0.0),
        ("Wed, 21 Oct 2026 07:28:30 GMT", "Wed, 21 Oct 2026 07:28:00 GMT", 30.0),
        ("Wed, 21 Oct 2026 07:28:30 GMT", None, None),  # a date without the response Date cannot be measured
        ("soon", "Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("inf", None, None),
        (None, None, None),
    ],
)
def test_retry_after_reads_seconds_and_http_dates(value: str | None, date: str | None, expected: float | None) -> None:
    assert sc.retry_after_s(value, date=date) == expected


@pytest.mark.parametrize(("value", "expected"), [("17s", 17.0), ("0.5s", 0.5), ("17", None), (17, None), ("xs", None)])
def test_duration_reads_protobuf_json_durations(value: object, expected: float | None) -> None:
    assert sc.duration_s(value) == expected


def test_wav_header_is_fixed_and_its_duration_read() -> None:
    streamed = streamed_wav(1500)
    fixed = sc.fix_wav_header(streamed)
    assert len(fixed) == len(streamed)
    assert int.from_bytes(fixed[4:8], "little") == len(fixed) - 8
    data = fixed.find(b"data")
    assert int.from_bytes(fixed[data + 4 : data + 8], "little") == len(fixed) - data - 8
    assert sc.wav_duration_ms(fixed) == 1500
    assert sc.wav_duration_ms(streamed) == 1500  # a placeholder size never inflates the duration
    assert sc.fix_wav_header(b"not a wav") == b"not a wav"


def test_audio_durations_for_ogg_opus_wav_and_unknown() -> None:
    assert sc.ogg_opus_duration_ms(make_ogg_opus(2340)) == 2340
    assert sc.audio_duration_ms(make_ogg_opus(800), "audio/ogg") == 800
    assert sc.audio_duration_ms(make_wav(250), "audio/wav") == 250
    assert sc.audio_duration_ms(b"OggS but not opus", "audio/ogg") == 0
    assert sc.audio_duration_ms(b"\xff\xfb mp3", "audio/mpeg") == 0


def test_key_kind_accepts_exactly_one_whole_key() -> None:
    assert sc.key_kind(GOOGLE_KEY) == "google_api_key"
    assert sc.key_kind(f"  {OPENROUTER_KEY}\n") == "openrouter_api_key"
    assert sc.key_kind(f"kalit: {GOOGLE_KEY}") is None
    assert sc.key_kind("hello") is None


async def test_resolve_key_reads_a_callable_and_refuses_empty() -> None:
    async def source() -> str:
        return f" {GOOGLE_KEY} "

    assert await sc.resolve_key(source, provider="google") == GOOGLE_KEY
    with pytest.raises(AuthFailed):
        await sc.resolve_key("", provider="google")
