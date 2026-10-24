"""Usage ledger use cases (SPEC §4.2; UC-07): record one call inside the caller's transaction, and the 400-day
retention. The P2 report (`xarajat?`) and the monthly caps (T51) read this ledger.

A turn records its cost in the same unit of work that closes the turn, so a turn and its usage commit together:

    async with deps.db.uow() as s:
        await store.mark_turn_done(s, …)
        await cost.record_usage(s, bot=deps.bot, at=now, usage=Usage(source="turn", provider=Provider.ANTHROPIC_PLAN,
                                model="opus", is_paid_api=False, cost_usd_micros=result.cost_usd_micros, …))
"""

from datetime import datetime

from yordamchi.core.json_value import JsonObject
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.shared.cost import domain, store
from yordamchi.shared.cost.domain import Usage
from yordamchi.shared.deps import SharedDeps

log = get_log(__name__)


async def record_usage(s: Session, *, bot: BotName, at: datetime, usage: Usage) -> int:
    """Append one usage row in the caller's transaction; returns its id. An impossible `usage` raises `ValueError`."""
    domain.require_valid(usage)
    units: JsonObject = dict(usage.units)
    row = await store.add_usage_record(
        s,
        bot=bot.value,
        at=at,
        source=usage.source,
        provider=usage.provider.value,
        is_paid_api=usage.is_paid_api,
        model=usage.model,
        project_id=usage.project_id,
        turn_id=usage.turn_id,
        cost_usd_micros=usage.cost_usd_micros,
        units=units,
        duration_ms=usage.duration_ms,
    )
    return row.id


async def apply_retention(deps: SharedDeps) -> int:
    """Delete usage rows older than 400 days, in batches (SPEC §4.2). Returns how many were deleted."""
    cutoff = domain.retention_cutoff(deps.clock.now())
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_usage_records_before(s, cutoff, limit=limit)
    )
    log.info("cost_retention_applied", deleted=deleted)
    return deleted
