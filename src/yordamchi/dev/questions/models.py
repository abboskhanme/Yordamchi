"""Tables of `dev/questions` (SPEC §4.2, §4.6).

- `questions`: one row per `AskUserQuestion` call of an agent (1–4 questions), keyed UNIQUE(turn_id, tool_use_id) so
  the question is posted once however often its event is handled. `questions_json` holds the scrubbed questions in
  the agent's own shape; `answer_json` the owner's answer as it went to the agent (redacted: his words passed intake,
  the labels were scrubbed). A question stays `open` after its hold elapsed (the agent was told to go on; the answer
  then arrives as a message, `answer_mode=message`). `answered` with no `answer_mode` yet is an answer still on its
  way to the agent: the startup pass delivers it. `open` rows are durable across restarts and never deleted; the rest
  go 90 days after they were asked.

`turn_id` is `ON DELETE CASCADE`: a turn is deleted only 400 days after it ended, long after its questions.
"""

from datetime import datetime

from sqlalchemy import ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class Question(Base):
    __tablename__ = "questions"
    __table_args__ = (UniqueConstraint("turn_id", "tool_use_id"), Index("ix_questions_status", "status"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[str] = mapped_column(String(64), ForeignKey("projects.id"))
    turn_id: Mapped[int] = mapped_column(Integer, ForeignKey("turns.id", ondelete="CASCADE"))
    tool_use_id: Mapped[str] = mapped_column(String(96))  # the executor's id of the held call (`QuestionAsked`)
    questions_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    status: Mapped[str] = mapped_column(String(16))  # open | answered | expired | cancelled
    answer_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=True)  # {answers: {question: answer}, shown}
    answer_mode: Mapped[str | None] = mapped_column(String(16))  # callback | message
    asked_at: Mapped[datetime]
    answered_at: Mapped[datetime | None]
