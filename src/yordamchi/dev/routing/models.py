"""Tables of `dev/routing` (SPEC §4.2).

- `route_decisions`: one row per routed burst (UC-10 AC "every decision → route_decisions"): how it was decided
  (`method`), the intent, the project (null while a question is open or for a projectless intent), the candidates and
  confidences, whether the bot asked, the model and the latency. A clarification answer fills `project_id` of the asked
  row; "bu X uchun edi" sets `corrected_to`. No message text is stored. Deleted after 90 days.
- `clarifications`: the one open "X'gami yoki Y'gami?" per routed burst, with its candidates in the order the owner's
  "2" refers to, and the bot message id once sent (a reply to it binds the answer). Expires after 30 minutes; the
  row is deleted 30 days after it was asked, unless still open.

`burst_id` is `ON DELETE SET NULL`: bursts are deleted after 30 days, routing rows live longer (PENDING_WIRING T16).
"""

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class RouteDecision(Base):
    __tablename__ = "route_decisions"
    __table_args__ = (Index("ix_route_decisions_at", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    burst_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bursts.id", ondelete="SET NULL"), index=True)
    at: Mapped[datetime]
    method: Mapped[str] = mapped_column(String(16))  # explicit | reply | binding | draft | llm | context | topic
    intent: Mapped[str] = mapped_column(String(32))  # instruct_project | report_project | … | wrong_target
    project_id: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.id"))
    candidates_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # [{project_id, confidence_milli}]
    confidence_milli: Mapped[int] = mapped_column(Integer)
    asked: Mapped[bool] = mapped_column(Boolean)
    model: Mapped[str | None] = mapped_column(String(96))  # the routing LLM's model, when it answered
    latency_ms: Mapped[int] = mapped_column(Integer)
    corrected_to: Mapped[str | None] = mapped_column(String(64), ForeignKey("projects.id"))


class Clarification(Base):
    __tablename__ = "clarifications"
    __table_args__ = (Index("ix_clarifications_status", "status"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    burst_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("bursts.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16))  # which_project | which_item | which_running | which_decision
    candidates_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)  # [project ids], in asked order
    status: Mapped[str] = mapped_column(String(16))  # open | resolved | expired
    message_id: Mapped[int | None] = mapped_column(BigInteger)  # the question's Telegram message, once sent
    asked_at: Mapped[datetime]
    resolved_at: Mapped[datetime | None]
