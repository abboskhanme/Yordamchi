"""SQL of the bridge: SELECTs on `dev.db`, nothing else (SPEC §2.3; UC-36).

`dev/*` owns these tables and `shared` may not import it (C1, A2), so the bridge declares the columns it reads as
Core tables in a **private** `MetaData`: Alembic never sees them (`migrations/env.py` reads `Base.metadata`), and
`tests/unit/shared/bridge/test_store.py` checks every column against the real models, so a dev-side change cannot
drift unnoticed. This module has no insert, update or delete, and the handle it is given is `mode=ro` with
`query_only=ON`, so a write would fail twice over.
"""

from datetime import datetime
from typing import Final

from sqlalchemy import Boolean, Column, Integer, MetaData, String, Table, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.types import JSONText, UTCDateTime

_READ_ONLY: Final = MetaData()

PROJECTS: Final = Table(
    "projects",
    _READ_ONLY,
    Column("id", String(64), primary_key=True),
    Column("description", Text),
    Column("is_archive_name", Boolean),
    Column("missing_since", UTCDateTime()),
)
DIGESTS: Final = Table(
    "digests",
    _READ_ONLY,
    Column("project_id", String(64), primary_key=True),
    Column("checked_at", UTCDateTime()),
    Column("git_branch", String(255)),
    Column("git_head", String(64)),
    Column("git_upstream", String(255)),
    Column("ahead", Integer),
    Column("behind", Integer),
    Column("dirty_count", Integer),
    Column("untracked_count", Integer),
    Column("git_error_code", String(48)),
    Column("recent_commits_json", JSONText()),
    Column("results_json", JSONText()),
    Column("open_questions", Integer),
    Column("pending_decisions", Integer),
    Column("queued", Integer),
    Column("parked", Integer),
    Column("handoff_note", Text),
)
SERVER_CHECKS: Final = Table(
    "server_checks",
    _READ_ONLY,
    Column("id", Integer, primary_key=True),
    Column("host", String(253)),
    Column("at", UTCDateTime()),
    Column("ok", Boolean),
    Column("summary_json", JSONText()),
    Column("error_code", String(64)),
)
TABLES: Final = (PROJECTS, DIGESTS, SERVER_CHECKS)


async def list_projects(s: AsyncSession) -> list[tuple[str, str, bool, bool]]:
    """`(id, description, is_archive_name, missing)` of every registered project, by id."""
    statement = select(
        PROJECTS.c.id, PROJECTS.c.description, PROJECTS.c.is_archive_name, PROJECTS.c.missing_since
    ).order_by(PROJECTS.c.id)
    rows = (await s.execute(statement)).all()
    return [(str(row[0]), str(row[1] or ""), bool(row[2]), row[3] is not None) for row in rows]


async def get_digest(s: AsyncSession, project_id: str) -> dict[str, object] | None:
    """One `digests` row as a column → value mapping, or None."""
    row = (await s.execute(select(DIGESTS).where(DIGESTS.c.project_id == project_id))).mappings().first()
    return dict(row) if row is not None else None


type CheckRow = tuple[str, datetime, bool, JsonValue, str | None]


async def list_latest_checks(s: AsyncSession, *, since: datetime) -> list[CheckRow]:
    """The newest check of each host since `since`: `(host, at, ok, summary_json, error_code)`, by host."""
    latest = (
        select(SERVER_CHECKS.c.host, func.max(SERVER_CHECKS.c.id).label("id"))
        .where(SERVER_CHECKS.c.at >= since)
        .group_by(SERVER_CHECKS.c.host)
        .subquery()
    )
    statement = (
        select(
            SERVER_CHECKS.c.host,
            SERVER_CHECKS.c.at,
            SERVER_CHECKS.c.ok,
            SERVER_CHECKS.c.summary_json,
            SERVER_CHECKS.c.error_code,
        )
        .join(latest, SERVER_CHECKS.c.id == latest.c.id)
        .order_by(SERVER_CHECKS.c.host)
    )
    rows = (await s.execute(statement)).all()
    return [(str(row[0]), row[1], bool(row[2]), row[3], row[4]) for row in rows]


async def get_query_only(s: AsyncSession) -> bool:
    """`PRAGMA query_only` of the connection this read runs on (1 = writes refused by SQLite itself)."""
    connection = await s.connection()
    result = await connection.exec_driver_sql("PRAGMA query_only")
    row = result.first()
    return row is not None and int(row[0]) == 1
