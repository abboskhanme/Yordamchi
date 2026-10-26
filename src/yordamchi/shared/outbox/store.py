"""SQL for `outbox_messages` and `message_links`. Never commits; the service's unit of work does.

Status changes are conditional (`WHERE status = …`), so a row is claimed by one sender only and a superseded or
cancelled row is never revived.
"""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence import bulk
from yordamchi.persistence.upsert import insert_or_ignore
from yordamchi.shared.outbox.domain import Status
from yordamchi.shared.outbox.models import MessageLink, OutboxMessage

_PENDING = Status.PENDING.value
_SENDING = Status.SENDING.value
_QUEUED = (_PENDING, _SENDING)


# --- outbox_messages: writes at enqueue ----------------------------------------------------------------------------
async def add_outbox_message(
    s: AsyncSession,
    *,
    bot: str,
    chat_id: int,
    op: str,
    kind: str,
    payload: JsonValue,
    dedupe_key: str,
    coalesce_key: str | None,
    priority: int,
    next_attempt_at: datetime,
    reply_to_message_id: int | None,
    disable_notification: bool,
    project_id: str | None,
    turn_id: int | None,
    created_at: datetime,
    thread_id: int | None = None,
) -> int | None:
    """Write one pending row keyed by `dedupe_key`. The new row id, or None if that key was already there."""
    inserted = await insert_or_ignore(
        s,
        OutboxMessage,
        {
            "bot": bot,
            "chat_id": chat_id,
            "op": op,
            "kind": kind,
            "payload_json": payload,
            "dedupe_key": dedupe_key,
            "coalesce_key": coalesce_key,
            "priority": priority,
            "status": _PENDING,
            "attempts": 0,
            "next_attempt_at": next_attempt_at,
            "last_error_code": None,
            "reply_to_message_id": reply_to_message_id,
            "disable_notification": disable_notification,
            "project_id": project_id,
            "turn_id": turn_id,
            "created_at": created_at,
            "sent_at": None,
            "thread_id": thread_id,
        },
        conflict_columns=("dedupe_key",),
    )
    if not inserted:
        return None
    return await s.scalar(select(OutboxMessage.id).where(OutboxMessage.dedupe_key == dedupe_key))


async def supersede_pending(s: AsyncSession, *, coalesce_key: str, op: str, keep_id: int) -> int:
    """Older pending rows of one coalesce key and op become `superseded`; `keep_id` (the newest) stays."""
    statement = (
        update(OutboxMessage)
        .where(
            OutboxMessage.coalesce_key == coalesce_key,
            OutboxMessage.op == op,
            OutboxMessage.status == _PENDING,
            OutboxMessage.id != keep_id,
        )
        .values(status=Status.SUPERSEDED.value)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def set_pending_payload(s: AsyncSession, row_id: int, *, payload: JsonValue) -> bool:
    """Replace the payload of a row that is still pending (a newer progress text rides along). False if too late."""
    statement = (
        update(OutboxMessage)
        .where(OutboxMessage.id == row_id, OutboxMessage.status == _PENDING)
        .values(payload_json=payload)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def cancel_pending(
    s: AsyncSession,
    *,
    bot: str,
    chat_id: int,
    op: str,
    code: str,
    in_flight: bool = False,
    every_thread: bool = True,
    thread_id: int | None = None,
) -> int:
    """Every pending row of one op in one chat → `cancelled` (typing that is no longer wanted). `in_flight` also takes
    a row being sent right now: its call finishes, but it is never re-armed (`set_message_pending` needs `sending`).
    `every_thread=False` keeps to the rows of one forum topic, `thread_id` (None: the chat itself)."""
    statement = update(OutboxMessage).where(
        OutboxMessage.bot == bot,
        OutboxMessage.chat_id == chat_id,
        OutboxMessage.op == op,
        OutboxMessage.status.in_(_QUEUED if in_flight else (_PENDING,)),
    )
    if not every_thread:
        statement = statement.where(
            OutboxMessage.thread_id.is_(None) if thread_id is None else OutboxMessage.thread_id == thread_id
        )
    statement = statement.values(status=Status.CANCELLED.value, last_error_code=code).execution_options(
        synchronize_session=False
    )
    return await bulk.affected_rows(s, statement)


async def move_thread(s: AsyncSession, *, bot: str, chat_id: int, from_thread: int, to_thread: int | None) -> int:
    """Every pending or in-flight row bound for topic `from_thread` goes to `to_thread` instead (None: the chat
    itself): the topic was reopened under the same id, opened anew, or cannot be had."""
    statement = (
        update(OutboxMessage)
        .where(
            OutboxMessage.bot == bot,
            OutboxMessage.chat_id == chat_id,
            OutboxMessage.thread_id == from_thread,
            OutboxMessage.status.in_(_QUEUED),
        )
        .values(thread_id=to_thread)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


# --- outbox_messages: reads ----------------------------------------------------------------------------------------
async def get_outbox_message(s: AsyncSession, row_id: int) -> OutboxMessage | None:
    return await s.get(OutboxMessage, row_id)


async def find_outbox_message(s: AsyncSession, dedupe_key: str) -> OutboxMessage | None:
    return await s.scalar(select(OutboxMessage).where(OutboxMessage.dedupe_key == dedupe_key))


async def list_queued_messages(s: AsyncSession, bot: str, *, max_priority: int, limit: int) -> list[OutboxMessage]:
    """Pending and in-flight rows, highest priority and oldest first (in-flight ones hold back what follows them)."""
    query = (
        select(OutboxMessage)
        .where(
            OutboxMessage.bot == bot,
            OutboxMessage.status.in_(_QUEUED),
            OutboxMessage.priority <= max_priority,
        )
        .order_by(OutboxMessage.priority, OutboxMessage.id)
        .limit(limit)
    )
    return list((await s.scalars(query)).all())


async def list_outbox_messages(s: AsyncSession, bot: str) -> list[OutboxMessage]:
    """Every row of one bot, oldest first."""
    query = select(OutboxMessage).where(OutboxMessage.bot == bot).order_by(OutboxMessage.id)
    return list((await s.scalars(query)).all())


async def get_last_sent_at(s: AsyncSession, coalesce_key: str, *, except_id: int | None = None) -> datetime | None:
    """The newest successful call for one coalesce key (IX coalesce_key, status covers it)."""
    query = select(func.max(OutboxMessage.sent_at)).where(OutboxMessage.coalesce_key == coalesce_key)
    if except_id is not None:
        query = query.where(OutboxMessage.id != except_id)
    newest: datetime | None = await s.scalar(query)
    return newest


async def count_outbox_messages(s: AsyncSession, *, coalesce_key: str, op: str) -> int:
    query = select(func.count()).where(OutboxMessage.coalesce_key == coalesce_key, OutboxMessage.op == op)
    return int(await s.scalar(query) or 0)


async def list_recent_sends(
    s: AsyncSession, bot: str, *, since: datetime, ops: Collection[str]
) -> list[tuple[int, datetime]]:
    """`(chat_id, sent_at)` of counted calls since `since`: the rate windows after a restart."""
    query = select(OutboxMessage.chat_id, OutboxMessage.sent_at).where(
        OutboxMessage.bot == bot,
        OutboxMessage.op.in_(list(ops)),
        OutboxMessage.sent_at.is_not(None),
        OutboxMessage.sent_at >= since,
    )
    return [(chat_id, sent_at) for chat_id, sent_at in (await s.execute(query)).all() if sent_at is not None]


# --- outbox_messages: the sender's writes --------------------------------------------------------------------------
async def claim_message(s: AsyncSession, row_id: int, *, at: datetime) -> OutboxMessage | None:
    """`pending` → `sending` and one more attempt, if the row is still pending and due. The fresh row, or None."""
    statement = (
        update(OutboxMessage)
        .where(OutboxMessage.id == row_id, OutboxMessage.status == _PENDING, OutboxMessage.next_attempt_at <= at)
        .values(status=_SENDING, attempts=OutboxMessage.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    if await bulk.affected_rows(s, statement) != 1:
        return None
    return await s.scalar(
        select(OutboxMessage).where(OutboxMessage.id == row_id).execution_options(populate_existing=True)
    )


async def set_message_pending(
    s: AsyncSession,
    row_id: int,
    *,
    next_attempt_at: datetime,
    attempts: int,
    error_code: str | None,
    sent_at: datetime | None = None,
) -> None:
    """Back to `pending` for a later attempt (a retry, a park, a target still on its way, the next typing repeat)."""
    values: dict[str, object] = {
        "status": _PENDING,
        "next_attempt_at": next_attempt_at,
        "attempts": attempts,
        "last_error_code": error_code,
    }
    if sent_at is not None:
        values["sent_at"] = sent_at
    statement = (
        update(OutboxMessage)
        .where(OutboxMessage.id == row_id, OutboxMessage.status == _SENDING)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def finish_message(
    s: AsyncSession, row_id: int, *, status: str, error_code: str | None, sent_at: datetime | None
) -> None:
    """A claimed row's final status: `sent`, `failed` or `cancelled`."""
    values: dict[str, object] = {"status": status, "last_error_code": error_code}
    if sent_at is not None:
        values["sent_at"] = sent_at
    statement = (
        update(OutboxMessage)
        .where(OutboxMessage.id == row_id, OutboxMessage.status == _SENDING)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def postpone_chat(s: AsyncSession, *, bot: str, chat_id: int, until: datetime) -> int:
    """Park a chat durably: every pending row of it waits at least until `until` (portable: no GREATEST/MAX)."""
    statement = (
        update(OutboxMessage)
        .where(
            OutboxMessage.bot == bot,
            OutboxMessage.chat_id == chat_id,
            OutboxMessage.status == _PENDING,
            OutboxMessage.next_attempt_at < until,
        )
        .values(next_attempt_at=until)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def reset_sending(s: AsyncSession, *, bot: str, at: datetime) -> int:
    """SPEC §2.7: rows a dead sender left `sending` go back to `pending`, due now (resend, duplicate allowed)."""
    statement = (
        update(OutboxMessage)
        .where(OutboxMessage.bot == bot, OutboxMessage.status == _SENDING)
        .values(status=_PENDING, next_attempt_at=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


# --- message_links -------------------------------------------------------------------------------------------------
async def add_message_link(
    s: AsyncSession,
    *,
    bot: str,
    chat_id: int,
    message_id: int,
    outbox_id: int,
    kind: str,
    project_id: str | None,
    turn_id: int | None,
    question_id: int | None,
    decision_id: int | None,
    created_at: datetime,
) -> bool:
    return await insert_or_ignore(
        s,
        MessageLink,
        {
            "bot": bot,
            "chat_id": chat_id,
            "message_id": message_id,
            "outbox_id": outbox_id,
            "kind": kind,
            "project_id": project_id,
            "turn_id": turn_id,
            "question_id": question_id,
            "decision_id": decision_id,
            "created_at": created_at,
        },
        conflict_columns=("bot", "chat_id", "message_id"),
    )


async def get_message_link(s: AsyncSession, *, bot: str, chat_id: int, message_id: int) -> MessageLink | None:
    return await s.get(MessageLink, (bot, chat_id, message_id))


async def find_link_of_outbox_message(s: AsyncSession, outbox_id: int) -> MessageLink | None:
    """The message an outbox row posted; the newest one if a resend posted it twice."""
    query = (
        select(MessageLink).where(MessageLink.outbox_id == outbox_id).order_by(MessageLink.message_id.desc()).limit(1)
    )
    return await s.scalar(query)


# --- retention -----------------------------------------------------------------------------------------------------
async def delete_messages_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Delete up to `limit` rows in `statuses` created before `cutoff`."""
    due = (
        select(OutboxMessage.id)
        .where(OutboxMessage.created_at < cutoff, OutboxMessage.status.in_(list(statuses)))
        .order_by(OutboxMessage.created_at)
        .limit(limit)
    )
    statement = delete(OutboxMessage).where(OutboxMessage.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)


async def delete_links_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    """Delete up to `limit` links created before `cutoff` (row-value IN: SQLite ≥ 3.15 and PostgreSQL)."""
    due = (
        select(MessageLink.bot, MessageLink.chat_id, MessageLink.message_id)
        .where(MessageLink.created_at < cutoff)
        .order_by(MessageLink.created_at)
        .limit(limit)
    )
    key = tuple_(MessageLink.bot, MessageLink.chat_id, MessageLink.message_id)
    statement = delete(MessageLink).where(key.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
