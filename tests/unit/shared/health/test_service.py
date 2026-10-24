"""health.service on a real SQLite file, `FakeClock` and the fakes: the heartbeat file, sleep and wake, the restart
notice and the clean-exit marker, the pinned "Onlayn", the CLI smoke test, the disk guard, the media purge, retention
and `holat?` within 3 s (SPEC §2.5 steps 7–8, §2.6, §2.8, §5.6, §6.2, §9.2; UC-06; NFR-REL-01/04/08, NFR-CAP-05)."""

import asyncio
import json
import os
import stat
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    FAKE_KEYS,
    HEALTH_PID,
    HEALTH_VERSION,
    OWNER_ID,
    Fakes,
    add_health_event,
    drain_outbox,
    list_health_events,
    make_health_state,
    make_media_dir,
    send_outbox,
    store_secret,
)
from tests.fakes.assistant import FakeAssistant
from tests.fakes.mac_host import GIB, FakeMacHost
from tests.owner_text import read, shown
from yordamchi.core import aio
from yordamchi.core.errors import CallTimedOut, ProviderUnavailable
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.health import domain, service, store
from yordamchi.shared.health.domain import (
    DiskLevel,
    Mode,
    OnlineMessage,
    OnlineStep,
    PollErrorKind,
    QueuedWork,
    SmokeOutcome,
    WorkStatus,
)
from yordamchi.shared.health.service import HealthState
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.models import OutboxMessage

V309 = RuntimeStatus(installed=True, version="2.1.309", logged_in=True, auth_method="claude.ai")
V310 = replace(V309, version="2.1.310")
# How a `getUpdates` attempt went, as the polling runner's `PollTick.error` says it.
OK, NETWORK, OTHER = PollErrorKind.NONE, PollErrorKind.NETWORK, PollErrorKind.OTHER


@pytest.fixture
def state(fakes: Fakes) -> HealthState:
    return make_health_state(fakes.clock)


async def system_texts(deps: SharedDeps, kind: str = "system") -> list[str]:
    async with deps.db.read() as s:
        rows = await outbox_store.list_outbox_messages(s, deps.bot.value)
    return [_text(row) for row in rows if row.kind == kind and row.op == "send_text"]


def _text(row: OutboxMessage) -> str:
    payload = row.payload_json
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


async def kinds(deps: SharedDeps) -> list[str]:
    return [event.kind for event in await list_health_events(deps.db, deps.bot)]


def heartbeat_file(paths: Paths, deps: SharedDeps) -> dict[str, object]:
    body = json.loads(paths.heartbeat(deps.bot).read_text(encoding="utf-8"))
    assert isinstance(body, dict)
    return body


async def until(check: Callable[[], object], *, timeout: float = 3.0, what: str = "condition") -> None:
    """Wait (real time) for a background loop to reach a state; the fake clock moves only when the test says so."""
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            msg = f"{what} not reached"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


@pytest.fixture
async def loops() -> AsyncIterator[list[asyncio.Task[None]]]:
    """Background loops a test starts; cancelled at the end."""
    tasks: list[asyncio.Task[None]] = []
    yield tasks
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# --- the heartbeat file ---------------------------------------------------------------------------------------------
async def test_a_successful_poll_writes_the_heartbeat_file_atomically_and_privately(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=2, error=OK)

    path = paths.heartbeat(dev_deps.bot)
    body = heartbeat_file(paths, dev_deps)
    assert set(body) == {
        "pid",
        "version",
        "started_at",
        "last_poll_ok_at",
        "mode",
        "loop_lag_ms",
        "last_poll_attempt_at",
        "last_poll_error_kind",
        "consecutive_poll_failures",
    }
    assert body["pid"] == HEALTH_PID
    assert body["version"] == HEALTH_VERSION
    assert body["started_at"] == state.started_at.isoformat()
    assert body["last_poll_ok_at"] == fakes.clock.now().isoformat()
    assert body["mode"] == "normal"
    assert isinstance(body["loop_lag_ms"], int)
    assert body["last_poll_attempt_at"] == fakes.clock.now().isoformat()
    assert body["last_poll_error_kind"] == "none"
    assert body["consecutive_poll_failures"] == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(item.name for item in paths.run.iterdir()) == ["dev.heartbeat.json"]  # no temp file left behind


async def test_every_poll_rewrites_the_file_with_its_time(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    await fakes.clock.advance(25)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    assert heartbeat_file(paths, dev_deps)["last_poll_ok_at"] == fakes.clock.now().isoformat()


async def test_a_failed_poll_writes_its_attempt_and_kind_and_keeps_the_last_success(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState
) -> None:
    last_ok = fakes.clock.now()
    await service.record_poll(dev_deps, state, at=last_ok, updates=0, error=OK)
    for _ in range(2):
        await fakes.clock.advance(10)
        await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=NETWORK)

    body = heartbeat_file(paths, dev_deps)
    assert body["last_poll_attempt_at"] == fakes.clock.now().isoformat()
    assert body["last_poll_error_kind"] == "network"
    assert body["consecutive_poll_failures"] == 2
    assert body["last_poll_ok_at"] == last_ok.isoformat()  # a failure is never a sign of a working poll

    await fakes.clock.advance(5)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OTHER)
    assert heartbeat_file(paths, dev_deps)["last_poll_error_kind"] == "other"
    assert heartbeat_file(paths, dev_deps)["consecutive_poll_failures"] == 3


async def test_a_successful_poll_clears_the_failure_streak(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=NETWORK)
    await fakes.clock.advance(2)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=1, error=OK)

    body = heartbeat_file(paths, dev_deps)
    assert body["last_poll_error_kind"] == "none"
    assert body["consecutive_poll_failures"] == 0
    assert body["last_poll_ok_at"] == body["last_poll_attempt_at"] == fakes.clock.now().isoformat()


async def test_while_polls_fail_on_the_network_the_timer_writes_every_30_s(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    await fakes.clock.advance(5)
    attempt = fakes.clock.now()
    await service.record_poll(dev_deps, state, at=attempt, updates=0, error=NETWORK)
    for _ in range(2):  # the loop waits ≤ 30 s and probes ≤ 10 s between attempts; the timer fills the gaps
        await fakes.clock.advance(domain.HEARTBEAT_EVERY_S)
        assert await service.heartbeat_tick(dev_deps, state)
    assert heartbeat_file(paths, dev_deps)["last_poll_attempt_at"] == attempt.isoformat()  # never the timer's time

    await fakes.clock.advance(domain.HEARTBEAT_EVERY_S)  # 90 s since the last attempt: the loop itself stopped
    assert not await service.heartbeat_tick(dev_deps, state)


async def test_the_timer_does_not_cover_polls_failing_for_another_reason(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OTHER)
    await fakes.clock.advance(domain.HEARTBEAT_EVERY_S)
    assert not await service.heartbeat_tick(dev_deps, state)


async def test_once_polling_runs_the_timer_never_refreshes_a_normal_bot(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    assert await service.heartbeat_tick(dev_deps, state)  # the start, before the first poll
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    await fakes.clock.advance(30)
    assert not await service.heartbeat_tick(dev_deps, state)  # a dead poller goes stale for the watchdog's 120 s


async def test_a_paused_bot_keeps_its_heartbeat_fresh_with_the_timer(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    await service.set_mode(dev_deps, state, Mode.PAUSED)
    assert heartbeat_file(paths, dev_deps)["mode"] == "paused"
    await fakes.clock.advance(3600)
    assert await service.heartbeat_tick(dev_deps, state)
    assert heartbeat_file(paths, dev_deps)["mode"] == "paused"


async def test_the_timer_loop_writes_at_once_and_then_every_30_s(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState, loops: list[asyncio.Task[None]]
) -> None:
    loops.append(aio.spawn("heartbeat", service.run_heartbeat(dev_deps, state)))
    await until(lambda: paths.heartbeat(dev_deps.bot).exists(), what="first heartbeat")
    assert heartbeat_file(paths, dev_deps)["last_poll_ok_at"] is None  # before the first poll
    assert fakes.clock.sleeps == [domain.HEARTBEAT_EVERY_S]
    first = state.last_tick_at
    await fakes.clock.advance(30)
    await until(lambda: state.last_tick_at != first, what="second tick")
    assert state.last_tick_at == fakes.clock.now()


# --- sleep and wake (NFR-REL-04) ------------------------------------------------------------------------------------
async def test_after_a_sleep_the_first_poll_batch_posts_the_wake_gap_with_its_size(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    before = fakes.clock.now()  # 14:00 in Tashkent
    await service.record_poll(dev_deps, state, at=before, updates=0, error=OK)
    fakes.clock.set(before + timedelta(hours=2, minutes=5))  # the Mac slept; nothing ran
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=3, error=OK)
    await service.record_poll(dev_deps, state, at=fakes.clock.now() + timedelta(seconds=25), updates=0, error=OK)

    assert await system_texts(dev_deps) == ["Mac uxlagan edi 14:00–16:05, 3 ta xabar qayta ishlanmoqda"]
    assert await kinds(dev_deps) == ["wake_gap"]
    [event] = await list_health_events(dev_deps.db)
    assert event.data_json == {"slept_s": 7500, "updates": 3}
    await send_outbox(dev_deps)
    assert [request.chat_id for request in fakes.messenger.sent()] == [DEV_GROUP_ID]


async def test_a_gap_the_timer_saw_first_is_told_after_the_next_successful_poll(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    await service.heartbeat_tick(dev_deps, state)
    fakes.clock.set(fakes.clock.now() + timedelta(minutes=40))
    await service.heartbeat_tick(dev_deps, state)  # woke: the timer ticks before getUpdates comes back
    assert await system_texts(dev_deps) == []
    await fakes.clock.advance(20)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    assert await system_texts(dev_deps) == ["Mac uxlagan edi 14:00–14:40, 0 ta xabar qayta ishlanmoqda"]


async def test_a_network_drop_while_the_mac_is_awake_is_not_a_sleep(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    for _ in range(20):  # 10 min of failing polls; the timer still ticks every 30 s
        await fakes.clock.advance(30)
        await service.heartbeat_tick(dev_deps, state)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=4, error=OK)
    assert await system_texts(dev_deps) == []


# --- network outages (T32b) ----------------------------------------------------------------------------------------
async def fail_on_the_network(deps: SharedDeps, fakes: Fakes, state: HealthState, *, minutes: int) -> None:
    """Polls that fail on the network every 30 s for `minutes`, the timer ticking in between."""
    for _ in range(minutes * 2):
        await service.record_poll(deps, state, at=fakes.clock.now(), updates=0, error=NETWORK)
        await fakes.clock.advance(15)
        await service.heartbeat_tick(deps, state)
        await fakes.clock.advance(15)


async def test_an_outage_of_5_min_or_more_is_told_once_after_the_first_successful_poll(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)  # 14:00 in Tashkent
    await fakes.clock.advance(60)
    await fail_on_the_network(dev_deps, fakes, state, minutes=39)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=2, error=OK)
    await fakes.clock.advance(25)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)

    assert await system_texts(dev_deps) == ["Internet uzilgan edi 14:01–14:40"]
    [event] = await list_health_events(dev_deps.db)
    assert event.kind == "network_gap"
    assert event.data_json == {"offline_s": 39 * 60}
    await send_outbox(dev_deps)
    assert [request.chat_id for request in fakes.messenger.sent()] == [DEV_GROUP_ID]


async def test_a_short_outage_is_not_told(dev_deps: DevDeps, fakes: Fakes, state: HealthState) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    await fail_on_the_network(dev_deps, fakes, state, minutes=4)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    assert await system_texts(dev_deps) == []
    assert await kinds(dev_deps) == []


async def test_polls_failing_for_another_reason_are_not_an_outage(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    for _ in range(20):  # 10 min of 409s or 401s
        await fakes.clock.advance(30)
        await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OTHER)
    await service.record_poll(dev_deps, state, at=fakes.clock.now(), updates=0, error=OK)
    assert await system_texts(dev_deps) == []


# --- restart notice and clean exit (SPEC §2.5 step 8, §2.6; UC-06) -------------------------------------------------
async def test_an_unclean_exit_is_reported_with_the_interrupted_projects_and_the_last_poll_time(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState
) -> None:
    paths.run.mkdir(parents=True)
    previous = {"pid": 1, "started_at": "x", "last_poll_ok_at": "2026-10-23T08:58:00+00:00", "mode": "normal"}
    paths.heartbeat(dev_deps.bot).write_text(json.dumps(previous), encoding="utf-8")

    started = await service.report_start(dev_deps, state, interrupted=("BonviHR", "HRMentor"))

    expected = (
        "**Qayta ishga tushdim** (oldingi ish kutilmaganda uzildi, oxirgi marta 13:58 da ishlagan edim).\n"
        "**Uzilib qolgan ishlar:** BonviHR, HRMentor — davom ettiraymi?"
    )
    assert (started.clean, started.notice) == (False, expected)
    assert await system_texts(dev_deps) == [expected]
    assert await kinds(dev_deps) == ["started", "unclean_restart"]
    events = await list_health_events(dev_deps.db)
    assert events[0].data_json == {"version": HEALTH_VERSION, "pid": HEALTH_PID, "clean": False}
    assert events[1].data_json == {"interrupted": 2, "down_s": 120}
    await send_outbox(dev_deps)  # the sender runs every 0.25 s: the owner reads it within seconds (≤ 60 s)
    assert [read(request) for request in fakes.messenger.sent()] == [shown(expected)]


async def test_a_clean_exit_leaves_a_private_marker_and_the_next_start_says_nothing(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths, state: HealthState
) -> None:
    await service.mark_clean_exit(dev_deps, state)
    marker = paths.clean_marker(dev_deps.bot)
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert json.loads(marker.read_text(encoding="utf-8"))["version"] == HEALTH_VERSION

    started = await service.report_start(dev_deps, make_health_state(fakes.clock), interrupted=())

    assert started == service.Started(clean=True, notice=None)
    assert not marker.exists()  # a crash of this run is caught by the next start
    assert await system_texts(dev_deps) == []
    assert await kinds(dev_deps) == ["clean_exit", "started"]


async def test_a_crash_after_a_clean_start_is_still_reported_next_time(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    await service.mark_clean_exit(dev_deps, state)
    await service.report_start(dev_deps, make_health_state(fakes.clock))
    # … `kill -9`: no marker written. The next process:
    started = await service.report_start(dev_deps, make_health_state(fakes.clock))
    assert started.clean is False
    assert started.notice == "**Qayta ishga tushdim** (oldingi ish kutilmaganda uzildi).\nUzilib qolgan ish yo'q."


async def test_the_personal_bot_tells_its_restart_in_the_owner_dm(
    personal_deps: PersonalDeps, fakes: Fakes, state: HealthState
) -> None:
    await service.report_start(personal_deps, state)
    await send_outbox(personal_deps)
    assert [request.chat_id for request in fakes.messenger.sent()] == [OWNER_ID]


# --- the pinned "Onlayn: HH:MM" (SPEC §2.8, UC-06) ------------------------------------------------------------------
async def online_state(deps: SharedDeps) -> OnlineMessage | None:
    async with deps.db.read() as s:
        row = await store.get_kv(s, bot=deps.bot.value, key=domain.KV_ONLINE)
    return None if row is None else OnlineMessage.from_json(row.value_json)


async def test_onlayn_is_sent_pinned_once_then_edited_in_place(dev_deps: DevDeps, fakes: Fakes) -> None:
    assert await service.refresh_online(dev_deps) is OnlineStep.SEND
    assert await service.refresh_online(dev_deps) is OnlineStep.WAIT  # still queued: nothing to edit yet
    await send_outbox(dev_deps)
    [sent] = fakes.messenger.sent()
    assert (sent.chat_id, read(sent), sent.payload["disable_notification"]) == (
        DEV_GROUP_ID,
        "Onlayn: 14:00",
        True,
    )
    [pin] = fakes.messenger.sent("pin")
    assert pin.payload["message_id"] == 101

    await fakes.clock.advance(300)
    assert await service.refresh_online(dev_deps) is OnlineStep.EDIT
    await drain_outbox(dev_deps, fakes.clock, seconds=1)
    [edit] = fakes.messenger.sent("edit_text")
    assert (edit.payload["message_id"], read(edit)) == (101, "Onlayn: 14:05")
    assert await online_state(dev_deps) == OnlineMessage(generation=1, message_id=101, edits=1)
    assert len(fakes.messenger.sent()) == 1  # one message for the life of the group


async def test_onlayn_survives_a_restart_by_its_stored_message_id(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.refresh_online(dev_deps)
    await send_outbox(dev_deps)
    await service.refresh_online(dev_deps)
    async with dev_deps.db.uow() as s:  # 30 days later the outbox rows are gone; the kv row stays
        for row in await outbox_store.list_outbox_messages(s, dev_deps.bot.value):
            await s.delete(row)
    await fakes.clock.advance(300)
    assert await service.refresh_online(dev_deps) is OnlineStep.EDIT
    await drain_outbox(dev_deps, fakes.clock, seconds=1)
    assert [edit.payload["message_id"] for edit in fakes.messenger.sent("edit_text")] == [101]


async def test_a_deleted_onlayn_message_is_sent_and_pinned_again(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.refresh_online(dev_deps)
    await send_outbox(dev_deps)
    await fakes.clock.advance(300)
    await service.refresh_online(dev_deps)
    fakes.messenger.fail_next("edit_text", ProviderUnavailable(provider="telegram", retryable=False))
    await drain_outbox(dev_deps, fakes.clock, seconds=1)  # "message to edit not found"

    await fakes.clock.advance(300)
    assert await service.refresh_online(dev_deps) is OnlineStep.SEND
    await send_outbox(dev_deps)
    assert [read(request) for request in fakes.messenger.sent()] == ["Onlayn: 14:00", "Onlayn: 14:10"]
    assert [request.payload["message_id"] for request in fakes.messenger.sent("pin")] == [101, 102]
    await fakes.clock.advance(300)
    assert await service.refresh_online(dev_deps) is OnlineStep.EDIT
    assert await online_state(dev_deps) == OnlineMessage(generation=2, message_id=102, edits=1)


async def test_an_edit_lost_to_a_network_drop_does_not_post_a_second_onlayn(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.refresh_online(dev_deps)
    await send_outbox(dev_deps)
    await fakes.clock.advance(300)
    await service.refresh_online(dev_deps)
    for _ in range(5):  # every retry of the edit fails: the network is down
        fakes.messenger.fail_next("edit_text", ProviderUnavailable(provider="telegram"))
    await drain_outbox(dev_deps, fakes.clock, seconds=40)
    await fakes.clock.advance(300)
    assert await service.refresh_online(dev_deps) is OnlineStep.EDIT
    assert len(fakes.messenger.sent()) == 1


async def test_the_onlayn_loop_edits_every_heartbeat_edit_min(
    dev_deps: DevDeps, fakes: Fakes, loops: list[asyncio.Task[None]]
) -> None:
    loops.append(aio.spawn("online", service.run_online(dev_deps)))
    await until(lambda: fakes.clock.pending_sleepers > 0, what="first refresh")
    assert fakes.clock.sleeps == [300.0]  # schedule.heartbeat_edit_min = 5


# --- self-checks and the CLI smoke test (NFR-REL-08) ----------------------------------------------------------------
class RecordingSmoke:
    def __init__(self, *outcomes: SmokeOutcome | Exception) -> None:
        self.outcomes = list(outcomes)
        self.versions: list[str] = []

    async def __call__(self, version: str) -> SmokeOutcome:
        self.versions.append(version)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


async def stored_cli_version(deps: SharedDeps) -> object:
    async with deps.db.read() as s:
        row = await store.get_kv(s, bot=deps.bot.value, key=domain.KV_CLI_VERSION)
    return None if row is None else row.value_json


async def test_the_first_cli_version_is_the_reference_with_no_smoke_turn(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    smoke = RecordingSmoke()
    check = await service.check_claude(dev_deps, state, smoke=smoke)
    assert (check.version, check.logged_in, check.auth_method) == ("2.1.309", True, "claude.ai")
    assert state.claude == check
    assert smoke.versions == []
    assert await stored_cli_version(dev_deps) == {"version": "2.1.309"}
    assert await kinds(dev_deps) == ["cli_version"]
    await service.check_claude(dev_deps, state, smoke=smoke)  # unchanged: nothing more
    assert await kinds(dev_deps) == ["cli_version"]


async def test_an_updated_cli_runs_one_smoke_turn_and_a_pass_tells_the_owner_nothing(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    smoke = RecordingSmoke(SmokeOutcome(ok=True))
    await service.check_claude(dev_deps, state, smoke=smoke)
    fakes.assistant.status = V310  # the CLI auto-updated under the running bot
    await service.check_claude(dev_deps, state, smoke=smoke)
    await service.check_claude(dev_deps, state, smoke=smoke)

    assert smoke.versions == ["2.1.310"]
    assert await kinds(dev_deps) == ["cli_version", "cli_version", "smoke_ok"]
    assert await stored_cli_version(dev_deps) == {"version": "2.1.310"}
    assert await system_texts(dev_deps) == []


async def test_a_failed_smoke_turn_is_recorded_and_told_once(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    smoke = RecordingSmoke(SmokeOutcome(ok=False, step="commit"))
    await service.check_claude(dev_deps, state, smoke=smoke)
    fakes.assistant.status = V310
    await service.check_claude(dev_deps, state, smoke=smoke)

    [text] = await system_texts(dev_deps)
    assert text.startswith("Claude CLI 2.1.310 ga yangilandi, lekin sinov ishi o'tmadi: fayl commit qilinmadi.")
    events = await list_health_events(dev_deps.db)
    assert [(event.kind, event.data_json) for event in events[1:]] == [
        ("cli_version", {"from": "2.1.309", "to": "2.1.310"}),
        ("smoke_failed", {"version": "2.1.310", "step": "commit"}),
    ]


async def test_a_smoke_runner_that_raises_counts_as_a_failed_run(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    smoke = RecordingSmoke(CallTimedOut(operation="session_open"))
    await service.check_claude(dev_deps, state, smoke=smoke)
    fakes.assistant.status = V310
    await service.check_claude(dev_deps, state, smoke=smoke)
    assert [event.data_json for event in await list_health_events(dev_deps.db) if event.kind == "smoke_failed"] == [
        {"version": "2.1.310", "step": "call_timed_out"}
    ]


async def test_a_crash_during_the_smoke_turn_repeats_the_test_after_the_restart(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    class Crash(Exception):
        pass

    smoke = RecordingSmoke(Crash(), SmokeOutcome(ok=True))
    await service.check_claude(dev_deps, state, smoke=smoke)
    fakes.assistant.status = V310
    with pytest.raises(Crash):
        await service.check_claude(dev_deps, state, smoke=smoke)
    assert await stored_cli_version(dev_deps) == {"version": "2.1.309"}
    await service.check_claude(dev_deps, make_health_state(fakes.clock), smoke=smoke)
    assert smoke.versions == ["2.1.310", "2.1.310"]


async def test_the_personal_bot_records_the_version_but_runs_no_smoke_turn(
    personal_deps: PersonalDeps, fakes: Fakes, state: HealthState
) -> None:
    await service.check_claude(personal_deps, state)
    fakes.assistant.status = V310
    await service.check_claude(personal_deps, state)
    assert await kinds(personal_deps) == ["cli_version", "cli_version"]


class FailingProbe(FakeAssistant):
    async def probe(self, *, timeout: float) -> RuntimeStatus:
        raise CallTimedOut(operation="claude_probe")


async def test_a_probe_that_fails_is_cached_for_holat_and_records_nothing(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState
) -> None:
    deps = replace(dev_deps, assistant=FailingProbe())
    check = await service.check_claude(deps, state)
    assert check.error_code == "call_timed_out"
    assert await kinds(deps) == []
    assert "Claude CLI: tekshirib bo'lmadi (call_timed_out)" in shown(await service.show_health(deps, state))


async def test_the_check_loop_runs_at_start_then_every_ten_minutes(
    dev_deps: DevDeps, fakes: Fakes, state: HealthState, loops: list[asyncio.Task[None]]
) -> None:
    loops.append(aio.spawn("health_checks", service.run_checks(dev_deps, state)))
    await until(lambda: fakes.clock.pending_sleepers > 0, what="first check")
    assert state.claude is not None
    assert fakes.clock.sleeps == [domain.CHECK_EVERY_S]


# --- disk guard (SPEC §9.2, NFR-CAP-05) -----------------------------------------------------------------------------
async def test_low_disk_is_told_at_most_once_per_six_hours(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.mac_host.free_bytes = 7 * GIB
    assert await service.guard_disk(dev_deps) is DiskLevel.LOW
    await fakes.clock.advance(5 * 3600)
    assert await service.guard_disk(dev_deps) is DiskLevel.LOW
    await fakes.clock.advance(3600)
    await service.guard_disk(dev_deps)

    low = "Diskda 7 GB joy qoldi (10 GB dan kam). Keraksiz fayllarni o'chirib, joy bo'shating."
    assert await system_texts(dev_deps) == [low, low]
    assert await kinds(dev_deps) == ["disk_low", "disk_low"]


async def test_disk_that_gets_worse_is_told_at_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.mac_host.free_bytes = 7 * GIB
    await service.guard_disk(dev_deps)
    await fakes.clock.advance(600)
    fakes.mac_host.free_bytes = 2 * GIB
    assert await service.guard_disk(dev_deps) is DiskLevel.BLOCKED
    texts = await system_texts(dev_deps)
    assert len(texts) == 2
    assert texts[1].startswith("Diskda atigi 2 GB qoldi (3 GB dan kam) — yangi kodlash ishlari navbatda turadi.")


async def test_enough_disk_says_nothing(dev_deps: DevDeps) -> None:
    assert await service.guard_disk(dev_deps) is DiskLevel.OK
    assert await system_texts(dev_deps) == []


class BrokenDisk(FakeMacHost):
    async def free_disk_bytes(self, path: Path, *, timeout: float) -> int:
        raise OSError("statvfs failed")


async def test_an_unreadable_disk_is_logged_not_told(dev_deps: DevDeps, caplog: pytest.LogCaptureFixture) -> None:
    deps = replace(dev_deps, mac_host=BrokenDisk())
    assert await service.guard_disk(deps) is None
    assert "disk_unreadable" in caplog.text
    assert await system_texts(deps) == []


# --- media purge and retention (SPEC §4.2, §9.2) --------------------------------------------------------------------
async def test_media_folders_older_than_the_retention_are_purged(
    dev_deps: DevDeps, paths: Paths, tmp_path: Path
) -> None:
    # The fake clock reads 2026-10-23 14:00 in Tashkent; media.retention_days = 7.
    for name in ("20261010", "20261015", "20261016", "20261023", "keep-me"):
        make_media_dir(paths, name, files=2)
    elsewhere = tmp_path / "owner-files"
    elsewhere.mkdir()
    (paths.media / "20200101").symlink_to(elsewhere, target_is_directory=True)

    assert await service.purge_media(dev_deps) == 2

    assert sorted(item.name for item in paths.media.iterdir()) == ["20200101", "20261016", "20261023", "keep-me"]
    assert elsewhere.exists()  # a symlink is never followed


async def test_purging_without_a_media_folder_does_nothing(dev_deps: DevDeps) -> None:
    assert await service.purge_media(dev_deps) == 0


async def test_events_older_than_ninety_days_are_deleted(dev_deps: DevDeps, fakes: Fakes) -> None:
    now = fakes.clock.now()
    for days in (91, 90, 3):
        await add_health_event(dev_deps.db, at=now - timedelta(days=days))
    await add_health_event(dev_deps.db, at=now - timedelta(days=200), bot=dev_deps.bot)
    assert await service.apply_retention(dev_deps) == 2
    assert [event.at for event in await list_health_events(dev_deps.db)] == [
        now - timedelta(days=90),
        now - timedelta(days=3),
    ]


# --- holat? (UC-06: ≤ 3 s, local state only) ------------------------------------------------------------------------
async def busy_dev(fakes: Fakes, deps: DevDeps) -> None:
    fakes.mac_host.pressure_level = 2
    fakes.mac_host.free_bytes = 85 * GIB
    fakes.mac_host.manager_state = ClaudeManagerState(
        active_id="b",
        auto_switch=True,
        accounts=(
            ClaudeAccount("owner@example.com", "ok", 42.0, 13.0, None, None, is_active=True),
            ClaudeAccount("other@example.com", "ok", 0.0, 0.0, None, None),
        ),
    )
    await store_secret(deps.db, fakes.keystore, "google_api_key", FAKE_KEYS["google_api_key"], bot=deps.bot)


async def test_holat_shows_the_bot_from_local_state(dev_deps: DevDeps, fakes: Fakes) -> None:
    state = make_health_state(fakes.clock, degraded=("ffmpeg",))
    await busy_dev(fakes, dev_deps)
    await service.check_claude(dev_deps, state)
    service.note_error(dev_deps, state, "stt_failed")
    await fakes.clock.advance(3 * 3600 + 12 * 60)

    async def work() -> WorkStatus:
        return WorkStatus(running=("BonviHR",), queued=(QueuedWork("HRMentor", 1),), decisions=2, questions=1)

    text = await service.show_health(dev_deps, state, work=work)

    assert shown(text).splitlines() == [
        "Holat · dev bot",
        "Ish vaqti: 3 soat 12 daq · Yordamchi 2026.10.24-abcdef0 · SDK 0.2.173",
        "Claude CLI 2.1.309: login — claude.ai",
        "ClaudeManager: owner@example.com · 5 soat: 42% · 7 kun: 13%",
        "Ovoz hozir: Google Gemini.",
        "Hozir ishlayapti: BonviHR",
        "Navbatda: HRMentor (1-o'rin)",
        "Javob kutayotgan qarorlar: 2 · savollar: 1",
        "Mac: xotira bosimi — o'rtacha · diskda 85 GB bo'sh",
        "Cheklangan imkoniyatlar: ffmpeg topilmadi",
        "Oxirgi xatolar: stt_failed (14:00)",
    ]
    assert fakes.assistant.requests == []  # holat? asks no model and never probes the CLI itself


async def test_holat_keeps_only_the_last_three_error_codes(dev_deps: DevDeps, fakes: Fakes, state: HealthState) -> None:
    for code in ("a_failed", "b_failed", "c_failed", "d_failed"):
        service.note_error(dev_deps, state, code)
        await fakes.clock.advance(60)
    lines = shown(await service.show_health(dev_deps, state)).splitlines()
    assert lines[-1] == "Oxirgi xatolar: d_failed (14:03), c_failed (14:02), b_failed (14:01)"


class HangingMac(FakeMacHost):
    async def claude_manager_state(self, *, timeout: float) -> ClaudeManagerState | None:
        await asyncio.Event().wait()  # a stuck file read
        return None


async def test_a_hanging_part_shows_as_unknown_and_holat_still_answers_within_3_s(
    dev_deps: DevDeps, state: HealthState
) -> None:
    deps = replace(dev_deps, mac_host=HangingMac())

    async def stuck_work() -> WorkStatus:
        await asyncio.Event().wait()
        return WorkStatus()

    started = time.monotonic()
    text = await service.show_health(deps, state, work=stuck_work)
    elapsed = time.monotonic() - started

    assert elapsed < 3.0
    assert "ClaudeManager: holatini o'qib bo'lmadi" in shown(text)
    assert "Ishlar: o'qib bo'lmadi" in shown(text)


async def test_holat_on_the_personal_bot_has_no_work_lines(
    personal_deps: PersonalDeps, fakes: Fakes, state: HealthState
) -> None:
    lines = shown(await service.show_health(personal_deps, state)).splitlines()
    assert lines[0] == "Holat · shaxsiy bot"
    assert "Claude CLI: hali tekshirilmadi" in lines
    assert "Ovoz: ishlaydigan kalit yo'q — ovozli xabarlarni matnga o'gira olmayman." in lines
    assert not any(line.startswith("Hozir ishlayapti") for line in lines)


async def test_new_state_takes_this_process_and_the_degraded_binaries_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    state = service.new_state(dev_deps, version="v1", sdk_version="0.2.173", degraded=["gh", "ffmpeg", "gh"])
    assert (state.pid, state.started_at, state.degraded, state.mode) == (
        os.getpid(),
        fakes.clock.now(),
        ("gh", "ffmpeg"),
        Mode.NORMAL,
    )
