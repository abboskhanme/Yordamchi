"""GitHub: the `gh` CLI operations Yordamchi runs itself (P2 new project; SPEC §3.2.8, NFR-SEC-11).

The agent runs its own `gh` through its shell; this port is for Yordamchi's deterministic calls: who is logged in,
whether a repository exists, and creating the private repository of a new project. A repository is created
**private** unless the caller says otherwise; making one public is an important decision (UC-13 `repo_public`), and
no caller of this port does it.

Failures arrive as `ExternalError`s: `NotInstalled(gh)`, `CallTimedOut`, `AuthFailed(GitHub)` (not logged in, or the
login was refused), `RateLimited(GitHub)`, `ProviderUnavailable(GitHub)` (no network, GitHub down) or
`GitHubFailed(operation)` (the name is taken, the source is not a git repository, …). No value here ever holds a
token.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class GitHubRepo:
    owner: str
    name: str
    url: str  # https://github.com/<owner>/<name>
    private: bool
    default_branch: str | None = None  # None while the repository has no commit


class GitHub(Protocol):
    async def auth_user(self, *, timeout: float) -> str | None:
        """The logged-in `gh` account, or None when nobody is logged in."""
        ...

    async def view_repo(self, owner: str, name: str, *, timeout: float) -> GitHubRepo | None:
        """The repository `<owner>/<name>`, or None when it does not exist (or this account cannot see it)."""
        ...

    async def create_repo(
        self, owner: str, name: str, *, source: Path, private: bool = True, timeout: float
    ) -> GitHubRepo:
        """`gh repo create <owner>/<name> --private --source <source> --remote origin`: the repository on GitHub, with
        the local repository at `source` pointing `origin` at it. Nothing is pushed."""
        ...
