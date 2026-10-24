"""`FakeMacHost`: settable memory pressure, free disk, Claude sessions registry and ClaudeManager state."""

from pathlib import Path

from yordamchi.ports.mac_host import ClaudeManagerState, InteractiveSession, MacHost

GIB = 1024**3


class FakeMacHost:
    def __init__(self) -> None:
        self.pressure_level = 1
        self.free_bytes = 100 * GIB
        self.sessions: list[InteractiveSession] = []
        self.manager_state: ClaudeManagerState | None = None

    async def memory_pressure_level(self, *, timeout: float) -> int:
        return self.pressure_level

    async def free_disk_bytes(self, path: Path, *, timeout: float) -> int:
        return self.free_bytes

    async def interactive_sessions(self, *, timeout: float) -> list[InteractiveSession]:
        return list(self.sessions)

    async def claude_manager_state(self, *, timeout: float) -> ClaudeManagerState | None:
        return self.manager_state


_: MacHost = FakeMacHost()
