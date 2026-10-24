"""`MacOsHost`: what the bot reads about the Mac it runs on (SPEC §5.6 scheduler, §9.2 disk guard, §3.2.9; UC-06).

Every read is local and read-only:
- **Memory pressure**: `sysctl -n kern.memorystatus_vm_pressure_level`, the kernel's own level (1 normal, 2 warning,
  4 critical), the value SPEC §5.6 tests. `memory_pressure` and `vm_stat` print page counts and a free percentage,
  and the level does not follow from them: on 2026-10-24 this Mac read level 2 at 47 % free, with 3.4 of 4 GB swap in
  use. So they are no fallback; an unreadable level raises, and the scheduler decides what that means.
- **Free disk**: `statvfs` through `shutil.disk_usage().free` (`f_bavail × f_frsize`, what `df` prints as Avail) for
  the path or its nearest existing ancestor, so a data dir that does not exist yet still reads its volume. APFS
  purgeable space is not counted: the guard sees what can be written now.
- **Sessions**: `sessions.py` (process scan plus the registry, spike S6). **ClaudeManager**: `claude_manager.py`.

`sysctl` and `ps` run through `core.proc` with an environment of their own (system PATH, `LC_ALL=C`, `TZ=UTC`): `ps`
must print `lstart` exactly as the registry's `procStart`, and nothing of the bot's environment reaches them. File
reads run in a worker thread, inside the call's timeout. Logs hold counts, codes and durations, never file content.
"""

import asyncio
import errno
import os
import re
import shutil
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Final

from yordamchi.adapters.macos.claude_manager import STATE_FILE, STATE_MAX_BYTES, parse_state
from yordamchi.adapters.macos.sessions import (
    PS_COLUMNS,
    RECORD_MAX_BYTES,
    Process,
    SessionRecord,
    live_record_pids,
    owner_sessions,
    parse_ps,
    parse_record,
)
from yordamchi.core import proc
from yordamchi.core.errors import CallTimedOut, NotInstalled, ProviderUnavailable
from yordamchi.core.log import get_log
from yordamchi.ports.mac_host import ClaudeManagerState, InteractiveSession

log = get_log(__name__)

SYSCTL: Final = Path("/usr/sbin/sysctl")
PS: Final = Path("/bin/ps")
PRESSURE_OID: Final = "kern.memorystatus_vm_pressure_level"
PRESSURE_LEVELS: Final = frozenset({1, 2, 4})
OP_PRESSURE: Final = "memory_pressure"
OP_DISK: Final = "free_disk"
OP_SCAN: Final = "process_scan"
PROBE_ENV: Final = proc.clean_env(path=":".join(proc.SYSTEM_PATH), base={}, extra={"LC_ALL": "C", "TZ": "UTC"})
_LEVEL: Final = re.compile(r"(?:kern\.memorystatus_vm_pressure_level:\s*)?(?P<level>[0-9]+)")


def parse_pressure_level(text: str) -> int:
    """`sysctl -n` prints the bare number; the `name: value` form is accepted too. Raises `ValueError` otherwise."""
    match = _LEVEL.fullmatch(text.strip())
    if match is None or (level := int(match["level"])) not in PRESSURE_LEVELS:
        msg = "not a memory pressure level"
        raise ValueError(msg)
    return level


def free_bytes(path: Path) -> int:
    """Bytes an unprivileged process may still write on the volume holding `path` (or its nearest ancestor)."""
    for candidate in (path, *path.parents):
        try:
            return shutil.disk_usage(candidate).free
        except (FileNotFoundError, NotADirectoryError):
            continue
    raise FileNotFoundError(errno.ENOENT, "no existing ancestor", str(path))


def read_head(path: Path, limit: int) -> bytes:
    """At most `limit + 1` bytes: enough for the parser to see that a file is over its cap."""
    with path.open("rb") as file:
        return file.read(limit + 1)


def read_records(directory: Path, live: Iterable[int]) -> dict[int, SessionRecord]:
    """Registry records of live pids. A file that vanishes or will not open is skipped, never fatal."""
    try:
        names = [entry.name for entry in directory.iterdir()]
    except FileNotFoundError:
        return {}
    records: dict[int, SessionRecord] = {}
    for pid in live_record_pids(names, live):
        try:
            raw = read_head(directory / f"{pid}.json", RECORD_MAX_BYTES)
        except OSError:
            continue
        record = parse_record(raw, pid=pid)
        if record is not None:
            records[pid] = record
    return records


class MacOsHost:
    """`MacHost` on macOS. `home` is the owner's home; `own_pid` is this bot's pid (default: this process), whose
    descendants are Yordamchi's own sessions. `sysctl` and `ps` are the system binaries; tests point them at fakes."""

    def __init__(self, *, home: Path, own_pid: int | None = None, sysctl: Path = SYSCTL, ps: Path = PS) -> None:
        self._sessions_dir = home / ".claude" / "sessions"
        self._manager_state = home / STATE_FILE
        self._own_pid = os.getpid() if own_pid is None else own_pid
        self._sysctl = sysctl
        self._ps = ps

    async def memory_pressure_level(self, *, timeout: float) -> int:
        text = await self._probe([str(self._sysctl), "-n", PRESSURE_OID], timeout=timeout, operation=OP_PRESSURE)
        try:
            return parse_pressure_level(text)
        except ValueError:
            log.warning("memory_pressure_unreadable", chars=len(text))
            raise ProviderUnavailable(provider=self._sysctl.name, retryable=False) from None

    async def free_disk_bytes(self, path: Path, *, timeout: float) -> int:
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.to_thread(free_bytes, path)
        except TimeoutError as exc:
            raise CallTimedOut(operation=OP_DISK) from exc
        except OSError as exc:
            log.warning("free_disk_unreadable", errno=exc.errno)
            raise ProviderUnavailable(provider="disk", retryable=False) from exc

    async def interactive_sessions(self, *, timeout: float) -> list[InteractiveSession]:
        deadline = time.monotonic() + timeout
        table = await self._probe([str(self._ps), "-axww", "-o", PS_COLUMNS], timeout=timeout, operation=OP_SCAN)
        processes, unparsed = parse_ps(table)
        if not processes:
            # An empty table means `ps` changed its format; "nobody is at the Mac" would be a guess.
            log.warning("process_scan_unparsed", lines=unparsed)
            raise ProviderUnavailable(provider=self._ps.name, retryable=False)
        records = await self._records(processes, timeout=deadline - time.monotonic())
        sessions = owner_sessions(processes, records, own_pid=self._own_pid)
        log.debug(
            "process_scan_finished",
            processes=len(processes),
            unparsed=unparsed,
            records=len(records),
            sessions=len(sessions),
        )
        return sessions

    async def claude_manager_state(self, *, timeout: float) -> ClaudeManagerState | None:
        try:
            async with asyncio.timeout(timeout):
                raw = await asyncio.to_thread(read_head, self._manager_state, STATE_MAX_BYTES)
        except FileNotFoundError:
            return None  # ClaudeManager is not installed (or has not run yet)
        except TimeoutError:
            log.warning("claude_manager_state_unreadable", reason="timed_out")
            return None
        except OSError as exc:
            log.warning("claude_manager_state_unreadable", reason="os_error", errno=exc.errno)
            return None
        try:
            return parse_state(raw)
        except ValueError:
            log.warning("claude_manager_state_unreadable", reason="malformed", bytes=len(raw))
            return None

    async def _records(self, processes: dict[int, Process], *, timeout: float) -> dict[int, SessionRecord]:
        """The registry is best-effort (R-11): unreadable or slow → no records, and the process scan stands alone."""
        if timeout <= 0:
            log.warning("session_registry_unreadable", reason="timed_out")
            return {}
        try:
            async with asyncio.timeout(timeout):
                return await asyncio.to_thread(read_records, self._sessions_dir, list(processes))
        except TimeoutError:
            log.warning("session_registry_unreadable", reason="timed_out")
        except OSError as exc:
            log.warning("session_registry_unreadable", reason="os_error", errno=exc.errno)
        return {}

    async def _probe(self, argv: Sequence[str], *, timeout: float, operation: str) -> str:
        binary = Path(argv[0]).name
        try:
            result = await proc.run(argv, env=PROBE_ENV, timeout=timeout)
        except TimeoutError as exc:
            raise CallTimedOut(operation=operation) from exc
        except OSError as exc:
            raise NotInstalled(binary=binary) from exc
        if result.returncode != 0:
            log.warning("mac_probe_failed", binary=binary, exit_code=result.returncode, duration_ms=result.duration_ms)
            raise ProviderUnavailable(provider=binary, retryable=False)
        return result.stdout.decode("utf-8", errors="replace")
