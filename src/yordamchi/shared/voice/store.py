"""SQL for `transcripts`. Never commits; the service's unit of work does."""

from collections.abc import Collection
from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from yordamchi.persistence import bulk
from yordamchi.persistence.upsert import insert_or_ignore
from yordamchi.shared.voice.models import Transcript


async def add_transcript(s: AsyncSession, *, bot: str, update_id: int, status: str, at: datetime) -> bool:
    """Write the row of a note whose job starts now. False if the note already has one (a re-run after a restart)."""
    return await insert_or_ignore(
        s,
        Transcript,
        {"bot": bot, "update_id": update_id, "status": status, "created_at": at},
        conflict_columns=("bot", "update_id"),
    )


async def get_transcript(s: AsyncSession, *, bot: str, update_id: int) -> Transcript | None:
    query = select(Transcript).where(Transcript.bot == bot, Transcript.update_id == update_id)
    return await s.scalar(query)


async def list_transcripts(s: AsyncSession, bot: str) -> list[Transcript]:
    """Every row of one bot in `update_id` order."""
    query = select(Transcript).where(Transcript.bot == bot).order_by(Transcript.update_id)
    return list((await s.scalars(query)).all())


async def set_transcript_result(
    s: AsyncSession,
    *,
    bot: str,
    update_id: int,
    status: str,
    text: str | None,
    provider: str | None,
    provider_path: str | None,
    model: str | None,
    audio_ms: int | None,
    latency_ms: int,
    error_code: str | None,
    at: datetime,
) -> None:
    statement = (
        update(Transcript)
        .where(Transcript.bot == bot, Transcript.update_id == update_id)
        .values(
            status=status,
            text=text,
            provider=provider,
            provider_path=provider_path,
            model=model,
            audio_ms=audio_ms,
            latency_ms=latency_ms,
            error_code=error_code,
            done_at=at,
        )
        .execution_options(synchronize_session=False)
    )
    await s.execute(statement)


async def clear_texts_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Null the text of up to `limit` rows in `statuses` created before `cutoff`."""
    due = (
        select(Transcript.id)
        .where(
            Transcript.created_at < cutoff,
            Transcript.status.in_(list(statuses)),
            Transcript.text.is_not(None),
        )
        .order_by(Transcript.created_at)
        .limit(limit)
    )
    statement = (
        update(Transcript).where(Transcript.id.in_(due)).values(text=None).execution_options(synchronize_session=False)
    )
    return await bulk.affected_rows(s, statement)


async def delete_transcripts_before(s: AsyncSession, cutoff: datetime, *, statuses: Collection[str], limit: int) -> int:
    """Delete up to `limit` rows in `statuses` created before `cutoff`."""
    due = (
        select(Transcript.id)
        .where(Transcript.created_at < cutoff, Transcript.status.in_(list(statuses)))
        .order_by(Transcript.created_at)
        .limit(limit)
    )
    statement = delete(Transcript).where(Transcript.id.in_(due)).execution_options(synchronize_session=False)
    return await bulk.affected_rows(s, statement)
