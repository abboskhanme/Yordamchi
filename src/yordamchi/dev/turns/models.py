"""Tables of `dev/turns` (SPEC §4.2, §4.6).

- `agent_sessions`: one executor session per (project, executor, kind); exactly one row of each is current
  (UNIQUE … WHERE is_current). `session_id` is the executor's id, so a session closed when idle or after a crash
  resumes by it; `prompt_version_seen` is the settings version of its last delivery (UC-09). Kept forever.
- `turns`: one routed instruction of a project, from `queued` to `done | failed | interrupted | cancelled`. It holds
  the git facts the result and the wrong-target revert need: `start_head`, whether the tree was dirty at the start
  (`start_dirty`, the gate's `dirty_before_turn`) and which files were (`start_files_json`: tracked files with
  uncommitted changes, untracked files; so "files the turn created" is exact and the owner's own edits are never
  reverted), then `end_head`, the commits, the changed files and the push. `handoff_json` is the turn's handoff
  note for `dev/digest`. Deleted 400 days after it was queued, once finished.
- `deliveries`: each input written into the session, keyed UNIQUE(turn_id, seq). `status=sending` is written before
  the port call, so after a crash an uncertain delivery is never resent (SPEC §2.7, NFR-REL-05). `text` is the
  redacted instruction (nulled after 30 days); `media_json` holds image paths and Telegram message ids only.
- `turn_progress`: the latest steps of a turn (at most 50 kept), for the progress line and the handoff note. 7 days.

`burst_id` is `ON DELETE SET NULL`: bursts are deleted after 30 days, turns live 400 (PENDING_WIRING T16).
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String, Text, UniqueConstraint, literal_column
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class AgentSession(Base):
    __tablename__ = "agent_sessions"
    __table_args__ = (Index("ix_agent_sessions_session_id", "session_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"))
    executor: Mapped[str] = mapped_column(String(16))  # claude | codex
    kind: Mapped[str] = mapped_column(String(16))  # coding | investigation | smoke
    session_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # idle | live | parked | closed | lost
    is_current: Mapped[bool] = mapped_column(Boolean)
    policy: Mapped[str] = mapped_column(String(16))  # FULL | READ_ONLY | PLAN_ONLY | SELF_UPDATE
    model: Mapped[str] = mapped_column(String(32))
    prompt_version_seen: Mapped[int] = mapped_column(Integer)
    mac_attached_at: Mapped[datetime | None]
    created_at: Mapped[datetime]
    last_active_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]


# UNIQUE(project_id, executor, kind) WHERE is_current: "X yangi sessiya" retires the old row, never edits it. The
# bare boolean column is the condition both SQLite and PostgreSQL accept (D1).
Index(
    "uq_agent_sessions_current",
    AgentSession.project_id,
    AgentSession.executor,
    AgentSession.kind,
    unique=True,
    sqlite_where=literal_column("is_current"),
    postgresql_where=literal_column("is_current"),
)


class Turn(Base):
    __tablename__ = "turns"
    __table_args__ = (
        Index("ix_turns_project_id_status", "project_id", "status"),
        Index("ix_turns_status_queued_at", "status", "queued_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"))
    agent_session_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("agent_sessions.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16))  # instruct | investigate | plan | build | selfupdate | smoke
    status: Mapped[str] = mapped_column(String(24))  # queued | starting | running | waiting_* | … | done
    queue_reason: Mapped[str | None] = mapped_column(String(16))  # cap | pressure | mac_attached | … | busy
    title: Mapped[str] = mapped_column(String(120))
    burst_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bursts.id", ondelete="SET NULL"), index=True)
    queued_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    start_head: Mapped[str | None] = mapped_column(String(64))
    start_dirty: Mapped[bool | None] = mapped_column(Boolean)
    start_files_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=True)  # {dirty: [...], untracked: [...]}
    end_head: Mapped[str | None] = mapped_column(String(64))
    natija: Mapped[str | None] = mapped_column(Text)
    tests_status: Mapped[str | None] = mapped_column(String(16))  # passed | failed | not_run | unknown
    commits_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=True)  # [sha], newest first
    pushed: Mapped[bool | None] = mapped_column(Boolean)
    changed_files_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=True)  # [paths]
    steps: Mapped[int] = mapped_column(Integer)
    cost_usd_micros: Mapped[int] = mapped_column(BigInteger)
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    progress_message_id: Mapped[int | None] = mapped_column(BigInteger)
    limit_resets_at: Mapped[datetime | None]
    waiting_since: Mapped[datetime | None]  # entered its current waiting state (usage-limit retry cadence)
    error_code: Mapped[str | None] = mapped_column(String(48))
    handoff_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=True)
    revert_status: Mapped[str | None] = mapped_column(String(16))  # offered | reverted | declined | failed


class Delivery(Base):
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("turn_id", "seq"), Index("ix_deliveries_status", "status"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn_id: Mapped[int] = mapped_column(Integer, ForeignKey("turns.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    mode: Mapped[str] = mapped_column(String(16))  # new_turn | next | answer | approval | nudge | resume
    source: Mapped[str] = mapped_column(String(8))  # owner | system
    text: Mapped[str | None] = mapped_column(Text)
    media_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    status: Mapped[str] = mapped_column(String(16))  # pending | sending | delivered | failed | queued_mac | …
    burst_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bursts.id", ondelete="SET NULL"), index=True)
    created_at: Mapped[datetime]
    sent_at: Mapped[datetime | None]


class TurnProgress(Base):
    __tablename__ = "turn_progress"
    __table_args__ = (Index("ix_turn_progress_turn_id_at", "turn_id", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    turn_id: Mapped[int] = mapped_column(Integer, ForeignKey("turns.id", ondelete="CASCADE"))
    at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(8))  # tool | text | todo
    summary: Mapped[str] = mapped_column(String(200))
