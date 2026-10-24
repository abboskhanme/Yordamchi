"""Audit use cases (NFR-SEC-10): record an event inside the caller's transaction, and the 90-day retention.

An event is written in the same unit of work as the change it describes, so a change and its audit row commit or
roll back together:

    async with deps.db.uow() as s:
        row = await store.add_secret(s, …)
        await audit.record_event(s, bot=deps.bot, at=now, actor=AuditActor.OWNER, kind=AuditKind.SECRET_ADDED,
                                 ref_type="vault_secret", ref_id=str(row.id), data={"name": row.name})
"""

from datetime import datetime

from yordamchi.core import redact
from yordamchi.core.json_value import JsonObject
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.shared.audit import domain, store
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.deps import SharedDeps

log = get_log(__name__)


async def record_event(
    s: Session,
    *,
    bot: BotName,
    at: datetime,
    actor: AuditActor,
    kind: AuditKind,
    data: JsonObject,
    project_id: str | None = None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> int:
    """Append one event in the caller's transaction; returns its id. Every string in `data` is scrubbed first."""
    row = await store.add_audit_event(
        s,
        bot=bot.value,
        at=at,
        actor=actor.value,
        kind=kind.value,
        project_id=project_id,
        ref_type=ref_type,
        ref_id=ref_id,
        data=domain.clean_data(data, redact.scrubbed),
    )
    return row.id


async def apply_retention(deps: SharedDeps) -> int:
    """Delete events older than 90 days, in batches (SPEC §4.2). Returns how many were deleted."""
    cutoff = domain.retention_cutoff(deps.clock.now())
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_audit_events_before(s, cutoff, limit=limit)
    )
    log.info("audit_retention_applied", deleted=deleted)
    return deleted
