"""Tables of `dev/digest` (SPEC §4.2).

- `digests`: one row per project, kept up to date by Yordamchi so a report never opens a session (UC-15): the git
  state, the latest commits (`git log -10 --since=7.days`) and the files changed today, the last results, counts of
  what waits for the owner, and the handoff note the next session starts from. `checked_at` is when git was last
  read (every report's freshness line counts from it); `updated_at` is when anything in it last changed. Text columns
  (✱: subjects, results, file names, the note) are redacted before they are written. One row per project, kept.

Beyond SPEC §4.2: `checked_at`, `git_upstream`, `untracked_count` and `git_error_code` (what the cheap poll compares
and how a report says git was unreadable), `recent_files_json` / `recent_files_count` (today's changed files),
`results_json` (the last five results, not only the last `Natija:`) and `handoff_turn_id` (the turn whose note is
stored, so a note is taken or synthesized once). Turn ids are plain integers, not foreign keys: turns are deleted
after 400 days while this row lives on and is rewritten whole by every refresh.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class Digest(Base):
    __tablename__ = "digests"

    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"), primary_key=True)
    updated_at: Mapped[datetime]
    checked_at: Mapped[datetime]
    git_branch: Mapped[str | None] = mapped_column(String(255))
    git_head: Mapped[str | None] = mapped_column(String(64))
    git_head_subject: Mapped[str | None] = mapped_column(Text)
    git_head_at: Mapped[datetime | None]
    git_upstream: Mapped[str | None] = mapped_column(String(255))
    ahead: Mapped[int] = mapped_column(Integer)
    behind: Mapped[int] = mapped_column(Integer)
    dirty_count: Mapped[int] = mapped_column(Integer)
    untracked_count: Mapped[int] = mapped_column(Integer)
    git_error_code: Mapped[str | None] = mapped_column(String(48))
    recent_commits_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # [{sha, subject, at}]
    recent_files_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # [path], ≤ 20
    recent_files_count: Mapped[int] = mapped_column(Integer)
    results_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # [{turn_id, status, natija, …}]
    last_natija: Mapped[str | None] = mapped_column(Text)
    last_natija_at: Mapped[datetime | None]
    last_turn_id: Mapped[int | None] = mapped_column(Integer)
    open_questions: Mapped[int] = mapped_column(Integer)
    pending_decisions: Mapped[int] = mapped_column(Integer)
    queued: Mapped[int] = mapped_column(Integer)
    parked: Mapped[int] = mapped_column(Integer)
    handoff_note: Mapped[str | None] = mapped_column(Text)
    handoff_note_at: Mapped[datetime | None]
    handoff_note_source: Mapped[str | None] = mapped_column(String(16))  # executor | synthesized
    handoff_turn_id: Mapped[int | None] = mapped_column(Integer)
    executor: Mapped[str] = mapped_column(String(16))  # claude | codex
    session_id: Mapped[str | None] = mapped_column(String(64))
