"""A stand-in for the port whose adapter arrives in a later phase (SPEC §7), so `DevDeps` is complete in P1.

`GitHub` (`adapters/github`, the P2 new-project flow) has no caller in P1: the intent that reaches it answers
`S.NOT_YET`. Should it be called anyway, it fails as a missing binary would, with the owner told what is not there —
never silently, never a guess. Text-to-speech (P3) needs no stand-in: `DevDeps.synthesizers` is simply empty.
"""

from pathlib import Path

from yordamchi.core.errors import NotInstalled


class GitHubNotYet:
    async def auth_user(self, *, timeout: float) -> str | None:
        raise NotInstalled(binary="gh (P2)")

    async def create_private_repo(self, owner: str, name: str, *, source: Path, timeout: float) -> str:
        raise NotInstalled(binary="gh (P2)")
