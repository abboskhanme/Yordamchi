"""SQL for `questions`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.core.json_value import JsonValue
from yordamchi.dev.questions.models import Question
from yordamchi.persistence import bulk
from yordamchi.persistence.upsert import insert_or_ignore


async def add_question(
    s: AsyncSession,
    *,
    project_id: str,
    turn_id: int,
    tool_use_id: str,
    questions: JsonValue,
    status: str,
    asked_at: datetime,
) -> int | None:
    """Write the question keyed by `(turn_id, tool_use_id)`. Its new id, or None if it was there already."""
    inserted = await insert_or_ignore(
        s,
        Question,
        {
            "project_id": project_id,
            "turn_id": turn_id,
            "tool_use_id": tool_use_id,
            "questions_json": questions,
            "status": status,
            "answer_json": None,
            "answer_mode": None,
            "asked_at": asked_at,
            "answered_at": None,
        },
        conflict_columns=("turn_id", "tool_use_id"),
    )
    if not inserted:
        return None
    return await s.scalar(select(Question.id).where(Question.turn_id == turn_id, Question.tool_use_id == tool_use_id))


async def get_question(s: AsyncSession, question_id: int) -> Question | None:
    return await s.get(Question, question_id)


async def list_questions_with_status(s: AsyncSession, status: str, *, limit: int = 200) -> list[Question]:
    """Oldest first (IX status)."""
    query = select(Question).where(Question.status == status).order_by(Question.asked_at, Question.id).limit(limit)
    return list((await s.scalars(query)).all())


async def list_undelivered_answers(s: AsyncSession, status: str, *, limit: int = 200) -> list[Question]:
    """Answered rows whose answer has not reached the agent yet (`answer_mode` still empty), oldest first."""
    query = (
        select(Question)
        .where(Question.status == status, Question.answer_mode.is_(None))
        .order_by(Question.answered_at, Question.id)
        .limit(limit)
    )
    return list((await s.scalars(query)).all())


async def mark_question_answered(
    s: AsyncSession, question_id: int, *, from_status: str, to_status: str, answer: JsonValue, at: datetime
) -> bool:
    """`from_status` → `to_status` with the answer, only if it is still in `from_status` (one row: single answer)."""
    statement = (
        update(Question)
        .where(Question.id == question_id, Question.status == from_status)
        .values(status=to_status, answer_json=answer, answered_at=at)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement) == 1


async def set_answer_mode(s: AsyncSession, question_id: int, *, mode: str) -> None:
    statement = update(Question).where(Question.id == question_id).values(answer_mode=mode)
    await s.execute(statement)


async def set_questions_status(
    s: AsyncSession, question_ids: Collection[int], *, from_status: str, to_status: str
) -> int:
    """Move the rows still in `from_status`; rows in any other status are left alone. Returns the count."""
    statement = (
        update(Question)
        .where(Question.id.in_(list(question_ids)), Question.status == from_status)
        .values(status=to_status)
        .execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def delete_questions_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Delete up to `limit` rows in `statuses` asked before `cutoff`."""
    due = (
        select(Question.id)
        .where(Question.asked_at < cutoff, Question.status.in_(list(statuses)))
        .order_by(Question.asked_at)
        .limit(limit)
    )
    statement = delete(Question).where(Question.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
