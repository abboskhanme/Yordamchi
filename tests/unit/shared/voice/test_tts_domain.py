"""voice.domain, the TTS half (SPEC §3.2.11 step 3 and 6, §3.2.13 step 6): provider order, voice by gender, the
spoken text, outcomes, notices and usage rows. Pure: every input is an argument."""

from datetime import UTC, datetime

import pytest

from tests.factories import make_default_snapshot, make_speech_audio
from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, NotInstalled, ProviderUnavailable, QuotaExhausted
from yordamchi.core.paths import BotName
from yordamchi.shared.cost.domain import Provider, require_valid
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.settings.registry import Setting, SettingValue
from yordamchi.shared.voice import domain
from yordamchi.shared.voice.domain import EDGE, GOOGLE, OPENROUTER, SkipReason, TtsAttempt, TtsResult


def snap_with(*changes: tuple[Setting[SettingValue], SettingValue], bot: BotName = BotName.PERSONAL) -> Snapshot:
    base = make_default_snapshot(bot)
    values = dict(base.values)
    values.update({setting.key: value for setting, value in changes})
    return Snapshot(bot=base.bot, version=base.version, values=values, sources=base.sources)


# --- provider order --------------------------------------------------------------------------------------------------
def test_auto_is_openrouter_then_google_then_edge() -> None:
    assert domain.pick_tts("auto", {EDGE, GOOGLE, OPENROUTER}) == (OPENROUTER, GOOGLE, EDGE)


def test_google_is_used_only_when_it_is_available() -> None:
    assert domain.pick_tts("auto", {OPENROUTER, EDGE}) == (OPENROUTER, EDGE)
    assert domain.pick_tts("auto", {EDGE}) == (EDGE,)
    assert domain.pick_tts("auto", ()) == ()


def test_a_named_provider_goes_first_and_the_rest_stay_as_fallbacks() -> None:
    assert domain.pick_tts("edge", {EDGE, GOOGLE, OPENROUTER}) == (EDGE, OPENROUTER, GOOGLE)
    assert domain.pick_tts("google", {EDGE, OPENROUTER}) == (OPENROUTER, EDGE)


def test_off_is_text_only() -> None:
    assert domain.pick_tts("off", {EDGE, GOOGLE, OPENROUTER}) == ()


def test_each_provider_gets_its_own_model_setting() -> None:
    snap = make_default_snapshot(BotName.PERSONAL)
    assert domain.tts_model(OPENROUTER, snap) == snap[defaults.TTS_OPENROUTER_MODEL]
    assert domain.tts_model(GOOGLE, snap) == snap[defaults.TTS_GOOGLE_MODEL]
    assert domain.tts_model(EDGE, snap) == domain.EDGE_MODEL


def test_google_speaks_on_a_live_model_and_falls_back_to_flash_tts() -> None:
    """Owner request 2026-11-01: `gemini-3.8-flash-tts` has a daily cap, the Live models do not."""
    snap = make_default_snapshot(BotName.PERSONAL)
    assert domain.tts_model(GOOGLE, snap) == "gemini-3.8-live"
    assert domain.tts_fallback_model(GOOGLE, snap) == "gemini-3.8-flash-tts"
    assert domain.tts_fallback_model(OPENROUTER, snap) == domain.tts_fallback_model(EDGE, snap) == ""


def test_the_tts_timeout_grows_with_the_words() -> None:
    snap = snap_with((defaults.TIMEOUTS_TTS_S, 15.0), (defaults.TIMEOUTS_TTS_PER_WORD_S, 0.6))
    assert domain.tts_timeout(snap, "") == 15.0
    assert domain.tts_timeout(snap, "Salom, bugun havo   yaxshi.") == pytest.approx(15.0 + 4 * 0.6)
    seventy = " ".join(["so'z"] * 70)  # a voice.max_words note: ~37 s of Live audio fits
    assert domain.tts_timeout(snap, seventy) == pytest.approx(57.0)


# --- voice by gender ("ovozingni ayolga o'zgartir") ------------------------------------------------------------------
def test_defaults_are_the_configured_female_voices() -> None:
    snap = make_default_snapshot(BotName.PERSONAL)
    assert domain.tts_voice(OPENROUTER, snap) == snap[defaults.VOICE_NAME]
    assert domain.tts_voice(EDGE, snap) == snap[defaults.TTS_EDGE_VOICE]


def test_a_male_gender_replaces_a_female_voice_and_picks_the_male_edge_voice() -> None:
    snap = snap_with((defaults.VOICE_GENDER, "male"))
    assert domain.tts_voice(GOOGLE, snap) == snap[defaults.TTS_MALE_VOICE]
    assert domain.tts_voice(EDGE, snap) == snap[defaults.TTS_EDGE_MALE_VOICE]


def test_back_to_female_from_a_male_voice_name() -> None:
    snap = snap_with((defaults.VOICE_GENDER, "female"), (defaults.VOICE_NAME, "Puck"))
    assert domain.tts_voice(OPENROUTER, snap) == snap[defaults.TTS_FEMALE_VOICE]


def test_a_voice_name_of_the_chosen_gender_or_an_unknown_one_is_kept() -> None:
    assert domain.tts_voice(GOOGLE, snap_with((defaults.VOICE_GENDER, "male"), (defaults.VOICE_NAME, "orus"))) == "orus"
    custom = snap_with((defaults.VOICE_GENDER, "male"), (defaults.VOICE_NAME, "voicekey_owner"))
    assert domain.tts_voice(GOOGLE, custom) == "voicekey_owner"


def test_the_gender_tables_cover_the_30_gemini_voices_once() -> None:
    assert len(domain.GEMINI_FEMALE_VOICES) + len(domain.GEMINI_MALE_VOICES) == 30
    assert not domain.GEMINI_FEMALE_VOICES & domain.GEMINI_MALE_VOICES


# --- the spoken text -------------------------------------------------------------------------------------------------
def test_speakable_drops_code_urls_markdown_and_emoji() -> None:
    text = (
        "## Javob\n**Muhim:** batafsil https://example.com/x?y=1 da 🙂👍🏽.\n"
        "- birinchi\n1. ikkinchi\n```python\nprint('x')\n```\n`kod` [havola](https://a.b) bor."
    )
    assert domain.speakable(text, 70) == "Javob Muhim: batafsil da. birinchi ikkinchi havola bor."


def test_speakable_cuts_at_the_last_sentence_end_within_the_word_cap() -> None:
    text = "Bir ikki uch. To'rt besh olti. Yetti sakkiz to'qqiz o'n."
    assert domain.speakable(text, 7) == "Bir ikki uch. To'rt besh olti."
    assert domain.speakable(text, 100) == text


def test_a_first_sentence_over_the_cap_is_cut_at_the_word_limit() -> None:
    assert domain.speakable("bir, ikki, uch, to'rt, besh", 3) == "bir, ikki, uch."


def test_speakable_of_nothing_is_empty() -> None:
    assert domain.speakable("```\ncode only\n```  🙂 https://x.y", 70) == ""


def test_the_default_cap_keeps_a_voice_summary_near_30_seconds() -> None:
    long = " ".join(f"so'z{i}." for i in range(200))
    assert len(domain.speakable(long, make_default_snapshot(BotName.PERSONAL)[defaults.VOICE_MAX_WORDS]).split()) <= 70


# --- outcomes and notices ------------------------------------------------------------------------------------------
def test_an_encoder_failure_stops_the_chain_and_a_provider_failure_does_not() -> None:
    missing = domain.tts_attempt(OPENROUTER, NotInstalled(binary="ffmpeg"))
    broken = domain.tts_attempt(GOOGLE, ProviderUnavailable(provider="ffmpeg", retryable=False))
    quota = domain.tts_attempt(OPENROUTER, QuotaExhausted(provider="openrouter"))
    assert (missing.encoder, broken.encoder, quota.encoder) == (True, True, False)
    assert not domain.tts_falls_back(missing)
    assert not domain.tts_falls_back(broken)
    assert domain.tts_falls_back(quota)
    assert domain.stopped(missing, [missing]).reason is SkipReason.NO_FFMPEG
    assert domain.stopped(broken, [broken]).reason is SkipReason.ENCODE


def test_a_note_from_a_fallback_says_who_failed_and_why() -> None:
    result = TtsResult(
        audio=make_speech_audio(provider=EDGE, model=domain.EDGE_MODEL),
        failed=(TtsAttempt(OPENROUTER, ErrorCode.QUOTA_EXHAUSTED), TtsAttempt(GOOGLE, ErrorCode.AUTH_FAILED)),
    )
    [notice] = domain.tts_notices(result)
    assert notice.key == "fallback-openrouter-quota_exhausted"
    assert notice.text == S.TTS_FELL_BACK(
        provider=S.SPEECH_OPENROUTER(), problem=S.STT_PROBLEM_QUOTA(), fallback=S.SPEECH_EDGE()
    )


def test_a_first_choice_note_says_nothing() -> None:
    assert domain.tts_notices(TtsResult(audio=make_speech_audio())) == ()


@pytest.mark.parametrize("reason", [SkipReason.DISABLED, SkipReason.EMPTY])
def test_the_owners_own_switch_and_an_empty_script_are_silent(reason: SkipReason) -> None:
    assert domain.tts_notices(TtsResult(reason=reason)) == ()


def test_every_provider_failing_lists_each_one() -> None:
    result = TtsResult(
        reason=SkipReason.PROVIDER,
        failed=(TtsAttempt(OPENROUTER, ErrorCode.RATE_LIMITED), TtsAttempt(EDGE, ErrorCode.PROVIDER_UNAVAILABLE)),
    )
    [notice] = domain.tts_notices(result)
    assert notice.key == "skipped-provider_unavailable-rate_limited"
    assert "OpenRouter (Gemini) — so'rovlar limitiga urildi; Edge TTS — javob bermayapti" in notice.text


@pytest.mark.parametrize(
    ("reason", "entry"),
    [
        (SkipReason.NO_FFMPEG, S.TTS_REASON_NO_FFMPEG),
        (SkipReason.ENCODE, S.TTS_REASON_ENCODE),
        (SkipReason.NO_PROVIDER, S.TTS_REASON_NO_PROVIDER),
        (SkipReason.SUMMARY, S.TTS_REASON_SUMMARY),
    ],
)
def test_a_skipped_voice_says_why(reason: SkipReason, entry: S.Msg) -> None:
    [notice] = domain.tts_notices(TtsResult(reason=reason))
    assert (notice.key, notice.text) == (f"skipped-{reason.value}", S.VOICE_SKIPPED(reason=entry()))


def test_notice_refs_repeat_within_the_hour_and_change_with_it() -> None:
    at = datetime(2026, 10, 24, 14, 5, tzinfo=UTC)
    same = datetime(2026, 10, 24, 14, 59, tzinfo=UTC)
    later = datetime(2026, 10, 24, 15, 0, tzinfo=UTC)
    key = "skipped-no_ffmpeg"
    assert domain.notice_ref(key, at) == domain.notice_ref(key, same) == "tts-skipped-no_ffmpeg-2026102414"
    assert domain.notice_ref(key, later) != domain.notice_ref(key, at)


def test_result_provider_is_the_answering_one_else_the_last_failure() -> None:
    assert TtsResult(audio=make_speech_audio(provider=GOOGLE)).provider == GOOGLE
    assert TtsResult(failed=(TtsAttempt(OPENROUTER, ErrorCode.AUTH_FAILED),)).provider == OPENROUTER
    assert TtsResult(reason=SkipReason.DISABLED).provider is None


# --- usage -----------------------------------------------------------------------------------------------------------
def test_usage_rows_are_valid_and_edge_is_never_paid() -> None:
    edge = domain.tts_usage_of(make_speech_audio(provider=EDGE, model=domain.EDGE_MODEL), duration_ms=2100)
    paid = domain.tts_usage_of(make_speech_audio(cost_usd_micros=4_200), duration_ms=3000)
    for usage in (edge, paid):
        require_valid(usage)
    assert (edge.provider, edge.is_paid_api, edge.cost_usd_micros) == (Provider.EDGE, False, 0)
    assert (paid.provider, paid.is_paid_api, paid.cost_usd_micros) == (Provider.OPENROUTER, True, 4_200)
    assert paid.units == {"audio_ms": 24_000, "characters": 380, "requests": 1}
    assert edge.source == "tts"


def test_voice_duration_rounds_a_part_second_up() -> None:
    assert domain.voice_duration_s(make_speech_audio(duration_ms=24_001)) == 25
    assert domain.voice_duration_s(make_speech_audio(duration_ms=400)) == 1
    assert domain.voice_duration_s(make_speech_audio(duration_ms=None)) is None


# --- voice samples (UC-34 "ovozing yoqmadi") -------------------------------------------------------------------------
def test_gemini_samples_skip_the_voice_in_use_and_carry_their_gender() -> None:
    samples = domain.sample_voices(GOOGLE, snap_with())  # voice.name Kore is in use

    assert [(sample.number, sample.voice) for sample in samples] == [(1, "Charon"), (2, "Aoede"), (3, "Puck")]
    assert samples[0].changes == (("voice.name", "Charon"), ("voice.gender", "male"))
    assert samples[1].changes == (("voice.name", "Aoede"), ("voice.gender", "female"))
    assert domain.pick_sample(samples, 2) == samples[1]
    assert domain.pick_sample(samples, 4) is None


def test_the_sample_count_and_list_are_settings() -> None:
    snap = snap_with((defaults.VOICE_SAMPLE_COUNT, 2), (defaults.VOICE_SAMPLE_VOICES, "Leda,Kore,Orus"))
    assert [sample.voice for sample in domain.sample_voices(OPENROUTER, snap)] == ["Leda", "Orus"]


def test_edge_offers_its_two_uzbek_voices_by_gender() -> None:
    samples = domain.sample_voices(EDGE, snap_with())

    assert [sample.voice for sample in samples] == ["uz-UZ-MadinaNeural", "uz-UZ-SardorNeural"]
    assert [sample.changes for sample in samples] == [(("voice.gender", "female"),), (("voice.gender", "male"),)]
