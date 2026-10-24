"""SQL for `assistant_sessions` and `answers`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.persistence import bulk
from yordamchi.personal.qa.models import Answer, AssistantSession

LIVE = "live"
CLOSED = "closed"


async def find_live_session(s: AsyncSession, chat_id: int) -> AssistantSession | None:
    statement = (
        select(AssistantSession)
        .where(AssistantSession.chat_id == chat_id, AssistantSession.status == LIVE)
        .order_by(AssistantSession.id.desc())
        .limit(1)
    )
    return (await s.scalars(statement)).first()


async def add_session(
    s: AsyncSession, *, chat_id: int, session_id: str, at: datetime, seed: str | None, prompt_version: int
) -> AssistantSession:
    row = AssistantSession(
        chat_id=chat_id,
        session_id=session_id,
        status=LIVE,
        summary=None,
        seed=seed,
        prompt_version_seen=prompt_version,
        websearch_count=0,
        answers_count=0,
        created_at=at,
        last_active_at=at,
    )
    s.add(row)
    await s.flush()
    return row


async def mark_session_closed(s: AsyncSession, row_id: int, *, at: datetime, reason: str) -> None:
    statement = (
        update(AssistantSession)
        .where(AssistantSession.id == row_id, AssistantSession.status == LIVE)
        .values(status=CLOSED, closed_at=at, close_reason=reason)
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def mark_session_answered(
    s: AsyncSession, row_id: int, *, at: datetime, web_searches: int, prompt_version: int, summary: str
) -> None:
    statement = (
        update(AssistantSession)
        .where(AssistantSession.id == row_id)
        .values(
            last_active_at=at,
            websearch_count=AssistantSession.websearch_count + web_searches,
            answers_count=AssistantSession.answers_count + 1,
            prompt_version_seen=prompt_version,
            summary=summary,
        )
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def add_answer(
    s: AsyncSession,
    *,
    session_row_id: int | None,
    chat_id: int,
    burst_id: int | None,
    text_full: str | None,
    provider: str,
    model: str,
    voice_status: str,
    first_text_ms: int | None,
    total_ms: int,
    fallback: bool,
    at: datetime,
) -> Answer:
    row = Answer(
        assistant_session_id=session_row_id,
        chat_id=chat_id,
        burst_id=burst_id,
        text_full=text_full,
        provider=provider,
        model=model,
        voice_status=voice_status,
        first_text_ms=first_text_ms,
        total_ms=total_ms,
        fallback=fallback,
        at=at,
    )
    s.add(row)
    await s.flush()
    return row


async def find_answer_by_burst(s: AsyncSession, burst_id: int) -> Answer | None:
    return (await s.scalars(select(Answer).where(Answer.burst_id == burst_id))).first()


async def find_latest_answer(s: AsyncSession, chat_id: int) -> Answer | None:
    statement = select(Answer).where(Answer.chat_id == chat_id).order_by(Answer.id.desc()).limit(1)
    return (await s.scalars(statement)).first()


async def mark_answer_expanded(s: AsyncSession, answer_id: int, *, at: datetime) -> None:
    statement = (
        update(Answer).where(Answer.id == answer_id).values(expanded_at=at).execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def set_answer_voice(
    s: AsyncSession, answer_id: int, *, status: str, error_code: str | None, voice_ms: int | None
) -> None:
    statement = (
        update(Answer)
        .where(Answer.id == answer_id)
        .values(voice_status=status, voice_error_code=error_code, voice_ms=voice_ms)
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def clear_answer_texts_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    due = select(Answer.id).where(Answer.at < cutoff, Answer.text_full.is_not(None)).order_by(Answer.at).limit(limit)
    statement = (
        update(Answer).where(Answer.id.in_(due)).values(text_full=None).execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def delete_answers_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    due = select(Answer.id).where(Answer.at < cutoff).order_by(Answer.at).limit(limit)
    statement = delete(Answer).where(Answer.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)


async def clear_session_texts_before(s: AsyncSession, cutoff: datetime, *, limit: int) -> int:
    """Null the summary and seed of conversations closed before `cutoff` (the row stays while answers point at it)."""
    due = (
        select(AssistantSession.id)
        .where(
            AssistantSession.status == CLOSED,
            AssistantSession.closed_at < cutoff,
            (AssistantSession.summary.is_not(None)) | (AssistantSession.seed.is_not(None)),
        )
        .limit(limit)
    )
    statement = update(AssistantSession).where(AssistantSession.id.in_(due)).values(summary=None, seed=None)
    return await bulk.affected_rows(s, statement.execution_options(synchronize_session=False))


async def delete_sessions_closed_before(
    s: AsyncSession, cutoff: datetime, *, statuses: Collection[str] = (CLOSED,), limit: int
) -> int:
    """Closed conversations past retention that no answer row points at any more."""
    referenced = select(Answer.assistant_session_id).where(Answer.assistant_session_id.is_not(None))
    due = (
        select(AssistantSession.id)
        .where(
            AssistantSession.status.in_(list(statuses)),
            AssistantSession.closed_at < cutoff,
            AssistantSession.id.not_in(referenced),
        )
        .limit(limit)
    )
    statement = (
        delete(AssistantSession).where(AssistantSession.id.in_(due)).execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)
