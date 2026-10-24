"""SQL for `self_updates`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.dev.selfupdate.models import SelfUpdate
from yordamchi.persistence import bulk


async def add_self_update(
    s: AsyncSession, *, status: str, kind: str, branch: str, base_commit: str | None, at: datetime
) -> SelfUpdate:
    row = SelfUpdate(
        status=status,
        kind=kind,
        branch=branch,
        base_commit=base_commit,
        changed_files_json=[],
        touches_core=False,
        created_at=at,
    )
    s.add(row)
    await s.flush()
    return row


async def get_self_update(s: AsyncSession, row_id: int) -> SelfUpdate | None:
    return await s.get(SelfUpdate, row_id)


async def set_self_update(s: AsyncSession, row_id: int, **values: object) -> None:
    statement = (
        update(SelfUpdate).where(SelfUpdate.id == row_id).values(**values).execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def move_self_update(
    s: AsyncSession, row_id: int, *, from_statuses: Collection[str], to_status: str, **values: object
) -> bool:
    """A conditional status change: False when the row had already left `from_statuses` (a second caller)."""
    statement = (
        update(SelfUpdate)
        .where(SelfUpdate.id == row_id, SelfUpdate.status.in_(list(from_statuses)))
        .values(status=to_status, **values)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def mark_self_update_reported(s: AsyncSession, row_id: int, *, at: datetime) -> bool:
    """The owner's outcome message is queued: exactly once (`… WHERE reported_at IS NULL`)."""
    statement = (
        update(SelfUpdate)
        .where(SelfUpdate.id == row_id, SelfUpdate.reported_at.is_(None))
        .values(reported_at=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def list_self_updates_to_follow(s: AsyncSession, open_statuses: Collection[str]) -> list[SelfUpdate]:
    """Rows still on their way, and final ones whose outcome the owner has not been told yet, oldest first."""
    query = (
        select(SelfUpdate)
        .where(or_(SelfUpdate.status.in_(list(open_statuses)), SelfUpdate.reported_at.is_(None)))
        .order_by(SelfUpdate.id)
    )
    return list((await s.scalars(query)).all())


async def list_recent_self_updates(s: AsyncSession, *, limit: int) -> list[SelfUpdate]:
    query = select(SelfUpdate).order_by(SelfUpdate.id.desc()).limit(limit)
    return list((await s.scalars(query)).all())
