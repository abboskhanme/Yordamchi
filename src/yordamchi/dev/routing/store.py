"""SQL for `route_decisions` and `clarifications`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.dev.routing.models import Clarification, RouteDecision
from yordamchi.persistence import bulk


# --- route_decisions --------------------------------------------------------------------------------------------------
async def add_route_decision(
    s: AsyncSession,
    *,
    burst_id: int | None,
    at: datetime,
    method: str,
    intent: str,
    project_id: str | None,
    candidates: JsonValue,
    confidence_milli: int,
    asked: bool,
    model: str | None,
    latency_ms: int,
) -> RouteDecision:
    row = RouteDecision(
        burst_id=burst_id,
        at=at,
        method=method,
        intent=intent,
        project_id=project_id,
        candidates_json=candidates,
        confidence_milli=confidence_milli,
        asked=asked,
        model=model,
        latency_ms=latency_ms,
        corrected_to=None,
    )
    s.add(row)
    await s.flush()
    return row


async def delete_route_decision(s: AsyncSession, route_id: int) -> None:
    await s.execute(delete(RouteDecision).where(RouteDecision.id == route_id))


async def list_route_decisions_since(s: AsyncSession, since: datetime, *, limit: int) -> list[RouteDecision]:
    """Decisions made at or after `since`, newest first (IX at)."""
    query = (
        select(RouteDecision)
        .where(RouteDecision.at >= since)
        .order_by(RouteDecision.at.desc(), RouteDecision.id.desc())
        .limit(limit)
    )
    return list((await s.scalars(query)).all())


async def find_asked_route(s: AsyncSession, burst_id: int) -> RouteDecision | None:
    """The decision that asked about `burst_id`, if any (the newest one)."""
    query = (
        select(RouteDecision)
        .where(RouteDecision.burst_id == burst_id, RouteDecision.asked.is_(True))
        .order_by(RouteDecision.id.desc())
        .limit(1)
    )
    return await s.scalar(query)


async def set_route_project(s: AsyncSession, route_id: int, *, project_id: str) -> int:
    """The owner answered the question: the asked decision now names its project."""
    statement = update(RouteDecision).where(RouteDecision.id == route_id).values(project_id=project_id)
    return await bulk.affected_rows(s, statement)


async def set_route_corrected(s: AsyncSession, route_id: int, *, corrected_to: str) -> int:
    statement = update(RouteDecision).where(RouteDecision.id == route_id).values(corrected_to=corrected_to)
    return await bulk.affected_rows(s, statement)


async def delete_route_decisions_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    due = select(RouteDecision.id).where(RouteDecision.at < cutoff).order_by(RouteDecision.at).limit(limit)
    statement = delete(RouteDecision).where(RouteDecision.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)


# --- clarifications ---------------------------------------------------------------------------------------------------
async def add_clarification(
    s: AsyncSession, *, burst_id: int | None, kind: str, candidates: JsonValue, status: str, asked_at: datetime
) -> Clarification:
    row = Clarification(
        burst_id=burst_id,
        kind=kind,
        candidates_json=candidates,
        status=status,
        message_id=None,
        asked_at=asked_at,
        resolved_at=None,
    )
    s.add(row)
    await s.flush()
    return row


async def list_clarifications_with_status(s: AsyncSession, status: str) -> list[Clarification]:
    """Oldest first (IX status)."""
    query = (
        select(Clarification).where(Clarification.status == status).order_by(Clarification.asked_at, Clarification.id)
    )
    return list((await s.scalars(query)).all())


async def get_clarification(s: AsyncSession, clarification_id: int) -> Clarification | None:
    return await s.get(Clarification, clarification_id)


async def set_clarification_status(
    s: AsyncSession, clarification_id: int, *, from_status: str, to_status: str, at: datetime
) -> int:
    """Move one clarification still in `from_status`; 0 when another pass already moved it."""
    statement = (
        update(Clarification)
        .where(Clarification.id == clarification_id, Clarification.status == from_status)
        .values(status=to_status, resolved_at=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def set_clarification_message(s: AsyncSession, clarification_id: int, *, message_id: int) -> int:
    statement = update(Clarification).where(Clarification.id == clarification_id).values(message_id=message_id)
    return await bulk.affected_rows(s, statement)


async def delete_clarifications_before(
    s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int
) -> int:
    due = (
        select(Clarification.id)
        .where(Clarification.asked_at < cutoff, Clarification.status.in_(list(statuses)))
        .order_by(Clarification.asked_at)
        .limit(limit)
    )
    statement = delete(Clarification).where(Clarification.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
