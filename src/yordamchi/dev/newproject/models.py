"""Tables of `dev/newproject` (SPEC §4.2).

- `project_drafts`: one new project from the owner's first "yangi loyiha …" to its built, pushed repository. `status`
  is the state machine of `domain.DraftStatus`. `parts_json` holds the description bursts, redacted by the inbox and
  scrubbed again before the write. `proposed_name` is the folder name chosen for the project, written just before the
  folder is made (so a restart reuses that folder, never another), and `project_id` is set once the project is
  registered. `plan_turn_id` is the latest plan or revision turn, `build_turn_id` the build. `repo_url` is the private
  GitHub repository once it exists; `error_code` why a draft failed. Deleted 400 days after it was closed.

SPEC §4.2 also lists `decision_id`: the plan approval is this row's `awaiting_approval` state instead of a gate
`decisions(kind=plan_approval)` row, because the gate (protected core) has no public call that writes other kinds.
`plan_turn_id` and `build_turn_id` are `ON DELETE SET NULL`: turns are deleted 400 days after they were queued.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class ProjectDraft(Base):
    __tablename__ = "project_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(24))  # collecting | creating | planning | awaiting_approval | …
    parts_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # [{text, at, burst_id}]
    proposed_name: Mapped[str | None] = mapped_column(String(64))
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.id"))
    plan_turn_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("turns.id", ondelete="SET NULL"))
    build_turn_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("turns.id", ondelete="SET NULL"))
    repo_url: Mapped[str | None] = mapped_column(String(200))
    error_code: Mapped[str | None] = mapped_column(String(48))
    opened_at: Mapped[datetime]
    last_part_at: Mapped[datetime]
    closed_at: Mapped[datetime | None]
