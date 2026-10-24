"""Offline harness for `GhCli`: a fake `gh` on PATH, replaying `tests/fixtures/github/` per command.

The fake is a `/bin/sh` script that calls its tools by absolute path, and the PATH the adapter gets holds only the
fake's directory, so the real `~/.local/bin/gh` can never be reached from a test. Each call records its argv and its
environment. Per command (`auth-status`, `repo-view`, `repo-create`: the first two arguments) it replays `stdout`,
`stderr`, `exit` and `sleep` files; a numbered subdirectory overrides them for that attempt (`replies/repo-view/2/…`
for the second call), which scripts retries. A command with no reply exits 0 and prints nothing.
"""

from dataclasses import dataclass
from pathlib import Path

from yordamchi.adapters.github.cli import GhCli

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "github"

_SCRIPT = """#!/bin/sh
state="{state}"
n=$(( $(/bin/cat "$state/calls.count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$state/calls.count"
{{ printf '%s\\n' "$0"; for arg in "$@"; do printf '%s\\n' "$arg"; done; }} > "$state/call-$n.argv"
/usr/bin/env > "$state/call-$n.env"
r="$state/replies/$1-$2"
a=$(( $(/bin/cat "$r/attempts" 2>/dev/null || echo 0) + 1 ))
if [ -d "$r" ]; then echo "$a" > "$r/attempts"; fi
d="$r"; if [ -d "$r/$a" ]; then d="$r/$a"; fi
if [ -f "$d/sleep" ]; then /bin/sleep "$(/bin/cat "$d/sleep")"; fi
if [ -f "$d/stdout" ]; then /bin/cat "$d/stdout"; fi
if [ -f "$d/stderr" ]; then /bin/cat "$d/stderr" >&2; fi
if [ -f "$d/exit" ]; then exit "$(/bin/cat "$d/exit")"; fi
exit 0
"""


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@dataclass
class FakeGh:
    bin: Path
    state: Path

    @property
    def path(self) -> str:
        """The PATH to hand the adapter: the fake's directory and nothing else."""
        return str(self.bin)

    def cli(self) -> GhCli:
        return GhCli(self.path)

    def reply(
        self,
        command: str,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        sleep_s: float | None = None,
        attempt: int | None = None,
    ) -> None:
        """What `gh <command>` (e.g. "repo-view") answers, on every call or only on attempt `attempt`."""
        target = self.state / "replies" / command
        if attempt is not None:
            target /= str(attempt)
        target.mkdir(parents=True, exist_ok=True)
        (target / "stdout").write_bytes(stdout)
        (target / "stderr").write_bytes(stderr)
        (target / "exit").write_text(str(exit_code), encoding="utf-8")
        if sleep_s is not None:
            (target / "sleep").write_text(str(sleep_s), encoding="utf-8")

    def fail(self, command: str, stderr_fixture: str, *, exit_code: int = 1, attempt: int | None = None) -> None:
        self.reply(command, stderr=fixture(stderr_fixture), exit_code=exit_code, attempt=attempt)

    def calls(self) -> list[list[str]]:
        """Each call's argv without the binary."""
        count_file = self.state / "calls.count"
        count = int(count_file.read_text(encoding="utf-8")) if count_file.exists() else 0
        argvs = [(self.state / f"call-{n}.argv").read_text(encoding="utf-8").splitlines() for n in range(1, count + 1)]
        return [argv[1:] for argv in argvs]

    def env(self, call: int = -1) -> dict[str, str]:
        """The environment of one call (default: the last), as `env` printed it."""
        count = int((self.state / "calls.count").read_text(encoding="utf-8"))
        number = count + 1 + call if call < 0 else call
        lines = (self.state / f"call-{number}.env").read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)


def fake_gh(directory: Path) -> FakeGh:
    bin_dir = directory / "bin"
    state = directory / "state"
    bin_dir.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "gh"
    script.write_text(_SCRIPT.format(state=state), encoding="utf-8")
    script.chmod(0o755)
    return FakeGh(bin=bin_dir, state=state)
