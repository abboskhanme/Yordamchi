"""RemoteShell: read-only server checks over OpenSSH using `~/.ssh/config` aliases (SPEC §3.2.4, UC-15).

The caller classifies the command with the gate first; only commands on `gate.readonly_remote_commands` reach here.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class RemoteResult:
    host: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class RemoteShell(Protocol):
    async def run(self, host: str, command: str, *, timeout: float) -> RemoteResult: ...
