"""SQL for `health_events` and `kv_state`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence import bulk
from yordamchi.shared.health.models import HealthEvent, KvState


async def add_health_event(s: AsyncSession, *, bot: str, at: datetime, kind: str, data: JsonValue) -> None:
    s.add(HealthEvent(bot=bot, at=at, kind=kind, data_json=data))
    await s.flush()


async def list_health_events(
    s: AsyncSession, bot: str, *, kinds: Collection[str] | None = None, limit: int = 100
) -> list[HealthEvent]:
    """Newest first (IX bot, at)."""
    query = select(HealthEvent).where(HealthEvent.bot == bot)
    if kinds is not None:
        query = query.where(HealthEvent.kind.in_(list(kinds)))
    query = query.order_by(HealthEvent.at.desc(), HealthEvent.id.desc()).limit(limit)
    return list((await s.scalars(query)).all())


async def delete_health_events_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    """Delete up to `limit` events, of any bot, older than `cutoff`."""
    due = select(HealthEvent.id).where(HealthEvent.at < cutoff).order_by(HealthEvent.at).limit(limit)
    statement = delete(HealthEvent).where(HealthEvent.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)


async def get_kv(s: AsyncSession, *, bot: str, key: str) -> KvState | None:
    return await s.get(KvState, (bot, key))


async def set_kv(s: AsyncSession, *, bot: str, key: str, value: JsonValue, at: datetime) -> None:
    row = await s.get(KvState, (bot, key))
    if row is None:
        s.add(KvState(bot=bot, key=key, value_json=value, updated_at=at))
    else:
        row.value_json = value
        row.updated_at = at
    await s.flush()
