"""Bursts in a forum dev group (D4 revised 2026-10-26; SPEC §3.2.16): a burst never holds parts of two topics.

The chat keeps one assembling burst at a time (its unique index is frozen in `0001`), so a part written in another
topic closes the assembling one first (`CloseReason.THREAD`) or, while that one waits for a transcript, waits itself
(`Into.WAIT`), still `received`, keeping its topic's order. An interrupt word from another topic stands alone and cuts
nothing short."""

from dataclasses import replace
from typing import Final

from tests.factories import Fakes, make_part, make_update, make_window_settings, origin_of
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.burst import domain, service, store
from yordamchi.shared.burst.domain import BurstEvents, CloseReason, Into, PartRole
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import IncomingUpdate, UpdateKind

WS: Final = make_window_settings()
TOPIC_A: Final = 501
TOPIC_B: Final = 502


def _in(update: IncomingUpdate, thread_id: int | None) -> IncomingUpdate:
    """The same message, written in forum topic `thread_id` (None: General)."""
    if thread_id is None:
        return replace(update, is_forum=True)
    return replace(update, message_thread_id=thread_id, is_topic_message=True, is_forum=True)


async def _send(deps: DevDeps, update: IncomingUpdate) -> BurstEvents:
    await inbox.accept_update(deps, origin_of(update), update)
    return await service.attach_received_updates(deps)


# --- domain ---------------------------------------------------------------------------------------------------------
def test_a_part_from_another_topic_closes_the_assembling_burst_and_opens_its_own() -> None:
    assembling = (make_part(1, at_s=0, text="README'ni yangila"),)
    placement = domain.place(make_part(2, at_s=0.5, text="CHANGELOG'ni yangila"), assembling, WS, other_thread=True)
    assert (placement.into, placement.close_current) == (Into.NEW, CloseReason.THREAD)


def test_a_part_from_another_topic_waits_while_the_burst_waits_for_a_transcript() -> None:
    assembling = (make_part(1, at_s=0, kind=UpdateKind.VOICE, duration_s=4),)  # still being transcribed
    placement = domain.place(make_part(2, at_s=1, text="CHANGELOG'ni yangila"), assembling, WS, other_thread=True)
    assert (placement.into, placement.close_current) == (Into.WAIT, None)


def test_an_interrupt_from_another_topic_stands_alone_and_cuts_nothing_short() -> None:
    assembling = (make_part(1, at_s=0, text="README'ni yangila"),)
    placement = domain.place(make_part(2, at_s=0.5, text="to'xta"), assembling, WS, other_thread=True)
    assert (placement.into, placement.close_current, placement.close_alone) == (
        Into.ALONE,
        None,
        CloseReason.INTERRUPT,
    )


def test_a_kut_from_another_topic_holds_there() -> None:
    assembling = (make_part(1, at_s=0, text="README'ni yangila"),)
    placement = domain.place(make_part(2, at_s=0.5, text="kut"), assembling, WS, other_thread=True)
    assert (placement.into, placement.close_current, placement.part.role) == (
        Into.NEW,
        CloseReason.THREAD,
        PartRole.HOLD,
    )


def test_the_turn_input_names_its_topic() -> None:
    parts = (make_part(1, at_s=0, text="README'ni yangila"),)
    item = domain.turn_input(
        7, -100, parts, opened_at=parts[0].at, closed_at=parts[0].at, reason=CloseReason.QUIET, thread_id=TOPIC_A
    )
    assert item.thread_id == TOPIC_A


# --- service --------------------------------------------------------------------------------------------------------
async def test_two_topics_written_together_become_two_bursts(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _send(dev_deps, _in(make_update(1, text="README'ni yangila"), TOPIC_A))
    events = await _send(dev_deps, _in(make_update(2, text="CHANGELOG'ni yangila"), TOPIC_B))
    [closed] = events.closed
    assert (closed.thread_id, [part.update_id for part in closed.parts]) == (TOPIC_A, [1])
    assert closed.close_reason is CloseReason.THREAD
    await fakes.clock.advance(WS.after_text_s + 1)
    [second] = (await service.close_due_bursts(dev_deps)).closed
    assert (second.thread_id, [part.update_id for part in second.parts]) == (TOPIC_B, [2])


async def test_general_and_a_topic_never_merge(dev_deps: DevDeps) -> None:
    await _send(dev_deps, _in(make_update(1, text="BonviHR qanday"), None))
    events = await _send(dev_deps, _in(make_update(2, text="testlarini ham yoz"), TOPIC_A))
    assert [item.thread_id for item in events.closed] == [None]


async def test_a_topic_waits_behind_a_voice_note_of_another_and_keeps_its_order(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    voice = _in(make_update(1, text=None, kind=UpdateKind.VOICE), TOPIC_A)
    await _send(dev_deps, voice)
    await _send(dev_deps, _in(make_update(2, text="CHANGELOG'ni"), TOPIC_B))
    await _send(dev_deps, _in(make_update(3, text="yangila"), TOPIC_B))
    async with dev_deps.db.read() as s:
        waiting = await inbox_store.list_updates_with_status(s, "dev", "received", limit=10)
    assert [row.update_id for row in waiting] == [2, 3]  # nothing of topic B jumped the queue

    await service.record_transcript(dev_deps, update_id=1, text="README'ni yangila", ok=True)
    events = await service.attach_received_updates(dev_deps)
    [closed] = events.closed  # A's burst, closed by B's first part
    assert (closed.thread_id, closed.close_reason) == (TOPIC_A, CloseReason.THREAD)
    await fakes.clock.advance(WS.after_text_s + 1)
    [topic_b] = (await service.close_due_bursts(dev_deps)).closed
    assert (topic_b.thread_id, [part.update_id for part in topic_b.parts]) == (TOPIC_B, [2, 3])


async def test_the_burst_row_keeps_its_topic(dev_deps: DevDeps) -> None:
    await _send(dev_deps, _in(make_update(1, text="README'ni yangila"), TOPIC_A))
    async with dev_deps.db.read() as s:
        [row] = await store.list_bursts(s, "dev")
    assert row.thread_id == TOPIC_A
