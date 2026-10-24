"""Offline harness for `OpenSshRemoteShell`: a fake `ssh` on PATH, replaying `tests/fixtures/ssh/` per alias.

The fake is a `/bin/sh` script that calls its tools by absolute path, and the PATH the adapter gets holds only the
fake's directory, so the real `/usr/bin/ssh` can never be reached from a test. Each call records its argv and its
environment. Per alias it replays `stdout`, `stderr`, `exit` and `sleep` files; a numbered subdirectory overrides
them for that attempt (`hosts/<alias>/2/…` for the second call), which scripts retries. `ssh -G` prints the alias's
`config` file, or an unconfigured alias's `hostname <alias>` when there is none, as the real client does.
"""

from dataclasses import dataclass
from pathlib import Path

from yordamchi.adapters.ssh.shell import OpenSshRemoteShell

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ssh"
HOST = "bonvi-srv"

_SCRIPT = """#!/bin/sh
state="{state}"
n=$(( $(/bin/cat "$state/calls.count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$state/calls.count"
{{ printf '%s\\n' "$0"; for arg in "$@"; do printf '%s\\n' "$arg"; done; }} > "$state/call-$n.argv"
/usr/bin/env > "$state/call-$n.env"
config=""; host=""; after=""
for arg in "$@"; do
  if [ -n "$after" ]; then host="$arg"; break; fi
  case "$arg" in -G) config=1 ;; --) after=1 ;; esac
done
h="$state/hosts/$host"
if [ -n "$config" ]; then
  if [ -f "$h/config" ]; then /bin/cat "$h/config"; else printf 'host %s\\nhostname %s\\nport 22\\n' "$host" "$host"; fi
  exit 0
fi
a=$(( $(/bin/cat "$h/attempts" 2>/dev/null || echo 0) + 1 ))
if [ -d "$h" ]; then echo "$a" > "$h/attempts"; fi
d="$h"; if [ -d "$h/$a" ]; then d="$h/$a"; fi
if [ -f "$d/sleep" ]; then /bin/sleep "$(/bin/cat "$d/sleep")"; fi
if [ -f "$d/stdout" ]; then /bin/cat "$d/stdout"; fi
if [ -f "$d/stderr" ]; then /bin/cat "$d/stderr" >&2; fi
if [ -f "$d/exit" ]; then exit "$(/bin/cat "$d/exit")"; fi
exit 0
"""


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@dataclass
class FakeSsh:
    bin: Path
    state: Path

    @property
    def path(self) -> str:
        """The PATH to hand the adapter: the fake's directory and nothing else."""
        return str(self.bin)

    def shell(self) -> OpenSshRemoteShell:
        return OpenSshRemoteShell(ssh="ssh", path=self.path)

    def reply(
        self,
        host: str,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
        sleep_s: float | None = None,
        attempt: int | None = None,
    ) -> None:
        target = self.state / "hosts" / host
        if attempt is not None:
            target /= str(attempt)
        target.mkdir(parents=True, exist_ok=True)
        (target / "stdout").write_bytes(stdout)
        (target / "stderr").write_bytes(stderr)
        (target / "exit").write_text(str(exit_code), encoding="utf-8")
        if sleep_s is not None:
            (target / "sleep").write_text(str(sleep_s), encoding="utf-8")

    def fail(self, host: str, stderr_fixture: str, *, attempt: int | None = None) -> None:
        """Exit 255 with a recorded or synthetic OpenSSH message, as the client does when ssh itself fails."""
        self.reply(host, stderr=fixture(stderr_fixture), exit_code=255, attempt=attempt)

    def configure(self, host: str, ssh_g_fixture: str) -> None:
        """What `ssh -G <host>` prints, i.e. the alias's resolved `~/.ssh/config`."""
        target = self.state / "hosts" / host
        target.mkdir(parents=True, exist_ok=True)
        (target / "config").write_bytes(fixture(ssh_g_fixture))

    def calls(self) -> list[list[str]]:
        count_file = self.state / "calls.count"
        count = int(count_file.read_text(encoding="utf-8")) if count_file.exists() else 0
        return [(self.state / f"call-{n}.argv").read_text(encoding="utf-8").splitlines() for n in range(1, count + 1)]

    def env(self, call: int = -1) -> dict[str, str]:
        """The environment of one call (default: the last), as `env` printed it."""
        count = int((self.state / "calls.count").read_text(encoding="utf-8"))
        number = count + 1 + call if call < 0 else call
        lines = (self.state / f"call-{number}.env").read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines if "=" in line)


def fake_ssh(directory: Path) -> FakeSsh:
    bin_dir = directory / "bin"
    state = directory / "state"
    bin_dir.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    script = bin_dir / "ssh"
    script.write_text(_SCRIPT.format(state=state), encoding="utf-8")
    script.chmod(0o755)
    return FakeSsh(bin=bin_dir, state=state)
