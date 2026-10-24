"""SQL for `decisions`. Never commits; the service's unit of work does.

Status changes are conditional UPDATEs (`… WHERE status = <expected>`), so two racing callers can never both
approve, consume or expire the same row: the one that changed it gets `True`, the other `False` (SPEC §4.6).
"""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.dev.gate.models import Decision
from yordamchi.persistence import bulk


async def add_decision(
    s: AsyncSession,
    *,
    kind: str,
    project_id: str | None,
    turn_id: int | None,
    gate_class: str | None,
    tool_name: str | None,
    input_sha256: str | None,
    input_redacted: str | None,
    target: str | None,
    payload: JsonValue,
    created_at: datetime,
    expires_at: datetime,
) -> Decision:
    """A new `pending` decision."""
    row = Decision(
        kind=kind,
        project_id=project_id,
        turn_id=turn_id,
        gate_class=gate_class,
        tool_name=tool_name,
        input_sha256=input_sha256,
        input_redacted=input_redacted,
        target=target,
        payload_json=payload,
        status="pending",
        created_at=created_at,
        expires_at=expires_at,
    )
    s.add(row)
    await s.flush()
    return row


async def get_decision(s: AsyncSession, decision_id: int) -> Decision | None:
    return await s.get(Decision, decision_id)


async def list_decisions_for_action(
    s: AsyncSession, *, project_id: str, tool_name: str, input_sha256: str, statuses: Collection[str]
) -> list[Decision]:
    """The decisions about one exact action (IX input_sha256, status), oldest first."""
    query = (
        select(Decision)
        .where(
            Decision.input_sha256 == input_sha256,
            Decision.status.in_(list(statuses)),
            Decision.project_id == project_id,
            Decision.tool_name == tool_name,
        )
        .order_by(Decision.created_at, Decision.id)
    )
    return list((await s.scalars(query)).all())


async def list_decisions_with_status(s: AsyncSession, statuses: Collection[str], *, limit: int) -> list[Decision]:
    """Oldest first (IX status)."""
    query = select(Decision).where(Decision.status.in_(list(statuses))).order_by(Decision.id).limit(limit)
    return list((await s.scalars(query)).all())


async def list_recent_decisions(s: AsyncSession, *, limit: int) -> list[Decision]:
    """The newest `limit` decisions, newest first."""
    query = select(Decision).order_by(Decision.id.desc()).limit(limit)
    return list((await s.scalars(query)).all())


async def list_consumed_grants(s: AsyncSession, *, project_id: str, since: datetime) -> list[Decision]:
    """Grants of this project consumed at or after `since` (IX project_id, status)."""
    query = (
        select(Decision)
        .where(Decision.project_id == project_id, Decision.status == "consumed", Decision.consumed_at >= since)
        .order_by(Decision.consumed_at)
    )
    return list((await s.scalars(query)).all())


async def set_decisions_expired(s: AsyncSession, decision_ids: Collection[int], *, at: datetime) -> int:
    """`pending` or unused `approved` → `expired`. Returns how many changed."""
    if not decision_ids:
        return 0
    statement = (
        update(Decision)
        .where(
            Decision.id.in_(list(decision_ids)),
            Decision.status.in_(("pending", "approved")),
            Decision.consumed_at.is_(None),
        )
        .values(status="expired", decided_at=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def mark_decision_resolved(
    s: AsyncSession,
    decision_id: int,
    *,
    status: str,
    at: datetime,
    update_id: int | None,
    grant_expires_at: datetime | None = None,
    from_status: str = "pending",
) -> bool:
    """The owner's answer: `pending` → `approved` / `denied`, or an unused `approved` grant → `cancelled`. False if
    the row had left `from_status` (or, for a grant, was consumed) meanwhile."""
    statement = (
        update(Decision)
        .where(Decision.id == decision_id, Decision.status == from_status, Decision.consumed_at.is_(None))
        .values(status=status, decided_at=at, decided_update_id=update_id, grant_expires_at=grant_expires_at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def mark_decision_consumed(s: AsyncSession, decision_id: int, *, at: datetime, payload: JsonValue) -> bool:
    """`approved` → `consumed`, exactly once (`… WHERE consumed_at IS NULL`). False if another call got it first."""
    statement = (
        update(Decision)
        .where(Decision.id == decision_id, Decision.status == "approved", Decision.consumed_at.is_(None))
        .values(status="consumed", consumed_at=at, payload_json=payload)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def set_decision_payload(s: AsyncSession, decision_id: int, payload: JsonValue) -> None:
    statement = update(Decision).where(Decision.id == decision_id).values(payload_json=payload)
    await s.execute(statement.execution_options(synchronize_session=False))


async def set_superseded_by(s: AsyncSession, decision_id: int, newer_id: int) -> None:
    statement = update(Decision).where(Decision.id == decision_id).values(superseded_by=newer_id)
    await s.execute(statement.execution_options(synchronize_session=False))


async def delete_decisions_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Delete up to `limit` rows in `statuses` created before `cutoff`, oldest first."""
    due = (
        select(Decision.id)
        .where(Decision.created_at < cutoff, Decision.status.in_(list(statuses)))
        .order_by(Decision.created_at)
        .limit(limit)
    )
    statement = delete(Decision).where(Decision.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
