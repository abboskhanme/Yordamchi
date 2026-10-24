"""Tables of `dev/servers` (SPEC §4.2).

- `server_checks`: one row per check of one host (every `schedule.server_check_min`, and on demand). `ok` is "the host
  answered and nothing is wrong"; `error_code` says why it did not answer. `summary_json` holds the parsed readings,
  the open issues and the alert memory the next check continues from, plus short excerpts: redacted content (it
  passed `core.redact` before the write), never the full command output. The latest row per host is the cache that
  status answers, `dev/digest` and the personal bridge read. Rows are deleted after 30 days.
"""

from datetime import datetime

from sqlalchemy import Boolean, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class ServerCheck(Base):
    __tablename__ = "server_checks"
    __table_args__ = (Index("ix_server_checks_host_at", "host", "at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    host: Mapped[str] = mapped_column(String(253))  # an `~/.ssh/config` alias from `servers.hosts`
    at: Mapped[datetime]
    ok: Mapped[bool] = mapped_column(Boolean)
    summary_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    duration_ms: Mapped[int] = mapped_column(Integer)
