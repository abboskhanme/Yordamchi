"""The durable outbound queue (SPEC §2.6, §2.7, §3.2.1 steps 3/8/9, §4.6; NFR-REL-06, NFR-CAP-02/03, UC-11, UC-12,
NFR-SEC-03). Every message either bot sends goes through here.

Two halves:
- **Enqueue** (`s` first: inside the caller's unit of work, DB only, A3). A feature writes what it wants sent in the
  same transaction as the change that caused it (the inbox ack with its update, a decision question with its
  `decisions` row), so a crash can never keep one and lose the other. Texts are split here, by `domain`.
- **Send** (`deps` first). `run_sender(deps)` runs for the life of the process under `core.aio.forever`. Each pass
  (`send_due`) claims the rows that may go now, calls the Messenger outside any transaction, and records the outcome
  in a short unit of work of its own. Chats go in parallel, each chat in order. Every call is bounded twice: by the
  adapter's `timeout`, and by the sender's own `asyncio.timeout` on top of it.

Text is scrubbed twice (NFR-SEC-03): when the row is written, and again just before the call, so a key the vault
learned in between is masked as well. No call can carry a keyboard: the Messenger port has no such parameter, and a
payload holding any key its op does not allow is refused at enqueue and again before the call (D8, UC-12).

    async with deps.db.uow() as s:
        decision_id = await store.add_decision(s, …)
        await outbox.enqueue_text(s, bot=deps.bot, chat_id=group, text=S.DECISION_ASK(…), kind=Kind.DECISION,
                                  ref=f"decision-{decision_id}", at=now, max_parts=snap[defaults.OUTBOX_MAX_PARTS],
                                  about=About(project_id=project, decision_id=decision_id))
"""

import asyncio
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Final

from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import ErrorCode, ExternalError, RateLimited
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.messenger import ChatAction, OutboundFile, ParseMode, SentMessage
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.outbox import domain, store
from yordamchi.shared.outbox.domain import (
    COUNTED_OPS,
    DEFAULT_CONFIG,
    DEFAULT_PRIORITY,
    FILE_OPS,
    LOWEST_PRIORITY,
    MESSAGE_OPS,
    ORDERED_OPS,
    TERMINAL,
    About,
    Code,
    Delivery,
    Drop,
    Failure,
    GiveUp,
    Kind,
    Link,
    NotYet,
    Op,
    Park,
    Payload,
    PayloadInvalid,
    Queued,
    Ready,
    Resolved,
    SenderConfig,
    Status,
    TextPlan,
)
from yordamchi.shared.outbox.models import MessageLink, OutboxMessage

log = get_log(__name__)

NO_ABOUT: Final = About()
_MARKDOWN: Final = "text/markdown"


# --- enqueue: one row -----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _Row:
    """Everything one `outbox_messages` row needs besides its op, payload and key."""

    bot: BotName
    chat_id: int
    kind: Kind
    at: datetime
    priority: int | None = None
    coalesce_key: str | None = None
    next_attempt_at: datetime | None = None
    reply_to: int | None = None
    disable_notification: bool = False
    about: About = NO_ABOUT


async def _add(s: Session, row: _Row, *, op: Op, payload: Payload, key: str) -> int | None:
    """One pending row; None if `key` was queued before. A payload its op does not allow raises `PayloadInvalid`
    (a wiring bug) before anything is written."""
    if domain.kind_of_key(key) is not row.kind:
        msg = f"dedupe key {key!r} does not start with its kind {row.kind.value!r}"
        raise ValueError(msg)
    if op in MESSAGE_OPS:
        payload = replace(payload, question_id=row.about.question_id, decision_id=row.about.decision_id)
    return await store.add_outbox_message(
        s,
        bot=row.bot.value,
        chat_id=row.chat_id,
        op=op.value,
        kind=row.kind.value,
        payload=domain.payload_json(op, payload, redact.scrubbed),
        dedupe_key=key,
        coalesce_key=row.coalesce_key,
        priority=DEFAULT_PRIORITY[row.kind] if row.priority is None else row.priority,
        next_attempt_at=row.next_attempt_at or row.at,
        reply_to_message_id=row.reply_to,
        disable_notification=row.disable_notification,
        project_id=row.about.project_id,
        turn_id=row.about.turn_id,
        created_at=row.at,
    )


async def _add_plan(s: Session, row: _Row, plan: TextPlan, *, ref: str, first_part: int = 0) -> tuple[int, ...]:
    """The parts of one text in order, or its `.md` file. Only the first part replies to `row.reply_to`."""
    if plan.file is not None:
        caption = S.OUTBOX_TEXT_IN_FILE(summary=plan.file.summary)
        payload = Payload(content=plan.file.content, filename=plan.file.filename, mime=_MARKDOWN, caption=caption)
        row_id = await _add(
            s, row, op=Op.SEND_DOCUMENT, payload=payload, key=domain.dedupe_key(row.kind, ref, first_part)
        )
        return () if row_id is None else (row_id,)
    ids: list[int] = []
    for index, part in enumerate(plan.parts):
        part_row = row if index == 0 else replace(row, reply_to=None)
        key = domain.dedupe_key(row.kind, ref, first_part + index)
        row_id = await _add(s, part_row, op=Op.SEND_TEXT, payload=Payload(text=part), key=key)
        if row_id is None:
            break  # this ref was queued before: nothing new
        ids.append(row_id)
    return tuple(ids)


# --- enqueue: the public calls --------------------------------------------------------------------------------------
async def enqueue_text(
    s: Session,
    *,
    bot: BotName,
    chat_id: int,
    text: BotText,
    kind: Kind,
    ref: str,
    at: datetime,
    max_parts: int,
    priority: int | None = None,
    reply_to: int | None = None,
    about: About = NO_ABOUT,
    disable_notification: bool = False,
) -> tuple[int, ...]:
    """Queue one text, keyed `<kind>:<ref>:<part>`. Over 4,096 it is split at paragraphs and code blocks; more than
    `max_parts` (`outbox.max_parts`) parts → one `.md` file captioned with its first line. Returns the new row ids:
    () when this ref was queued before."""
    plan = domain.plan_text(text, max_parts=max_parts, file_stem=f"{kind.value}-{ref}")
    row = _Row(
        bot=bot,
        chat_id=chat_id,
        kind=kind,
        at=at,
        priority=priority,
        reply_to=reply_to,
        disable_notification=disable_notification,
        about=about,
    )
    return await _add_plan(s, row, plan, ref=ref)


async def enqueue_reply(
    deps: SharedDeps,
    *,
    chat_id: int,
    text: BotText,
    kind: Kind,
    ref: str,
    max_parts: int,
    reply_to: int | None = None,
    about: About = NO_ABOUT,
) -> tuple[int, ...]:
    """`enqueue_text` in its own unit of work, for an answer that has nothing else to commit with it (a `holat?`
    reply, a routing question, a stop that found nothing): the bot's chat router queues these (T34)."""
    async with deps.db.uow() as s:
        return await enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=text,
            kind=kind,
            ref=ref,
            at=deps.clock.now(),
            max_parts=max_parts,
            reply_to=reply_to,
            about=about,
        )


async def enqueue_text_after(
    s: Session,
    *,
    bot: BotName,
    chat_id: int,
    after: str,
    text: BotText | None,
    text_if_after_failed: BotText | None,
    kind: Kind,
    ref: str,
    at: datetime,
    priority: int | None = None,
) -> int | None:
    """One message sent once the row keyed `after` has finished: `text` if it was sent, `text_if_after_failed`
    otherwise; nothing if the chosen text is empty. The inbox uses it for the masked reply that follows the delete
    of a key message (UC-04). Each text is cut to one message."""
    payload = Payload(
        text=domain.fit_one(text) if text else None,
        after=after,
        text_if_after_failed=domain.fit_one(text_if_after_failed) if text_if_after_failed else None,
    )
    row = _Row(bot=bot, chat_id=chat_id, kind=kind, at=at, priority=priority)
    return await _add(s, row, op=Op.SEND_TEXT, payload=payload, key=domain.dedupe_key(kind, ref, 0))


async def enqueue_edit(
    s: Session,
    *,
    bot: BotName,
    chat_id: int,
    text: BotText,
    kind: Kind,
    ref: str,
    part: str,
    at: datetime,
    message_id: int | None = None,
    target: str | None = None,
    coalesce_key: str | None = None,
    every_s: float | None = None,
) -> int | None:
    """Replace the text of a message: one we know by `message_id` (the pinned heartbeat), or our own by the
    `target` dedupe key once it is sent. With a `coalesce_key`, older pending edits are superseded and the message
    is updated at most once per `every_s`."""
    last = None if coalesce_key is None else await store.get_last_sent_at(s, coalesce_key)
    row = _Row(
        bot=bot,
        chat_id=chat_id,
        kind=kind,
        at=at,
        coalesce_key=coalesce_key,
        next_attempt_at=domain.edit_due_at(at, last, every_s),
    )
    payload = Payload(text=domain.fit_one(text), message_id=message_id, target=target, every_s=every_s)
    row_id = await _add(s, row, op=Op.EDIT_TEXT, payload=payload, key=domain.dedupe_key(kind, ref, part))
    if row_id is not None and coalesce_key is not None:
        await store.supersede_pending(s, coalesce_key=coalesce_key, op=Op.EDIT_TEXT.value, keep_id=row_id)
    return row_id


async def show_progress(
    s: Session,
    *,
    bot: BotName,
    chat_id: int,
    turn_id: int,
    text: BotText,
    at: datetime,
    every_s: float,
    project_id: str | None = None,
) -> int | None:
    """The one live progress message of a turn (SPEC §3.2.1 step 8, UC-11): the first call posts it, later calls
    edit it, at most once per `every_s` (`outbox.progress_edit_s`), only the newest text kept. While the first post
    is still queued, a newer text simply replaces it. Returns the row that will carry `text`."""
    key = domain.progress_key(turn_id)
    ref = str(turn_id)
    send_key = domain.dedupe_key(Kind.PROGRESS, ref, 0)
    first = await store.find_outbox_message(s, send_key)
    if first is None:
        about = About(project_id=project_id, turn_id=turn_id)
        row = _Row(bot=bot, chat_id=chat_id, kind=Kind.PROGRESS, at=at, coalesce_key=key, about=about)
        return await _add(s, row, op=Op.SEND_TEXT, payload=Payload(text=domain.fit_one(text)), key=send_key)
    if first.status == Status.PENDING.value:
        payload = domain.payload_json(Op.SEND_TEXT, Payload(text=domain.fit_one(text)), redact.scrubbed)
        if await store.set_pending_payload(s, first.id, payload=payload):
            return first.id
    edits = await store.count_outbox_messages(s, coalesce_key=key, op=Op.EDIT_TEXT.value)
    return await enqueue_edit(
        s,
        bot=bot,
        chat_id=chat_id,
        text=text,
        kind=Kind.PROGRESS,
        ref=ref,
        part=f"edit{edits + 1}",
        at=at,
        target=send_key,
        coalesce_key=key,
        every_s=every_s,
    )


async def enqueue_delete(
    s: Session, *, bot: BotName, chat_id: int, message_id: int, ref: str, at: datetime, kind: Kind = Kind.ACK
) -> int | None:
    row = _Row(bot=bot, chat_id=chat_id, kind=kind, at=at, priority=domain.HIGHEST_PRIORITY)
    payload = Payload(message_id=message_id)
    return await _add(s, row, op=Op.DELETE, payload=payload, key=domain.op_key(kind, ref, Op.DELETE))


async def enqueue_reaction(
    s: Session, *, bot: BotName, chat_id: int, message_id: int, emoji: str, ref: str, at: datetime
) -> int | None:
    row = _Row(bot=bot, chat_id=chat_id, kind=Kind.ACK, at=at)
    payload = Payload(message_id=message_id, emoji=emoji)
    return await _add(s, row, op=Op.REACT, payload=payload, key=domain.op_key(Kind.ACK, ref, Op.REACT))


async def request_typing(s: Session, *, bot: BotName, chat_id: int, ref: str, at: datetime) -> int | None:
    """Show "typing…" in a chat for up to 60 s, repeated every 4 s; a hint sent < 4 s ago is not sent again. The
    newest request replaces an older pending one, and the next reply (or `stop_typing`) ends it. `ref` names what
    caused it (the inbox passes `update-<id>`), so a redelivered cause asks only once."""
    key = domain.typing_key(chat_id)
    last = await store.get_last_sent_at(s, key)
    row = _Row(
        bot=bot,
        chat_id=chat_id,
        kind=Kind.ACK,
        at=at,
        coalesce_key=key,
        next_attempt_at=domain.first_typing_at(at, last),
    )
    payload = Payload(action=ChatAction.TYPING.value, until=domain.typing_until(at))
    row_id = await _add(s, row, op=Op.CHAT_ACTION, payload=payload, key=domain.op_key(Kind.ACK, ref, Op.CHAT_ACTION))
    if row_id is not None:
        await store.supersede_pending(s, coalesce_key=key, op=Op.CHAT_ACTION.value, keep_id=row_id)
    return row_id


async def stop_typing(s: Session, *, bot: BotName, chat_id: int) -> int:
    """No more work for this chat: the typing hint stops repeating. Returns how many hints were cancelled."""
    return await store.cancel_pending(
        s, bot=bot.value, chat_id=chat_id, op=Op.CHAT_ACTION.value, code=Code.TYPING_ENDED.value
    )


async def enqueue_pin(
    s: Session,
    *,
    bot: BotName,
    chat_id: int,
    ref: str,
    at: datetime,
    kind: Kind = Kind.HEARTBEAT,
    message_id: int | None = None,
    target: str | None = None,
) -> int | None:
    """Pin a message we know by id, or our own by its `target` dedupe key once it is sent (the "Onlayn" heartbeat)."""
    row = _Row(bot=bot, chat_id=chat_id, kind=kind, at=at)
    payload = Payload(message_id=message_id, target=target)
    return await _add(s, row, op=Op.PIN, payload=payload, key=domain.op_key(kind, ref, Op.PIN))


async def enqueue_file(
    s: Session,
    *,
    bot: BotName,
    chat_id: int,
    op: Op,
    path: Path,
    kind: Kind,
    ref: str,
    at: datetime,
    max_parts: int,
    caption: BotText | None = None,
    filename: str | None = None,
    mime: str | None = None,
    duration_s: int | None = None,
    priority: int | None = None,
    reply_to: int | None = None,
    about: About = NO_ABOUT,
    disable_notification: bool = False,
) -> tuple[int, ...]:
    """Queue a photo, document, voice note or video made by another feature (it must still exist when sent). A
    caption over 1,024 keeps its first part and the rest follows as text."""
    if op not in FILE_OPS:
        msg = f"{op.value} does not send a file"
        raise ValueError(msg)
    row = _Row(
        bot=bot,
        chat_id=chat_id,
        kind=kind,
        at=at,
        priority=priority,
        reply_to=reply_to,
        disable_notification=disable_notification,
        about=about,
    )
    first_caption: str | None = None
    rest: TextPlan | None = None
    if caption:
        first_caption, rest = domain.plan_caption(caption, max_parts=max_parts, file_stem=f"{kind.value}-{ref}")
    payload = Payload(path=str(path), filename=filename, mime=mime, caption=first_caption, duration_s=duration_s)
    row_id = await _add(s, row, op=op, payload=payload, key=domain.dedupe_key(kind, ref, 0))
    if row_id is None:
        return ()
    more = () if rest is None else await _add_plan(s, replace(row, reply_to=None), rest, ref=ref, first_part=1)
    return (row_id, *more)


# --- reads ---------------------------------------------------------------------------------------------------------
def _link(row: MessageLink) -> Link:
    return Link(
        chat_id=row.chat_id,
        message_id=row.message_id,
        kind=Kind(row.kind),
        about=About(
            project_id=row.project_id, turn_id=row.turn_id, question_id=row.question_id, decision_id=row.decision_id
        ),
        created_at=row.created_at,
    )


async def find_message_link(deps: SharedDeps, *, chat_id: int, message_id: int) -> Link | None:
    """What a message we posted was about: the reply-to signal of routing (SPEC §3.2.6). None if it is not ours."""
    async with deps.db.read() as s:
        row = await store.get_message_link(s, bot=deps.bot.value, chat_id=chat_id, message_id=message_id)
    return None if row is None else _link(row)


async def get_delivery(deps: SharedDeps, dedupe_key: str) -> Delivery | None:
    """Where one queued message stands, and the id of the message it posted (to pin it or remember it)."""
    async with deps.db.read() as s:
        row = await store.find_outbox_message(s, dedupe_key)
        link = None if row is None else await store.find_link_of_outbox_message(s, row.id)
    if row is None:
        return None
    return Delivery(
        dedupe_key=row.dedupe_key,
        status=Status(row.status),
        attempts=row.attempts,
        last_error_code=row.last_error_code,
        message_id=None if link is None else link.message_id,
        sent_at=row.sent_at,
    )


# --- the sender ----------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class SenderState:
    """What one sender remembers between passes: its calls inside the rate windows, and the chats Telegram asked to
    wait. Parking is also written to the rows, so it survives a restart; this copy holds back rows queued since."""

    config: SenderConfig = DEFAULT_CONFIG
    parked: dict[int, datetime] = field(default_factory=dict)
    chat_sent: dict[int, list[datetime]] = field(default_factory=dict)
    all_sent: list[datetime] = field(default_factory=list)

    def may_call(self, chat_id: int, now: datetime) -> bool:
        chat_sent = self.chat_sent.get(chat_id, [])
        return domain.next_send_at(now=now, chat_id=chat_id, chat_sent=chat_sent, all_sent=self.all_sent) <= now

    def record_call(self, chat_id: int, at: datetime) -> None:
        self.chat_sent[chat_id] = [*domain.keep_recent(self.chat_sent.get(chat_id, []), at), at]
        self.all_sent = [*domain.keep_recent(self.all_sent, at), at]

    def is_parked(self, chat_id: int, now: datetime) -> bool:
        until = self.parked.get(chat_id)
        return until is not None and until > now


@dataclass(frozen=True, slots=True)
class _Attempt:
    called: bool  # a Messenger call was made
    done: bool  # the row is finished, so the next message of its chat may go


_SKIPPED: Final = _Attempt(called=False, done=False)


@dataclass(frozen=True, slots=True)
class _Outcome:
    message: SentMessage | None = None
    deleted: bool | None = None


def _queued(row: OutboxMessage) -> Queued | None:
    try:
        op, kind, status = Op(row.op), Kind(row.kind), Status(row.status)
    except ValueError:
        return None
    return Queued(
        id=row.id,
        chat_id=row.chat_id,
        op=op,
        kind=kind,
        priority=row.priority,
        status=status,
        next_attempt_at=row.next_attempt_at,
        attempts=row.attempts,
        dedupe_key=row.dedupe_key,
        coalesce_key=row.coalesce_key,
    )


async def recover_queue(deps: SharedDeps, state: SenderState | None = None) -> int:
    """SPEC §2.7 at sender start: rows left `sending` are resent (duplicate allowed, ≤ 2 %), `pending` ones simply
    go. `state` gets the calls of the last minute back, so a restart does not burst past Telegram's limits."""
    now = deps.clock.now()
    async with deps.db.uow() as s:
        resent = await store.reset_sending(s, bot=deps.bot.value, at=now)
    recent: list[tuple[int, datetime]] = []
    if state is not None:
        async with deps.db.read() as s:
            recent = await store.list_recent_sends(
                s, deps.bot.value, since=domain.recent_cutoff(now), ops=[op.value for op in COUNTED_OPS]
            )
        for chat_id, at in sorted(recent, key=lambda item: item[1]):
            state.record_call(chat_id, at)
    log.info("outbox_recovered", bot=deps.bot.value, resent=resent, recent_calls=len(recent))
    return resent


async def send_due(deps: SharedDeps, state: SenderState | None = None, *, max_priority: int = LOWEST_PRIORITY) -> int:
    """One pass: every row that may go now goes; chats in parallel, each chat in order. Returns the calls made."""
    state = SenderState() if state is None else state
    now = deps.clock.now()
    async with deps.db.read() as s:
        rows = await store.list_queued_messages(s, deps.bot.value, max_priority=max_priority, limit=state.config.batch)
    queued: list[Queued] = []
    for row in rows:
        item = _queued(row)
        if item is None:
            await _fail_unreadable(deps, row, now=now)
        else:
            queued.append(item)
    by_chat: dict[int, list[Queued]] = {}
    for item in domain.sendable(queued, now=now, parked=state.parked):
        by_chat.setdefault(item.chat_id, []).append(item)
    async with asyncio.TaskGroup() as group:
        passes = [group.create_task(_send_chat(deps, state, chat_rows)) for chat_rows in by_chat.values()]
    return sum(task.result() for task in passes)


async def run_sender(deps: SharedDeps, *, config: SenderConfig = DEFAULT_CONFIG) -> None:
    """The long-lived sender: `core.aio.forever("outbox", lambda: outbox.run_sender(deps))`. Each (re)start first
    recovers what a dead sender left behind; then it passes again at once after any call, else every
    `config.idle_poll_s`."""
    state = SenderState(config=config)
    await recover_queue(deps, state)
    while True:
        if await send_due(deps, state) == 0:
            await deps.clock.sleep(config.idle_poll_s)


async def flush_queue(deps: SharedDeps, *, max_priority: int, within_s: float, state: SenderState | None = None) -> int:
    """Shutdown (SPEC §2.6: priority 0–1 for up to 5 s): pass until nothing more goes or `within_s` of real time is
    up (launchd's ExitTimeOut is real time). What is left stays queued for the next start. Returns the calls made."""
    state = SenderState() if state is None else state
    calls = 0
    try:
        async with asyncio.timeout(within_s):
            while (made := await send_due(deps, state, max_priority=max_priority)) > 0:
                calls += made
    except TimeoutError:
        log.warning("outbox_flush_timed_out", bot=deps.bot.value, calls=calls)
    return calls


async def _send_chat(deps: SharedDeps, state: SenderState, rows: list[Queued]) -> int:
    """One chat's rows in order. A message that is not done holds back the messages after it, never the rest."""
    calls = 0
    held = False
    for row in rows:
        ordered = row.op in ORDERED_OPS
        if ordered and held:
            continue
        now = deps.clock.now()
        if state.is_parked(row.chat_id, now):
            break
        if row.op in COUNTED_OPS and not state.may_call(row.chat_id, now):
            held = held or ordered
            continue
        attempt = await _attempt(deps, state, row)
        calls += attempt.called
        held = held or (ordered and not attempt.done)
    return calls


async def _attempt(deps: SharedDeps, state: SenderState, row: Queued) -> _Attempt:
    now = deps.clock.now()
    async with deps.db.uow() as s:
        claimed = await store.claim_message(s, row.id, at=now)
    if claimed is None:
        return _SKIPPED  # another pass took it, or it was superseded or postponed meanwhile
    return await _deliver(deps, state, replace(row, attempts=claimed.attempts), claimed)


async def _deliver(deps: SharedDeps, state: SenderState, row: Queued, claimed: OutboxMessage) -> _Attempt:
    try:
        payload = domain.payload_from_json(row.op, claimed.payload_json)
    except PayloadInvalid:
        log.error("outbox_payload_refused", outbox_id=row.id, op=row.op.value, kind=row.kind.value)
        await _finish(deps, row, Status.FAILED, Code.PAYLOAD_INVALID)
        return _Attempt(called=False, done=True)
    preparation = domain.prepare(row.op, payload, await _resolve(deps, row, payload), now=deps.clock.now())
    if isinstance(preparation, NotYet):
        await _release(deps, row, claimed, at=preparation.at)
        return _SKIPPED
    if isinstance(preparation, Drop):
        await _finish(deps, row, preparation.status, preparation.code)
        return _Attempt(called=False, done=True)
    file, written = await _file_of(deps, row, payload)
    if row.op in FILE_OPS and file is None:
        await _finish(deps, row, Status.FAILED, Code.FILE_MISSING)
        return _Attempt(called=False, done=True)
    try:
        return await _call_and_record(deps, state, row, claimed, payload, preparation, file)
    finally:
        if written is not None:
            await asyncio.to_thread(written.unlink, missing_ok=True)


async def _call_and_record(
    deps: SharedDeps,
    state: SenderState,
    row: Queued,
    claimed: OutboxMessage,
    payload: Payload,
    ready: Ready,
    file: OutboundFile | None,
) -> _Attempt:
    started = deps.clock.now()
    if row.op in COUNTED_OPS:
        state.record_call(row.chat_id, started)
    timeout = domain.call_timeout(row.op, row.kind, state.config)
    clock_started = time.monotonic()
    try:
        async with asyncio.timeout(timeout + state.config.hard_grace_s):
            outcome = await _call(deps, row, claimed, payload, ready, file, timeout=timeout)
    except ExternalError as error:
        retry_after = error.retry_after if isinstance(error, RateLimited) else None
        failure = domain.failure_of(row.op, error)
        return await _failed(deps, state, row, failure, error_code=error.code.value, retry_after=retry_after)
    except TimeoutError:  # the adapter overran its own timeout
        failure = domain.timeout_failure(row.op)
        return await _failed(deps, state, row, failure, error_code=ErrorCode.CALL_TIMED_OUT.value, retry_after=None)
    except Exception:
        # Not a Telegram failure but a bug at the call (an adapter, or a request it cannot build): fail this row so a
        # restart does not repeat it, and let forever() log the traceback. Database errors elsewhere are not caught
        # here: the row stays `sending` and the restart resends it.
        await _finish(deps, row, Status.FAILED, Code.INTERNAL)
        raise
    duration_ms = round((time.monotonic() - clock_started) * 1000)
    return await _succeeded(deps, row, claimed, payload, outcome, duration_ms=duration_ms)


def _outgoing(stored: str | None) -> tuple[BotText, ParseMode]:
    """Scrubbed again with what the vault knows now (NFR-SEC-03), then rendered; the result is scrubbed once more,
    because escaping can hide a known value that holds `&`, `<` or `>`."""
    rendered = domain.render(redact.scrubbed(stored or ""))
    return redact.scrubbed(rendered.text), ("HTML" if rendered.html else None)


def _caption(stored: str | None) -> BotText | None:
    return redact.scrubbed(stored) if stored else None


def _id(value: int | None) -> int:
    if value is None:  # pragma: no cover - domain.prepare always resolves one for these ops
        msg = "no message id for this op"
        raise AssertionError(msg)
    return value


async def _call(
    deps: SharedDeps,
    row: Queued,
    claimed: OutboxMessage,
    payload: Payload,
    ready: Ready,
    file: OutboundFile | None,
    *,
    timeout: float,
) -> _Outcome:
    """The one Messenger call of this row."""
    messenger, chat_id = deps.messenger, row.chat_id
    if row.op in FILE_OPS:
        return _Outcome(message=await _send_file(deps, row, claimed, payload, file, timeout=timeout))
    if row.op is Op.SEND_TEXT:
        text, mode = _outgoing(ready.text)
        sent = await messenger.send_text(
            chat_id,
            text,
            reply_to=claimed.reply_to_message_id,
            parse_mode=mode,
            disable_notification=claimed.disable_notification,
            timeout=timeout,
        )
        return _Outcome(message=sent)
    if row.op is Op.DELETE:
        return _Outcome(deleted=await messenger.delete(chat_id, _id(ready.message_id), timeout=timeout))
    if row.op is Op.EDIT_TEXT:
        text, mode = _outgoing(ready.text)
        await messenger.edit_text(chat_id, _id(ready.message_id), text, parse_mode=mode, timeout=timeout)
    elif row.op is Op.REACT:
        await messenger.react(chat_id, _id(ready.message_id), payload.emoji or "", timeout=timeout)
    elif row.op is Op.CHAT_ACTION:
        await messenger.chat_action(chat_id, ChatAction(payload.action or ChatAction.TYPING), timeout=timeout)
    else:
        await messenger.pin(chat_id, _id(ready.message_id), timeout=timeout)
    return _Outcome()


async def _send_file(
    deps: SharedDeps,
    row: Queued,
    claimed: OutboxMessage,
    payload: Payload,
    file: OutboundFile | None,
    *,
    timeout: float,
) -> SentMessage:
    if file is None:  # pragma: no cover - _deliver fails a file row without its file
        msg = "a file op without its file"
        raise AssertionError(msg)
    messenger, chat_id = deps.messenger, row.chat_id
    caption = _caption(payload.caption)
    reply_to, quiet = claimed.reply_to_message_id, claimed.disable_notification
    if row.op is Op.SEND_PHOTO:
        return await messenger.send_photo(
            chat_id, file, caption=caption, reply_to=reply_to, disable_notification=quiet, timeout=timeout
        )
    if row.op is Op.SEND_VOICE:
        return await messenger.send_voice(
            chat_id,
            file,
            caption=caption,
            reply_to=reply_to,
            disable_notification=quiet,
            duration_s=payload.duration_s,
            timeout=timeout,
        )
    if row.op is Op.SEND_VIDEO:
        return await messenger.send_video(
            chat_id, file, caption=caption, reply_to=reply_to, disable_notification=quiet, timeout=timeout
        )
    return await messenger.send_document(
        chat_id, file, caption=caption, reply_to=reply_to, disable_notification=quiet, timeout=timeout
    )


# --- the sender: lookups and files ---------------------------------------------------------------------------------
async def _resolve(deps: SharedDeps, row: Queued, payload: Payload) -> Resolved:
    """The few facts `domain.prepare` needs from other rows: the target's message, the `after` row's status, the
    newest call for this coalesce key."""
    if payload.target is None and payload.after is None and row.op is not Op.EDIT_TEXT:
        return Resolved()
    target_status: Status | None = None
    target_message_id: int | None = None
    after_status: Status | None = None
    last_sent_at: datetime | None = None
    async with deps.db.read() as s:
        if payload.target is not None and (target := await store.find_outbox_message(s, payload.target)) is not None:
            target_status = Status(target.status)
            link = await store.find_link_of_outbox_message(s, target.id)
            target_message_id = None if link is None else link.message_id
        if payload.after is not None and (after := await store.find_outbox_message(s, payload.after)) is not None:
            after_status = Status(after.status)
        if row.op is Op.EDIT_TEXT and row.coalesce_key is not None:
            last_sent_at = await store.get_last_sent_at(s, row.coalesce_key, except_id=row.id)
    return Resolved(
        target_status=target_status,
        target_message_id=target_message_id,
        after_status=after_status,
        last_sent_at=last_sent_at,
    )


def _write_private_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(text, encoding="utf-8")


async def _file_of(deps: SharedDeps, row: Queued, payload: Payload) -> tuple[OutboundFile | None, Path | None]:
    """The file to upload, and the path written for it (removed after the call). A `.md` fallback is written now,
    scrubbed, into the media cache; a file from another feature must still exist."""
    if row.op not in FILE_OPS:
        return None, None
    if payload.content is not None:
        day = deps.clock.now().strftime("%Y%m%d")
        written = deps.paths.media / day / f"outbox-{row.id}.md"
        await asyncio.to_thread(_write_private_text, written, redact.scrubbed(payload.content))
        return OutboundFile(path=written, filename=payload.filename, mime=payload.mime), written
    if payload.path is None or not await asyncio.to_thread(Path(payload.path).is_file):
        return None, None
    return OutboundFile(path=Path(payload.path), filename=payload.filename, mime=payload.mime), None


# --- the sender: recording outcomes --------------------------------------------------------------------------------
async def _finish(deps: SharedDeps, row: Queued, status: Status, code: str | None) -> None:
    async with deps.db.uow() as s:
        await store.finish_message(s, row.id, status=status.value, error_code=code, sent_at=None)
    if status is Status.FAILED:
        log.warning("outbox_gave_up", outbox_id=row.id, op=row.op.value, kind=row.kind.value, error_code=code)


async def _release(deps: SharedDeps, row: Queued, claimed: OutboxMessage, *, at: datetime) -> None:
    """Not yet (an edit whose message, or a reply whose delete, is on its way): pending again, attempt given back."""
    async with deps.db.uow() as s:
        await store.set_message_pending(
            s, row.id, next_attempt_at=at, attempts=max(0, row.attempts - 1), error_code=claimed.last_error_code
        )


async def _fail_unreadable(deps: SharedDeps, row: OutboxMessage, *, now: datetime) -> None:
    """A row whose op, kind or status this release does not know: claimed and failed, so it is reported once."""
    async with deps.db.uow() as s:
        claimed = await store.claim_message(s, row.id, at=now)
        if claimed is not None:
            await store.finish_message(
                s, row.id, status=Status.FAILED.value, error_code=Code.PAYLOAD_INVALID.value, sent_at=None
            )
    if claimed is not None:
        log.error("outbox_row_unreadable", outbox_id=row.id)


async def _succeeded(
    deps: SharedDeps, row: Queued, claimed: OutboxMessage, payload: Payload, outcome: _Outcome, *, duration_ms: int
) -> _Attempt:
    now = deps.clock.now()
    if row.op is Op.CHAT_ACTION:
        repeat = None if payload.until is None else domain.next_typing_at(now, payload.until)
        async with deps.db.uow() as s:
            if repeat is not None:
                await store.set_message_pending(
                    s, row.id, next_attempt_at=repeat, attempts=0, error_code=None, sent_at=now
                )
            else:
                await store.finish_message(s, row.id, status=Status.SENT.value, error_code=None, sent_at=now)
        return _Attempt(called=True, done=True)
    if outcome.deleted is False:
        async with deps.db.uow() as s:
            await store.finish_message(
                s, row.id, status=Status.FAILED.value, error_code=Code.NOT_DELETED.value, sent_at=None
            )
        log.warning("outbox_delete_refused", outbox_id=row.id, chat_id=row.chat_id, kind=row.kind.value)
        return _Attempt(called=True, done=True)
    async with deps.db.uow() as s:
        await store.finish_message(s, row.id, status=Status.SENT.value, error_code=None, sent_at=now)
        if outcome.message is not None:
            await store.add_message_link(
                s,
                bot=deps.bot.value,
                chat_id=outcome.message.chat_id,
                message_id=outcome.message.message_id,
                outbox_id=row.id,
                kind=row.kind.value,
                project_id=claimed.project_id,
                turn_id=claimed.turn_id,
                question_id=payload.question_id,
                decision_id=payload.decision_id,
                created_at=now,
            )
        if domain.ends_typing(row.op, row.kind):
            await store.cancel_pending(
                s, bot=deps.bot.value, chat_id=row.chat_id, op=Op.CHAT_ACTION.value, code=Code.TYPING_ENDED.value
            )
    log.info(
        "outbox_message_sent",
        outbox_id=row.id,
        op=row.op.value,
        kind=row.kind.value,
        chat_id=row.chat_id,
        attempts=row.attempts,
        duration_ms=duration_ms,
    )
    return _Attempt(called=True, done=True)


async def _failed(
    deps: SharedDeps,
    state: SenderState,
    row: Queued,
    failure: Failure,
    *,
    error_code: str,
    retry_after: float | None,
) -> _Attempt:
    now = deps.clock.now()
    decision = domain.after_failure(row, failure, error_code=error_code, now=now, retry_after=retry_after)
    if isinstance(decision, GiveUp):
        async with deps.db.uow() as s:
            await store.finish_message(s, row.id, status=Status.FAILED.value, error_code=decision.code, sent_at=None)
        log.warning(
            "outbox_gave_up",
            outbox_id=row.id,
            op=row.op.value,
            kind=row.kind.value,
            chat_id=row.chat_id,
            failure=failure.value,
            error_code=decision.code,
            attempts=row.attempts,
        )
        return _Attempt(called=True, done=True)
    async with deps.db.uow() as s:
        await store.set_message_pending(
            s,
            row.id,
            next_attempt_at=decision.until if isinstance(decision, Park) else decision.at,
            attempts=decision.attempts,
            error_code=error_code,
        )
        if isinstance(decision, Park):
            await store.postpone_chat(s, bot=deps.bot.value, chat_id=row.chat_id, until=decision.until)
    if isinstance(decision, Park):
        state.parked[row.chat_id] = decision.until
        log.warning("outbox_chat_parked", outbox_id=row.id, chat_id=row.chat_id, retry_after_s=retry_after)
    else:
        log.warning(
            "outbox_call_failed",
            outbox_id=row.id,
            op=row.op.value,
            kind=row.kind.value,
            chat_id=row.chat_id,
            failure=failure.value,
            error_code=error_code,
            attempts=row.attempts,
        )
    return _Attempt(called=True, done=False)


# --- retention -----------------------------------------------------------------------------------------------------
async def apply_retention(deps: SharedDeps) -> int:
    """SPEC §4.2: finished rows are deleted 30 days after they were queued, links after 90, in batches. A pending
    or sending row is never touched. Returns how many rows went."""
    now = deps.clock.now()
    finished = sorted(status.value for status in TERMINAL)
    sent_cutoff, link_cutoff = domain.sent_cutoff(now), domain.link_cutoff(now)
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_messages_before(s, sent_cutoff, statuses=finished, limit=limit)
    )
    unlinked = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_links_before(s, link_cutoff, limit=limit)
    )
    log.info("outbox_retention_applied", rows_deleted=deleted, links_deleted=unlinked)
    return deleted + unlinked
