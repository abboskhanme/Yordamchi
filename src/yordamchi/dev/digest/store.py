"""SQL for `digests`. Never commits; the service's unit of work does."""

from collections.abc import Collection, Mapping
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.dev.digest.models import Digest
from yordamchi.persistence import bulk


async def get_digest(s: AsyncSession, project_id: str) -> Digest | None:
    return await s.get(Digest, project_id)


async def list_digests(s: AsyncSession) -> list[Digest]:
    """Every project's digest (one row each), by project id."""
    return list((await s.scalars(select(Digest).order_by(Digest.project_id))).all())


async def set_digest(s: AsyncSession, project_id: str, values: Mapping[str, object]) -> None:
    """Write the whole row: insert it the first time, else replace its values."""
    row = await s.get(Digest, project_id)
    if row is None:
        s.add(Digest(project_id=project_id, **values))
    else:
        for name, value in values.items():
            setattr(row, name, value)
    await s.flush()


async def set_digests_checked(s: AsyncSession, project_ids: Collection[str], *, at: datetime) -> int:
    """The poll read git and nothing changed: only `checked_at` moves. Returns how many rows it touched."""
    if not project_ids:
        return 0
    statement = (
        update(Digest)
        .where(Digest.project_id.in_(list(project_ids)))
        .values(checked_at=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)
