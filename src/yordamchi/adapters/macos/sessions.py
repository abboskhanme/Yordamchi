"""Which `claude` sessions the owner has open at the Mac: the process table plus the CLI's session registry
(SPEC §3.2.9; spike S6, docs/SPIKES.md). Pure functions; `host.py` runs `ps` and reads the files.

The process scan is primary, because the registry is an internal format (R-11) and a SIGKILL leaves stale files:
- `ps -axww -o pid=,ppid=,lstart=,command=` with `TZ=UTC LC_ALL=C` gives every live process. Its `lstart` then has
  the exact form of the registry's `procStart` ("Wed Oct  7 12:54:31 2026", UTC; checked on CLI 2.1.292–2.1.310).
- `~/.claude/sessions/<pid>.json` is read only for live pids. A record counts if its `procStart` equals the live
  process's start (so a reused pid is not mistaken for the session), or, when that differs or is missing, if the
  live process is a `claude` (a new `claude` on a reused pid rewrites its own file at start).
- Yordamchi's own sessions are excluded: `entrypoint == "sdk-py"` (every Agent SDK child; the owner's terminal
  sessions say `cli`), and any descendant of this process. `kind` is `interactive` for both, so it decides nothing.
- A live `claude` with no valid record still counts if its command line names the session (`--resume <uuid>`,
  `-r`, `--session-id`), as SPEC §3.2.9's fallback says. Only descent from this process excludes it then: when in
  doubt the Mac session wins (UC-16).
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from yordamchi.ports.mac_host import InteractiveSession

PS_COLUMNS: Final = "pid=,ppid=,lstart=,command="
SDK_ENTRYPOINT: Final = "sdk-py"
PS_KIND: Final = "ps"
UNKNOWN_KIND: Final = "unknown"
RECORD_MAX_BYTES: Final = 64 * 1024

_PS_LINE: Final = re.compile(
    r"^\s*(?P<pid>\d+)\s+(?P<ppid>\d+)\s+"
    r"(?P<started>[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\s*(?P<command>.*)$"
)
_RECORD_NAME: Final = re.compile(r"(?P<pid>\d+)\.json")
# argv[0] is `claude` (a bare name or a path to it): the native CLI and the npm build both show it that way.
_CLAUDE_COMMAND: Final = re.compile(r"^(?:\S*/)?claude(?:\s|$)")
_UUID: Final = r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
_SESSION_ARG: Final = re.compile(rf"(?:^|\s)(?:--resume|-r|--session-id)(?:=|\s+)(?P<id>{_UUID})(?=\s|$)")


@dataclass(frozen=True, slots=True)
class Process:
    pid: int
    ppid: int
    started: str  # `lstart` in UTC, whitespace collapsed
    command: str


@dataclass(frozen=True, slots=True)
class SessionRecord:
    pid: int
    session_id: str
    kind: str
    entrypoint: str | None
    proc_start: str | None


def _collapsed(text: str) -> str:
    return " ".join(text.split())


def parse_ps(text: str) -> tuple[dict[int, Process], int]:
    """Live processes by pid, and how many lines did not parse (a changed `ps` format shows up there)."""
    processes: dict[int, Process] = {}
    unparsed = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _PS_LINE.match(line)
        if match is None:
            unparsed += 1
            continue
        pid = int(match["pid"])
        processes[pid] = Process(
            pid=pid, ppid=int(match["ppid"]), started=_collapsed(match["started"]), command=match["command"].strip()
        )
    return processes, unparsed


def descendants(processes: Mapping[int, Process], root: int) -> set[int]:
    """`root` and every process below it."""
    children: dict[int, list[int]] = {}
    for process in processes.values():
        if process.pid != process.ppid:
            children.setdefault(process.ppid, []).append(process.pid)
    found = {root}
    pending = [root]
    while pending:
        for child in children.get(pending.pop(), ()):
            if child not in found:
                found.add(child)
                pending.append(child)
    return found


def is_claude(command: str) -> bool:
    return _CLAUDE_COMMAND.match(command) is not None


def session_id_from_command(command: str) -> str | None:
    match = _SESSION_ARG.search(command)
    return match["id"].lower() if match else None


def record_pid(file_name: str) -> int | None:
    """`31496.json` → 31496; the `<pid>.<sha>.key` files and anything else → None."""
    match = _RECORD_NAME.fullmatch(file_name)
    return int(match["pid"]) if match else None


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def parse_record(raw: bytes, *, pid: int) -> SessionRecord | None:
    """One registry file, or None when it is not a usable record for `pid` (bad JSON, no session id, another pid)."""
    if len(raw) > RECORD_MAX_BYTES:
        return None
    try:
        data: object = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    session_id = _optional_str(data.get("sessionId"))
    recorded_pid = data.get("pid")
    if session_id is None or (recorded_pid is not None and recorded_pid != pid):
        return None
    proc_start = _optional_str(data.get("procStart"))
    return SessionRecord(
        pid=pid,
        session_id=session_id,
        kind=_optional_str(data.get("kind")) or UNKNOWN_KIND,
        entrypoint=_optional_str(data.get("entrypoint")),
        proc_start=_collapsed(proc_start) if proc_start else None,
    )


def record_is_live(record: SessionRecord, process: Process) -> bool:
    """The record describes this live process, not an earlier owner of its pid."""
    return record.proc_start == process.started or is_claude(process.command)


def owner_sessions(
    processes: Mapping[int, Process], records: Mapping[int, SessionRecord], *, own_pid: int
) -> list[InteractiveSession]:
    """The owner's live sessions, by pid: the S6 rules in the module docstring."""
    ours = descendants(processes, own_pid)
    sessions: list[InteractiveSession] = []
    for pid in sorted(processes):
        process = processes[pid]
        if pid in ours:
            continue
        record = records.get(pid)
        if record is not None and record_is_live(record, process):
            if record.entrypoint != SDK_ENTRYPOINT:
                sessions.append(InteractiveSession(pid=pid, session_id=record.session_id, kind=record.kind))
            continue
        session_id = session_id_from_command(process.command) if is_claude(process.command) else None
        if session_id is not None:
            sessions.append(InteractiveSession(pid=pid, session_id=session_id, kind=PS_KIND))
    return sessions


def live_record_pids(file_names: Iterable[str], live: Iterable[int]) -> list[int]:
    """Registry pids worth reading: a file whose pid is gone is stale by definition."""
    alive = set(live)
    return sorted(pid for name in file_names if (pid := record_pid(name)) is not None and pid in alive)
