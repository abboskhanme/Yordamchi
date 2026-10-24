"""Burst assembly (SPEC §3.2.1 step 4, §4.4, §2.7; UC-02, UC-03): received updates become one turn input per burst.

`run_bursts` is the loop body that `bots/<bot>/app.py` runs under `core.aio.forever` (the "burst timers" of SPEC §2.5).
1. On entry, `recover_bursts` (SPEC §2.7): received updates re-attach in `update_id` order, an assembling burst past
   its window closes at once, the others keep `closes_at`, and voice parts still waiting for a transcript are listed
   so their STT jobs start again.
2. Each pass, `attach_received_updates` takes the inbox's `received` rows in `update_id` order. Each joins its chat's
   assembling burst or opens one (`domain.place`). An interrupt word closes the assembling burst at once and stands
   alone, never merged; a "kut" never does: it holds the window for the next part, with or without a burst
   assembling, and gets the `ui.hold_reaction` so the owner sees it waits.
3. `close_due_bursts` closes every assembling burst whose window has passed.
4. `notify(events)` hands closed bursts on to pre-parse and routing, and new voice parts to `shared/voice`.
5. The loop sleeps on `deps.clock` until the earliest `closes_at`, or until `wake` is set (an update was accepted, a
   transcript was recorded).

Each attach and each close is one short unit of work. The row is the truth and the timer only a wake-up: a stale
wake-up does nothing, and a restart re-arms from `closes_at`. Nothing here talks to the owner but that one reaction,
so a lone photo proceeds after its window without a question (UC-02). A burst that held nothing but a "kut" when it
closed has no turn input: it ends `cancelled`, and nothing is routed.
"""

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Final

from yordamchi.core import redact
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.clock import Clock
from yordamchi.shared.burst import domain, store
from yordamchi.shared.burst.domain import (
    BurstEvents,
    BurstPart,
    BurstStatus,
    CloseReason,
    Into,
    PartRole,
    TranscriptState,
    TurnInput,
    WindowSettings,
)
from yordamchi.shared.burst.models import Burst
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox.domain import StoredUpdate
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

log = get_log(__name__)

ATTACH_BATCH: Final = 100  # received updates read per query; the pass repeats until the queue is empty
_ASSEMBLING: Final = sorted(status.value for status in domain.ASSEMBLING)

type Notify = Callable[[BurstEvents], None]


class _AttachedElsewhere(Exception):  # rolls one attach back; never leaves this module
    """The update was no longer `received` when this pass tried to attach it."""


# --- rows ---------------------------------------------------------------------------------------------------------
def _parts_of(row: Burst) -> tuple[BurstPart, ...]:
    return domain.parts_from_json(row.input_json)


def _turn_input(row: Burst, parts: Sequence[BurstPart]) -> TurnInput:
    if row.closed_at is None or row.close_reason is None:
        msg = f"burst {row.id} is not closed"
        raise ValueError(msg)
    return domain.turn_input(
        row.id,
        row.chat_id,
        parts,
        opened_at=row.opened_at,
        closed_at=row.closed_at,
        reason=CloseReason(row.close_reason),
    )


async def _store_window(s: Session, row: Burst, parts: Sequence[BurstPart], ws: WindowSettings) -> None:
    window = domain.window(parts, ws)
    await store.set_burst_parts(
        s,
        row,
        status=window.status.value,
        last_part_at=max(part.at for part in parts),
        closes_at=window.closes_at,
        input_json=domain.parts_json(parts, redact.scrubbed),
    )


async def _open(s: Session, deps: SharedDeps, chat_id: int, part: BurstPart, ws: WindowSettings) -> Burst:
    window = domain.window((part,), ws)
    return await store.add_burst(
        s,
        bot=deps.bot.value,
        chat_id=chat_id,
        status=window.status.value,
        opened_at=part.at,
        last_part_at=part.at,
        closes_at=window.closes_at,
        input_json=domain.parts_json((part,), redact.scrubbed),
    )


async def _add_closed(
    s: Session, deps: SharedDeps, chat_id: int, part: BurstPart, *, reason: CloseReason, at: datetime
) -> TurnInput:
    """A one-part burst that closes as it opens: an interrupt word or the self-test."""
    row = await store.add_burst(
        s,
        bot=deps.bot.value,
        chat_id=chat_id,
        status=BurstStatus.CLOSED.value,
        opened_at=part.at,
        last_part_at=part.at,
        closes_at=part.at,
        closed_at=at,
        close_reason=reason.value,
        input_json=domain.parts_json((part,), redact.scrubbed),
    )
    return _turn_input(row, (part,))


async def _close(
    s: Session, row: Burst, parts: Sequence[BurstPart], *, reason: CloseReason, at: datetime
) -> TurnInput | None:
    """The burst's turn input, `closed` for pre-parse and routing. A burst holding nothing but a "kut" (the owner said
    wait, then sent nothing in time) has none: it ends `cancelled` and None comes back."""
    content = domain.has_content(parts)
    await store.set_burst_closed(
        s,
        row,
        status=(BurstStatus.CLOSED if content else BurstStatus.CANCELLED).value,
        closed_at=at,
        close_reason=reason.value,
        input_json=domain.parts_json(parts, redact.scrubbed),
    )
    if not content:
        log.info("burst_hold_lapsed", burst_id=row.id, reason=reason.value)
        return None
    return _turn_input(row, parts)


async def _acknowledge_hold(
    s: Session, deps: SharedDeps, chat_id: int, part: BurstPart, *, emoji: str, at: datetime
) -> None:
    """ "kut" is understood as "wait, more is coming": its own reaction (`ui.hold_reaction`), after the "seen" one. The
    bot is not writing anything while it waits, so the "typing…" its intake started stops."""
    if part.message_id is None or part.synthetic:
        return
    await outbox.enqueue_reaction(
        s, bot=deps.bot, chat_id=chat_id, message_id=part.message_id, emoji=emoji, ref=f"hold-{part.update_id}", at=at
    )
    await outbox.stop_typing(s, bot=deps.bot, chat_id=chat_id)


async def _readable_parts(s: Session, row: Burst) -> tuple[BurstPart, ...]:
    """The row's parts. A row with none readable (a corrupt or foreign `input_json`) is cancelled, so one bad row
    never stalls the chat's intake; it returns no parts."""
    parts = _parts_of(row)
    if not parts:
        await store.set_burst_status(
            s,
            bot=row.bot,
            burst_id=row.id,
            from_status=row.status,
            to_status=BurstStatus.CANCELLED.value,
            route_id=None,
        )
        log.warning("burst_unreadable", burst_id=row.id)
    return parts


def _log_closed(deps: SharedDeps, closed: Sequence[TurnInput]) -> None:
    for item in closed:
        log.info(
            "burst_closed",
            bot=deps.bot.value,
            burst_id=item.burst_id,
            reason=item.close_reason.value,
            parts=len(item.parts),
            duration_ms=round((item.closed_at - item.opened_at).total_seconds() * 1000),
        )


# --- intake -------------------------------------------------------------------------------------------------------
async def _attach(deps: SharedDeps, update: StoredUpdate, ws: WindowSettings, *, hold_reaction: str) -> BurstEvents:
    """One received update into its burst, in one unit of work with `received → attached` (and a "kut"'s reaction)."""
    part = domain.part_from_update(update)
    now = deps.clock.now()
    closed: list[TurnInput] = []
    try:
        async with deps.db.uow() as s:
            row = await store.find_burst_in(s, bot=deps.bot.value, chat_id=update.chat_id, statuses=_ASSEMBLING)
            current = () if row is None else await _readable_parts(s, row)
            if not current:
                row = None
            placement = domain.place(part, current, ws)
            if row is not None and placement.close_current is not None:
                ended = await _close(s, row, current, reason=placement.close_current, at=now)
                closed += [ended] if ended is not None else []
            if placement.into is Into.ALONE:
                reason = placement.close_alone or CloseReason.INTERRUPT
                alone = await _add_closed(s, deps, update.chat_id, placement.part, reason=reason, at=now)
                closed.append(alone)
                burst_id = alone.burst_id
            elif placement.into is Into.NEW or row is None:  # CURRENT and EDIT come only with an assembling row
                burst_id = (await _open(s, deps, update.chat_id, placement.part, ws)).id
            else:
                await _store_window(s, row, domain.joined(current, placement), ws)
                burst_id = row.id
            if placement.part.role is PartRole.HOLD:
                await _acknowledge_hold(s, deps, update.chat_id, placement.part, emoji=hold_reaction, at=now)
            attached = await inbox.mark_updates_attached(
                s, bot=deps.bot, update_ids=[update.update_id], burst_id=burst_id
            )
            if attached == 0:
                raise _AttachedElsewhere
    except _AttachedElsewhere:
        log.info("update_attached_elsewhere", bot=deps.bot.value, update_id=update.update_id)
        return BurstEvents()
    log.info(
        "burst_part_attached",
        bot=deps.bot.value,
        burst_id=burst_id,
        update_id=update.update_id,
        kind=update.kind.value,
        into=placement.into.value,
        role=placement.part.role.value,
    )
    _log_closed(deps, closed)
    waiting = placement.into is not Into.ALONE and placement.part.transcript is TranscriptState.PENDING
    return BurstEvents(closed=domain.in_route_order(closed), transcribe=(placement.part,) if waiting else ())


async def attach_received_updates(deps: SharedDeps, *, batch: int = ATTACH_BATCH) -> BurstEvents:
    """Attach every `received` inbox update, oldest `update_id` first: the live path and the restart path (SPEC §2.7).

    Placement is decided by each update's `received_at`, so an update that arrived after its chat's burst would have
    closed opens a new burst even if the timer never fired (a restart, a slow pass). Returns the bursts that closed on
    the way (interrupts, and the bursts they cut short) and the voice parts whose transcript must be started.
    """
    snap = await settings.snapshot(deps)
    ws, hold_reaction = domain.window_settings(snap), snap[defaults.UI_HOLD_REACTION]
    events = BurstEvents()
    while True:
        updates = await inbox.list_received_updates(deps, limit=batch)
        for update in updates:
            events += await _attach(deps, update, ws, hold_reaction=hold_reaction)
        if len(updates) < batch:
            return events


# --- transcripts --------------------------------------------------------------------------------------------------
async def record_transcript(deps: SharedDeps, *, update_id: int, text: str, ok: bool) -> BurstEvents:
    """`shared/voice` reports the STT result of voice part `update_id`: `text` is the transcript, or (`ok=False`) its
    "not understood" marker, which stays in the burst (UC-05 F). The hold is released and the window recomputed; a
    short transcript that is an interrupt word ("to'xta") leaves the burst at once (SPEC §3.2.5). The caller then sets
    the loop's `wake`. A transcript for a burst that is no longer assembling is ignored and logged. A spoken "kut"
    becomes a hold, with its reaction.
    """
    snap = await settings.snapshot(deps)
    ws = domain.window_settings(snap)
    now = deps.clock.now()
    closed: list[TurnInput] = []
    async with deps.db.uow() as s:
        rows = await store.list_bursts_in(s, bot=deps.bot.value, statuses=_ASSEMBLING)
        assembling = [(row, _parts_of(row)) for row in rows]
        found = next(((row, parts) for row, parts in assembling if domain.waits_for(parts, update_id)), None)
        if found is None:
            log.warning("transcript_late", bot=deps.bot.value, update_id=update_id)
            return BurstEvents()
        row, parts = found
        outcome = domain.apply_transcript(parts, update_id, text=text, ok=ok, at=now)
        if outcome.alone is not None:
            reason = CloseReason.INTERRUPT
            closed.append(await _add_closed(s, deps, row.chat_id, outcome.alone, reason=reason, at=now))
        if outcome.close_now is not None:
            ended = await _close(s, row, outcome.parts, reason=outcome.close_now, at=now)
            closed += [ended] if ended is not None else []
        else:
            await _store_window(s, row, outcome.parts, ws)
        held = next((part for part in outcome.parts if part.update_id == update_id), None)
        if held is not None and held.role is PartRole.HOLD:  # a spoken "kut"
            await _acknowledge_hold(s, deps, row.chat_id, held, emoji=snap[defaults.UI_HOLD_REACTION], at=now)
    log.info("transcript_recorded", bot=deps.bot.value, burst_id=row.id, update_id=update_id, ok=ok)
    _log_closed(deps, closed)
    return BurstEvents(closed=domain.in_route_order(closed))


# --- timers -------------------------------------------------------------------------------------------------------
async def close_due_bursts(deps: SharedDeps) -> BurstEvents:
    """Close every assembling burst whose window has passed. The window is recomputed with this pass's settings; one
    that a changed setting moved later is re-armed instead (a setting applies from the next pass)."""
    ws = domain.window_settings(await settings.snapshot(deps))
    now = deps.clock.now()
    closed: list[TurnInput] = []
    async with deps.db.uow() as s:
        for row in await store.list_bursts_due(s, bot=deps.bot.value, statuses=_ASSEMBLING, now=now):
            parts = await _readable_parts(s, row)
            if not parts:
                continue
            window = domain.window(parts, ws)
            if window.is_due(now):
                ended = await _close(s, row, parts, reason=window.reason, at=now)
                closed += [ended] if ended is not None else []
            else:
                await _store_window(s, row, parts, ws)
    _log_closed(deps, closed)
    return BurstEvents(closed=domain.in_route_order(closed))


async def next_closes_at(deps: SharedDeps) -> datetime | None:
    """The earliest `closes_at` of this bot's assembling bursts, or None when nothing is assembling."""
    async with deps.db.read() as s:
        return await store.get_next_closes_at(s, bot=deps.bot.value, statuses=_ASSEMBLING)


async def recover_bursts(deps: SharedDeps) -> BurstEvents:
    """The startup pass (SPEC §2.7). Received updates re-attach in `update_id` order; an assembling burst past its
    window closes at once; the rest keep their `closes_at`, which the loop re-arms; every voice part still waiting
    for its transcript is listed, because the STT job that would have answered died with the old process."""
    events = await attach_received_updates(deps)
    events += await close_due_bursts(deps)
    async with deps.db.read() as s:
        rows = await store.list_bursts_in(s, bot=deps.bot.value, statuses=_ASSEMBLING)
    pending = tuple(part for row in rows for part in _parts_of(row) if part.transcript is TranscriptState.PENDING)
    log.info("bursts_recovered", bot=deps.bot.value, closed=len(events.closed), transcripts=len(pending))
    return BurstEvents(closed=events.closed, transcribe=pending)


async def _sleep_until(clock: Clock, at: datetime | None, wake: asyncio.Event) -> None:
    """Sleep on `clock` until `at` (no limit when None), or until `wake` is set, whichever comes first."""
    if wake.is_set():
        return
    if at is None:
        await wake.wait()
        return
    seconds = (at - clock.now()).total_seconds()
    if seconds <= 0:
        return
    async with asyncio.TaskGroup() as group:
        sleeping = group.create_task(clock.sleep(seconds))
        waking = group.create_task(wake.wait())
        await asyncio.wait({sleeping, waking}, return_when=asyncio.FIRST_COMPLETED)
        sleeping.cancel()
        waking.cancel()


async def run_bursts(deps: SharedDeps, *, wake: asyncio.Event, notify: Notify) -> None:
    """The burst loop: recover, then attach, close and sleep until the next `closes_at` or `wake`, forever.

    Set `wake` after every accepted update and every recorded transcript. `notify` gets each pass's events: closed
    bursts for the router (interrupts first), and voice parts whose STT job must start. It must not block.
    """
    events = await recover_bursts(deps)
    while True:
        if events:
            notify(events)
        await _sleep_until(deps.clock, await next_closes_at(deps), wake)
        wake.clear()
        events = await attach_received_updates(deps)
        events += await close_due_bursts(deps)


# --- the router side ----------------------------------------------------------------------------------------------
async def list_closed_bursts(deps: SharedDeps, *, limit: int) -> tuple[TurnInput, ...]:
    """Bursts closed and not yet routed, interrupts first, then oldest first: the live path and the restart path
    (SPEC §2.7 "route now")."""
    async with deps.db.read() as s:
        rows = await store.list_bursts_with_status(
            s,
            bot=deps.bot.value,
            status=BurstStatus.CLOSED.value,
            first_reason=CloseReason.INTERRUPT.value,
            limit=limit,
        )
    inputs: list[TurnInput] = []
    for row in rows:
        parts = _parts_of(row)
        if parts:
            inputs.append(_turn_input(row, parts))
    return domain.in_route_order(inputs)


async def get_turn_input(deps: SharedDeps, burst_id: int) -> TurnInput | None:
    """A closed burst's input, whatever happened to it since: routing re-delivers it once a clarification picked its
    project, and `dev/questions` reads the answer it held (T20, T29). None if it is gone or not closed yet."""
    async with deps.db.read() as s:
        row = await store.get_burst(s, bot=deps.bot.value, burst_id=burst_id)
    if row is None or row.closed_at is None or row.close_reason is None:
        return None
    parts = _parts_of(row)
    return _turn_input(row, parts) if parts else None


async def cancel_burst(deps: SharedDeps, burst_id: int) -> bool:
    """`mark_burst_cancelled` in its own unit of work: the bot handled the burst itself (an interrupt, a chat command
    such as "holat?"). False if another pass took it first."""
    async with deps.db.uow() as s:
        cancelled = await mark_burst_cancelled(s, bot=deps.bot, burst_id=burst_id)
    if cancelled:
        log.info("burst_cancelled", bot=deps.bot.value, burst_id=burst_id)
    return cancelled


async def _finish(s: Session, bot: BotName, burst_id: int, to_status: BurstStatus, route_id: int | None) -> bool:
    moved = await store.set_burst_status(
        s,
        bot=bot.value,
        burst_id=burst_id,
        from_status=BurstStatus.CLOSED.value,
        to_status=to_status.value,
        route_id=route_id,
    )
    return moved == 1


async def mark_burst_routed(s: Session, *, bot: BotName, burst_id: int, route_id: int | None) -> bool:
    """In the router's transaction: `closed` → `routed`. False if it was not `closed` (already handled)."""
    return await _finish(s, bot, burst_id, BurstStatus.ROUTED, route_id)


async def mark_burst_merged(s: Session, *, bot: BotName, burst_id: int, route_id: int | None) -> bool:
    """In the turn's transaction: `closed` → `merged`, delivered into a turn already working (UC-02 follow-up)."""
    return await _finish(s, bot, burst_id, BurstStatus.MERGED, route_id)


async def mark_burst_cancelled(s: Session, *, bot: BotName, burst_id: int) -> bool:
    """`closed` → `cancelled`: the burst needs no turn (an interrupt that was handled, the self-test)."""
    return await _finish(s, bot, burst_id, BurstStatus.CANCELLED, None)


# --- retention ----------------------------------------------------------------------------------------------------
async def apply_retention(deps: SharedDeps) -> int:
    """SPEC §4.2: routed, merged and cancelled bursts are deleted 30 days after they closed, in batches. An
    assembling or closed burst is never touched. `inbound_updates.burst_id` of a deleted burst becomes NULL."""
    cutoff = domain.row_cutoff(deps.clock.now())
    finished = sorted(status.value for status in domain.TERMINAL)
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_bursts_before(s, cutoff, statuses=finished, limit=limit)
    )
    log.info("burst_retention_applied", rows_deleted=deleted)
    return deleted
