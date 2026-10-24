"""Tables of `dev/gate` (SPEC §4.2, §4.6).

- `decisions`: one row per question the owner is asked (UC-13). A `gate` decision is keyed by the exact action,
  `(project_id, tool_name, input_sha256)`; `pending` → `approved` (with `grant_expires_at`) → `consumed` by one
  conditional UPDATE, so a grant runs at most once even under a race. `input_redacted` is the scrubbed command shown
  to the owner and replayed to the agent; `payload_json` holds ids and codes only: the owner message id, the gate
  reason, and once consumed the session key plus the ssh/scp/rsync argv the shims may let through. Kept 90 days; a
  `pending` or `approved` row is never deleted. `project_id` and `turn_id` name rows of other features without a
  foreign key: the gate must be able to record a question even for a project the registry has not caught up with.
"""

from datetime import datetime

from sqlalchemy import BigInteger, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_status", "status"),
        Index("ix_decisions_project_id_status", "project_id", "status"),
        Index("ix_decisions_input_sha256_status", "input_sha256", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # gate | plan_approval | selfupdate_core | settings_weaken | …
    project_id: Mapped[str | None] = mapped_column(String(64))
    turn_id: Mapped[int | None] = mapped_column(Integer)
    gate_class: Mapped[str | None] = mapped_column(String(32))
    tool_name: Mapped[str | None] = mapped_column(String(128))
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    input_redacted: Mapped[str | None] = mapped_column(Text)
    target: Mapped[str | None] = mapped_column(String(200))
    payload_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    status: Mapped[str] = mapped_column(String(16))  # pending | approved | denied | expired | consumed | superseded…
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    decided_update_id: Mapped[int | None] = mapped_column(BigInteger)  # the owner's Telegram update
    grant_expires_at: Mapped[datetime | None]
    consumed_at: Mapped[datetime | None]
    superseded_by: Mapped[int | None] = mapped_column(Integer, ForeignKey("decisions.id", ondelete="SET NULL"))
