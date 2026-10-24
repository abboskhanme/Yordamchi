"""Tables of `dev/projects` (SPEC §4.2).

- `projects`: one row per top-level folder of `projects.root`, keyed by the folder name. Written by the scan
  (`sync_projects`): new folders are added, seen ones refreshed, vanished ones get `missing_since` and stay. The
  owner's aliases, archive choices and descriptions live in settings (`projects.*`) and are applied when the row is
  read; the row mirrors the description and executor so the personal bot's bridge can read them. Kept forever.
- `deploy_branches_json` holds `domain.DeployBranches.to_json()`; `deploy_checked_sha` is `domain.deploy_key()` of the
  HEAD commit and workflow files it was computed from.
"""

from datetime import datetime

from sqlalchemy import Boolean, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from yordamchi.core.json_value import JsonValue
from yordamchi.persistence.base import Base
from yordamchi.persistence.types import JSONText


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # the folder name, exactly
    path: Mapped[str] = mapped_column(String(1024), unique=True)
    description: Mapped[str] = mapped_column(Text)
    description_source: Mapped[str] = mapped_column(String(16))  # readme | claude_md | spec | owner | none
    is_worktree: Mapped[bool] = mapped_column(Boolean)
    is_archive_name: Mapped[bool] = mapped_column(Boolean)
    executor: Mapped[str] = mapped_column(String(16))  # claude | codex
    git_remote_url: Mapped[str | None] = mapped_column(String(1024))  # never with credentials
    default_branch: Mapped[str | None] = mapped_column(String(255))
    deploy_branches_json: Mapped[JsonValue] = mapped_column(JSONText, nullable=False)
    deploy_checked_sha: Mapped[str | None] = mapped_column(String(64))
    discovered_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    missing_since: Mapped[datetime | None]
    last_activity_at: Mapped[datetime | None]
