"""Git: the local git operations Yordamchi itself runs (digest, gate facts, wrong-target revert, new project).

The agent runs its own git through its shell; this port is for Yordamchi's deterministic calls. Tests of git
semantics use the real adapter on `tmp_path` repos (CONVENTIONS §4); `FakeGit` scripts facts and failures.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class GitStatus:
    """`git status --porcelain=v2 --branch`."""

    branch: str | None  # None when detached
    head: str | None  # None in an empty repo
    upstream: str | None
    ahead: int
    behind: int
    changed: int
    untracked: int


@dataclass(frozen=True, slots=True)
class Commit:
    sha: str
    subject: str
    committed_at: datetime


@dataclass(frozen=True, slots=True)
class FileChange:
    """One `git diff --name-status` line: `A`, `M`, `D`, `R`, …"""

    status: str
    path: str
    old_path: str | None = None


class Git(Protocol):
    async def head(self, repo: Path, *, timeout: float) -> str | None: ...

    async def status(self, repo: Path, *, timeout: float) -> GitStatus: ...

    async def log(self, repo: Path, *, since_days: int, limit: int, timeout: float) -> list[Commit]: ...

    async def commits_since(self, repo: Path, base: str, *, timeout: float) -> list[Commit]: ...

    async def changed_files(self, repo: Path, base: str, *, timeout: float) -> list[FileChange]: ...

    async def untracked_files(self, repo: Path, *, timeout: float) -> list[str]:
        """`git ls-files -o --exclude-standard` (untracked and not ignored)."""
        ...

    async def ignored(self, repo: Path, paths: Sequence[str], *, timeout: float) -> frozenset[str]:
        """The subset of `paths` git ignores (`git check-ignore`)."""
        ...

    async def default_branch(self, repo: Path, *, timeout: float) -> str | None: ...

    async def remote_url(self, repo: Path, *, remote: str, timeout: float) -> str | None: ...

    async def init(self, path: Path, *, branch: str, timeout: float) -> None: ...

    async def create_branch(self, repo: Path, name: str, *, timeout: float) -> None:
        """Create and switch to `name`."""
        ...

    async def checkout_paths(self, repo: Path, ref: str, paths: Sequence[str], *, timeout: float) -> None: ...

    async def revert(self, repo: Path, commits: Sequence[str], *, timeout: float) -> None:
        """`git revert --no-edit`, newest first. Never a reset, never a force push."""
        ...
