"""MacHost: what the bot needs to know about the Mac it runs on (SPEC §5.6, §3.2.9; UC-06).

Memory pressure and free disk gate the scheduler; the Claude sessions registry detects the owner attaching at the Mac;
ClaudeManager's token-free `state.json` feeds `holat?`.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class InteractiveSession:
    """A live interactive `claude` (from `~/.claude/sessions/*.json`, or the `ps` fallback).

    `kind` is the registry's `kind` (`interactive` on CLI 2.1.x), or `ps` when only the process's command line
    (`--resume <id>`) named the session because no valid registry record backs it.
    """

    pid: int
    session_id: str
    kind: str


@dataclass(frozen=True, slots=True)
class ClaudeAccount:
    """One ClaudeManager account, token-free. `is_active` marks the account `ClaudeManagerState.active_id` names."""

    email: str
    status: str
    five_hour_pct: float | None
    seven_day_pct: float | None
    five_hour_resets_at: datetime | None
    seven_day_resets_at: datetime | None
    is_active: bool = False


@dataclass(frozen=True, slots=True)
class ClaudeManagerState:
    active_id: str | None
    auto_switch: bool
    accounts: tuple[ClaudeAccount, ...]


class MacHost(Protocol):
    async def memory_pressure_level(self, *, timeout: float) -> int:
        """`sysctl kern.memorystatus_vm_pressure_level`: 1 normal, 2 warning, 4 critical."""
        ...

    async def free_disk_bytes(self, path: Path, *, timeout: float) -> int: ...

    async def interactive_sessions(self, *, timeout: float) -> list[InteractiveSession]:
        """The owner's live sessions at the Mac (SPEC §3.2.9, spike S6). Yordamchi's own are excluded (registry
        `entrypoint` `sdk-py`, or a descendant of this process), and a registry file whose process is gone is
        ignored."""
        ...

    async def claude_manager_state(self, *, timeout: float) -> ClaudeManagerState | None:
        """None when ClaudeManager is not installed or its state cannot be read."""
        ...
