"""SQL for `usage_records`. Append-only, plus the 400-day retention delete. Never commits."""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence import bulk
from yordamchi.shared.cost.models import UsageRecord


async def add_usage_record(
    s: AsyncSession,
    *,
    bot: str,
    at: datetime,
    source: str,
    provider: str,
    is_paid_api: bool,
    model: str,
    project_id: str | None,
    turn_id: int | None,
    cost_usd_micros: int,
    units: JsonValue,
    duration_ms: int,
) -> UsageRecord:
    row = UsageRecord(
        bot=bot,
        at=at,
        source=source,
        provider=provider,
        is_paid_api=is_paid_api,
        model=model,
        project_id=project_id,
        turn_id=turn_id,
        cost_usd_micros=cost_usd_micros,
        units_json=units,
        duration_ms=duration_ms,
    )
    s.add(row)
    await s.flush()
    return row


async def list_usage_records(s: AsyncSession) -> list[UsageRecord]:
    """Oldest first (the P2 report adds its own aggregate queries)."""
    return list((await s.scalars(select(UsageRecord).order_by(UsageRecord.at, UsageRecord.id))).all())


async def delete_usage_records_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    oldest = select(UsageRecord.id).where(UsageRecord.at < cutoff).order_by(UsageRecord.at).limit(limit)
    statement = delete(UsageRecord).where(UsageRecord.id.in_(oldest)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
