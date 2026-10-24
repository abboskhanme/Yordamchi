"""Tables of `personal/qa` (SPEC §4.2; UC-30). Written only by the personal bot, in `personal.db`.

- `assistant_sessions`: one row per conversation. `session_id` is the conversation key the Claude adapter uses as the
  CLI session id (a UUID chosen here), so a restart resumes it. `summary` (✱, redacted) is the rolling five-line
  digest of its last exchanges, which seeds the next conversation after an idle or web-search rotation.
  `websearch_count` counts toward `qa.web_search_rotate_at`. Kept 90 days after it closes.
- `answers`: one row per answer sent. `text_full` (✱) is the complete answer when the sent one was shortened
  ("to'liqroq" sends it); it is cleared after 30 days, the row after 400. `fallback` marks a "zaxira model" answer.

Beyond SPEC §4.2: `assistant_sessions.chat_id` (one conversation per chat: the DM and the optional group) and
`answers_count` (a session resumes only once it has answered), `answers.chat_id`, `provider` and `model` (the owner
review of T78 reads which model answered), and the `pending` voice status until the voice step reports. `burst_id` is a
plain integer: bursts are pruned after 30 days, answers live on.
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.persistence.base import Base


class AssistantSession(Base):
    __tablename__ = "assistant_sessions"
    __table_args__ = (Index("ix_assistant_sessions_chat_id_status", "chat_id", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    session_id: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[str] = mapped_column(String(16))  # live | closed
    summary: Mapped[str | None] = mapped_column(Text)
    seed: Mapped[str | None] = mapped_column(Text)  # the previous conversation's summary, given on its first ask
    prompt_version_seen: Mapped[int] = mapped_column(Integer)
    websearch_count: Mapped[int] = mapped_column(Integer)
    answers_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime]
    last_active_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
    close_reason: Mapped[str | None] = mapped_column(String(16))  # idle | web_searches | owner


class Answer(Base):
    __tablename__ = "answers"
    __table_args__ = (Index("ix_answers_at", "at"), Index("ix_answers_chat_id_at", "chat_id", "at"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    assistant_session_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("assistant_sessions.id"))
    chat_id: Mapped[int] = mapped_column(BigInteger)
    burst_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    text_full: Mapped[str | None] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(String(32))  # claude | google | openrouter | bridge
    model: Mapped[str] = mapped_column(String(96))
    voice_status: Mapped[str] = mapped_column(String(16))  # pending | sent | skipped | failed | off
    voice_error_code: Mapped[str | None] = mapped_column(String(64))
    first_text_ms: Mapped[int | None] = mapped_column(Integer)
    total_ms: Mapped[int] = mapped_column(Integer)
    voice_ms: Mapped[int | None] = mapped_column(Integer)
    fallback: Mapped[bool] = mapped_column(Boolean)
    expanded_at: Mapped[datetime | None]  # "to'liqroq" sent the full version
    at: Mapped[datetime]
