"""SQL for `agent_sessions`, `turns`, `deliveries` and `turn_progress`. Never commits; the service's unit of work does.

Status changes are conditional UPDATEs (`… WHERE status IN (from)`): a stop, the event pump and the scheduler may all
reach for the same turn, and only the one whose precondition still holds moves it.
"""

from collections.abc import Collection, Mapping
from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.dev.turns.models import AgentSession, Delivery, Turn, TurnProgress
from yordamchi.persistence import bulk
from yordamchi.persistence.upsert import insert_or_ignore

type Values = Mapping[str, object]


# --- agent_sessions -------------------------------------------------------------------------------------------------
async def get_agent_session(s: AsyncSession, session_row_id: int) -> AgentSession | None:
    return await s.get(AgentSession, session_row_id)


async def get_current_session(s: AsyncSession, *, project_id: str, executor: str, kind: str) -> AgentSession | None:
    query = select(AgentSession).where(
        AgentSession.project_id == project_id,
        AgentSession.executor == executor,
        AgentSession.kind == kind,
        AgentSession.is_current.is_(True),
    )
    return await s.scalar(query)


async def list_current_sessions(s: AsyncSession, *, statuses: Collection[str]) -> list[AgentSession]:
    query = (
        select(AgentSession)
        .where(AgentSession.is_current.is_(True), AgentSession.status.in_(list(statuses)))
        .order_by(AgentSession.id)
    )
    return list((await s.scalars(query)).all())


async def add_agent_session(
    s: AsyncSession,
    *,
    project_id: str,
    executor: str,
    kind: str,
    session_id: str,
    status: str,
    policy: str,
    model: str,
    at: datetime,
) -> AgentSession:
    row = AgentSession(
        project_id=project_id,
        executor=executor,
        kind=kind,
        session_id=session_id,
        status=status,
        is_current=True,
        policy=policy,
        model=model,
        prompt_version_seen=0,
        mac_attached_at=None,
        created_at=at,
        last_active_at=at,
        closed_at=None,
    )
    s.add(row)
    await s.flush()
    return row


async def set_agent_session(s: AsyncSession, session_row_id: int, **values: object) -> None:
    statement = update(AgentSession).where(AgentSession.id == session_row_id).values(**values)
    await s.execute(statement)


async def retire_agent_session(s: AsyncSession, session_row_id: int, *, status: str, at: datetime) -> None:
    """The row stops being current: a new session replaces it (lost transcript, "yangi sessiya")."""
    statement = (
        update(AgentSession)
        .where(AgentSession.id == session_row_id)
        .values(status=status, is_current=False, closed_at=at)
    )
    await s.execute(statement)


async def set_sessions_status(s: AsyncSession, *, from_status: str, to_status: str) -> int:
    statement = (
        update(AgentSession)
        .where(AgentSession.status == from_status)
        .values(status=to_status)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


# --- turns ----------------------------------------------------------------------------------------------------------
async def add_turn(
    s: AsyncSession, *, project_id: str, kind: str, status: str, title: str, burst_id: int | None, at: datetime
) -> Turn:
    row = Turn(
        project_id=project_id,
        agent_session_id=None,
        kind=kind,
        status=status,
        queue_reason=None,
        title=title,
        burst_id=burst_id,
        queued_at=at,
        steps=0,
        cost_usd_micros=0,
    )
    s.add(row)
    await s.flush()
    return row


async def get_turn(s: AsyncSession, turn_id: int) -> Turn | None:
    return await s.get(Turn, turn_id)


async def list_turns_with_status(s: AsyncSession, statuses: Collection[str], *, limit: int = 500) -> list[Turn]:
    """Oldest queued first (IX status, queued_at)."""
    query = select(Turn).where(Turn.status.in_(list(statuses))).order_by(Turn.queued_at, Turn.id).limit(limit)
    return list((await s.scalars(query)).all())


async def list_project_turns(
    s: AsyncSession, project_id: str, *, statuses: Collection[str] | None = None, limit: int = 50
) -> list[Turn]:
    """The project's turns, newest first (IX project_id, status)."""
    query = select(Turn).where(Turn.project_id == project_id)
    if statuses is not None:
        query = query.where(Turn.status.in_(list(statuses)))
    query = query.order_by(Turn.queued_at.desc(), Turn.id.desc()).limit(limit)
    return list((await s.scalars(query)).all())


async def find_turn_by_burst(s: AsyncSession, burst_id: int) -> Turn | None:
    """The turn a burst was delivered into (as its first input or as a follow-up)."""
    query = (
        select(Turn)
        .join(Delivery, Delivery.turn_id == Turn.id)
        .where(Delivery.burst_id == burst_id)
        .order_by(Turn.id.desc())
        .limit(1)
    )
    return await s.scalar(query)


async def move_turn(
    s: AsyncSession, turn_id: int, *, from_statuses: Collection[str], to_status: str, **values: object
) -> bool:
    """`status: from → to` (plus `values`) only if the turn is still in one of `from_statuses`."""
    statement = (
        update(Turn)
        .where(Turn.id == turn_id, Turn.status.in_(list(from_statuses)))
        .values(status=to_status, **values)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def set_turn(s: AsyncSession, turn_id: int, **values: object) -> None:
    statement = update(Turn).where(Turn.id == turn_id).values(**values).execution_options(synchronize_session=False)
    await s.execute(statement)


async def add_turn_cost(s: AsyncSession, turn_id: int, *, cost_usd_micros: int, duration_ms: int) -> None:
    statement = (
        update(Turn)
        .where(Turn.id == turn_id)
        .values(
            cost_usd_micros=Turn.cost_usd_micros + cost_usd_micros,
            duration_ms=func.coalesce(Turn.duration_ms, 0) + duration_ms,
        )
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def set_turns_status(
    s: AsyncSession, *, from_statuses: Collection[str], to_status: str, **values: object
) -> list[int]:
    """Bulk move, returning the ids that moved (the recovery pass)."""
    ids = list((await s.scalars(select(Turn.id).where(Turn.status.in_(list(from_statuses))).order_by(Turn.id))).all())
    if ids:
        statement = (
            update(Turn)
            .where(Turn.id.in_(ids))
            .values(status=to_status, **values)
            .execution_options(synchronize_session=False)
        )
        await s.execute(statement)
    return ids


async def find_latest_handoff(s: AsyncSession, project_id: str) -> Turn | None:
    query = (
        select(Turn)
        .where(Turn.project_id == project_id, Turn.handoff_json.is_not(None))
        .order_by(Turn.ended_at.desc(), Turn.id.desc())
        .limit(1)
    )
    return await s.scalar(query)


async def move_turn_revert(s: AsyncSession, turn_id: int, *, from_status: str, to_status: str) -> bool:
    """Claim a revert offer: only the first answer moves it (two "ha"s never revert twice)."""
    statement = (
        update(Turn)
        .where(Turn.id == turn_id, Turn.revert_status == from_status)
        .values(revert_status=to_status)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def list_turns_with_revert(s: AsyncSession, status: str) -> list[Turn]:
    query = select(Turn).where(Turn.revert_status == status).order_by(Turn.id)
    return list((await s.scalars(query)).all())


async def delete_turns_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Delete up to `limit` finished turns queued before `cutoff`; their deliveries and progress go with them."""
    due = (
        select(Turn.id)
        .where(Turn.queued_at < cutoff, Turn.status.in_(list(statuses)))
        .order_by(Turn.queued_at)
        .limit(limit)
    )
    statement = delete(Turn).where(Turn.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)


# --- deliveries -----------------------------------------------------------------------------------------------------
async def add_delivery(
    s: AsyncSession,
    *,
    turn_id: int,
    mode: str,
    source: str,
    text: str,
    media: JsonValue,
    burst_id: int | None,
    at: datetime,
) -> Delivery | None:
    """The next `seq` of the turn, written with its idempotency key `(turn_id, seq)`. None if that key was taken."""
    last = await s.scalar(select(func.max(Delivery.seq)).where(Delivery.turn_id == turn_id))
    seq = (last or 0) + 1
    inserted = await insert_or_ignore(
        s,
        Delivery,
        {
            "turn_id": turn_id,
            "seq": seq,
            "mode": mode,
            "source": source,
            "text": text,
            "media_json": media,
            "status": "pending",
            "burst_id": burst_id,
            "created_at": at,
            "sent_at": None,
        },
        conflict_columns=("turn_id", "seq"),
    )
    if not inserted:
        return None
    return await s.scalar(select(Delivery).where(Delivery.turn_id == turn_id, Delivery.seq == seq))


async def get_delivery(s: AsyncSession, delivery_id: int) -> Delivery | None:
    return await s.get(Delivery, delivery_id)


async def list_deliveries(s: AsyncSession, turn_id: int, *, statuses: Collection[str] | None = None) -> list[Delivery]:
    """In `seq` order."""
    query = select(Delivery).where(Delivery.turn_id == turn_id)
    if statuses is not None:
        query = query.where(Delivery.status.in_(list(statuses)))
    return list((await s.scalars(query.order_by(Delivery.seq))).all())


async def list_deliveries_with_status(s: AsyncSession, status: str, *, limit: int = 500) -> list[Delivery]:
    query = select(Delivery).where(Delivery.status == status).order_by(Delivery.id).limit(limit)
    return list((await s.scalars(query)).all())


async def find_delivery_by_burst(s: AsyncSession, burst_id: int) -> Delivery | None:
    return await s.scalar(select(Delivery).where(Delivery.burst_id == burst_id).order_by(Delivery.id).limit(1))


async def count_deliveries(s: AsyncSession, turn_id: int, *, mode: str) -> int:
    query = select(func.count()).select_from(Delivery).where(Delivery.turn_id == turn_id, Delivery.mode == mode)
    return int(await s.scalar(query) or 0)


async def move_delivery(
    s: AsyncSession, delivery_id: int, *, from_status: str, to_status: str, sent_at: datetime | None = None
) -> bool:
    values: dict[str, object] = {"status": to_status}
    if sent_at is not None:
        values["sent_at"] = sent_at
    statement = (
        update(Delivery)
        .where(Delivery.id == delivery_id, Delivery.status == from_status)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def move_turn_deliveries(s: AsyncSession, turn_id: int, *, from_status: str, to_status: str) -> int:
    statement = (
        update(Delivery)
        .where(Delivery.turn_id == turn_id, Delivery.status == from_status)
        .values(status=to_status)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def reassign_deliveries(s: AsyncSession, delivery_ids: Collection[int], *, turn_id: int) -> None:
    """Pending deliveries of an interrupted turn move to the turn that will deliver them, keeping their order."""
    last = await s.scalar(select(func.max(Delivery.seq)).where(Delivery.turn_id == turn_id)) or 0
    for offset, delivery_id in enumerate(sorted(delivery_ids), start=1):
        statement = (
            update(Delivery)
            .where(Delivery.id == delivery_id)
            .values(turn_id=turn_id, seq=last + offset)
            .execution_options(synchronize_session=False)
        )
        await s.execute(statement)


async def clear_delivery_texts_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    """Null the text of up to `limit` deliveries that are no longer waiting and were created before `cutoff`."""
    due = (
        select(Delivery.id)
        .where(Delivery.created_at < cutoff, Delivery.text.is_not(None), Delivery.status != "pending")
        .order_by(Delivery.created_at)
        .limit(limit)
    )
    statement = (
        update(Delivery).where(Delivery.id.in_(due)).values(text=None).execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


# --- turn_progress --------------------------------------------------------------------------------------------------
async def add_progress(s: AsyncSession, *, turn_id: int, at: datetime, kind: str, summary: str, keep: int) -> None:
    """One step, then only the newest `keep` steps of the turn are kept (SPEC §4.2: last 50 per turn)."""
    s.add(TurnProgress(turn_id=turn_id, at=at, kind=kind, summary=summary))
    await s.flush()
    oldest_kept = (
        select(TurnProgress.id)
        .where(TurnProgress.turn_id == turn_id)
        .order_by(TurnProgress.id.desc())
        .offset(keep - 1)
        .limit(1)
        .scalar_subquery()
    )
    statement = (
        delete(TurnProgress)
        .where(TurnProgress.turn_id == turn_id, TurnProgress.id < oldest_kept)
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def list_progress(s: AsyncSession, turn_id: int, *, limit: int) -> list[TurnProgress]:
    """The newest `limit` steps, oldest first."""
    query = select(TurnProgress).where(TurnProgress.turn_id == turn_id).order_by(TurnProgress.id.desc()).limit(limit)
    return list(reversed((await s.scalars(query)).all()))


async def delete_progress_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    due = select(TurnProgress.id).where(TurnProgress.at < cutoff).order_by(TurnProgress.at).limit(limit)
    statement = delete(TurnProgress).where(TurnProgress.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
