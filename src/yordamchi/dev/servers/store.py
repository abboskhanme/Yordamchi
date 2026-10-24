"""SQL for `server_checks`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.dev.servers.models import ServerCheck
from yordamchi.persistence import bulk


async def add_server_check(
    s: AsyncSession,
    *,
    host: str,
    at: datetime,
    ok: bool,
    summary: JsonValue,
    error_code: str | None,
    duration_ms: int,
) -> ServerCheck:
    row = ServerCheck(host=host, at=at, ok=ok, summary_json=summary, error_code=error_code, duration_ms=duration_ms)
    s.add(row)
    await s.flush()
    return row


async def list_latest_checks(s: AsyncSession, hosts: Collection[str]) -> list[ServerCheck]:
    """The newest row of each host in `hosts`, in one query (rows are only ever appended, so the newest has the
    highest id)."""
    if not hosts:
        return []
    newest = select(func.max(ServerCheck.id)).where(ServerCheck.host.in_(list(hosts))).group_by(ServerCheck.host)
    query = select(ServerCheck).where(ServerCheck.id.in_(newest)).order_by(ServerCheck.host)
    return list((await s.scalars(query)).all())


async def delete_checks_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    """Delete up to `limit` rows checked before `cutoff`."""
    due = select(ServerCheck.id).where(ServerCheck.at < cutoff).order_by(ServerCheck.at).limit(limit)
    statement = delete(ServerCheck).where(ServerCheck.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
