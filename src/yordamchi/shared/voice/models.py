"""`transcripts`: one row per voice note sent to STT (SPEC §4.2; UC-05).

Keyed by the note's `(bot, update_id)`, the same natural key as `inbound_updates` and the burst parts, so a job that
a restart re-runs finds its row (no FK: the inbox and the burst each delete their own rows on their own schedule).
A row is `pending` while the job runs, then `done` with the transcript or `failed` with `error_code`: an `ErrorCode`
value (`quota_exhausted`, …) or a voice reason (`stt_no_key`, …). `text` is scrubbed before the write (✱). The text
is nulled after 7 days and the row deleted after 30; a `pending` row is never touched.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.persistence.base import Base


class Transcript(Base):
    __tablename__ = "transcripts"
    __table_args__ = (UniqueConstraint("bot", "update_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot: Mapped[str] = mapped_column(String(16))
    update_id: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(16))  # pending | done | failed
    text: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(String(16))  # google | openrouter: answered, or failed last
    provider_path: Mapped[str | None] = mapped_column(String(32))  # interactions | chat_completions | …
    model: Mapped[str | None] = mapped_column(String(96))
    audio_ms: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)  # the whole job: download, STT and any fallback
    error_code: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime]
    done_at: Mapped[datetime | None]
