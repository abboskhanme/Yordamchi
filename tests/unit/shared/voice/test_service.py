"""voice.service on a real SQLite file, fed through the real inbox and burst, with scripted STT fakes on virtual time
(FakeClock; nothing sleeps for real): UC-05's 5 s target, provider fallback, failed notes, restart recovery, the
vocabulary (owner terms and the dev bot's projects), speech keys (SPEC §3.2.11) and retention."""

import asyncio
import logging
import tomllib
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import Final

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    FAKE_KEYS,
    OWNER_ID,
    SECOND_SPEECH_KEYS,
    Fakes,
    add_usage,
    make_media_update,
    make_owner_origin,
    make_project,
    make_transcript,
    make_usage,
    origin_of,
    store_secret,
)
from tests.fakes.clock import FakeClock
from tests.fakes.speech import FakeTranscriber
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    AuthFailed,
    ErrorCode,
    ProviderUnavailable,
    QuotaExhausted,
    SttTermInvalid,
)
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.projects import service as projects
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.speech import SpeechKeyCheck
from yordamchi.shared.burst import domain as burst_domain
from yordamchi.shared.burst import service as burst
from yordamchi.shared.burst import store as burst_store
from yordamchi.shared.burst.domain import BurstEvents, BurstPart, CloseReason, TranscriptState, TurnInput
from yordamchi.shared.cost import store as cost_store
from yordamchi.shared.cost.domain import Provider
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox.domain import UpdateKind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings import store as settings_store
from yordamchi.shared.vault import service as vault
from yordamchi.shared.vault.domain import SecretRef
from yordamchi.shared.voice import domain, service, store
from yordamchi.shared.voice.domain import GOOGLE, OPENROUTER, Attempt, FailReason, SttResult
from yordamchi.shared.voice.models import Transcript as TranscriptRow

LATENCY: Final = tomllib.loads(
    (Path(__file__).resolve().parents[3] / "eval" / "thresholds.toml").read_text(encoding="utf-8")
)["latency"]
GOOGLE_KEY: Final = FAKE_KEYS["google_api_key"]
OPENROUTER_KEY: Final = FAKE_KEYS["openrouter_api_key"]
SPOKEN: Final = "BonviHR'da login sahifasini tuzat, потом проверим логи"
SERVICE_LOGGER: Final = "yordamchi.shared.voice.service"


class _ObservedClock(FakeClock):
    """A FakeClock that says when someone starts sleeping on it: a scripted STT call is "on the wire"."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await super().sleep(seconds)


class _Loop:
    """What the burst loop hands a voice job: its `wake` event, and `notify` for the router."""

    def __init__(self) -> None:
        self.wake = asyncio.Event()
        self.closed: list[TurnInput] = []

    def notify(self, events: BurstEvents) -> None:
        self.closed.extend(events.closed)


@pytest.fixture
def clock() -> _ObservedClock:
    return _ObservedClock()


@pytest.fixture
def fakes(clock: _ObservedClock) -> Fakes:
    return Fakes(clock=clock)


@pytest.fixture
def google(clock: _ObservedClock) -> FakeTranscriber:
    return FakeTranscriber(provider=GOOGLE, clock=clock)


@pytest.fixture
def openrouter(clock: _ObservedClock) -> FakeTranscriber:
    return FakeTranscriber(provider=OPENROUTER, clock=clock)


@pytest.fixture
def deps(dev_deps: DevDeps, google: FakeTranscriber, openrouter: FakeTranscriber) -> DevDeps:
    return replace(dev_deps, transcribers={GOOGLE: google, OPENROUTER: openrouter})


@pytest.fixture
def loop() -> _Loop:
    return _Loop()


# --- helpers --------------------------------------------------------------------------------------------------------
async def _keys(deps: SharedDeps, fakes: Fakes, *kinds: str) -> list[SecretRef]:
    return [await store_secret(deps.db, fakes.keystore, kind, FAKE_KEYS[kind], bot=deps.bot) for kind in kinds]


async def _voice_note(deps: SharedDeps, fakes: Fakes, update_id: int = 1, *, duration_s: int = 60) -> BurstPart:
    """The owner sends a voice note: the inbox takes it in, the burst attaches it and lists its STT job."""
    fakes.messenger.files[f"file-{update_id}"] = b"OggS" + bytes(4096)
    update = make_media_update(update_id, kind=UpdateKind.VOICE, duration_s=duration_s, bot=deps.bot)
    await inbox.accept_update(deps, origin_of(update), update)
    [part] = (await burst.attach_received_updates(deps)).transcribe
    return part


async def _transcribe(deps: SharedDeps, part: BurstPart, loop: _Loop) -> SttResult:
    return await service.transcribe_part(deps, part, wake=loop.wake, notify=loop.notify)


async def _until_sleeping(clock: _ObservedClock) -> None:
    """The timeout only guards against a hung job; no virtual or real time is waited for."""
    async with asyncio.timeout(5):
        await clock.sleeping.wait()
    clock.sleeping.clear()


async def _row(deps: SharedDeps, update_id: int = 1) -> TranscriptRow:
    async with deps.db.read() as s:
        row = await store.get_transcript(s, bot=deps.bot.value, update_id=update_id)
    assert row is not None
    return row


async def _burst_part(deps: SharedDeps, update_id: int = 1) -> BurstPart:
    async with deps.db.read() as s:
        rows = await burst_store.list_bursts(s, deps.bot.value)
    return next(
        part for row in rows for part in burst_domain.parts_from_json(row.input_json) if part.update_id == update_id
    )


def _sent(fakes: Fakes) -> list[tuple[int, object, object]]:
    """Each message sent to the owner: (chat, text, the message it replies to)."""
    return [(sent.chat_id, sent.payload["text"], sent.payload["reply_to"]) for sent in fakes.messenger.sent()]


# --- UC-05: the happy path and its 5 s target -----------------------------------------------------------------------
async def test_a_60_s_note_is_transcribed_within_5_s(
    deps: DevDeps, fakes: Fakes, clock: _ObservedClock, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes, duration_s=60)
    google.script(make_transcript(SPOKEN, audio_ms=60_000), latency_s=3.5)
    started = clock.now()
    async with asyncio.TaskGroup() as group:
        job = group.create_task(_transcribe(deps, part, loop))
        await _until_sleeping(clock)
        await clock.advance(3.5)
    result = job.result()

    assert (result.ok, result.text, result.provider) == (True, SPOKEN, GOOGLE)
    assert clock.now() - started <= timedelta(seconds=LATENCY["stt_60s_note_s"])
    row = await _row(deps)
    assert (row.status, row.text, row.provider, row.audio_ms) == ("done", SPOKEN, GOOGLE, 60_000)
    assert row.latency_ms is not None
    assert row.latency_ms <= LATENCY["stt_60s_note_s"] * 1000
    assert loop.wake.is_set()  # the burst loop recomputes the window now
    held = await _burst_part(deps)
    assert (held.transcript, held.text) == (TranscriptState.DONE, SPOKEN)


async def test_the_call_carries_this_jobs_settings(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    origin = make_owner_origin()
    await settings.record_version(deps, origin, defaults.STT_MODE, "verbatim", reason="change")
    await settings.record_version(deps, origin, defaults.STT_LANGUAGES, ["uz-UZ", "en-US"], reason="change")
    part = await _voice_note(deps, fakes)
    await _transcribe(deps, part, loop)
    snap = await settings.snapshot(deps)
    [call] = google.calls
    assert call.languages == ("uz-UZ", "en-US")
    assert call.mode == "verbatim"
    assert call.model == snap[defaults.STT_GOOGLE_MODEL]
    assert call.fallback_model == snap[defaults.STT_OPENROUTER_FALLBACK_MODEL]
    assert call.timeout == snap[defaults.TIMEOUTS_STT_S]
    assert (call.mime, call.audio_bytes) == ("audio/ogg", 4100)
    download = fakes.messenger.sent("download")[0].payload
    assert (download["file_id"], download["max_bytes"]) == ("file-1", domain.MAX_AUDIO_BYTES)


async def test_the_personal_bot_transcribes_with_its_own_keys(
    personal_deps: PersonalDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    deps = replace(personal_deps, transcribers={GOOGLE: google})
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes)
    result = await _transcribe(deps, part, loop)
    assert result.ok
    assert google.calls[0].vocabulary == ()  # no registry handed in, no owner terms yet


# --- fallback and failure (SPEC §3.2.11 step 6; UC-05 F) -----------------------------------------------------------
async def test_google_out_of_quota_falls_back_to_openrouter(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, openrouter: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key", "openrouter_api_key")
    part = await _voice_note(deps, fakes)
    google.script(QuotaExhausted(provider=GOOGLE))
    openrouter.script(
        make_transcript(SPOKEN, provider=OPENROUTER, model="google/gemini-3.8-flash", cost_usd_micros=410)
    )
    result = await _transcribe(deps, part, loop)

    assert (result.ok, result.provider) == (True, OPENROUTER)
    assert result.failed == (Attempt(GOOGLE, ErrorCode.QUOTA_EXHAUSTED),)
    assert google.calls[0].vocabulary == openrouter.calls[0].vocabulary
    assert openrouter.calls[0].model == (await settings.snapshot(deps))[defaults.STT_OPENROUTER_MODEL]
    row = await _row(deps)
    assert (row.status, row.provider, row.provider_path) == ("done", OPENROUTER, "chat_completions")
    async with deps.db.read() as s:
        [usage] = await cost_store.list_usage_records(s)
    assert (usage.source, usage.provider, usage.is_paid_api, usage.cost_usd_micros) == ("stt", OPENROUTER, True, 410)
    assert fakes.messenger.sent() == []  # nothing to tell the owner


@pytest.mark.parametrize(
    "error",
    [AuthFailed(provider=GOOGLE), ProviderUnavailable(provider=GOOGLE)],
    ids=["auth_failed", "provider_unavailable"],
)
async def test_a_key_or_availability_failure_falls_back_too(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, openrouter: FakeTranscriber, loop: _Loop, error: Exception
) -> None:
    await _keys(deps, fakes, "google_api_key", "openrouter_api_key")
    part = await _voice_note(deps, fakes)
    google.script(error)
    result = await _transcribe(deps, part, loop)
    assert (result.ok, result.provider, len(openrouter.calls)) == (True, OPENROUTER, 1)


async def test_a_failed_note_keeps_the_marker_in_its_burst_and_the_owner_is_told(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, openrouter: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key", "openrouter_api_key")
    part = await _voice_note(deps, fakes)
    google.script(QuotaExhausted(provider=GOOGLE))
    openrouter.script(QuotaExhausted(provider=OPENROUTER))
    result = await _transcribe(deps, part, loop)

    assert (result.ok, result.reason) == (False, FailReason.PROVIDER)
    held = await _burst_part(deps)
    assert (held.transcript, held.text) == (TranscriptState.FAILED, domain.marker(result))
    assert str(held.text).startswith("[ovoz tushunilmadi: ")
    assert _sent(fakes) == [(DEV_GROUP_ID, domain.failure_notice(result), part.message_id)]
    row = await _row(deps)
    assert (row.status, row.text, row.error_code, row.provider) == ("failed", None, "quota_exhausted", OPENROUTER)
    async with deps.db.read() as s:
        assert await cost_store.list_usage_records(s) == []
    assert loop.wake.is_set()


async def test_without_a_key_for_the_other_provider_a_quota_failure_is_final(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, openrouter: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes)
    google.script(QuotaExhausted(provider=GOOGLE))
    result = await _transcribe(deps, part, loop)
    assert (result.ok, result.failed, openrouter.calls) == (False, (Attempt(GOOGLE, ErrorCode.QUOTA_EXHAUSTED),), [])
    assert len(_sent(fakes)) == 1


async def test_a_timeout_fails_at_once_without_a_fallback(
    deps: DevDeps,
    fakes: Fakes,
    clock: _ObservedClock,
    google: FakeTranscriber,
    openrouter: FakeTranscriber,
    loop: _Loop,
) -> None:
    await _keys(deps, fakes, "google_api_key", "openrouter_api_key")
    part = await _voice_note(deps, fakes)
    timeout = (await settings.snapshot(deps))[defaults.TIMEOUTS_STT_S]
    google.script("kech", latency_s=timeout + 1)
    async with asyncio.TaskGroup() as group:
        job = group.create_task(_transcribe(deps, part, loop))
        await _until_sleeping(clock)
        await clock.advance(timeout)
    result = job.result()
    assert result.failed == (Attempt(GOOGLE, ErrorCode.CALL_TIMED_OUT),)
    assert openrouter.calls == []
    assert (await _row(deps)).error_code == "call_timed_out"


async def test_no_speech_key_fails_and_says_which_key_to_send(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    part = await _voice_note(deps, fakes)
    result = await _transcribe(deps, part, loop)
    assert (result.reason, google.calls) == (FailReason.NO_KEY, [])
    assert fakes.messenger.sent("download") == []  # nothing is fetched that nobody can transcribe
    assert _sent(fakes) == [(DEV_GROUP_ID, S.STT_FAILED_NEEDS_KEY(reason=S.STT_REASON_NO_KEY()), part.message_id)]
    assert (await _row(deps)).error_code == "stt_no_key"


async def test_a_download_failure_is_a_failed_note(deps: DevDeps, fakes: Fakes, loop: _Loop) -> None:
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes)
    fakes.messenger.fail_next("download", ProviderUnavailable(provider="telegram"))
    result = await _transcribe(deps, part, loop)
    assert result.reason is FailReason.DOWNLOAD
    assert (await _burst_part(deps)).transcript is TranscriptState.FAILED


async def test_an_empty_transcript_is_no_speech(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes)
    google.script(make_transcript(""))
    result = await _transcribe(deps, part, loop)
    assert result.reason is FailReason.NO_SPEECH
    async with deps.db.read() as s:
        assert len(await cost_store.list_usage_records(s)) == 1  # the call was made, so it is metered


async def test_a_failed_notice_send_is_logged_not_raised(
    deps: DevDeps, fakes: Fakes, loop: _Loop, caplog: pytest.LogCaptureFixture
) -> None:
    part = await _voice_note(deps, fakes)
    fakes.messenger.fail_next("send_text", ProviderUnavailable(provider="telegram"))
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        result = await _transcribe(deps, part, loop)
    assert result.reason is FailReason.NO_KEY
    assert "voice_notice_failed" in caplog.messages
    assert (await _burst_part(deps)).transcript is TranscriptState.FAILED  # the burst still has its marker


async def test_a_spoken_interrupt_closes_at_once_and_reaches_the_router(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes, duration_s=1)
    google.script(make_transcript("to'xta"))
    await _transcribe(deps, part, loop)
    [closed] = loop.closed
    assert (closed.close_reason, [item.update_id for item in closed.parts]) == (CloseReason.INTERRUPT, [1])


async def test_logs_hold_no_transcript(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop, caplog: pytest.LogCaptureFixture
) -> None:
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes)
    google.script(make_transcript(SPOKEN))
    with caplog.at_level(logging.INFO, logger=SERVICE_LOGGER):
        await _transcribe(deps, part, loop)
    assert "voice_transcribed" in caplog.messages
    fields = " ".join(str(getattr(record, "yd_fields", {})) for record in caplog.records)
    assert "BonviHR'da" not in fields
    assert GOOGLE_KEY not in fields


# --- monthly caps (NFR-COST-02, SPEC §3.2.11 step 6) ----------------------------------------------------------------
async def _spent(deps: SharedDeps, provider: str, dollars: int) -> None:
    """This month's paid spend on `provider`, as earlier calls recorded it."""
    usage = make_usage(provider=Provider(provider), model="m", cost_usd_micros=dollars * 1_000_000)
    await add_usage(deps.db, usage, at=deps.clock.now(), bot=deps.bot)


async def test_a_provider_at_its_cap_is_skipped_and_stt_falls_back(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, openrouter: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key", "openrouter_api_key")
    await _spent(deps, GOOGLE, 40)  # the $40 Google cap is used up
    part = await _voice_note(deps, fakes)
    openrouter.script(
        make_transcript(SPOKEN, provider=OPENROUTER, model="google/gemini-3.8-flash", cost_usd_micros=410)
    )
    result = await _transcribe(deps, part, loop)

    assert (result.ok, result.provider) == (True, OPENROUTER)
    assert result.failed == (Attempt(GOOGLE, ErrorCode.COST_CAP_REACHED),)
    assert (google.calls, len(openrouter.calls)) == ([], 1)  # no paid call went to the capped provider
    assert fakes.messenger.sent() == []  # it worked: nothing to tell the owner


async def test_with_every_provider_at_its_cap_the_owner_is_told_plainly(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, openrouter: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key", "openrouter_api_key")
    await _spent(deps, GOOGLE, 40)
    await _spent(deps, OPENROUTER, 10)
    part = await _voice_note(deps, fakes)
    result = await _transcribe(deps, part, loop)

    assert (result.ok, result.reason) == (False, FailReason.PROVIDER)
    assert (google.calls, openrouter.calls, fakes.messenger.sent("download")) == ([], [], [])
    [(chat, text, reply_to)] = _sent(fakes)
    assert (chat, reply_to) == (DEV_GROUP_ID, part.message_id)
    assert text == (
        "Ovozli xabarni matnga o'gira olmadim: Google Gemini — oylik xarajat limiti tugagan; OpenRouter (Gemini) — "
        "oylik xarajat limiti tugagan. Matn bilan yozing yoki qayta yuboring."
    )
    assert (await _row(deps)).error_code == "cost_cap_reached"


async def test_google_stt_is_priced_from_the_audio_length(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    google.script(make_transcript(SPOKEN, audio_ms=90_000))
    await _transcribe(deps, await _voice_note(deps, fakes), loop)
    await settings.record_version(
        deps, make_owner_origin(), defaults.COST_GOOGLE_STT_USD_MICROS_PER_MIN, 0, reason="change"
    )  # a free-tier key
    google.script(make_transcript(SPOKEN, audio_ms=90_000))
    await _transcribe(deps, await _voice_note(deps, fakes, update_id=2), loop)

    async with deps.db.read() as s:
        priced, free = await cost_store.list_usage_records(s)
    assert (priced.provider, priced.is_paid_api, priced.cost_usd_micros) == (GOOGLE, True, 3_000)  # 1.5 min × $0.002
    assert (free.is_paid_api, free.cost_usd_micros) == (False, 0)


# --- restart (SPEC §2.7) --------------------------------------------------------------------------------------------
async def test_after_a_restart_a_pending_job_runs_again(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes)
    async with deps.db.uow() as s:  # the old process started the job, then died
        await store.add_transcript(
            s, bot=deps.bot.value, update_id=part.update_id, status="pending", at=fakes.clock.now()
        )

    recovered = await burst.recover_bursts(deps)
    assert [item.update_id for item in recovered.transcribe] == [part.update_id]
    google.script(make_transcript(SPOKEN))
    [result] = await service.transcribe_parts(deps, recovered.transcribe, wake=loop.wake, notify=loop.notify)

    assert (result.ok, len(google.calls)) == (True, 1)
    assert (await _row(deps)).status == "done"
    assert (await _burst_part(deps)).transcript is TranscriptState.DONE


async def test_after_a_restart_a_done_transcript_is_reused_without_a_new_call(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    part = await _voice_note(deps, fakes)
    now = fakes.clock.now()
    async with deps.db.uow() as s:  # the old process transcribed it and died before the burst heard
        await store.add_transcript(s, bot=deps.bot.value, update_id=part.update_id, status="pending", at=now)
        await store.set_transcript_result(
            s,
            bot=deps.bot.value,
            update_id=part.update_id,
            status="done",
            text=SPOKEN,
            provider=GOOGLE,
            provider_path="interactions",
            model="gemini-3.5-transcribe",
            audio_ms=60_000,
            latency_ms=1_900,
            error_code=None,
            at=now,
        )

    recovered = await burst.recover_bursts(deps)
    [result] = await service.transcribe_parts(deps, recovered.transcribe, wake=loop.wake, notify=loop.notify)
    assert (result.ok, result.text, google.calls) == (True, SPOKEN, [])
    assert (await _burst_part(deps)).text == SPOKEN


async def test_several_notes_are_transcribed_concurrently_in_order(
    deps: DevDeps, fakes: Fakes, clock: _ObservedClock, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    first = await _voice_note(deps, fakes, 1)
    second = await _voice_note(deps, fakes, 2)
    google.script(make_transcript("birinchi"), latency_s=3.0)
    google.script(make_transcript("ikkinchi"), latency_s=3.0)
    async with asyncio.TaskGroup() as group:
        job = group.create_task(service.transcribe_parts(deps, (first, second), wake=loop.wake, notify=loop.notify))
        while clock.pending_sleepers < 2:
            await _until_sleeping(clock)
        await clock.advance(3.0)  # both calls were in flight at once
    assert [result.text for result in job.result()] == ["birinchi", "ikkinchi"]


# --- vocabulary (UC-05) ---------------------------------------------------------------------------------------------
async def test_a_chat_added_term_is_in_the_next_request(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    reply = await service.add_owner_term(deps, make_owner_origin(), "«Wunderkind».")
    assert reply == S.STT_TERM_ADDED(term="Wunderkind")

    part = await _voice_note(deps, fakes)
    await _transcribe(deps, part, loop)
    assert google.calls[0].vocabulary[0] == "Wunderkind"


async def test_a_term_already_there_changes_nothing(deps: DevDeps) -> None:
    origin = make_owner_origin()
    await service.add_owner_term(deps, origin, "OvozYoz")
    assert await service.add_owner_term(deps, origin, "OvozYoz") == S.STT_TERM_KNOWN(term="OvozYoz")
    async with deps.db.read() as s:
        assert await settings_store.get_latest_version_number(s, defaults.STT_EXTRA_TERMS.key) == 1
    assert (await settings.snapshot(deps))[defaults.STT_EXTRA_TERMS] == ["OvozYoz"]


async def test_an_empty_term_is_refused(deps: DevDeps) -> None:
    with pytest.raises(SttTermInvalid):
        await service.add_owner_term(deps, make_owner_origin(), "  ")


async def test_the_dev_bot_injects_its_project_names_and_aliases(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber, loop: _Loop
) -> None:
    await _keys(deps, fakes, "google_api_key")
    async with deps.db.uow() as s:
        await make_project(s, "BonviHR", at=fakes.clock.now())
        await make_project(s, "HRMentor", at=fakes.clock.now())
    await settings.record_version(
        deps, make_owner_origin(), defaults.PROJECTS_ALIASES, {"hr": "BonviHR"}, reason="change"
    )
    await service.add_owner_term(deps, make_owner_origin(), "Wunderkind")

    part = await _voice_note(deps, fakes)
    named: tuple[service.NamedSource, ...] = (partial(projects.list_projects, deps),)
    await service.transcribe_part(deps, part, wake=loop.wake, notify=loop.notify, named=named)
    assert google.calls[0].vocabulary == ("Wunderkind", "BonviHR", "hr", "HRMentor")


# --- speech keys (SPEC §3.2.11) -------------------------------------------------------------------------------------
async def test_a_new_google_key_is_checked_and_reported_in_one_line(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber
) -> None:
    [ref] = await _keys(deps, fakes, "google_api_key")
    origin = make_owner_origin()
    text = await service.configure_speech_keys(deps, origin, [ref])

    assert text == "Ovoz: Google Gemini ulandi."
    snap = await settings.snapshot(deps)
    [call] = google.key_calls
    assert (call.key, call.models, call.timeout) == (
        GOOGLE_KEY,
        (snap[defaults.STT_GOOGLE_MODEL],),
        snap[defaults.TIMEOUTS_STT_S],
    )
    assert _sent(fakes) == [(origin.chat_id, text, None)]


async def test_an_openrouter_free_tier_key_says_its_limit_runs_out_fast(
    deps: DevDeps, fakes: Fakes, openrouter: FakeTranscriber
) -> None:
    [ref] = await _keys(deps, fakes, "openrouter_api_key")
    model = (await settings.snapshot(deps))[defaults.STT_OPENROUTER_MODEL]
    openrouter.script_key_check(
        SpeechKeyCheck(provider=OPENROUTER, available_models=(model,), missing_models=(), is_free_tier=True)
    )
    text = await service.configure_speech_keys(deps, make_owner_origin(), [ref])
    assert text == f"Ovoz: OpenRouter (Gemini) ulandi. {S.SPEECH_FREE_TIER(provider=S.SPEECH_OPENROUTER())}"


async def test_both_keys_name_google_first_and_openrouter_as_the_fallback(deps: DevDeps, fakes: Fakes) -> None:
    refs = await _keys(deps, fakes, "google_api_key", "openrouter_api_key")
    text = await service.configure_speech_keys(deps, make_owner_origin(), refs)
    assert text == "Ovoz: Google Gemini, zaxira — OpenRouter (Gemini) ulandi."


async def test_a_rejected_key_is_removed_and_the_working_one_stays(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber
) -> None:
    [old] = await _keys(deps, fakes, "google_api_key")
    await service.configure_speech_keys(deps, make_owner_origin(), [old])
    new = await store_secret(
        deps.db, fakes.keystore, "google_api_key", SECOND_SPEECH_KEYS["google_api_key"], update_id=2
    )
    assert new.name == "google_api_key_2"
    google.script_key_check(AuthFailed(provider=GOOGLE))

    text = await service.configure_speech_keys(deps, make_owner_origin(update_id=2), [new])
    assert text == f"{S.SPEECH_ACTIVE(providers=S.SPEECH_GOOGLE())} {S.SPEECH_KEY_REJECTED(provider=S.SPEECH_GOOGLE())}"
    assert [ref.name for ref in await vault.list_secrets(deps)] == ["google_api_key"]
    assert await service.read_speech_key(deps, GOOGLE) == GOOGLE_KEY


async def test_a_newer_working_key_retires_the_older_one(deps: DevDeps, fakes: Fakes) -> None:
    await _keys(deps, fakes, "google_api_key")
    new = await store_secret(
        deps.db, fakes.keystore, "google_api_key", SECOND_SPEECH_KEYS["google_api_key"], update_id=2
    )
    await service.configure_speech_keys(deps, make_owner_origin(update_id=2), [new])
    assert [ref.name for ref in await vault.list_secrets(deps)] == ["google_api_key_2"]
    assert await service.read_speech_key(deps, GOOGLE) == SECOND_SPEECH_KEYS["google_api_key"]


async def test_a_key_out_of_quota_is_kept_and_said_plainly(
    deps: DevDeps, fakes: Fakes, google: FakeTranscriber
) -> None:
    [ref] = await _keys(deps, fakes, "google_api_key")
    google.script_key_check(QuotaExhausted(provider=GOOGLE))
    text = await service.configure_speech_keys(deps, make_owner_origin(), [ref])
    assert text is not None
    assert S.SPEECH_KEY_OUT_OF_QUOTA(provider=S.SPEECH_GOOGLE()) in text
    assert [secret.name for secret in await vault.list_secrets(deps)] == ["google_api_key"]


async def test_the_only_key_rejected_says_voice_does_not_work(
    deps: DevDeps, fakes: Fakes, openrouter: FakeTranscriber
) -> None:
    [ref] = await _keys(deps, fakes, "openrouter_api_key")
    openrouter.script_key_check(AuthFailed(provider=OPENROUTER))
    text = await service.configure_speech_keys(deps, make_owner_origin(), [ref])
    assert text == f"{S.SPEECH_NONE()} {S.SPEECH_KEY_REJECTED(provider=S.SPEECH_OPENROUTER())}"
    assert await vault.list_secrets(deps) == ()
    assert await service.read_speech_key(deps, OPENROUTER) == ""


async def test_a_bot_without_the_adapter_reports_the_key_as_unchecked(dev_deps: DevDeps, fakes: Fakes) -> None:
    deps = replace(dev_deps, transcribers={})
    [ref] = await _keys(deps, fakes, "google_api_key")
    text = await service.configure_speech_keys(deps, make_owner_origin(), [ref])
    assert text == (
        f"{S.SPEECH_NONE()} {S.SPEECH_KEY_UNCHECKED(provider=S.SPEECH_GOOGLE(), problem=S.STT_PROBLEM_UNAVAILABLE())}"
    )


async def test_other_secrets_need_no_speech_reply(deps: DevDeps, fakes: Fakes) -> None:
    ref = await store_secret(deps.db, fakes.keystore, "github_token", FAKE_KEYS["github_token"])
    assert await service.configure_speech_keys(deps, make_owner_origin(), [ref]) is None
    assert fakes.messenger.sent() == []


async def test_the_personal_bot_reports_in_the_chat_the_key_came_from(
    personal_deps: PersonalDeps, fakes: Fakes, google: FakeTranscriber
) -> None:
    deps = replace(personal_deps, transcribers={GOOGLE: google})
    [ref] = await _keys(deps, fakes, "google_api_key")
    await service.configure_speech_keys(deps, make_owner_origin(BotName.PERSONAL), [ref])
    assert [chat for chat, _, _ in _sent(fakes)] == [OWNER_ID]


# --- retention ------------------------------------------------------------------------------------------------------
async def test_retention_nulls_old_texts_deletes_old_rows_and_keeps_pending_ones(deps: DevDeps, fakes: Fakes) -> None:
    now = fakes.clock.now()
    ages = {1: timedelta(days=31), 2: timedelta(days=8), 3: timedelta(days=1), 4: timedelta(days=40)}
    async with deps.db.uow() as s:
        for update_id, age in ages.items():
            await store.add_transcript(s, bot=deps.bot.value, update_id=update_id, status="pending", at=now - age)
            if update_id != 4:
                await store.set_transcript_result(
                    s,
                    bot=deps.bot.value,
                    update_id=update_id,
                    status="done",
                    text="matn",
                    provider=GOOGLE,
                    provider_path="interactions",
                    model="m",
                    audio_ms=1,
                    latency_ms=1,
                    error_code=None,
                    at=now - age,
                )
    assert await service.apply_retention(deps) == 2
    async with deps.db.read() as s:
        rows = {row.update_id: (row.status, row.text) for row in await store.list_transcripts(s, deps.bot.value)}
    assert rows == {2: ("done", None), 3: ("done", "matn"), 4: ("pending", None)}
