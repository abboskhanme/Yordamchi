"""voice.service, the TTS half, on a real SQLite file with scripted synthesizer fakes and the real outbox (SPEC
§3.2.11 step 3 and 6, §3.2.13 step 6; UC-30 voice): the provider chain OpenRouter → Google (only with a key) → Edge
→ text only, one notice per fallback or skip (at most once per reason per hour), the voice after the text, the
settings of this job reaching the provider, and the usage rows."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.factories import (
    FAKE_KEYS,
    OWNER_ID,
    Fakes,
    add_usage,
    drain_outbox,
    make_owner_origin,
    make_speech_audio,
    make_synthesizers,
    make_usage,
    store_secret,
    usd,
)
from tests.fakes.clock import FakeClock
from tests.fakes.speech import FakeSynthesizer
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ErrorCode,
    NotInstalled,
    ProviderUnavailable,
    QuotaExhausted,
    RateLimited,
)
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.cost import store as cost_store
from yordamchi.shared.cost.domain import Provider
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.registry import AnySetting
from yordamchi.shared.voice import domain, service
from yordamchi.shared.voice.domain import EDGE, GOOGLE, OPENROUTER, SkipReason, TtsAttempt

ANSWER = "Toshkentda bugun quyoshli, +18°. Ertaga yomg'ir kutilmoqda, soyabon oling. Batafsil: https://ob-havo.uz"
SCRIPT = "Bugun quyoshli, o'n sakkiz daraja. Ertaga yomg'ir, soyabon oling."


@pytest.fixture
def synthesizers(fakes: Fakes) -> dict[str, FakeSynthesizer]:
    return make_synthesizers(fakes.clock)


@pytest.fixture
def deps(personal_deps: PersonalDeps, synthesizers: dict[str, FakeSynthesizer]) -> PersonalDeps:
    return replace(personal_deps, synthesizers=synthesizers)


async def _keys(deps: SharedDeps, fakes: Fakes, *kinds: str) -> None:
    for kind in kinds:
        await store_secret(deps.db, fakes.keystore, kind, FAKE_KEYS[kind], bot=deps.bot)


async def _set(deps: SharedDeps, setting: AnySetting, value: JsonValue) -> None:
    await settings.record_version(deps, make_owner_origin(deps.bot), setting, value, reason="change")


async def _speak(deps: SharedDeps, *, ref: str = "qa-1", script: str = SCRIPT) -> domain.TtsResult:
    return await service.send_voice(deps, chat_id=OWNER_ID, script=script, ref=ref)


async def _sent(deps: SharedDeps, fakes: Fakes) -> tuple[list[dict[str, object]], list[BotText]]:
    """Send what the outbox holds (one message per chat per second, so a few virtual seconds): (voice notes, texts)
    as the messenger saw them."""
    await drain_outbox(deps, fakes.clock, seconds=5)
    voices = [request.payload for request in fakes.messenger.sent("send_voice")]
    texts = [BotText(str(request.payload["text"])) for request in fakes.messenger.sent("send_text")]
    return voices, texts


async def _usage(deps: SharedDeps) -> list[tuple[str, str, str, bool]]:
    async with deps.db.read() as s:
        rows = await cost_store.list_usage_records(s)
    return [(row.source, row.provider, row.model, row.is_paid_api) for row in rows]


# --- the happy path --------------------------------------------------------------------------------------------------
async def test_openrouter_is_first_and_the_note_follows_the_text(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key", "google_api_key")
    async with deps.db.uow() as s:  # the caller queues its text answer first
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=OWNER_ID,
            text=BotText(ANSWER),
            kind=Kind.RESULT,
            ref="qa-1",
            at=deps.clock.now(),
            max_parts=3,
        )
    synthesizers[OPENROUTER].script(make_speech_audio(provider=OPENROUTER, duration_ms=21_400))

    result = await _speak(deps)

    assert (result.ok, result.provider, result.failed, result.reason) == (True, OPENROUTER, (), None)
    assert synthesizers[GOOGLE].calls == synthesizers[EDGE].calls == []
    await drain_outbox(deps, fakes.clock, seconds=5)
    methods = [request.method for request in fakes.messenger.requests]
    assert methods == ["send_text", "send_voice"]  # text first, then the voice; no notice
    voice = fakes.messenger.sent("send_voice")[0].payload
    assert (voice["mime"], voice["duration_s"]) == ("audio/ogg", 22)
    note = Path(str(voice["path"]))
    assert note.parent.parent == deps.paths.media
    assert (await asyncio.to_thread(note.stat)).st_mode & 0o777 == 0o600
    assert await _usage(deps) == [("tts", "openrouter", "google/gemini-3.8-flash-tts", False)]


async def test_the_jobs_settings_reach_the_provider(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key")
    await _set(deps, defaults.VOICE_STYLE, "shoshilmasdan, Toshkent talaffuzida")
    await _set(deps, defaults.TTS_SEGMENT_WORDS, 30)
    await _set(deps, defaults.TIMEOUTS_TTS_S, 9.0)
    await _set(deps, defaults.TIMEOUTS_TTS_PER_WORD_S, 0.5)
    await _speak(deps, script="**Salom!** 🙂 https://x.uz")
    [call] = synthesizers[OPENROUTER].calls
    snap = await settings.snapshot(deps)
    assert call.text == "Salom!"  # spoken text: no markdown, emoji or URL
    assert (call.voice, call.style, call.language_code) == ("Kore", "shoshilmasdan, Toshkent talaffuzida", "uz-UZ")
    # timeouts.tts_s plus timeouts.tts_per_word_s for its one word; OpenRouter has no second model
    assert (call.model, call.segment_words, call.timeout) == (snap[defaults.TTS_OPENROUTER_MODEL], 30, 9.5)
    assert call.fallback_model == ""


async def test_google_gets_its_live_model_its_fallback_model_and_a_timeout_per_word(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "google_api_key")
    await _set(deps, defaults.TTS_GOOGLE_FALLBACK_MODEL, "gemini-3.9-flash-tts")
    result = await _speak(deps)
    assert result.provider == GOOGLE
    [call] = synthesizers[GOOGLE].calls
    snap = await settings.snapshot(deps)
    assert (call.model, call.fallback_model) == ("gemini-3.8-live", "gemini-3.9-flash-tts")
    words = len(SCRIPT.split())
    assert call.timeout == pytest.approx(snap[defaults.TIMEOUTS_TTS_S] + words * snap[defaults.TIMEOUTS_TTS_PER_WORD_S])


async def test_changing_the_gender_by_chat_changes_the_next_voice(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _set(deps, defaults.VOICE_GENDER, "male")  # "ovozingni erkakka o'zgartir"
    await _speak(deps, ref="qa-1")
    await _set(deps, defaults.VOICE_GENDER, "female")  # "ovozingni ayolga o'zgartir"
    await _speak(deps, ref="qa-2")
    assert [call.voice for call in synthesizers[EDGE].calls] == ["uz-UZ-SardorNeural", "uz-UZ-MadinaNeural"]


async def test_the_script_is_capped_at_voice_max_words(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _set(deps, defaults.VOICE_MAX_WORDS, 12)
    await _speak(deps, script=" ".join(["Bu gap besh so'zli."] * 10))
    [call] = synthesizers[EDGE].calls
    assert len(call.text.split()) <= 12
    assert call.text.endswith(".")


# --- the fallback chain ----------------------------------------------------------------------------------------------
async def test_without_keys_edge_speaks_and_nothing_is_said_about_it(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    result = await _speak(deps)
    assert (result.provider, result.failed) == (EDGE, ())
    assert synthesizers[OPENROUTER].calls == synthesizers[GOOGLE].calls == []
    voices, texts = await _sent(deps, fakes)
    assert (len(voices), texts) == (1, [])
    assert await _usage(deps) == [("tts", "edge", "edge-tts", False)]


async def test_google_is_tried_only_with_its_own_key(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key")
    synthesizers[OPENROUTER].script(QuotaExhausted(provider=OPENROUTER))
    result = await _speak(deps)
    assert result.provider == EDGE
    assert synthesizers[GOOGLE].calls == []
    _, texts = await _sent(deps, fakes)
    assert texts == [
        S.TTS_FELL_BACK(provider=S.SPEECH_OPENROUTER(), problem=S.STT_PROBLEM_QUOTA(), fallback=S.SPEECH_EDGE())
    ]


async def test_openrouter_failing_moves_to_google_then_says_so(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key", "google_api_key")
    synthesizers[OPENROUTER].script(RateLimited(provider=OPENROUTER, retry_after=40))
    synthesizers[GOOGLE].script(make_speech_audio(provider=GOOGLE, model="gemini-3.8-flash-tts"))
    result = await _speak(deps)
    assert (result.provider, result.failed) == (GOOGLE, (TtsAttempt(OPENROUTER, ErrorCode.RATE_LIMITED),))
    voices, texts = await _sent(deps, fakes)
    assert len(voices) == 1
    assert texts == [
        S.TTS_FELL_BACK(
            provider=S.SPEECH_OPENROUTER(), problem=S.STT_PROBLEM_RATE_LIMITED(), fallback=S.SPEECH_GOOGLE()
        )
    ]
    assert [row[1] for row in await _usage(deps)] == ["google"]


async def test_two_failures_fall_through_to_edge(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key", "google_api_key")
    synthesizers[OPENROUTER].script(AuthFailed(provider=OPENROUTER))
    synthesizers[GOOGLE].script(CallTimedOut(operation="tts"))
    result = await _speak(deps)
    assert result.provider == EDGE
    assert [attempt.provider for attempt in result.failed] == [OPENROUTER, GOOGLE]


async def test_every_provider_failing_leaves_the_text_and_one_notice(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key")
    synthesizers[OPENROUTER].script(QuotaExhausted(provider=OPENROUTER))
    synthesizers[EDGE].script(ProviderUnavailable(provider=EDGE))
    result = await _speak(deps)
    assert (result.ok, result.reason) == (False, SkipReason.PROVIDER)
    voices, texts = await _sent(deps, fakes)
    assert voices == []
    [notice] = texts
    assert notice.startswith("Ovozli javob yo'q: OpenRouter (Gemini) — kvota yoki balans tugagan; Edge TTS")
    assert await _usage(deps) == []


async def test_a_missing_ffmpeg_stops_the_chain_with_one_notice(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key", "google_api_key")
    synthesizers[OPENROUTER].script(NotInstalled(binary="ffmpeg"))
    result = await _speak(deps)
    assert result.reason is SkipReason.NO_FFMPEG
    assert synthesizers[GOOGLE].calls == synthesizers[EDGE].calls == []  # they would need ffmpeg too
    _, texts = await _sent(deps, fakes)
    assert texts == [S.VOICE_SKIPPED(reason=S.TTS_REASON_NO_FFMPEG())]


async def test_a_failing_encoder_stops_the_chain(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    synthesizers[EDGE].script(ProviderUnavailable(provider="ffmpeg", retryable=False))
    result = await _speak(deps)
    assert result.reason is SkipReason.ENCODE


async def test_the_same_reason_is_told_once_an_hour(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    for step in range(3):
        synthesizers[EDGE].script(NotInstalled(binary="ffmpeg"))
        await _speak(deps, ref=f"qa-{step}")
    _, texts = await _sent(deps, fakes)
    assert len(texts) == 1
    await fakes.clock.advance(timedelta(hours=1).total_seconds())
    synthesizers[EDGE].script(NotInstalled(binary="ffmpeg"))
    await _speak(deps, ref="qa-9")
    _, texts = await _sent(deps, fakes)
    assert len(texts) == 2


async def test_a_preferred_provider_goes_first(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key")
    await _set(deps, defaults.SPEECH_TTS_PROVIDER, "edge")
    assert (await _speak(deps)).provider == EDGE
    assert synthesizers[OPENROUTER].calls == []


async def test_no_adapter_at_all_is_said_once(deps: PersonalDeps, fakes: Fakes) -> None:
    result = await _speak(replace(deps, synthesizers={}))
    assert result.reason is SkipReason.NO_PROVIDER
    _, texts = await _sent(deps, fakes)
    assert texts == [S.VOICE_SKIPPED(reason=S.TTS_REASON_NO_PROVIDER())]


# --- monthly caps (NFR-COST-02, T51) --------------------------------------------------------------------------------
async def _spent(deps: SharedDeps, provider: Provider, dollars: int) -> None:
    """This month's paid spend on `provider` (its STT calls, say): the cap is one budget per provider."""
    usage = make_usage(provider=provider, model="m", cost_usd_micros=usd(dollars))
    await add_usage(deps.db, usage, at=deps.clock.now(), bot=deps.bot)


async def test_paid_tts_at_its_cap_goes_to_edge_and_says_why(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "openrouter_api_key", "google_api_key")
    await _spent(deps, Provider.OPENROUTER, 10)
    await _spent(deps, Provider.GOOGLE, 40)

    result = await _speak(deps)

    assert result.provider == EDGE
    assert result.failed == (
        TtsAttempt(OPENROUTER, ErrorCode.COST_CAP_REACHED),
        TtsAttempt(GOOGLE, ErrorCode.COST_CAP_REACHED),
    )
    assert synthesizers[OPENROUTER].calls == synthesizers[GOOGLE].calls == []  # no paid call was made
    voices, texts = await _sent(deps, fakes)
    assert len(voices) == 1
    assert texts == [
        S.TTS_FELL_BACK(provider=S.SPEECH_OPENROUTER(), problem=S.COST_STT_PROBLEM_CAP(), fallback=S.SPEECH_EDGE())
    ]


async def test_paid_tts_at_its_cap_without_edge_is_text_only(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    deps = replace(deps, synthesizers={OPENROUTER: synthesizers[OPENROUTER]})
    await _keys(deps, fakes, "openrouter_api_key")
    await _spent(deps, Provider.OPENROUTER, 10)

    result = await _speak(deps)

    assert (result.ok, result.reason, synthesizers[OPENROUTER].calls) == (False, SkipReason.PROVIDER, [])
    voices, texts = await _sent(deps, fakes)
    assert voices == []
    assert texts == [S.VOICE_SKIPPED(reason="OpenRouter (Gemini) — oylik xarajat limiti tugagan")]


# --- the owner's switches ------------------------------------------------------------------------------------------
async def test_voice_off_sends_nothing_and_says_nothing(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _set(deps, defaults.VOICE_ENABLED, False)
    result = await _speak(deps)
    assert result.reason is SkipReason.DISABLED
    assert all(not fake.calls for fake in synthesizers.values())
    assert await _sent(deps, fakes) == ([], [])


async def test_tts_provider_off_is_text_only_even_on_request(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _set(deps, defaults.SPEECH_TTS_PROVIDER, "off")
    result = await service.send_voice(deps, chat_id=OWNER_ID, script=SCRIPT, ref="qa-1", requested=True)
    assert result.reason is SkipReason.DISABLED


async def test_the_dev_bot_speaks_only_on_request(
    dev_deps: SharedDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    deps = replace(dev_deps, synthesizers=synthesizers)
    assert deps.bot is BotName.DEV
    assert (await service.send_voice(deps, chat_id=OWNER_ID, script=SCRIPT, ref="d-1")).reason is SkipReason.DISABLED
    requested = await service.send_voice(deps, chat_id=OWNER_ID, script=SCRIPT, ref="d-2", requested=True)
    assert requested.provider == EDGE


async def test_an_unspeakable_script_is_skipped_silently(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    result = await _speak(deps, script="```\nprint(1)\n``` https://x.uz")
    assert result.reason is SkipReason.EMPTY
    assert synthesizers[EDGE].calls == []
    assert await _sent(deps, fakes) == ([], [])


# --- the summary interface (T72 fills `summarize`) ------------------------------------------------------------------
async def test_the_summary_of_the_answer_is_what_is_spoken(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    seen: list[str] = []

    async def summarize(answer: str) -> str:
        seen.append(answer)
        return SCRIPT

    result = await service.send_voice_summary(deps, chat_id=OWNER_ID, answer=ANSWER, summarize=summarize, ref="qa-1")
    assert seen == [ANSWER]
    assert result.ok
    assert synthesizers[EDGE].calls[0].text == SCRIPT


async def test_with_voice_off_the_answer_is_not_even_summarized(deps: PersonalDeps, fakes: Fakes) -> None:
    await _set(deps, defaults.VOICE_ENABLED, False)

    async def summarize(answer: str) -> str:
        raise AssertionError(answer)

    result = await service.send_voice_summary(deps, chat_id=OWNER_ID, answer=ANSWER, summarize=summarize, ref="qa-1")
    assert result.reason is SkipReason.DISABLED


async def test_a_failed_summary_is_a_notice_not_a_raise(deps: PersonalDeps, fakes: Fakes) -> None:
    async def summarize(answer: str) -> str:
        raise ProviderUnavailable(provider="anthropic_plan")

    result = await service.send_voice_summary(deps, chat_id=OWNER_ID, answer=ANSWER, summarize=summarize, ref="qa-1")
    assert result.reason is SkipReason.SUMMARY
    _, texts = await _sent(deps, fakes)
    assert texts == [S.VOICE_SKIPPED(reason=S.TTS_REASON_SUMMARY())]


async def test_the_same_answer_is_voiced_once(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _speak(deps, ref="qa-7")
    await _speak(deps, ref="qa-7")  # a restarted job runs again
    voices, _ = await _sent(deps, fakes)
    assert len(voices) == 1


# --- synthesis without sending ---------------------------------------------------------------------------------------
class _ObservedClock(FakeClock):
    """A FakeClock that says when someone starts sleeping on it: a scripted TTS call is "on the wire"."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await super().sleep(seconds)


async def test_synthesize_voice_returns_the_note_and_records_its_usage(deps: PersonalDeps, fakes: Fakes) -> None:
    clock = _ObservedClock()
    synthesizers = make_synthesizers(clock)
    deps = replace(deps, clock=clock, synthesizers=synthesizers)
    synthesizers[EDGE].script(make_speech_audio(provider=EDGE, model="edge-tts"), latency_s=1.5)
    async with asyncio.TaskGroup() as group:
        job = group.create_task(service.synthesize_voice(deps, SCRIPT))
        async with asyncio.timeout(5):  # guards a hung job only; no time is waited for
            await clock.sleeping.wait()
        await clock.advance(1.5)
    result = job.result()
    assert result.audio is not None
    assert result.latency_ms == 1500
    assert await _usage(deps) == [("tts", "edge", "edge-tts", False)]
    assert fakes.messenger.requests == []


# --- voice samples (UC-34 "ovozing yoqmadi") -------------------------------------------------------------------------
async def test_the_samples_go_as_numbered_voice_notes_in_their_own_voices(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    await _keys(deps, fakes, "google_api_key")
    await _set(deps, defaults.VOICE_ENABLED, False)  # asked for by name: voice off does not stop it
    synthesizers[GOOGLE].script(make_speech_audio(provider=GOOGLE))
    synthesizers[GOOGLE].script(CallTimedOut(operation="tts"))
    synthesizers[GOOGLE].script(make_speech_audio(provider=GOOGLE))

    sent = await service.offer_samples(deps, chat_id=OWNER_ID, ref="samples-1")

    assert [sample.number for sample in sent] == [1, 3]
    assert [call.voice for call in synthesizers[GOOGLE].calls] == ["Charon", "Aoede", "Puck"]
    assert synthesizers[GOOGLE].calls[0].text == S.VOICE_SAMPLE_SCRIPT(number=1)
    voices, texts = await _sent(deps, fakes)
    assert len(voices) == 2
    assert texts == [S.VOICE_SAMPLES_INTRO(count=3), S.VOICE_SAMPLE_FAILED(number=2)]


async def test_without_any_voice_provider_the_owner_is_told(deps: PersonalDeps, fakes: Fakes) -> None:
    deps = replace(deps, synthesizers={})

    assert await service.offer_samples(deps, chat_id=OWNER_ID, ref="samples-1") == ()

    _, texts = await _sent(deps, fakes)
    assert texts == [S.VOICE_SAMPLES_UNAVAILABLE()]


async def test_edge_says_it_has_only_two_voices(
    deps: PersonalDeps, fakes: Fakes, synthesizers: dict[str, FakeSynthesizer]
) -> None:
    sent = await service.offer_samples(deps, chat_id=OWNER_ID, ref="samples-1")  # no key: Edge only

    assert [sample.provider for sample in sent] == [EDGE, EDGE]
    _, texts = await _sent(deps, fakes)
    assert texts == [BotText(f"{S.VOICE_SAMPLES_FEW(count=2)} {S.VOICE_SAMPLES_INTRO(count=2)}")]
