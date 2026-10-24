"""burst.service on a real SQLite file, fed through the real inbox, on virtual time (FakeClock; nothing sleeps for
real): the UC-02 and UC-03 acceptance cases, voice holds, recovery after a restart (SPEC §2.7), the loop, the router
hand-off and retention (SPEC §3.2.1 step 4, §4.2, §4.4)."""

import asyncio
import tomllib
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest
from sqlalchemy.exc import IntegrityError

from tests.factories import (
    Fakes,
    make_dev_deps,
    make_media_update,
    make_owner_origin,
    make_update,
    make_window_settings,
    origin_of,
    send_outbox,
)
from tests.fakes.clock import FakeClock
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.persistence.uow import Database
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.burst import service, store
from yordamchi.shared.burst.domain import BurstEvents, BurstStatus, CloseReason, TranscriptState, TurnInput
from yordamchi.shared.burst.models import Burst
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import IncomingUpdate, UpdateKind
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.domain import Op
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

WS = make_window_settings()
BURST_THRESHOLDS: Final = tomllib.loads(
    (Path(__file__).resolve().parents[3] / "eval" / "thresholds.toml").read_text(encoding="utf-8")
)["burst"]

type Deps = DevDeps | PersonalDeps


async def _send(deps: Deps, update: IncomingUpdate) -> BurstEvents:
    """The owner sends `update` now: the inbox takes it in, then the burst pass attaches it."""
    await inbox.accept_update(deps, origin_of(update), update)
    return await service.attach_received_updates(deps)


async def _wait(deps: Deps, fakes: Fakes, seconds: float) -> BurstEvents:
    """`seconds` of virtual time pass, then the timer pass runs."""
    await fakes.clock.advance(seconds)
    return await service.close_due_bursts(deps)


async def _closes_in(deps: Deps, fakes: Fakes) -> float | None:
    closes_at = await service.next_closes_at(deps)
    return None if closes_at is None else (closes_at - fakes.clock.now()).total_seconds()


async def _bursts(deps: Deps) -> list[Burst]:
    async with deps.db.read() as s:
        return await store.list_bursts(s, deps.bot.value)


def _ids(item: TurnInput) -> list[int]:
    return [part.update_id for part in item.parts]


def _text(update_id: int, text: str) -> IncomingUpdate:
    return make_update(update_id, text=text)


class _ObservedClock(FakeClock):
    """A FakeClock that says when someone starts sleeping on it: the loop has finished its pass."""

    def __init__(self) -> None:
        super().__init__()
        self.sleeping = asyncio.Event()

    async def sleep(self, seconds: float) -> None:
        self.sleeping.set()
        await super().sleep(seconds)


async def _wait_for(event: asyncio.Event) -> None:
    """The timeout only guards against a hung loop; no virtual or real time is waited for."""
    async with asyncio.timeout(5):
        await event.wait()
    event.clear()


# --- UC-02 acceptance -----------------------------------------------------------------------------------------------
async def test_a_photo_then_text_6_s_later_is_one_turn(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    assert not await _wait(dev_deps, fakes, 6)
    await _send(dev_deps, _text(2, "shu xatoni tuzat"))
    assert await _closes_in(dev_deps, fakes) == WS.after_text_s

    events = await _wait(dev_deps, fakes, WS.after_text_s)
    [item] = events.closed
    assert (_ids(item), item.close_reason) == ([1, 2], CloseReason.QUIET)
    async with dev_deps.db.read() as s:
        rows = await inbox_store.list_inbound_updates(s, dev_deps.bot.value)
    assert {(row.status, row.burst_id) for row in rows} == {("attached", item.burst_id)}


async def test_text_then_a_voice_note_within_2_s_is_one_turn(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, _text(1, "BonviHR'da login sahifasini tuzat"))
    await fakes.clock.advance(1.9)
    events = await _send(dev_deps, make_media_update(2, kind=UpdateKind.VOICE, duration_s=14))
    assert [part.update_id for part in events.transcribe] == [2]  # voice starts the STT job
    [row] = await _bursts(dev_deps)
    assert row.status == BurstStatus.HOLDING

    assert not await _wait(dev_deps, fakes, 10)  # transcription is slow: the window holds, however long it takes
    await service.record_transcript(dev_deps, update_id=2, text="registratsiya sahifasini ham", ok=True)
    assert await _closes_in(dev_deps, fakes) == WS.after_voice_s
    [item] = (await _wait(dev_deps, fakes, WS.after_voice_s)).closed
    assert _ids(item) == [1, 2]
    assert (item.parts[1].transcript, item.parts[1].text) == (TranscriptState.DONE, "registratsiya sahifasini ham")


async def test_an_album_of_4_then_text_within_3_s_is_one_turn_with_5_parts(dev_deps: DevDeps, fakes: Fakes) -> None:
    for update_id in range(1, 5):
        await _send(dev_deps, make_media_update(update_id, media_group_id="album-1"))
        await fakes.clock.advance(0.2)
    await fakes.clock.advance(2.8)  # 3 s after the last photo
    await _send(dev_deps, _text(5, "shu to'rttala xatoni tuzat"))
    [item] = (await _wait(dev_deps, fakes, WS.after_text_s)).closed
    assert (_ids(item), item.media_group_ids) == ([1, 2, 3, 4, 5], frozenset({"album-1"}))


async def test_lone_photo_proceeds_after_window(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    window = await _closes_in(dev_deps, fakes)
    assert window is not None
    assert window <= BURST_THRESHOLDS["lone_photo_proceed_max_s"]
    assert not await _wait(dev_deps, fakes, window - 0.1)

    [item] = (await _wait(dev_deps, fakes, 0.1)).closed
    assert (_ids(item), item.close_reason, item.is_media_only) == ([1], CloseReason.QUIET, True)
    await send_outbox(dev_deps)  # acks are durable outbox rows since T15; one sender pass delivers them
    assert fakes.messenger.sent("send_text") == []  # it proceeds; nothing asks the owner anything
    assert {request.method for request in fakes.messenger.requests} == {"chat_action", "mark_read"}


async def test_a_complete_question_adds_at_most_1_5_s(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, _text(1, "BonviHR qanday ishlayapti?"))
    added = await _closes_in(dev_deps, fakes)
    assert added == WS.after_complete_text_s
    assert added <= BURST_THRESHOLDS["complete_text_added_delay_max_s"]


async def test_a_part_after_a_late_timer_still_opens_a_new_burst(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, _text(1, "login sahifasini tuzat"))
    await fakes.clock.advance(5)  # the timer pass never ran: placement goes by arrival time, not by the timer
    events = await _send(dev_deps, _text(2, "HRMentor'da testlarni yurgiz"))
    [first] = events.closed
    assert (_ids(first), first.close_reason) == ([1], CloseReason.QUIET)
    assert [row.status for row in await _bursts(dev_deps)] == [BurstStatus.CLOSED, BurstStatus.OPEN]


async def test_the_hard_cap_closes_a_burst_that_keeps_growing(dev_deps: DevDeps, fakes: Fakes) -> None:
    for update_id in range(1, 5):
        await _send(dev_deps, make_media_update(update_id))
        if update_id < 4:
            assert not await _wait(dev_deps, fakes, 9)
    [item] = (await _wait(dev_deps, fakes, WS.max_total_s - 27)).closed
    assert (_ids(item), item.close_reason) == ([1, 2, 3, 4], CloseReason.CAP)


async def test_each_update_is_attached_exactly_once(dev_deps: DevDeps) -> None:
    await _send(dev_deps, _text(1, "salom"))
    assert not await service.attach_received_updates(dev_deps)
    assert len(await _bursts(dev_deps)) == 1


async def test_a_setting_change_applies_from_the_next_part(dev_deps: DevDeps, fakes: Fakes) -> None:
    origin = make_owner_origin(update_id=900)
    await settings.record_version(dev_deps, origin, defaults.BURST_AFTER_MEDIA_S, 4.0, reason="change")
    await _send(dev_deps, make_media_update(1))
    assert await _closes_in(dev_deps, fakes) == 4.0


async def test_the_personal_bot_assembles_in_its_own_db(
    personal_deps: PersonalDeps, dev_deps: DevDeps, fakes: Fakes
) -> None:
    await _send(personal_deps, make_media_update(1, bot=BotName.PERSONAL))
    await fakes.clock.advance(4)
    await _send(personal_deps, make_update(2, text="bu nima?", bot=BotName.PERSONAL))
    [item] = (await _wait(personal_deps, fakes, WS.after_text_s)).closed
    assert _ids(item) == [1, 2]
    assert await _bursts(dev_deps) == []


# --- UC-03: interrupt words and "kut" -------------------------------------------------------------------------------
async def test_an_interrupt_word_closes_the_burst_at_once_and_is_never_merged(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    await fakes.clock.advance(1)
    events = await _send(dev_deps, _text(2, "to'xta"))

    stop, photo = events.closed  # the stop first: it must not wait behind routing the photo
    assert (_ids(stop), stop.close_reason, stop.closed_at) == ([2], CloseReason.INTERRUPT, fakes.clock.now())
    assert (_ids(photo), photo.close_reason) == ([1], CloseReason.INTERRUPTED)
    assert await service.next_closes_at(dev_deps) is None  # nothing is left assembling
    assert [item.burst_id for item in await service.list_closed_bursts(dev_deps, limit=10)] == [
        stop.burst_id,
        photo.burst_id,
    ]


async def test_kut_holds_the_assembling_burst_for_the_next_part(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    await fakes.clock.advance(1)
    assert not await _send(dev_deps, _text(2, "kut"))
    [row] = await _bursts(dev_deps)
    assert row.status == BurstStatus.HOLDING
    assert not await _wait(dev_deps, fakes, 60)

    await _send(dev_deps, _text(3, "mana, shu xatoni tuzat"))
    [item] = (await _wait(dev_deps, fakes, WS.after_text_s)).closed
    assert _ids(item) == [1, 3]  # "kut" held the window and never reaches the turn


async def test_a_kut_hold_ends_after_hold_max(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    await fakes.clock.advance(1)
    await _send(dev_deps, _text(2, "kut"))
    assert not await _wait(dev_deps, fakes, WS.hold_max_s - 0.1)
    [item] = (await _wait(dev_deps, fakes, 0.1)).closed
    assert (_ids(item), item.close_reason) == ([1], CloseReason.HOLD_EXPIRED)


async def _reactions(deps: Deps) -> list[tuple[object, object]]:
    """The reactions queued for the owner's messages, in order: (message id, emoji)."""
    async with deps.db.read() as s:
        rows = await outbox_store.list_outbox_messages(s, deps.bot.value)
    payloads = [row.payload_json for row in rows if row.op == Op.REACT.value]
    return [(payload.get("message_id"), payload.get("emoji")) for payload in payloads if isinstance(payload, dict)]


async def test_kut_with_nothing_assembling_holds_for_the_next_burst_and_closes_nothing(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    assert not await _send(dev_deps, _text(1, "kut"))  # no interrupt: nothing closes, nothing is stopped
    [row] = await _bursts(dev_deps)
    assert row.status == BurstStatus.HOLDING
    held = defaults.UI_HOLD_REACTION.default
    assert await _reactions(dev_deps) == [(5001, held)]  # "I am waiting" (no "seen" reaction by default)
    assert not await _wait(dev_deps, fakes, 90)  # the photo is still being picked
    await _send(dev_deps, make_media_update(2))
    await _send(dev_deps, _text(3, "shu xatoni tuzat"))
    [item] = (await _wait(dev_deps, fakes, WS.after_text_s)).closed
    assert (_ids(item), item.close_reason, item.interrupt) == ([2, 3], CloseReason.QUIET, None)


async def test_a_lone_kut_that_nothing_follows_lapses_without_a_turn(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, _text(1, "kut"))
    assert not await _wait(dev_deps, fakes, WS.hold_max_s)  # lapsed: no turn input, nothing for the router
    [row] = await _bursts(dev_deps)
    assert (row.status, row.close_reason) == (BurstStatus.CANCELLED, CloseReason.HOLD_EXPIRED)
    assert await service.list_closed_bursts(dev_deps, limit=10) == ()
    await _send(dev_deps, _text(2, "login sahifasini tuzat"))
    [item] = (await _wait(dev_deps, fakes, WS.after_text_s)).closed
    assert _ids(item) == [2]  # the next message is a burst of its own


async def test_a_stop_after_a_lone_kut_is_an_interrupt_and_the_hold_lapses(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, _text(1, "kut"))
    await fakes.clock.advance(5)
    [stop] = (await _send(dev_deps, _text(2, "to'xta"))).closed
    assert (_ids(stop), stop.interrupt) == ([2], "stop")
    statuses = sorted((row.id, row.status) for row in await _bursts(dev_deps))
    assert [status for _, status in statuses] == [BurstStatus.CANCELLED, BurstStatus.CLOSED]


async def test_a_spoken_kut_alone_holds_with_its_reaction(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1, kind=UpdateKind.VOICE, duration_s=1))
    await fakes.clock.advance(1)
    assert not await service.record_transcript(dev_deps, update_id=1, text="kut", ok=True)
    [row] = await _bursts(dev_deps)
    assert row.status == BurstStatus.HOLDING
    assert (5001, defaults.UI_HOLD_REACTION.default) in await _reactions(dev_deps)


async def test_a_short_voice_stop_leaves_the_burst_at_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    await fakes.clock.advance(1)
    await _send(dev_deps, make_media_update(2, kind=UpdateKind.VOICE, duration_s=1))
    await fakes.clock.advance(1)
    events = await service.record_transcript(dev_deps, update_id=2, text="To'xta.", ok=True)
    stop, photo = events.closed
    assert (_ids(stop), stop.interrupt, _ids(photo), photo.close_reason) == (
        [2],
        "stop",
        [1],
        CloseReason.INTERRUPTED,
    )


async def test_a_failed_transcript_stays_in_the_burst(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1, kind=UpdateKind.VOICE, duration_s=8))
    await fakes.clock.advance(3)
    await service.record_transcript(dev_deps, update_id=1, text="[ovoz tushunilmadi: shovqin]", ok=False)
    [item] = (await _wait(dev_deps, fakes, WS.after_voice_s)).closed
    assert (item.parts[0].transcript, item.parts[0].text) == (TranscriptState.FAILED, "[ovoz tushunilmadi: shovqin]")


async def test_a_transcript_for_a_closed_burst_is_ignored(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1, kind=UpdateKind.VOICE, duration_s=8))
    [item] = (await _wait(dev_deps, fakes, WS.hold_max_s)).closed  # the STT job never answered
    assert item.close_reason is CloseReason.HOLD_EXPIRED
    assert not await service.record_transcript(dev_deps, update_id=1, text="kech qoldi", ok=True)
    [row] = await _bursts(dev_deps)
    assert row.status == BurstStatus.CLOSED


async def test_the_self_test_closes_at_once_and_leaves_the_owners_burst_alone(dev_deps: DevDeps) -> None:
    await _send(dev_deps, make_media_update(1))
    [item] = (await _send(dev_deps, make_update(2, text="selftest", synthetic=True))).closed
    assert (_ids(item), item.close_reason, item.synthetic) == ([2], CloseReason.SYNTHETIC, True)
    assert [row.status for row in await _bursts(dev_deps)] == [BurstStatus.OPEN, BurstStatus.CLOSED]


# --- recovery (SPEC §2.7) -------------------------------------------------------------------------------------------
async def test_a_restart_closes_a_burst_past_its_window_at_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    await fakes.clock.advance(WS.after_media_s + 30)  # the bot was down when the window ended
    await dev_deps.db.dispose()  # the next query is a new process's engine on the same file
    [item] = (await service.recover_bursts(dev_deps)).closed
    assert (_ids(item), item.close_reason) == ([1], CloseReason.QUIET)


async def test_a_restart_rearms_an_open_burst_at_its_closes_at(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    armed = await service.next_closes_at(dev_deps)
    await fakes.clock.advance(3)
    await dev_deps.db.dispose()
    assert not await service.recover_bursts(dev_deps)
    assert await service.next_closes_at(dev_deps) == armed
    [item] = (await _wait(dev_deps, fakes, WS.after_media_s - 3)).closed
    assert _ids(item) == [1]


async def test_a_restart_reattaches_received_updates_in_update_id_order(dev_deps: DevDeps, fakes: Fakes) -> None:
    for update_id, text in ((1, "login sahifasini tuzat"), (2, "registratsiyani ham")):
        await inbox.accept_update(dev_deps, origin_of(_text(update_id, text)), _text(update_id, text))
        await fakes.clock.advance(1)
    await fakes.clock.advance(4)
    late = _text(3, "HRMentor'da testlarni yurgiz")
    await inbox.accept_update(dev_deps, origin_of(late), late)  # the process died before attaching any of them
    await dev_deps.db.dispose()

    events = await service.recover_bursts(dev_deps)
    [first] = events.closed  # 1 and 2 were one burst, which had closed before 3 arrived
    assert (_ids(first), first.close_reason) == ([1, 2], CloseReason.QUIET)
    assert [row.status for row in await _bursts(dev_deps)] == [BurstStatus.CLOSED, BurstStatus.OPEN]


async def test_messages_held_while_the_mac_slept_stay_apart_by_when_the_owner_sent_them(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    """T36 BUG-1: Telegram hands over what it held in one batch; the owner's send times keep his messages apart."""
    asleep = fakes.clock.now()
    backlog = (
        make_update(1, text="BonviHR'da login sahifasini tuzat", sent_at=asleep + timedelta(minutes=6)),
        make_update(2, text="HRMentor'da shu xato chiqyapti:", sent_at=asleep + timedelta(minutes=9)),
        replace(make_media_update(3), sent_at=asleep + timedelta(minutes=9, seconds=4)),  # its screenshot
        make_update(4, text="Markaz'da testlarini yoz", sent_at=asleep + timedelta(minutes=12)),
    )
    await fakes.clock.advance(20 * 60)  # the Mac wakes
    for update in backlog:
        await inbox.accept_update(dev_deps, origin_of(update), update)
    closed = (await service.attach_received_updates(dev_deps)).closed
    assert [(_ids(item), item.close_reason) for item in closed] == [
        ([1], CloseReason.QUIET),
        ([2, 3], CloseReason.QUIET),
    ]
    [last] = (await _wait(dev_deps, fakes, WS.after_text_s)).closed
    assert (_ids(last), last.close_reason) == ([4], CloseReason.QUIET)


async def test_a_restart_lists_voice_parts_whose_transcript_never_came(dev_deps: DevDeps) -> None:
    await _send(dev_deps, make_media_update(1, kind=UpdateKind.VOICE, duration_s=20))
    await dev_deps.db.dispose()
    events = await service.recover_bursts(dev_deps)
    assert ([part.update_id for part in events.transcribe], events.closed) == ([1], ())


async def test_an_unreadable_burst_is_cancelled_and_never_stalls_intake(dev_deps: DevDeps, fakes: Fakes) -> None:
    now = fakes.clock.now()
    async with dev_deps.db.uow() as s:
        await store.add_burst(
            s,
            bot=dev_deps.bot.value,
            chat_id=make_update().chat_id,
            status=BurstStatus.OPEN.value,
            opened_at=now,
            last_part_at=now,
            closes_at=now,
            input_json={"from": "an older release"},
        )
    await _send(dev_deps, _text(1, "salom"))
    assert not await service.close_due_bursts(dev_deps)
    assert [row.status for row in await _bursts(dev_deps)] == [BurstStatus.CANCELLED, BurstStatus.OPEN]


# --- the loop -------------------------------------------------------------------------------------------------------
async def test_the_loop_closes_on_virtual_time_and_wakes_for_each_update(dev_db: Database, paths: Paths) -> None:
    clock = _ObservedClock()
    deps = make_dev_deps(db=dev_db, paths=paths, fakes=Fakes(clock=clock))
    wake, notified = asyncio.Event(), asyncio.Event()
    seen: list[BurstEvents] = []

    def notify(events: BurstEvents) -> None:
        seen.append(events)
        notified.set()

    async with asyncio.TaskGroup() as group:
        loop = group.create_task(service.run_bursts(deps, wake=wake, notify=notify))
        await inbox.accept_update(deps, origin_of(make_media_update(1)), make_media_update(1))
        wake.set()
        await _wait_for(clock.sleeping)  # asleep until the photo's window ends
        await clock.advance(3)

        text = _text(2, "shu xatoni tuzat")
        await inbox.accept_update(deps, origin_of(text), text)
        wake.set()  # the text moves the close earlier: 3 + 2 s, not 10 s
        await _wait_for(clock.sleeping)
        await clock.advance(WS.after_text_s - 0.1)
        assert seen == []
        await clock.advance(0.1)
        await _wait_for(notified)
        loop.cancel()
    [events] = seen
    [item] = events.closed
    assert (_ids(item), item.closed_at - item.opened_at) == ([1, 2], timedelta(seconds=3 + WS.after_text_s))


# --- the router side ------------------------------------------------------------------------------------------------
async def test_a_closed_burst_is_handed_on_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, make_media_update(1))
    [item] = (await _wait(dev_deps, fakes, WS.after_media_s)).closed
    assert await service.list_closed_bursts(dev_deps, limit=10) == (item,)

    async with dev_deps.db.uow() as s:
        assert await service.mark_burst_routed(s, bot=dev_deps.bot, burst_id=item.burst_id, route_id=17)
    async with dev_deps.db.uow() as s:
        assert not await service.mark_burst_merged(s, bot=dev_deps.bot, burst_id=item.burst_id, route_id=18)
        assert not await service.mark_burst_cancelled(s, bot=dev_deps.bot, burst_id=item.burst_id)
    assert await service.list_closed_bursts(dev_deps, limit=10) == ()
    [row] = await _bursts(dev_deps)
    assert (row.status, row.route_id) == (BurstStatus.ROUTED, 17)


async def test_a_follow_up_burst_can_be_marked_merged(dev_deps: DevDeps) -> None:
    [stop] = (await _send(dev_deps, _text(1, "to'xta"))).closed
    async with dev_deps.db.uow() as s:
        assert await service.mark_burst_cancelled(s, bot=dev_deps.bot, burst_id=stop.burst_id)
    [selftest] = (await _send(dev_deps, make_update(2, text="selftest", synthetic=True))).closed
    async with dev_deps.db.uow() as s:
        assert await service.mark_burst_merged(s, bot=dev_deps.bot, burst_id=selftest.burst_id, route_id=None)
    assert [row.status for row in await _bursts(dev_deps)] == [BurstStatus.CANCELLED, BurstStatus.MERGED]


# --- retention (SPEC §4.2) ------------------------------------------------------------------------------------------
async def test_retention_deletes_finished_bursts_after_30_days_and_never_unrouted_ones(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    start = fakes.clock.now()

    async def closed_burst(update_id: int, *, days_ago: float, routed: bool) -> int:
        fakes.clock.set(start - timedelta(days=days_ago))
        await _send(dev_deps, make_media_update(update_id))
        [item] = (await _wait(dev_deps, fakes, WS.after_media_s)).closed
        if routed:
            async with dev_deps.db.uow() as s:
                await service.mark_burst_routed(s, bot=dev_deps.bot, burst_id=item.burst_id, route_id=None)
        return item.burst_id

    old = await closed_burst(1, days_ago=31, routed=True)
    unrouted = await closed_burst(2, days_ago=31, routed=False)
    recent = await closed_burst(3, days_ago=10, routed=True)
    fakes.clock.set(start)

    assert await service.apply_retention(dev_deps) == 1
    assert [row.id for row in await _bursts(dev_deps)] == [unrouted, recent]
    async with dev_deps.db.read() as s:
        by_update = {row.update_id: row for row in await inbox_store.list_inbound_updates(s, dev_deps.bot.value)}
    assert (by_update[1].burst_id, by_update[2].burst_id) == (None, unrouted)  # ON DELETE SET NULL
    assert old not in {row.id for row in await _bursts(dev_deps)}
    assert await service.apply_retention(dev_deps) == 0


async def test_an_update_cannot_point_at_a_burst_that_does_not_exist(dev_deps: DevDeps) -> None:
    update = _text(1, "salom")
    await inbox.accept_update(dev_deps, origin_of(update), update)
    with pytest.raises(IntegrityError, match="FOREIGN KEY"):
        async with dev_deps.db.uow() as s:
            await inbox.mark_updates_attached(s, bot=dev_deps.bot, update_ids=[1], burst_id=4040)


# --- get_turn_input / cancel_burst (T34: the router's own paths) ------------------------------------------------
async def test_a_closed_bursts_input_can_be_read_again_and_cancelled_once(dev_deps: DevDeps) -> None:
    from tests.factories import make_closed_burst, make_part

    async with dev_deps.db.uow() as s:
        closed = await make_closed_burst(s, make_part(text="holat?"))
    again = await service.get_turn_input(dev_deps, closed.burst_id)
    assert again is not None
    assert (again.burst_id, [part.text for part in again.parts]) == (closed.burst_id, ["holat?"])
    assert await service.cancel_burst(dev_deps, closed.burst_id)
    assert not await service.cancel_burst(dev_deps, closed.burst_id)  # handled once
    assert await service.list_closed_bursts(dev_deps, limit=10) == ()
    assert await service.get_turn_input(dev_deps, closed.burst_id) is not None  # still readable after it
    assert await service.get_turn_input(dev_deps, 12345) is None
