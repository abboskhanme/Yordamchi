"""SQL for `projects`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.dev.projects.domain import ProjectRecord
from yordamchi.dev.projects.models import Project
from yordamchi.persistence import bulk


async def list_projects(s: AsyncSession) -> list[Project]:
    """Every registered project, missing ones included, by id."""
    return list((await s.scalars(select(Project).order_by(Project.id))).all())


async def get_project(s: AsyncSession, project_id: str) -> Project | None:
    return await s.get(Project, project_id)


async def set_project(s: AsyncSession, record: ProjectRecord) -> None:
    """Insert the row, or overwrite every column of the existing one."""
    row = await s.get(Project, record.id)
    if row is None:
        row = Project(id=record.id)
        s.add(row)
    row.path = record.path
    row.description = record.description
    row.description_source = record.description_source.value
    row.is_worktree = record.is_worktree
    row.is_archive_name = record.is_archive_name
    row.executor = record.executor
    row.git_remote_url = record.git_remote_url
    row.default_branch = record.default_branch
    row.deploy_branches_json = record.deploy.to_json()
    row.deploy_checked_sha = record.deploy_checked_sha
    row.discovered_at = record.discovered_at
    row.last_seen_at = record.last_seen_at
    row.missing_since = record.missing_since
    row.last_activity_at = record.last_activity_at
    await s.flush()


async def mark_projects_missing(s: AsyncSession, project_ids: Collection[str], *, at: datetime) -> int:
    """Set `missing_since` on rows that do not have it yet. Returns how many rows changed."""
    if not project_ids:
        return 0
    statement = (
        update(Project)
        .where(Project.id.in_(list(project_ids)), Project.missing_since.is_(None))
        .values(missing_since=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)
