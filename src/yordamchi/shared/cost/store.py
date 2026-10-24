"""SQL for `usage_records` (append-only, plus the 400-day retention delete) and `cap_notices`. Never commits.

The report adds the ledger up in SQL, one grouped query per DB file, so "xarajat?" stays fast with 400 days of rows.
Each DB file holds one bot's rows (SPEC §2.3), so filtering by `bot` only lets the query use IX(bot, at).
"""

from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import ColumnElement, SQLColumnExpression, and_, case, delete, distinct, func, not_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.persistence import bulk, upsert
from yordamchi.shared.cost.domain import TURN_SOURCE, Figures, Period, ProjectTotal, Provider, UsageTotal
from yordamchi.shared.cost.models import CapNotice, UsageRecord

_FIGURE_COLUMNS = 4  # cost, calls, turns, duration per period


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
    """Oldest first."""
    return list((await s.scalars(select(UsageRecord).order_by(UsageRecord.at, UsageRecord.id))).all())


def _sum_where(condition: SQLColumnExpression[bool], column: SQLColumnExpression[int]) -> ColumnElement[int]:
    return func.coalesce(func.sum(case((condition, column), else_=0)), 0)


def _period_columns(start: datetime) -> list[ColumnElement[int]]:
    inside = UsageRecord.at >= start
    return [
        _sum_where(inside, UsageRecord.cost_usd_micros),
        func.count(case((inside, UsageRecord.id))),
        func.count(distinct(case((inside, UsageRecord.turn_id)))),
        _sum_where(inside, UsageRecord.duration_ms),
    ]


async def list_usage_totals(s: AsyncSession, *, bot: str, starts: Mapping[Period, datetime]) -> list[UsageTotal]:
    """`bot`'s rows since the earliest of `starts`, one entry per (provider, source, paid) group, added up for each
    period (a row counts in every period whose start it is not before)."""
    periods = list(starts)
    columns = [column for period in periods for column in _period_columns(starts[period])]
    group = (UsageRecord.provider, UsageRecord.source, UsageRecord.is_paid_api)
    statement = (
        select(*group, *columns)
        .where(UsageRecord.bot == bot, UsageRecord.at >= min(starts.values()))
        .group_by(*group)
        .order_by(*group)
    )
    totals: list[UsageTotal] = []
    for provider, source, is_paid_api, *values in (await s.execute(statement)).all():
        figures = {
            period: Figures(*(int(value) for value in values[index * _FIGURE_COLUMNS : (index + 1) * _FIGURE_COLUMNS]))
            for index, period in enumerate(periods)
        }
        totals.append(
            UsageTotal(
                bot=BotName(bot), provider=provider, source=source, is_paid_api=bool(is_paid_api), figures=figures
            )
        )
    return totals


async def list_project_totals(s: AsyncSession, *, bot: str, since: datetime) -> list[ProjectTotal]:
    """`bot`'s rows that name a project, since `since`: turns, the Claude plan estimate and paid spend per project."""
    plan = and_(not_(UsageRecord.is_paid_api), UsageRecord.provider == Provider.ANTHROPIC_PLAN.value)
    statement = (
        select(
            UsageRecord.project_id,
            func.count(distinct(case((UsageRecord.source == TURN_SOURCE, UsageRecord.turn_id)))),
            _sum_where(plan, UsageRecord.cost_usd_micros),
            _sum_where(UsageRecord.is_paid_api, UsageRecord.cost_usd_micros),
        )
        .where(UsageRecord.bot == bot, UsageRecord.at >= since, UsageRecord.project_id.is_not(None))
        .group_by(UsageRecord.project_id)
        .order_by(UsageRecord.project_id)
    )
    return [
        ProjectTotal(project_id=str(project), turns=int(turns), plan_usd_micros=int(plan_), paid_usd_micros=int(paid))
        for project, turns, plan_, paid in (await s.execute(statement)).all()
    ]


async def get_paid_spend(s: AsyncSession, *, provider: str, since: datetime) -> int:
    """Micro-USD paid to `provider` since `since` (IX provider, at)."""
    statement = select(func.coalesce(func.sum(UsageRecord.cost_usd_micros), 0)).where(
        UsageRecord.provider == provider, UsageRecord.at >= since, UsageRecord.is_paid_api
    )
    return int(await s.scalar(statement) or 0)


async def delete_usage_records_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    oldest = select(UsageRecord.id).where(UsageRecord.at < cutoff).order_by(UsageRecord.at).limit(limit)
    statement = delete(UsageRecord).where(UsageRecord.id.in_(oldest)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)


# --- cap_notices ----------------------------------------------------------------------------------------------------
_NOTICE_KEY = ("provider", "month", "level", "cap_usd")


async def list_cap_notices(s: AsyncSession, *, month: str) -> list[CapNotice]:
    statement = select(CapNotice).where(CapNotice.month == month).order_by(CapNotice.sent_at)
    return list((await s.scalars(statement)).all())


async def add_cap_notice(
    s: AsyncSession, *, provider: str, month: str, level: int, cap_usd: int, sent_at: datetime
) -> bool:
    """True if this notice is new (written before the message is queued, CONVENTIONS §8)."""
    values = {"provider": provider, "month": month, "level": level, "cap_usd": cap_usd, "sent_at": sent_at}
    return await upsert.insert_or_ignore(s, CapNotice, values, conflict_columns=_NOTICE_KEY)


async def delete_cap_notices_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    key = tuple_(CapNotice.provider, CapNotice.month, CapNotice.level, CapNotice.cap_usd)
    oldest = (
        select(CapNotice.provider, CapNotice.month, CapNotice.level, CapNotice.cap_usd)
        .where(CapNotice.sent_at < cutoff)
        .order_by(CapNotice.sent_at)
        .limit(limit)
    )
    statement = delete(CapNotice).where(key.in_(oldest)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
