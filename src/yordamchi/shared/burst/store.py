"""SQL for `bursts`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import case, delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence import bulk
from yordamchi.shared.burst.models import Burst


async def add_burst(
    s: AsyncSession,
    *,
    bot: str,
    chat_id: int,
    status: str,
    opened_at: datetime,
    last_part_at: datetime,
    closes_at: datetime,
    input_json: JsonValue,
    closed_at: datetime | None = None,
    close_reason: str | None = None,
) -> Burst:
    row = Burst(
        bot=bot,
        chat_id=chat_id,
        status=status,
        opened_at=opened_at,
        last_part_at=last_part_at,
        closes_at=closes_at,
        closed_at=closed_at,
        close_reason=close_reason,
        input_json=input_json,
        route_id=None,
    )
    s.add(row)
    await s.flush()
    return row


async def find_burst_in(s: AsyncSession, *, bot: str, chat_id: int, statuses: Collection[str]) -> Burst | None:
    """The chat's burst in one of `statuses` (at most one is assembling: UNIQUE index)."""
    query = select(Burst).where(Burst.bot == bot, Burst.chat_id == chat_id, Burst.status.in_(list(statuses)))
    return await s.scalar(query.order_by(Burst.id).limit(1))


async def list_bursts_in(s: AsyncSession, *, bot: str, statuses: Collection[str]) -> list[Burst]:
    """Every burst of one bot in `statuses`, oldest first (IX bot, status)."""
    query = select(Burst).where(Burst.bot == bot, Burst.status.in_(list(statuses))).order_by(Burst.id)
    return list((await s.scalars(query)).all())


async def list_bursts_due(s: AsyncSession, *, bot: str, statuses: Collection[str], now: datetime) -> list[Burst]:
    """Bursts in `statuses` whose `closes_at` is not after `now`, earliest first."""
    query = (
        select(Burst)
        .where(Burst.bot == bot, Burst.status.in_(list(statuses)), Burst.closes_at <= now)
        .order_by(Burst.closes_at, Burst.id)
    )
    return list((await s.scalars(query)).all())


async def get_next_closes_at(s: AsyncSession, *, bot: str, statuses: Collection[str]) -> datetime | None:
    query = (
        select(Burst.closes_at)
        .where(Burst.bot == bot, Burst.status.in_(list(statuses)))
        .order_by(Burst.closes_at)
        .limit(1)
    )
    return await s.scalar(query)


async def list_bursts_with_status(
    s: AsyncSession, *, bot: str, status: str, first_reason: str, limit: int
) -> list[Burst]:
    """Bursts in `status`, those closed for `first_reason` first, then oldest first."""
    query = (
        select(Burst)
        .where(Burst.bot == bot, Burst.status == status)
        .order_by(case((Burst.close_reason == first_reason, 0), else_=1), Burst.id)
        .limit(limit)
    )
    return list((await s.scalars(query)).all())


async def list_bursts(s: AsyncSession, bot: str) -> list[Burst]:
    """Every burst of one bot, oldest first."""
    return list((await s.scalars(select(Burst).where(Burst.bot == bot).order_by(Burst.id))).all())


async def set_burst_parts(
    s: AsyncSession, row: Burst, *, status: str, last_part_at: datetime, closes_at: datetime, input_json: JsonValue
) -> None:
    """A still assembling burst after a part, an edit or a transcript."""
    row.status = status
    row.last_part_at = last_part_at
    row.closes_at = closes_at
    row.input_json = input_json
    await s.flush()


async def set_burst_closed(
    s: AsyncSession, row: Burst, *, status: str, closed_at: datetime, close_reason: str, input_json: JsonValue
) -> None:
    row.status = status
    row.closed_at = closed_at
    row.close_reason = close_reason
    row.input_json = input_json
    await s.flush()


async def set_burst_status(
    s: AsyncSession, *, bot: str, burst_id: int, from_status: str, to_status: str, route_id: int | None
) -> int:
    """Move one burst still in `from_status` to `to_status`. Returns 1 if it moved, 0 if it was elsewhere."""
    statement = (
        update(Burst)
        .where(Burst.id == burst_id, Burst.bot == bot, Burst.status == from_status)
        .values(status=to_status, route_id=route_id)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def delete_bursts_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Delete up to `limit` bursts in `statuses` closed before `cutoff`."""
    due = (
        select(Burst.id)
        .where(Burst.closed_at < cutoff, Burst.status.in_(list(statuses)))
        .order_by(Burst.closed_at)
        .limit(limit)
    )
    statement = delete(Burst).where(Burst.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
