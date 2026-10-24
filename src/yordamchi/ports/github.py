"""GitHub: the `gh` CLI operations Yordamchi runs itself (P2 new project)."""

from pathlib import Path
from typing import Protocol


class GitHub(Protocol):
    async def auth_user(self, *, timeout: float) -> str | None:
        """The logged-in `gh` account, or None."""
        ...

    async def create_private_repo(self, owner: str, name: str, *, source: Path, timeout: float) -> str:
        """`gh repo create <owner>/<name> --private --source <source> --remote origin`; returns the repo URL."""
        ...
