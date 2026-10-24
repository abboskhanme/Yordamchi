"""Git: the local git operations Yordamchi itself runs (digest, gate facts, wrong-target revert, new project).

The agent runs its own git through its shell; this port is for Yordamchi's deterministic calls. Tests of git
semantics use the real adapter on `tmp_path` repos (CONVENTIONS §4); `FakeGit` scripts facts and failures.

Every call is local (no fetch, no push). Failures arrive as `ExternalError`s: `NotInstalled(git)`, `CallTimedOut`, or
`GitFailed(operation)` (not a repository, an unknown ref, a conflict). A path is relative to `repo` unless it says
otherwise, and paths come back exactly as git prints them (repo-relative, `/`-separated).
"""

from collections.abc import Mapping, Sequence
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
    changed: int  # tracked entries with staged or unstaged changes (renames and conflicts included)
    untracked: int  # untracked, not ignored (a new directory counts once)


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

    async def current_branch(self, repo: Path, *, timeout: float) -> str | None:
        """The checked-out branch (also before its first commit); None when HEAD is detached."""
        ...

    async def find_commit(self, repo: Path, ref: str, *, timeout: float) -> Commit | None:
        """The commit `ref` names, or None when it names none (an empty repo's HEAD, an unknown ref)."""
        ...

    async def status(self, repo: Path, *, timeout: float) -> GitStatus: ...

    async def log(self, repo: Path, *, since_days: int, limit: int, timeout: float) -> list[Commit]:
        """Up to `limit` commits reachable from HEAD and committed within `since_days`, newest first."""
        ...

    async def commits_since(self, repo: Path, base: str, *, timeout: float) -> list[Commit]:
        """`git log <base>..HEAD`, newest first."""
        ...

    async def changed_files(self, repo: Path, base: str, *, timeout: float) -> list[FileChange]:
        """`git diff --name-status -M <base>`: tracked files whose working-tree content differs from `base`,
        committed or not. Untracked files are `untracked_files`."""
        ...

    async def untracked_files(self, repo: Path, *, timeout: float) -> list[str]:
        """`git ls-files -o --exclude-standard` (untracked and not ignored)."""
        ...

    async def ignored(self, repo: Path, paths: Sequence[str], *, timeout: float) -> frozenset[str]:
        """The subset of `paths` git ignores (`git check-ignore`)."""
        ...

    async def default_branch(self, repo: Path, *, timeout: float) -> str | None:
        """`origin/HEAD`'s branch, else a local `main` or `master`, else the checked-out branch; None if detached."""
        ...

    async def remote_url(self, repo: Path, *, remote: str, timeout: float) -> str | None:
        """The remote's URL without any `user:password@` part, or None when there is no such remote."""
        ...

    async def stash_entries(self, repo: Path, *, timeout: float) -> list[Commit]:
        """`git stash list`, newest first (`stash@{0}` first): one commit per entry, `committed_at` = when it was
        stashed. Empty when nothing is stashed."""
        ...

    async def tree_files(
        self, repo: Path, ref: str, directory: str, *, max_bytes: int, timeout: float
    ) -> Mapping[str, bytes | None]:
        """The files directly in `directory` as committed at `ref`, by file name. A file over `max_bytes` maps to
        None (not read). Empty when `ref` names no commit or the directory is not in it."""
        ...

    async def init(self, path: Path, *, branch: str, timeout: float) -> None: ...

    async def create_branch(self, repo: Path, name: str, *, timeout: float) -> None:
        """Create and switch to `name`."""
        ...

    async def checkout_paths(self, repo: Path, ref: str, paths: Sequence[str], *, timeout: float) -> None:
        """`git checkout <ref> -- <paths>`, each path taken literally (no globs)."""
        ...

    async def revert(self, repo: Path, commits: Sequence[str], *, timeout: float) -> None:
        """`git revert --no-edit`, newest first whatever the order given. Never a reset, never a force push. On a
        conflict the revert is aborted, the repo is left as it was, and `GitFailed` is raised."""
        ...
