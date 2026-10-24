"""Offline harness for `MacOsHost`: fake `sysctl` and `ps` binaries replaying `tests/fixtures/macos/`, and a home
directory holding a Claude sessions registry and a ClaudeManager `state.json`.

The fakes are `/bin/sh` scripts that record their argv and environment. `recorded_mac()` builds the snapshot taken on
the owner's Mac on 2026-10-24 (sanitized: see the fixtures README); `fake_mac()` an empty one for edge cases.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

from yordamchi.adapters.macos.claude_manager import STATE_FILE
from yordamchi.adapters.macos.host import MacOsHost

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "macos"
# Not a pid in any fixture table: the bot has no children there unless a test says so.
NO_SUCH_PID = 424242

_SCRIPT = """#!/bin/sh
{{ printf '%s\\n' "$0"; for arg in "$@"; do printf '%s\\n' "$arg"; done; }} > "{bin}/{name}.argv"
/usr/bin/env > "{bin}/{name}.env"
{sleep}/bin/cat "{bin}/{name}.out"
/bin/cat "{bin}/{name}.err" >&2
exit {exit_code}
"""


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@dataclass
class FakeMac:
    home: Path
    bin: Path

    def host(self, *, own_pid: int = NO_SUCH_PID) -> MacOsHost:
        return MacOsHost(home=self.home, own_pid=own_pid, sysctl=self.bin / "sysctl", ps=self.bin / "ps")

    def binary(
        self, name: str, *, stdout: str = "", stderr: str = "", exit_code: int = 0, sleep_s: float | None = None
    ) -> None:
        """(Re)write the fake `name`: it records argv and env, then prints `stdout`/`stderr` and exits."""
        (self.bin / f"{name}.out").write_text(stdout, encoding="utf-8")
        (self.bin / f"{name}.err").write_text(stderr, encoding="utf-8")
        sleep = f"/bin/sleep {sleep_s}\n" if sleep_s is not None else ""
        script = self.bin / name
        script.write_text(_SCRIPT.format(bin=self.bin, name=name, sleep=sleep, exit_code=exit_code), encoding="utf-8")
        script.chmod(0o755)

    def argv(self, name: str) -> list[str]:
        return (self.bin / f"{name}.argv").read_text(encoding="utf-8").splitlines()

    def env(self, name: str) -> dict[str, str]:
        lines = (self.bin / f"{name}.env").read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)

    @property
    def registry(self) -> Path:
        return self.home / ".claude" / "sessions"

    def use_registry(self, fixture_dir: str) -> None:
        """Copy a registry fixture, plus the `<pid>.<sha256>.key` file the CLI keeps next to each record. The key
        files are made here with placeholder content: `*.key` never enters git (.gitignore)."""
        shutil.copytree(FIXTURES / fixture_dir, self.registry, dirs_exist_ok=True)
        for record in sorted(self.registry.glob("*.json")):
            (self.registry / f"{record.stem}.{'0' * 64}.key").write_text("placeholder, not a key\n", encoding="utf-8")

    @property
    def manager_state(self) -> Path:
        return self.home / STATE_FILE

    def use_manager_state(self, raw: bytes) -> None:
        self.manager_state.parent.mkdir(parents=True, exist_ok=True)
        self.manager_state.write_bytes(raw)


def fake_mac(directory: Path) -> FakeMac:
    home, bin_dir = directory / "home", directory / "bin"
    home.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    mac = FakeMac(home=home, bin=bin_dir)
    mac.binary("sysctl", stdout="1\n")
    mac.binary("ps", stdout=fixture_text("ps.txt"))
    return mac


def recorded_mac(directory: Path) -> FakeMac:
    """The owner's Mac as recorded: level 2, five terminal sessions plus one SDK session, ClaudeManager with three."""
    mac = fake_mac(directory)
    mac.binary("sysctl", stdout=fixture_text("sysctl_pressure_level.txt"))
    mac.use_registry("sessions")
    mac.use_manager_state((FIXTURES / "claude_manager" / "state.json").read_bytes())
    return mac
