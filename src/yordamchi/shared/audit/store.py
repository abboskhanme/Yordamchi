"""SQL for `audit_events`. Append-only: an insert, and the retention delete of rows past `AUDIT_KEEP`. Never commits."""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonObject
from yordamchi.persistence import bulk
from yordamchi.shared.audit.models import AuditEvent


async def add_audit_event(
    s: AsyncSession,
    *,
    bot: str,
    at: datetime,
    actor: str,
    kind: str,
    project_id: str | None,
    ref_type: str | None,
    ref_id: str | None,
    data: JsonObject,
) -> AuditEvent:
    row = AuditEvent(
        bot=bot,
        at=at,
        actor=actor,
        kind=kind,
        project_id=project_id,
        ref_type=ref_type,
        ref_id=ref_id,
        data_json=data,
    )
    s.add(row)
    await s.flush()
    return row


async def list_audit_events(s: AsyncSession, *, kind: str | None = None) -> list[AuditEvent]:
    """Oldest first; all kinds, or one."""
    query = select(AuditEvent).order_by(AuditEvent.at, AuditEvent.id)
    if kind is not None:
        query = query.where(AuditEvent.kind == kind)
    return list((await s.scalars(query)).all())


async def delete_audit_events_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    """Delete up to `limit` events with `at < cutoff`, oldest first."""
    oldest = select(AuditEvent.id).where(AuditEvent.at < cutoff).order_by(AuditEvent.at).limit(limit)
    statement = delete(AuditEvent).where(AuditEvent.id.in_(oldest)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
