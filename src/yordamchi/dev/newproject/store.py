"""SQL for `project_drafts`. Never commits; the service's unit of work does.

Every state change is conditional (`move_draft`, `add_draft_part`): it happens only from the states it names, so two
callers racing for one draft (a "tamom" and the silence timer, two "ha"s) change it once.
"""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.dev.newproject.models import ProjectDraft
from yordamchi.persistence import bulk


async def add_draft(
    s: AsyncSession, *, status: str, parts: JsonValue, opened_at: datetime, last_part_at: datetime
) -> ProjectDraft:
    row = ProjectDraft(
        status=status,
        parts_json=parts,
        proposed_name=None,
        project_id=None,
        plan_turn_id=None,
        build_turn_id=None,
        repo_url=None,
        error_code=None,
        opened_at=opened_at,
        last_part_at=last_part_at,
        closed_at=None,
    )
    s.add(row)
    await s.flush()
    return row


async def get_draft(s: AsyncSession, draft_id: int) -> ProjectDraft | None:
    """The row as stored now, even if this session read it before a conditional update."""
    return await s.get(ProjectDraft, draft_id, populate_existing=True)


async def list_drafts_with_status(s: AsyncSession, statuses: Collection[str]) -> list[ProjectDraft]:
    """Oldest first."""
    query = select(ProjectDraft).where(ProjectDraft.status.in_(list(statuses))).order_by(ProjectDraft.id)
    return list((await s.scalars(query)).all())


async def move_draft(
    s: AsyncSession, draft_id: int, *, from_statuses: Collection[str], to_status: str, **values: object
) -> bool:
    """`status: from → to` (plus `values`) only if the draft is still in one of `from_statuses`."""
    statement = (
        update(ProjectDraft)
        .where(ProjectDraft.id == draft_id, ProjectDraft.status.in_(list(from_statuses)))
        .values(status=to_status, **values)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def set_draft(s: AsyncSession, draft_id: int, **values: object) -> None:
    statement = (
        update(ProjectDraft)
        .where(ProjectDraft.id == draft_id)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def add_draft_part(
    s: AsyncSession, draft_id: int, *, part: JsonValue, at: datetime, statuses: Collection[str]
) -> bool:
    """Append one description part, only while the draft is in `statuses`. False when it moved on meanwhile. Call it
    in a unit of work (BEGIN IMMEDIATE): the read and the write see the same row."""
    row = await s.get(ProjectDraft, draft_id, populate_existing=True)
    if row is None or row.status not in statuses:
        return False
    parts = list(row.parts_json) if isinstance(row.parts_json, list) else []
    statement = (
        update(ProjectDraft)
        .where(ProjectDraft.id == draft_id, ProjectDraft.status == row.status)
        .values(parts_json=[*parts, part], last_part_at=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def delete_drafts_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Delete up to `limit` drafts in `statuses` closed before `cutoff`."""
    due = (
        select(ProjectDraft.id)
        .where(ProjectDraft.closed_at < cutoff, ProjectDraft.status.in_(list(statuses)))
        .order_by(ProjectDraft.closed_at)
        .limit(limit)
    )
    statement = delete(ProjectDraft).where(ProjectDraft.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
