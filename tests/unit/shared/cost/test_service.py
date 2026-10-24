"""cost.service on real SQLite files with a FakeClock: usage joins the caller's transaction as integer micro-USD; the
"xarajat?" report adds up both bots' ledgers from local data within 3 s (UC-07); the monthly caps warn once at 80 %,
pause at 100 % and reset with the month, and raising one asks first (NFR-COST-02); the optional daily summary; and
400-day retention."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Final

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    Fakes,
    add_setting_row,
    add_usage,
    add_usage_rows,
    gate_asker,
    list_replies,
    make_dev_deps,
    make_owner_origin,
    make_turn_usage,
    make_usage,
    usd,
)
from tests.fakes.mac_host import FakeMacHost
from tests.owner_text import shown
from yordamchi.core import aio
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate_service
from yordamchi.dev.gate.service import AnswerKind, DecisionReply
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.uow import Database
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState
from yordamchi.shared.cost import domain, service, store
from yordamchi.shared.cost.domain import CapLevel, Provider, Usage
from yordamchi.shared.outbox.models import OutboxMessage
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import domain as settings_domain
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.registry import OneOf
from yordamchi.shared.settings.service import OutcomeKind

# FakeClock starts at 2026-10-23 09:00 UTC: 14:00 in Tashkent (`ui.timezone`).
NOW: Final = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)
YESTERDAY: Final = NOW - timedelta(days=1)
THREE_DAYS_AGO: Final = NOW - timedelta(days=3)
TEN_DAYS_AGO: Final = NOW - timedelta(days=10)  # this month, not the last 7 days
TWENTY_DAYS_AGO: Final = NOW - timedelta(days=20)
LAST_MONTH: Final = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
MANAGER: Final = ClaudeManagerState(
    active_id="a",
    auto_switch=True,
    accounts=(ClaudeAccount("owner@example.com", "ok", 42.0, 13.0, None, None, is_active=True),),
)


def stt(provider: Provider, micros: int) -> Usage:
    return make_usage(provider=provider, model="google/gemini-3.8-flash", cost_usd_micros=micros)


def text_of(row: OutboxMessage) -> str:
    assert isinstance(row.payload_json, dict)
    text = row.payload_json.get("text")
    assert isinstance(text, str)
    return text


async def replies_of(db: Database, kind: str) -> list[str]:
    return [text_of(row) for row in await list_replies(db) if row.kind == kind]


@pytest.fixture
async def personal_db_readonly(paths: Paths, personal_db: Database) -> AsyncIterator[Database]:
    """The dev bot's view of personal.db (`mode=ro`): spend there counts against the same caps."""
    del personal_db  # created first, so the file and schema exist
    db = open_database(paths.db_file(BotName.PERSONAL), readonly=True)
    yield db
    await db.dispose()


@pytest.fixture
def dev_with_peer(dev_db: Database, personal_db_readonly: Database, paths: Paths, fakes: Fakes) -> DevDeps:
    return make_dev_deps(db=dev_db, paths=paths, fakes=fakes, peer_db=personal_db_readonly)


@pytest.fixture
async def loops() -> AsyncIterator[list[asyncio.Task[None]]]:
    tasks: list[asyncio.Task[None]] = []
    yield tasks
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def until(check: Callable[[], object], *, timeout: float = 3.0, what: str = "condition") -> None:
    """Wait (real time) for a background loop to reach a state; the fake clock moves only when the test says so."""
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            msg = f"{what} not reached"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


# --- recording ------------------------------------------------------------------------------------------------------
async def test_a_usage_row_is_recorded_in_the_callers_transaction(dev_deps: DevDeps) -> None:
    now = dev_deps.clock.now()
    usage = make_usage(project_id="BonviHR", turn_id=17)
    async with dev_deps.db.uow() as s:
        row_id = await service.record_usage(s, bot=dev_deps.bot, at=now, usage=usage)
    async with dev_deps.db.read() as s:
        [row] = await store.list_usage_records(s)
    assert (row.id, row.bot, row.at, row.source, row.provider) == (row_id, "dev", now, "stt", "google")
    assert (row.is_paid_api, row.model, row.project_id, row.turn_id) == (True, "gemini-3.5-transcribe", "BonviHR", 17)
    assert row.cost_usd_micros == 1_250
    assert isinstance(row.cost_usd_micros, int)
    assert (row.units_json, row.duration_ms) == ({"audio_ms": 61_000}, 900)


async def test_an_impossible_usage_writes_nothing(dev_deps: DevDeps) -> None:
    with pytest.raises(ValueError, match="not_a_paid_api"):
        async with dev_deps.db.uow() as s:
            await service.record_usage(
                s, bot=dev_deps.bot, at=dev_deps.clock.now(), usage=make_usage(provider=Provider.EDGE)
            )
    async with dev_deps.db.read() as s:
        assert await store.list_usage_records(s) == []


# --- "xarajat?" (UC-07) ---------------------------------------------------------------------------------------------
async def busy_month(dev: Database, personal: Database) -> None:
    """Both bots' ledgers this month, plus a row from last month that no period may count."""
    routing = make_usage(
        source="routing", provider=Provider.ANTHROPIC_PLAN, model="haiku", is_paid_api=False, cost_usd_micros=usd(0, 5)
    )
    await add_usage(
        dev,
        make_turn_usage("BonviHR", turn_id=1, cost_usd_micros=usd(0, 40), duration_ms=10 * 60_000),
        replace(routing, project_id="BonviHR"),
        stt(Provider.OPENROUTER, usd(0, 12)),
        at=NOW - timedelta(hours=1),
    )
    await add_usage(
        dev, make_turn_usage("BonviHR", turn_id=2, cost_usd_micros=usd(1), duration_ms=20 * 60_000), at=THREE_DAYS_AGO
    )
    await add_usage(dev, stt(Provider.GOOGLE, usd(0, 30)), at=TEN_DAYS_AGO)
    await add_usage(
        dev, make_turn_usage("HRMentor", turn_id=3, cost_usd_micros=usd(2), duration_ms=30 * 60_000), at=TWENTY_DAYS_AGO
    )
    await add_usage(dev, make_turn_usage("Wunderkind", turn_id=4, cost_usd_micros=usd(5)), at=LAST_MONTH)
    tts = make_usage(source="tts", provider=Provider.OPENROUTER, model="google/gemini-3.8-flash-tts")
    await add_usage(personal, replace(tts, cost_usd_micros=usd(0, 20)), at=NOW, bot=BotName.PERSONAL)
    await add_usage(personal, stt(Provider.GOOGLE, usd(0, 5)), at=YESTERDAY, bot=BotName.PERSONAL)


async def test_the_report_adds_up_both_bots_ledgers_from_local_data(
    dev_with_peer: DevDeps, personal_db: Database, fakes: Fakes
) -> None:
    await busy_month(dev_with_peer.db, personal_db)
    fakes.mac_host.manager_state = MANAGER

    text = shown(await service.cost_report(dev_with_peer))

    assert text.splitlines() == [
        "Xarajat · bugun / 7 kun / shu oy",
        "Pullik API: $0.32 / $0.37 / $0.67",
        "• Google Gemini: $0 / $0.05 / $0.35 · limit $40, 0% ishlatildi",
        "• OpenRouter: $0.32 / $0.32 / $0.32 · limit $10, 3% ishlatildi",
        "Nimaga: STT $0.12 / $0.17 / $0.47 · TTS $0.20 / $0.20 / $0.20",
        "Claude obunasi (API narxida taxmin; obuna doirasida, pul to'lanmaydi): ~$0.45 / ~$1.45 / ~$3.45",
        "Ishlar: 1 / 2 / 3 · vaqt: 10 daq / 30 daq / 1 soat 0 daq",
        "Claude limiti (ClaudeManager): 5 soat 42% · 7 kun 13%",
        "Eng faol loyihalar (shu oy; Claude — taxmin): HRMentor — 1 ish, ~$2.00 · BonviHR — 2 ish, ~$1.45",
        "Botlar (shu oy): dev — pullik $0.42, Claude taxmin ~$3.45 · shaxsiy — pullik $0.25, Claude taxmin $0",
    ]
    assert fakes.assistant.requests == []  # local data only: no model is asked


async def test_the_figures_match_the_ledger_sums(dev_deps: DevDeps) -> None:
    """UC-07 AC: what the report says is what the DB holds, to the micro-dollar."""
    usages = [stt(Provider.OPENROUTER, micros) for micros in (1, 333_333, 4_999, 2_500_000)]
    await add_usage(dev_deps.db, *usages, at=NOW)
    async with dev_deps.db.read() as s:
        recorded = sum(row.cost_usd_micros for row in await store.list_usage_records(s) if row.is_paid_api)
    allowance = await service.allowance(dev_deps, Provider.OPENROUTER)
    assert allowance.spent_usd_micros == recorded == 2_838_333
    assert f"Pullik API: {domain.usd_text(recorded)} / " in shown(await service.cost_report(dev_deps))


async def test_the_report_answers_within_3_s_with_400_days_of_ledger(dev_deps: DevDeps) -> None:
    rows: list[tuple[datetime, Usage]] = []
    for day in range(400):
        at = NOW - timedelta(days=day, minutes=1)
        for n in range(75):
            project = f"Project{n % 40}"
            rows.append((at, make_turn_usage(project, turn_id=day * 100 + n, cost_usd_micros=10_000)))
        rows.extend((at, stt(Provider.OPENROUTER, 100)) for _ in range(15))
    await add_usage_rows(dev_deps.db, rows)

    started = time.monotonic()
    text = shown(await service.cost_report(dev_deps))
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert "Ishlar: 75 / 525 / " in text  # today's and the last 7 days' turns, counted by SQL


class HangingMac(FakeMacHost):
    async def claude_manager_state(self, *, timeout: float) -> ClaudeManagerState | None:
        await asyncio.Event().wait()  # a stuck file read
        return None


async def test_a_hanging_claude_manager_leaves_its_line_out_within_3_s(dev_deps: DevDeps) -> None:
    deps = replace(dev_deps, mac_host=HangingMac())
    started = time.monotonic()
    text = shown(await service.cost_report(deps))
    assert time.monotonic() - started < 3.0
    assert not any(line.startswith("Claude limiti") for line in text.splitlines())


async def test_subscription_usage_is_labelled_and_never_counted_as_money(dev_deps: DevDeps) -> None:
    await add_usage(dev_deps.db, make_turn_usage(cost_usd_micros=usd(500)), at=NOW)  # far above every cap

    text = shown(await service.cost_report(dev_deps))

    assert "Pullik API: $0 / $0 / $0" in text.splitlines()
    assert (
        "Claude obunasi (API narxida taxmin; obuna doirasida, pul to'lanmaydi): ~$500.00 / ~$500.00 / ~$500.00"
        in text.splitlines()
    )
    for provider in domain.PAYABLE_ORDER:
        allowance = await service.allowance(dev_deps, provider)
        assert (allowance.allowed, allowance.spent_usd_micros) == (True, 0)
    assert await service.notify_caps(dev_deps) == ()


async def test_xarajat_is_queued_as_the_reply(dev_deps: DevDeps) -> None:
    text = await service.show_cost(dev_deps, chat_id=DEV_GROUP_ID, ref="route-7", reply_to=5001)
    [row] = await list_replies(dev_deps.db)
    assert (row.kind, row.chat_id, row.reply_to_message_id, row.dedupe_key) == (
        "report",
        DEV_GROUP_ID,
        5001,
        "report:cost-route-7:0",
    )
    assert text_of(row) == text


# --- caps (NFR-COST-02) ---------------------------------------------------------------------------------------------
async def test_the_80_pct_warning_is_sent_once(dev_deps: DevDeps) -> None:
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(8)), at=NOW)  # $8 of the $10 cap

    [notice] = await service.notify_caps(dev_deps)
    assert (notice.allowance.provider, notice.level) == (Provider.OPENROUTER, CapLevel.WARN)
    assert await service.notify_caps(dev_deps) == ()
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(1)), at=NOW)  # 90 %: still the same warning
    assert await service.notify_caps(dev_deps) == ()

    [row] = await list_replies(dev_deps.db)
    assert (row.kind, row.chat_id) == ("system", DEV_GROUP_ID)
    assert shown(text_of(row)) == (
        "OpenRouter: oylik limitning 80 foizi ishlatildi ($8.00 / $10). $10 ga yetganda pullik chaqiruvlar to'xtaydi."
    )
    assert (await service.allowance(dev_deps, Provider.OPENROUTER)).allowed


async def test_at_100_pct_paid_calls_pause_and_the_owner_is_told_once(dev_deps: DevDeps) -> None:
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(10)), at=NOW)

    allowance = await service.allowance(dev_deps, Provider.OPENROUTER)
    assert (allowance.allowed, allowance.level, allowance.remaining_usd_micros) == (False, CapLevel.REACHED, 0)
    [notice] = await service.notify_caps(dev_deps)  # straight to 100 %: only the pause is told
    assert notice.level is CapLevel.REACHED
    assert await service.notify_caps(dev_deps) == ()
    [text] = await replies_of(dev_deps.db, "system")
    assert shown(text).startswith(
        "OpenRouter: oylik limit ($10) tugadi — $10.00 sarflandi. Pullik chaqiruvlar to'xtatildi"
    )
    assert (await service.allowance(dev_deps, Provider.GOOGLE)).allowed  # the other provider has its own cap
    assert "limit $10 tugadi — pullik chaqiruvlar to'xtatilgan" in shown(await service.cost_report(dev_deps))


async def test_spend_on_the_other_bot_counts_against_the_same_cap(
    dev_with_peer: DevDeps, personal_db: Database
) -> None:
    await add_usage(dev_with_peer.db, stt(Provider.OPENROUTER, usd(6)), at=NOW)
    await add_usage(personal_db, stt(Provider.OPENROUTER, usd(4)), at=NOW, bot=BotName.PERSONAL)
    allowance = await service.allowance(dev_with_peer, Provider.OPENROUTER)
    assert (allowance.spent_usd_micros, allowance.allowed) == (usd(10), False)


async def test_the_personal_bot_reads_the_dev_bots_spend_too(personal_deps: PersonalDeps, dev_db: Database) -> None:
    await add_usage(dev_db, stt(Provider.GOOGLE, usd(40)), at=NOW)
    assert not (await service.allowance(personal_deps, Provider.GOOGLE)).allowed


async def test_raising_a_cap_asks_once_and_the_paused_provider_resumes_after_ha(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(10)), at=NOW)
    ask = gate_asker(dev_deps)
    key = defaults.CAPS_OPENROUTER_MONTHLY_USD.key

    first = await settings.change_setting(dev_deps, make_owner_origin(update_id=901), key, 20, ask=ask)
    again = await settings.change_setting(dev_deps, make_owner_origin(update_id=902), key, 20, ask=ask)

    assert first.kind is again.kind is OutcomeKind.ASKED
    assert first.decision_id == again.decision_id
    assert len(fakes.messenger.sent()) == 1  # one question
    assert not (await service.allowance(dev_deps, Provider.OPENROUTER)).allowed  # nothing changes before "ha"
    answer = await gate_service.answer_decision(dev_deps, make_owner_origin(update_id=903), DecisionReply("ha"))
    assert answer.kind is AnswerKind.APPROVED
    resumed = await service.allowance(dev_deps, Provider.OPENROUTER)
    assert (resumed.allowed, resumed.cap_usd, resumed.used_pct) == (True, 20, 50)


async def test_lowering_a_cap_tightens_it_at_once(dev_deps: DevDeps) -> None:
    key = defaults.CAPS_GOOGLE_MONTHLY_USD.key
    outcome = await settings.change_setting(dev_deps, make_owner_origin(), key, 5, ask=gate_asker(dev_deps))
    assert outcome.kind is OutcomeKind.APPLIED
    assert (await service.allowance(dev_deps, Provider.GOOGLE)).cap_usd == 5


CLAUDE_MODEL_KEYS: Final = [
    setting.key
    for setting in defaults.REGISTRY
    if isinstance(setting.check, OneOf)
    and setting.check.values == defaults.CLAUDE_MODELS
    and settings_domain.in_scope(setting, BotName.DEV)
]


@pytest.mark.parametrize("key", CLAUDE_MODEL_KEYS)
@pytest.mark.parametrize("model", sorted(defaults.CREDIT_BILLED_MODELS))
async def test_a_model_that_bills_usage_credits_is_a_spending_decision_that_asks(
    dev_deps: DevDeps, key: str, model: str
) -> None:
    before = (await settings.snapshot(dev_deps)).values[key]
    outcome = await settings.change_setting(dev_deps, make_owner_origin(), key, model, ask=gate_asker(dev_deps))
    assert outcome.kind is OutcomeKind.ASKED  # D9: money is an important decision
    assert (await settings.snapshot(dev_deps)).values[key] == before


async def test_a_new_month_resets_the_spend_and_owes_its_own_notices(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.clock.set(datetime(2026, 10, 31, 18, 30, tzinfo=UTC))  # 31 Oct 23:30 in Tashkent
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(10)), at=fakes.clock.now())
    assert not (await service.allowance(dev_deps, Provider.OPENROUTER)).allowed
    assert len(await service.notify_caps(dev_deps)) == 1

    fakes.clock.set(datetime(2026, 10, 31, 19, 0, 1, tzinfo=UTC))  # 1 Nov 00:00:01 there, still 31 Oct in UTC
    november = await service.allowance(dev_deps, Provider.OPENROUTER)
    assert (november.month, november.spent_usd_micros, november.allowed) == ("2026-11", 0, True)
    assert await service.notify_caps(dev_deps) == ()

    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(10)), at=fakes.clock.now())
    [notice] = await service.notify_caps(dev_deps)
    assert (notice.allowance.month, notice.level) == ("2026-11", CapLevel.REACHED)
    assert len(await replies_of(dev_deps.db, "system")) == 2
    async with dev_deps.db.read() as s:
        assert [row.month for row in await store.list_cap_notices(s, month="2026-11")] == ["2026-11"]


# --- the watch loop and the daily summary ---------------------------------------------------------------------------
async def test_the_daily_summary_is_off_by_default(dev_deps: DevDeps) -> None:
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(1)), at=NOW)
    assert await service.watch_costs(dev_deps) is None
    assert await replies_of(dev_deps.db, "report") == []


async def test_the_daily_summary_goes_once_a_day_at_its_time(dev_deps: DevDeps, fakes: Fakes) -> None:
    await add_setting_row(dev_deps.db, defaults.SCHEDULE_COST_SUMMARY_AT.key, "14:00", at=NOW)  # now, in Tashkent
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(0, 42)), make_turn_usage(), at=NOW)

    sent = await service.watch_costs(dev_deps)
    assert sent == date(2026, 10, 23)
    assert await service.watch_costs(dev_deps, last_summary=sent) == sent
    assert await service.watch_costs(dev_deps) == sent  # a restart forgets `last_summary`; the outbox does not
    [summary] = await replies_of(dev_deps.db, "report")
    assert shown(summary) == (
        "Xarajat, so'nggi 24 soat: pullik API $0.42 (shu oy $0.42) · Claude ~$0.40 — obuna doirasida, pul "
        "to'lanmaydi · 1 ta ish"
    )

    await fakes.clock.advance(24 * 3600)
    assert await service.watch_costs(dev_deps, last_summary=sent) == date(2026, 10, 24)
    assert len(await replies_of(dev_deps.db, "report")) == 2


async def test_the_personal_bot_has_no_daily_summary(personal_deps: PersonalDeps) -> None:
    assert await service.watch_costs(personal_deps) is None
    assert await list_replies(personal_deps.db, BotName.PERSONAL) == []


async def test_the_watch_loop_checks_the_caps_every_5_minutes(
    dev_deps: DevDeps, fakes: Fakes, loops: list[asyncio.Task[None]]
) -> None:
    loops.append(aio.spawn("cost", service.run_cost_watch(dev_deps)))
    await until(lambda: fakes.clock.sleeps == [domain.WATCH_EVERY_S], what="first pass")
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(9)), at=NOW)  # a call crossed 80 % meanwhile
    await fakes.clock.advance(domain.WATCH_EVERY_S)
    await until(lambda: len(fakes.clock.sleeps) == 2, what="second pass")
    assert len(await replies_of(dev_deps.db, "system")) == 1


# --- retention ------------------------------------------------------------------------------------------------------
async def test_retention_deletes_only_rows_older_than_400_days(dev_deps: DevDeps) -> None:
    now = dev_deps.clock.now()
    async with dev_deps.db.uow() as s:
        await service.record_usage(s, bot=dev_deps.bot, at=now - timedelta(days=401), usage=make_usage(source="old"))
        await service.record_usage(s, bot=dev_deps.bot, at=now - timedelta(days=399), usage=make_usage(source="kept"))
    assert await service.apply_retention(dev_deps) == 1
    async with dev_deps.db.read() as s:
        assert [row.source for row in await store.list_usage_records(s)] == ["kept"]


async def test_retention_deletes_cap_notices_older_than_400_days(dev_deps: DevDeps, fakes: Fakes) -> None:
    await add_usage(dev_deps.db, stt(Provider.OPENROUTER, usd(10)), at=NOW)
    assert len(await service.notify_caps(dev_deps)) == 1
    fakes.clock.set(NOW + timedelta(days=401))
    assert await service.apply_retention(dev_deps) == 2  # the usage row and its notice
    async with dev_deps.db.read() as s:
        assert await store.list_cap_notices(s, month="2026-10") == []
