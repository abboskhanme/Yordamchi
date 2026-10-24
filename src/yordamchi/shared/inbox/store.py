"""SQL for `inbound_updates` and `poll_offsets`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence import bulk
from yordamchi.persistence.upsert import insert_or_ignore
from yordamchi.shared.inbox.models import InboundUpdate, PollOffset


async def add_inbound_update(
    s: AsyncSession,
    *,
    bot: str,
    update_id: int,
    chat_id: int,
    user_id: int | None,
    message_id: int | None,
    kind: str,
    media_group_id: str | None,
    reply_to_message_id: int | None,
    synthetic: bool,
    status: str,
    received_at: datetime,
) -> int | None:
    """Write the idempotency key `(bot, update_id)` with no payload yet. The new row id, or None if it was there."""
    inserted = await insert_or_ignore(
        s,
        InboundUpdate,
        {
            "bot": bot,
            "update_id": update_id,
            "chat_id": chat_id,
            "user_id": user_id,
            "message_id": message_id,
            "kind": kind,
            "media_group_id": media_group_id,
            "reply_to_message_id": reply_to_message_id,
            "payload_json": None,
            "synthetic": synthetic,
            "status": status,
            "burst_id": None,
            "received_at": received_at,
        },
        conflict_columns=("bot", "update_id"),
    )
    if not inserted:
        return None
    return await s.scalar(
        select(InboundUpdate.id).where(InboundUpdate.bot == bot, InboundUpdate.update_id == update_id)
    )


async def set_update_payload(s: AsyncSession, row_id: int, *, payload: JsonValue, status: str) -> None:
    statement = update(InboundUpdate).where(InboundUpdate.id == row_id).values(payload_json=payload, status=status)
    await s.execute(statement)


async def list_inbound_updates(s: AsyncSession, bot: str) -> list[InboundUpdate]:
    """Every row of one bot in `update_id` order."""
    query = select(InboundUpdate).where(InboundUpdate.bot == bot).order_by(InboundUpdate.update_id)
    return list((await s.scalars(query)).all())


async def list_updates_with_status(s: AsyncSession, bot: str, status: str, *, limit: int) -> list[InboundUpdate]:
    """Oldest first by `update_id` (IX bot, status, update_id)."""
    query = (
        select(InboundUpdate)
        .where(InboundUpdate.bot == bot, InboundUpdate.status == status)
        .order_by(InboundUpdate.update_id)
        .limit(limit)
    )
    return list((await s.scalars(query)).all())


async def set_update_status(
    s: AsyncSession,
    *,
    bot: str,
    update_ids: Collection[int],
    from_status: str,
    to_status: str,
    burst_id: int | None,
) -> int:
    """Move rows still in `from_status` to `to_status`; rows in any other status are left alone. Returns the count."""
    statement = (
        update(InboundUpdate)
        .where(
            InboundUpdate.bot == bot,
            InboundUpdate.update_id.in_(list(update_ids)),
            InboundUpdate.status == from_status,
        )
        .values(status=to_status, burst_id=burst_id)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def clear_payloads_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Null the payload of up to `limit` rows in `statuses` received before `cutoff`."""
    due = (
        select(InboundUpdate.id)
        .where(
            InboundUpdate.received_at < cutoff,
            InboundUpdate.status.in_(list(statuses)),
            InboundUpdate.payload_json.is_not(None),
        )
        .order_by(InboundUpdate.received_at)
        .limit(limit)
    )
    statement = (
        update(InboundUpdate)
        .where(InboundUpdate.id.in_(due))
        .values(payload_json=None)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def delete_updates_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Delete up to `limit` rows in `statuses` received before `cutoff`."""
    due = (
        select(InboundUpdate.id)
        .where(InboundUpdate.received_at < cutoff, InboundUpdate.status.in_(list(statuses)))
        .order_by(InboundUpdate.received_at)
        .limit(limit)
    )
    statement = delete(InboundUpdate).where(InboundUpdate.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)


async def get_poll_offset(s: AsyncSession, bot: str) -> PollOffset | None:
    return await s.get(PollOffset, bot)


async def set_poll_offset(s: AsyncSession, *, bot: str, next_offset: int, at: datetime) -> None:
    row = await s.get(PollOffset, bot)
    if row is None:
        s.add(PollOffset(bot=bot, next_offset=next_offset, updated_at=at))
    else:
        row.next_offset = next_offset
        row.updated_at = at
    await s.flush()
