"""Server checks, pure (SPEC §3.2.4, §4.2 `server_checks`, §5.7; UC-15; REQUIREMENTS Q3).

One check of one host, in the order the service runs it (it owns every SSH call and every write):
1. `plan_probes(log_paths_for(host, …))`: the read-only commands of a check: uptime (load), disk, memory, running
   containers, failed systemd units, then one `tail` per log path configured for the host (`servers.log_paths`).
2. `refused(host, probe, …)` asks the gate (`dev/gate/domain.shim_verdict`, READ_ONLY policy): a command runs only
   if `gate.readonly_remote_commands` covers it, and nothing ever runs on a `gate.jump_only_hosts` host. This module
   keeps no allowlist of its own; a probe the gate refuses is reported as refused and never sent.
3. The service runs the allowed probes one by one through `RemoteShell` and hands back `ProbeOutcome`s.
4. `evaluate(…)` parses them into a `HostReport`, derives the issues (disk ≥ `servers.disk_warn_pct`, memory ≥
   `servers.memory_warn_pct`, a failed unit, a container that stopped, restarts or is unhealthy, Docker itself no
   longer answering, error lines in a log, a host that stayed unreachable), keeps what a failed probe could not
   re-check, and names the issues the owner has not been alerted about: one alert per issue, and none again while it
   flaps (`REALERT_QUIET`).
5. `summary_json` / `read_summary` store and read it back. Only parsed facts and short scrubbed excerpts are
   stored: `RemoteShell` output is unbounded (`core.proc.run` has no cap), so it is clipped before parsing too.

stderr is never read: a login banner (the Bonvi VM prints one) or a remote error can echo anything.

Parsing is positional where it can be, so a localized `df`/`free`/`uptime` header (the Mac's `LANG` reaches the
server through `SendEnv`) does not break it.
"""

import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.text import BotText, join
from yordamchi.dev.gate import domain as gate
from yordamchi.dev.gate.domain import GateContext, GateRules

# --- fixed shape of a check (SPEC §3.2.4; owner behaviour is in `servers.*` / `timeouts.server_*` settings) --------
UPTIME_COMMAND: Final = "uptime"
DISK_COMMAND: Final = "df -h"
MEMORY_COMMAND: Final = "free -m"
CONTAINERS_COMMAND: Final = "docker ps"
# On the default `gate.readonly_remote_commands`. Taken off that list, the gate refuses this probe and the check
# reports the units as not checked.
UNITS_COMMAND: Final = "systemctl --failed --no-legend"
LOG_TAIL_LINES: Final = 200
PARSE_MAX_CHARS: Final = 64_000  # stdout kept for parsing; the rest of a huge answer is dropped
EXCERPT_MAX_CHARS: Final = 400  # stored per probe
LOG_LINES_KEPT: Final = 5  # the latest error lines stored per log
LINE_MAX_CHARS: Final = 200
LIST_MAX: Final = 100  # disks, containers, units stored per check
SHIM_SESSION: Final = "servers"  # the gate's session id for Yordamchi's own checks (no grant ever matches it)
MIN_COMMAND_S: Final = 2.0  # less budget than this left: the remaining probes are skipped
UNREACHABLE_AFTER: Final = 2  # consecutive checks without a connection before it is an issue (a blip is not)
REALERT_QUIET: Final = timedelta(hours=24)  # a resolved issue that comes back within this is not alerted again
CONTAINER_GONE_KEEP: Final = timedelta(hours=24)  # a vanished container is flagged this long after it was last seen
CHECK_TICK_S: Final = 60.0  # the loop looks for due hosts at least this often (a new host is checked within it)
CHECK_KEEP: Final = timedelta(days=30)  # SPEC §4.2 `server_checks`: 30 d
SUMMARY_VERSION: Final = 1

_PCT: Final = re.compile(r"(\d{1,3})%")
_NUMBER: Final = re.compile(r"\d+(?:[.,]\d+)?")
_LOAD: Final = re.compile(r"(\d+[.,]\d+),?\s+(\d+[.,]\d+),?\s+(\d+[.,]\d+)\s*$")
_UP: Final = re.compile(r"\bup\s+(.+?),\s+\d+\s+users?\b")
_ERROR_LINE: Final = re.compile(
    r"(?i)\b(errors?|err|fatal|crit(?:ical)?|panic|exception|traceback|fail(?:ed|ure)?|segfault|oom|out of memory)\b"
)
_UNIT_BULLETS: Final = frozenset({"●", "*", "×"})  # systemd's own markers
# Pseudo and duplicate filesystems: their Use% says nothing about free space (snap loops sit at 100 %).
_SKIP_FILESYSTEMS: Final = frozenset({"tmpfs", "devtmpfs", "overlay", "shm", "udev", "none", "efivarfs", "squashfs"})
_SKIP_MOUNTS: Final = ("/snap", "/run", "/dev", "/sys", "/proc", "/var/lib/docker")  # and what lies under them
_HOST_WORD: Final = r"(?<![\w.-]){}(?![\w.-])"
_LOG_ENTRY: Final = re.compile(r"(?P<host>[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?):(?P<path>/\S+)")


# --- probes --------------------------------------------------------------------------------------------------------
class ProbeKind(StrEnum):
    UPTIME = "uptime"
    DISK = "disk"
    MEMORY = "memory"
    CONTAINERS = "containers"
    UNITS = "units"
    LOG = "log"


@dataclass(frozen=True, slots=True)
class Probe:
    kind: ProbeKind
    command: str
    path: str = ""  # LOG only

    @property
    def key(self) -> str:
        """Stable per host: one per kind, one per log path."""
        return f"{self.kind.value}:{self.path}" if self.kind is ProbeKind.LOG else self.kind.value


class ProbeStatus(StrEnum):
    OK = "ok"  # ran, exit 0
    FAILED = "failed"  # ran, non-zero exit (no docker, no permission): nothing learned, no issue raised
    ERROR = "error"  # ssh itself failed (connect, auth, timeout)
    REFUSED = "refused"  # the gate does not allow it: never sent
    SKIPPED = "skipped"  # not run: the host did not answer, or the time budget ran out


RAN: Final = frozenset({ProbeStatus.OK, ProbeStatus.FAILED})  # the host answered


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    probe: Probe
    status: ProbeStatus
    exit_code: int | None = None
    stdout: str = field(default="", repr=False)
    error_code: str | None = None


def log_paths_for(host: str, entries: Iterable[str]) -> tuple[str, ...]:
    """The `servers.log_paths` entries (`<alias>:<absolute path>`) of one host, in the order given."""
    paths: list[str] = []
    for entry in entries:
        match = _LOG_ENTRY.fullmatch(entry)
        if match is not None and match["host"].lower() == host.lower() and match["path"] not in paths:
            paths.append(match["path"])
    return tuple(paths)


def log_command(path: str) -> str:
    return f"tail -n {LOG_TAIL_LINES} {path}"


def plan_probes(log_paths: Sequence[str]) -> tuple[Probe, ...]:
    """The probes of one check, in the order they run: the cheap ones first, the logs last."""
    fixed = (
        Probe(ProbeKind.UPTIME, UPTIME_COMMAND),
        Probe(ProbeKind.DISK, DISK_COMMAND),
        Probe(ProbeKind.MEMORY, MEMORY_COMMAND),
        Probe(ProbeKind.CONTAINERS, CONTAINERS_COMMAND),
        Probe(ProbeKind.UNITS, UNITS_COMMAND),
    )
    return (*fixed, *(Probe(ProbeKind.LOG, log_command(path), path) for path in log_paths))


def is_jump_only(host: str, rules: GateRules) -> bool:
    """`gate.jump_only_hosts` (the Windows host with production 1C): a jump, never a target. The same list and the
    same lower-casing the gate's ssh rule uses."""
    return host.lower() in rules.jump_only_hosts


def refused(host: str, probe: Probe, *, rules: GateRules, ctx: GateContext, now: datetime) -> bool:
    """True unless the gate allows `ssh <host> <command>` under `ctx.policy` (READ_ONLY: nothing is asked, what is
    not read-only is denied). The Layer 2 shim decides the same way."""
    verdict = gate.shim_verdict(
        "ssh", [host, probe.command], session_id=SHIM_SESSION, rules=rules, ctx=ctx, grants=(), now=now, window_s=0
    )
    return not verdict.allowed


def clip(text: str, limit: int) -> str:
    """At most `limit` characters; a cut text ends in "…"."""
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def kept_output(probe: Probe, stdout: str) -> str:
    """The part of a command's output that is parsed: at most `PARSE_MAX_CHARS`. A table keeps its start (the header
    and the first rows); a log keeps its end, from a whole line on: the newest lines are the ones that matter."""
    if len(stdout) <= PARSE_MAX_CHARS:
        return stdout
    if probe.kind is not ProbeKind.LOG:
        return stdout[:PARSE_MAX_CHARS]
    tail = stdout[-PARSE_MAX_CHARS:]
    return tail.partition("\n")[2] if "\n" in tail else tail


def scrubbed_clip(text: str, limit: int, scrub: Callable[[str], str]) -> str:
    """Scrub, then clip: a key cut in half would no longer match its pattern. Only twice the limit is scrubbed;
    whatever lies beyond that is cut off anyway."""
    return clip(scrub(text[: limit * 2]), limit)


def command_timeout_s(remaining_s: float, command_s: float) -> float | None:
    """The bound of the next command: its own setting, within what is left of the host's budget. None: skip."""
    if remaining_s < MIN_COMMAND_S:
        return None
    return min(command_s, remaining_s)


# --- parsed facts --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class DiskUse:
    mount: str
    used_pct: int
    size: str = ""
    avail: str = ""


@dataclass(frozen=True, slots=True)
class MemoryUse:
    total_mb: int
    available_mb: int
    used_pct: int


class ContainerState(StrEnum):
    UP = "up"
    UNHEALTHY = "unhealthy"
    RESTARTING = "restarting"
    PAUSED = "paused"
    DOWN = "down"  # exited or dead (only shown by `docker ps -a`), or gone since the last check
    CREATED = "created"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Container:
    name: str
    status: str
    state: ContainerState


@dataclass(frozen=True, slots=True)
class LogErrors:
    path: str
    count: int
    lines: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    """What is stored about one probe: never the full output."""

    key: str
    status: ProbeStatus
    exit_code: int | None = None
    error_code: str | None = None
    excerpt: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class HostReport:
    host: str
    reachable: bool
    load: tuple[float, float, float] | None = None
    up: str | None = None
    disks: tuple[DiskUse, ...] = ()
    memory: MemoryUse | None = None
    containers: tuple[Container, ...] | None = None  # None: not read this time
    failed_units: tuple[str, ...] | None = None
    logs: tuple[LogErrors, ...] = ()
    probes: tuple[ProbeRecord, ...] = ()
    error_code: str | None = None  # why the host did not answer

    @property
    def all_refused(self) -> bool:
        """The gate allowed none of the probes: the host was not contacted at all."""
        return bool(self.probes) and all(probe.status is ProbeStatus.REFUSED for probe in self.probes)


def parse_uptime(stdout: str) -> tuple[tuple[float, float, float] | None, str | None]:
    """`uptime` → (1, 5, 15 min load averages, "12 days,  3:04"). The load is the last three numbers of the line, so
    a translated "load average" label or a decimal comma still parse."""
    line = next((item.strip() for item in stdout.splitlines() if item.strip()), "")
    match = _LOAD.search(line)
    load = None
    if match is not None:
        first, five, fifteen = (float(value.replace(",", ".")) for value in match.groups())
        load = (first, five, fifteen)
    up = _UP.search(line)
    return load, (clip(" ".join(up.group(1).split()), 40) if up is not None else None)


def _skipped_filesystem(source: str, mount: str) -> bool:
    under = any(mount == root or mount.startswith(f"{root}/") for root in _SKIP_MOUNTS)
    return under or source in _SKIP_FILESYSTEMS or source.startswith("/dev/loop")


def parse_disks(stdout: str) -> tuple[DiskUse, ...]:
    """`df -h` → real filesystems, fullest first; one row per source device. The header line is skipped by position,
    and a device name that wrapped onto its own line is joined with the next."""
    rows = [line.split() for line in stdout.splitlines()[1:] if line.strip()]
    disks: list[DiskUse] = []
    seen: set[str] = set()
    index = 0
    while index < len(rows):
        fields = rows[index]
        index += 1
        if len(fields) == 1 and index < len(rows):  # `df` wraps a long device name
            fields = [*fields, *rows[index]]
            index += 1
        positions = [i for i, item in enumerate(fields) if _PCT.fullmatch(item)]
        if not positions or len(fields) < 6:
            continue
        source, mount = fields[0], " ".join(fields[positions[-1] + 1 :])
        if not mount or source in seen or _skipped_filesystem(source, mount):
            continue
        seen.add(source)
        pct = int(fields[positions[0]].rstrip("%"))
        avail = fields[3] if positions[0] >= 4 else ""
        disks.append(DiskUse(mount=mount, used_pct=pct, size=fields[1], avail=avail))
    disks.sort(key=lambda disk: -disk.used_pct)
    return tuple(disks[:LIST_MAX])


def _numbers(tokens: Sequence[str]) -> list[int]:
    return [int(float(token.replace(",", "."))) for token in tokens if _NUMBER.fullmatch(token)]


def parse_memory(stdout: str) -> MemoryUse | None:
    """`free -m` → memory in use. By position, not by the (translatable) labels: the first row after the header
    holds RAM; procps-ng's last column is `available`. Old procps prints a "-/+ buffers/cache" row instead, whose
    first number is what is really used."""
    lines = [line for line in stdout.splitlines()[1:] if line.strip()]
    if not lines:
        return None
    ram = _numbers(lines[0].split()[1:])
    if len(ram) < 3 or ram[0] <= 0:
        return None
    total = ram[0]
    adjusted = next((line for line in lines[1:] if line.lstrip().startswith("-/+")), None)
    if adjusted is not None and (values := _numbers(adjusted.split())):
        available = total - values[0]
    elif len(ram) >= 6:
        available = ram[5]
    else:
        available = ram[2]
    available = max(0, min(total, available))
    return MemoryUse(total_mb=total, available_mb=available, used_pct=round((total - available) * 100 / total))


def container_state(status: str) -> ContainerState:
    """`docker ps` STATUS → its state (`Up 3 hours (unhealthy)`, `Restarting (1) 5 seconds ago`, …)."""
    text = status.strip()
    if text.startswith("Up"):
        if "(unhealthy)" in text:
            return ContainerState.UNHEALTHY
        return ContainerState.PAUSED if "(Paused)" in text else ContainerState.UP
    prefixes = (
        ("Restarting", ContainerState.RESTARTING),
        ("Exited", ContainerState.DOWN),
        ("Dead", ContainerState.DOWN),
        ("Created", ContainerState.CREATED),
    )
    return next((state for prefix, state in prefixes if text.startswith(prefix)), ContainerState.OTHER)


_STATUS_WORDS: Final = re.compile(r"\b(?:Up|Restarting|Exited|Created|Dead|Removal)\b.*?(?=\s{2,}|$)")


def _container_row(line: str, columns: tuple[int, int, int] | None) -> Container | None:
    if columns is not None:
        status_at, ports_at, names_at = columns
        status, name = line[status_at:ports_at].strip(), line[names_at:].strip()
    else:  # no usable header: the name is the last word, the status the first state phrase
        found = _STATUS_WORDS.search(line)
        status, name = (found.group(0).strip() if found else ""), (line.split() or [""])[-1]
    if not name or not status:
        return None
    return Container(name=clip(name, LINE_MAX_CHARS), status=clip(status, 80), state=container_state(status))


def parse_containers(stdout: str) -> tuple[Container, ...]:
    """`docker ps` (default table) → its containers. Docker pads its columns, so the header's offsets cut each row;
    COMMAND and PORTS may hold anything and are never parsed."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines:
        return ()
    header = lines[0]
    offsets = (header.find("STATUS"), header.find("PORTS"), header.find("NAMES"))
    columns = offsets if -1 not in offsets and offsets[0] < offsets[1] < offsets[2] else None
    rows = (_container_row(line, columns) for line in lines[1:])
    return tuple(row for row in rows if row is not None)[:LIST_MAX]


def parse_failed_units(stdout: str) -> tuple[str, ...]:
    """`systemctl --failed --no-legend` → the unit names (a leading ● or * marker is dropped)."""
    units: list[str] = []
    for line in stdout.splitlines():
        tokens = [token for token in line.split() if token not in _UNIT_BULLETS]
        if tokens and "." in tokens[0] and tokens[0] not in units:
            units.append(clip(tokens[0], LINE_MAX_CHARS))
    return tuple(units[:LIST_MAX])


def parse_log_errors(path: str, stdout: str, scrub: Callable[[str], str]) -> LogErrors:
    """The error lines in a `tail` of a log: their count and the latest few, clipped and scrubbed."""
    hits = [line.strip() for line in stdout.splitlines() if _ERROR_LINE.search(line)]
    kept = tuple(scrubbed_clip(line, LINE_MAX_CHARS, scrub) for line in hits[-LOG_LINES_KEPT:])
    return LogErrors(path=path, count=len(hits), lines=kept)


# --- issues ---------------------------------------------------------------------------------------------------------
class IssueKind(StrEnum):
    """In the order the owner reads them."""

    UNREACHABLE = "unreachable"
    DISK_FULL = "disk_full"
    MEMORY_HIGH = "memory_high"
    UNIT_FAILED = "unit_failed"
    DOCKER_DOWN = "docker_down"  # `docker ps` used to work and now fails: the daemon is down
    CONTAINER_DOWN = "container_down"
    CONTAINER_RESTARTING = "container_restarting"
    CONTAINER_UNHEALTHY = "container_unhealthy"
    LOG_ERRORS = "log_errors"


_ISSUE_ORDER: Final = tuple(IssueKind)
# The probe that re-checks an issue: while it does not run, the issue stays as it was (unknown is not resolved).
_SOURCE: Final[Mapping[IssueKind, ProbeKind]] = {
    IssueKind.DISK_FULL: ProbeKind.DISK,
    IssueKind.MEMORY_HIGH: ProbeKind.MEMORY,
    IssueKind.UNIT_FAILED: ProbeKind.UNITS,
    IssueKind.DOCKER_DOWN: ProbeKind.CONTAINERS,
    IssueKind.CONTAINER_DOWN: ProbeKind.CONTAINERS,
    IssueKind.CONTAINER_RESTARTING: ProbeKind.CONTAINERS,
    IssueKind.CONTAINER_UNHEALTHY: ProbeKind.CONTAINERS,
    IssueKind.LOG_ERRORS: ProbeKind.LOG,
}
_CONTAINER_ISSUES: Final[Mapping[ContainerState, IssueKind]] = {
    ContainerState.DOWN: IssueKind.CONTAINER_DOWN,
    ContainerState.RESTARTING: IssueKind.CONTAINER_RESTARTING,
    ContainerState.UNHEALTHY: IssueKind.CONTAINER_UNHEALTHY,
}


@dataclass(frozen=True, slots=True)
class Issue:
    kind: IssueKind
    subject: str  # the mount, container, unit or log path; "" for the host itself
    since: datetime
    value: str = ""  # the reading shown to the owner ("91" for a disk, "3" error lines)

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.subject}"

    @property
    def source_key(self) -> str | None:
        source = _SOURCE.get(self.kind)
        if source is None:
            return None
        return f"{source.value}:{self.subject}" if source is ProbeKind.LOG else source.value


@dataclass(frozen=True, slots=True)
class Limits:
    """This check's thresholds (`servers.disk_warn_pct`, `servers.memory_warn_pct`)."""

    disk_warn_pct: int
    memory_warn_pct: int


@dataclass(frozen=True, slots=True)
class CheckState:
    """What one check hands the next: the open issues (with when each began), the alert memory (issue key → when it
    was last seen open), the containers last seen running, and how many checks in a row could not connect."""

    issues: tuple[Issue, ...] = ()
    alerted: Mapping[str, datetime] = field(default_factory=dict)
    containers_seen: Mapping[str, datetime] = field(default_factory=dict)
    failures: int = 0


NO_STATE: Final = CheckState()


@dataclass(frozen=True, slots=True)
class Evaluation:
    report: HostReport
    state: CheckState
    new_issues: tuple[Issue, ...]  # not alerted before: the owner is told once

    @property
    def ok(self) -> bool:
        return self.report.reachable and not self.state.issues


def _outcome(outcomes: Sequence[ProbeOutcome], kind: ProbeKind) -> ProbeOutcome | None:
    return next((item for item in outcomes if item.probe.kind is kind and item.status is ProbeStatus.OK), None)


def _record(outcome: ProbeOutcome, scrub: Callable[[str], str]) -> ProbeRecord:
    excerpt = scrubbed_clip(outcome.stdout.strip(), EXCERPT_MAX_CHARS, scrub)
    return ProbeRecord(
        key=outcome.probe.key,
        status=outcome.status,
        exit_code=outcome.exit_code,
        error_code=outcome.error_code,
        excerpt=excerpt,
    )


def _report(host: str, outcomes: Sequence[ProbeOutcome], scrub: Callable[[str], str]) -> HostReport:
    reachable = any(item.status in RAN for item in outcomes)
    error_code = next((item.error_code for item in outcomes if item.status is ProbeStatus.ERROR), None)
    uptime, disk, memory = (_outcome(outcomes, kind) for kind in (ProbeKind.UPTIME, ProbeKind.DISK, ProbeKind.MEMORY))
    containers, units = _outcome(outcomes, ProbeKind.CONTAINERS), _outcome(outcomes, ProbeKind.UNITS)
    load, up = parse_uptime(uptime.stdout) if uptime is not None else (None, None)
    logs = tuple(
        parse_log_errors(item.probe.path, item.stdout, scrub)
        for item in outcomes
        if item.probe.kind is ProbeKind.LOG and item.status is ProbeStatus.OK
    )
    return HostReport(
        host=host,
        reachable=reachable,
        load=load,
        up=up,
        disks=parse_disks(disk.stdout) if disk is not None else (),
        memory=parse_memory(memory.stdout) if memory is not None else None,
        containers=parse_containers(containers.stdout) if containers is not None else None,
        failed_units=parse_failed_units(units.stdout) if units is not None else None,
        logs=logs,
        probes=tuple(_record(item, scrub) for item in outcomes),
        error_code=None if reachable else error_code,
    )


def _seen(report: HostReport, previous: CheckState, now: datetime) -> dict[str, datetime]:
    """Containers last seen running: refreshed when `docker ps` ran; one gone longer than `CONTAINER_GONE_KEEP` is
    forgotten (it was removed on purpose, or the owner has been told for a day)."""
    if report.containers is None:
        return dict(previous.containers_seen)
    running = {item.name for item in report.containers if item.state is not ContainerState.DOWN}
    seen = {name: at for name, at in previous.containers_seen.items() if now - at < CONTAINER_GONE_KEEP}
    seen.update(dict.fromkeys(running, now))
    return seen


def _found(
    report: HostReport, seen: Mapping[str, datetime], limits: Limits, now: datetime, *, docker_failed: bool
) -> list[Issue]:
    """The issues this check's own readings show (`since` is filled in later)."""
    found = [
        Issue(IssueKind.DISK_FULL, disk.mount, now, str(disk.used_pct))
        for disk in report.disks
        if disk.used_pct >= limits.disk_warn_pct
    ]
    if report.memory is not None and report.memory.used_pct >= limits.memory_warn_pct:
        found.append(Issue(IssueKind.MEMORY_HIGH, "", now, str(report.memory.used_pct)))
    found.extend(Issue(IssueKind.UNIT_FAILED, unit, now) for unit in report.failed_units or ())
    if report.containers is not None:
        listed = {item.name for item in report.containers}
        found.extend(
            Issue(_CONTAINER_ISSUES[item.state], item.name, now)
            for item in report.containers
            if item.state in _CONTAINER_ISSUES
        )
        if len(report.containers) < LIST_MAX:  # a cut listing cannot tell a gone container from an unlisted one
            found.extend(Issue(IssueKind.CONTAINER_DOWN, name, now) for name in sorted(seen) if name not in listed)
    elif docker_failed and seen:  # it listed containers before; a permission problem from the start is not this
        found.append(Issue(IssueKind.DOCKER_DOWN, "", now))
    found.extend(Issue(IssueKind.LOG_ERRORS, log.path, now, str(log.count)) for log in report.logs if log.count)
    return found


def _alerts(
    issues: Sequence[Issue], alerted: Mapping[str, datetime], now: datetime
) -> tuple[tuple[Issue, ...], dict[str, datetime]]:
    """Which issues to tell the owner about now, and the alert memory after that: every issue he was told about,
    with when it was last seen open. A resolved one is remembered for `REALERT_QUIET` after that, so an issue that
    flaps (a disk at 89 %, 90 %, 89 %) alerts once."""
    memory = {key: at for key, at in alerted.items() if now - at < REALERT_QUIET}
    new = tuple(issue for issue in issues if issue.key not in memory)
    memory.update({issue.key: now for issue in issues})
    return new, memory


def _ordered(issues: Iterable[Issue]) -> tuple[Issue, ...]:
    return tuple(sorted(issues, key=lambda issue: (_ISSUE_ORDER.index(issue.kind), issue.subject)))


def evaluate(
    host: str,
    outcomes: Sequence[ProbeOutcome],
    previous: CheckState,
    *,
    now: datetime,
    limits: Limits,
    scrub: Callable[[str], str],
) -> Evaluation:
    """One check's outcomes, against the state the last check left (`NO_STATE` for the first)."""
    report = _report(host, outcomes, scrub)
    planned = {item.probe.key for item in outcomes}
    ran = {item.probe.key for item in outcomes if item.status is ProbeStatus.OK}
    docker_failed = any(
        item.probe.kind is ProbeKind.CONTAINERS and item.status is ProbeStatus.FAILED for item in outcomes
    )
    seen = _seen(report, previous, now)
    since = {issue.key: issue.since for issue in previous.issues}
    found = _found(report, seen, limits, now, docker_failed=docker_failed)
    issues = {issue.key: replace(issue, since=since.get(issue.key, now)) for issue in found}
    # What this check meant to re-check but could not stays as it was. A probe no longer planned (a log path the
    # owner removed) takes its issues with it.
    for issue in previous.issues:
        source = issue.source_key
        if source is not None and source in planned and source not in ran and issue.key not in issues:
            issues[issue.key] = issue
    # Only a connection attempt counts: a check the gate refused entirely says nothing about the host.
    attempted = any(item.status in RAN or item.status is ProbeStatus.ERROR for item in outcomes)
    failures = (0 if report.reachable else previous.failures + 1) if attempted else previous.failures
    if failures >= UNREACHABLE_AFTER:
        key = Issue(IssueKind.UNREACHABLE, "", now).key
        issues[key] = Issue(IssueKind.UNREACHABLE, "", since.get(key, now))
    ordered = _ordered(issues.values())
    new, alerted = _alerts(ordered, previous.alerted, now)
    state = CheckState(issues=ordered, alerted=alerted, containers_seen=seen, failures=failures)
    return Evaluation(report=report, state=state, new_issues=new)


# --- schedule and cache ---------------------------------------------------------------------------------------------
def is_fresh(checked_at: datetime | None, now: datetime, *, max_age_min: int) -> bool:
    """UC-15: a check this recent answers a status question directly (`digest.stale_after_min`, 30)."""
    return checked_at is not None and now - checked_at <= timedelta(minutes=max_age_min)


def due_hosts(
    hosts: Sequence[str], checked_at: Mapping[str, datetime], now: datetime, *, interval_min: int
) -> tuple[str, ...]:
    """Hosts whose last check is `schedule.server_check_min` old (or that were never checked)."""
    interval = timedelta(minutes=interval_min)
    return tuple(host for host in hosts if host not in checked_at or now - checked_at[host] >= interval)


def next_pass_in_s(
    hosts: Sequence[str], checked_at: Mapping[str, datetime], now: datetime, *, interval_min: int
) -> float:
    """Seconds until the next host falls due, at most `CHECK_TICK_S` (settings are re-read that often)."""
    interval = timedelta(minutes=interval_min)
    waits = [(checked_at[host] + interval - now).total_seconds() for host in hosts if host in checked_at]
    return max(1.0, min([CHECK_TICK_S, *waits]))


def checking_minutes(check_s: float) -> int:
    """The "~N daqiqa" of `S.SERVERS_CHECKING`."""
    return max(1, math.ceil(check_s / 60))


def retention_cutoff(now: datetime) -> datetime:
    return now - CHECK_KEEP


def mentioned_hosts(text: str, hosts: Sequence[str]) -> tuple[str, ...]:
    """The configured aliases the owner's words name (whole words, any case); () when none is named."""
    lowered = text.lower()
    return tuple(host for host in hosts if re.search(_HOST_WORD.format(re.escape(host.lower())), lowered))


# --- what a reader gets (dev/digest, reports, the personal bridge) --------------------------------------------------
@dataclass(frozen=True, slots=True)
class HostStatus:
    """The cached state of one configured host: the latest `server_checks` row, read without touching SSH."""

    host: str
    checked_at: datetime | None = None  # None: never checked yet
    report: HostReport | None = None
    issues: tuple[Issue, ...] = ()
    error_code: str | None = None
    jump_only: bool = False  # configured, but `gate.jump_only_hosts`: never checked

    @property
    def reachable(self) -> bool:
        return self.report is not None and self.report.reachable

    @property
    def ok(self) -> bool:
        return self.reachable and not self.issues


def status_of(
    host: str, *, checked_at: datetime, report: HostReport | None, state: CheckState, error_code: str | None
) -> HostStatus:
    return HostStatus(host=host, checked_at=checked_at, report=report, issues=state.issues, error_code=error_code)


# --- owner text -----------------------------------------------------------------------------------------------------
def issue_text(issue: Issue) -> BotText:  # noqa: PLR0911  # one line per kind
    match issue.kind:
        case IssueKind.UNREACHABLE:
            return S.SERVERS_ISSUE_UNREACHABLE()
        case IssueKind.DISK_FULL:
            return S.SERVERS_ISSUE_DISK(mount=issue.subject, pct=issue.value)
        case IssueKind.MEMORY_HIGH:
            return S.SERVERS_ISSUE_MEMORY(pct=issue.value)
        case IssueKind.UNIT_FAILED:
            return S.SERVERS_ISSUE_UNIT(unit=issue.subject)
        case IssueKind.DOCKER_DOWN:
            return S.SERVERS_ISSUE_DOCKER_DOWN()
        case IssueKind.CONTAINER_DOWN:
            return S.SERVERS_ISSUE_CONTAINER_DOWN(name=issue.subject)
        case IssueKind.CONTAINER_RESTARTING:
            return S.SERVERS_ISSUE_CONTAINER_RESTARTING(name=issue.subject)
        case IssueKind.CONTAINER_UNHEALTHY:
            return S.SERVERS_ISSUE_CONTAINER_UNHEALTHY(name=issue.subject)
        case IssueKind.LOG_ERRORS:
            return S.SERVERS_ISSUE_LOG(path=issue.subject, count=issue.value)


def issues_text(issues: Iterable[Issue]) -> BotText:
    return join((issue_text(issue) for issue in issues), "; ")


def reason_text(error_code: str | None) -> BotText:
    entry = S.SERVERS_REASONS.get(error_code or "")
    return entry() if entry is not None else S.SERVERS_REASON_UNKNOWN()


def details_text(report: HostReport) -> BotText:
    """The readings in one line: load, the fullest disk, memory, containers."""
    parts: list[BotText] = []
    if report.load is not None:
        parts.append(S.SERVERS_LOAD(load=f"{report.load[0]:.2f}"))
    if report.disks:
        parts.append(S.SERVERS_DISK(mount=report.disks[0].mount, pct=report.disks[0].used_pct))
    if report.memory is not None:
        parts.append(S.SERVERS_MEMORY(pct=report.memory.used_pct))
    if report.containers is not None:
        parts.append(S.SERVERS_CONTAINERS(count=len(report.containers)))
    return join(parts, " · ") if parts else S.SERVERS_NO_DETAILS()


def status_line(status: HostStatus) -> BotText:
    """One host in a status answer or a digest: it starts with the host name."""
    if status.jump_only:
        return S.SERVERS_HOST_JUMP_ONLY(host=status.host)
    if status.report is None:
        return S.SERVERS_HOST_UNCHECKED(host=status.host)
    if status.report.all_refused:
        return S.SERVERS_HOST_REFUSED(host=status.host)
    if not status.report.reachable:
        return S.SERVERS_HOST_UNREACHABLE(host=status.host, reason=reason_text(status.error_code))
    details = details_text(status.report)
    if status.issues:
        return S.SERVERS_HOST_PROBLEMS(host=status.host, issues=issues_text(status.issues), details=details)
    return S.SERVERS_HOST_OK(host=status.host, details=details)


def freshness_text(statuses: Iterable[HostStatus], now: datetime) -> BotText | None:
    """`Ma'lumot: N daqiqa oldin` for the oldest check shown; None if none was checked."""
    times = [status.checked_at for status in statuses if status.checked_at is not None]
    if not times:
        return None
    minutes = max(0, round((now - min(times)).total_seconds() / 60))
    return S.SERVERS_FRESHNESS(minutes=minutes) if minutes else S.SERVERS_FRESH_NOW()


def status_answer(statuses: Sequence[HostStatus], now: datetime) -> BotText:
    lines = [status_line(status) for status in statuses]
    fresh = freshness_text(statuses, now)
    return join([*lines, fresh] if fresh is not None else lines)


def alert_text(host: str, issues: Sequence[Issue]) -> BotText:
    return S.SERVERS_ALERT(host=host, issues=issues_text(issues))


def check_facts(status: HostStatus) -> str:
    """The last check as plain data for an investigation's input (English, like the prompt it fills)."""
    report = status.report
    if status.jump_only:
        return f"- {status.host}: jump-only host, never checked"
    if report is None or status.checked_at is None:
        return f"- {status.host}: not checked yet"
    if not report.reachable:
        return f"- {status.host} ({status.checked_at.isoformat()}): did not answer ({status.error_code or 'unknown'})"
    facts = [f"- {status.host} ({status.checked_at.isoformat()}):"]
    if report.load is not None:
        facts.append(f"load {report.load[0]:.2f} {report.load[1]:.2f} {report.load[2]:.2f};")
    facts.extend(f"disk {disk.mount} {disk.used_pct}%;" for disk in report.disks[:5])
    if report.memory is not None:
        facts.append(f"memory {report.memory.used_pct}% used;")
    if report.containers is not None:
        facts.append(f"{len(report.containers)} containers;")
    facts.append(f"issues: {', '.join(issue.key for issue in status.issues) or 'none'}")
    return " ".join(facts)


# --- storage (`server_checks.summary_json`) -------------------------------------------------------------------------
def _iso(moment: datetime) -> str:
    return moment.isoformat()


def summary_json(report: HostReport, state: CheckState, *, trigger: str) -> JsonObject:
    """Everything one check stores. Excerpts and log lines were clipped and scrubbed when parsed."""
    disks: list[JsonValue] = [
        {"mount": disk.mount, "used_pct": disk.used_pct, "size": disk.size, "avail": disk.avail}
        for disk in report.disks
    ]
    memory: JsonValue = None
    if report.memory is not None:
        memory = {
            "total_mb": report.memory.total_mb,
            "available_mb": report.memory.available_mb,
            "used_pct": report.memory.used_pct,
        }
    containers: JsonValue = None
    if report.containers is not None:
        containers = [
            {"name": item.name, "status": item.status, "state": item.state.value} for item in report.containers
        ]
    units: JsonValue = None if report.failed_units is None else list(report.failed_units)
    logs: list[JsonValue] = [{"path": log.path, "count": log.count, "lines": list(log.lines)} for log in report.logs]
    probes: list[JsonValue] = [
        {
            "key": probe.key,
            "status": probe.status.value,
            "exit_code": probe.exit_code,
            "error_code": probe.error_code,
            "excerpt": probe.excerpt,
        }
        for probe in report.probes
    ]
    issues: list[JsonValue] = [
        {"kind": issue.kind.value, "subject": issue.subject, "value": issue.value, "since": _iso(issue.since)}
        for issue in state.issues
    ]
    alerted: JsonObject = {key: _iso(at) for key, at in state.alerted.items()}
    seen: JsonObject = {name: _iso(at) for name, at in state.containers_seen.items()}
    load: JsonValue = None if report.load is None else list(report.load)
    return {
        "v": SUMMARY_VERSION,
        "trigger": trigger,
        "reachable": report.reachable,
        "error_code": report.error_code,
        "load": load,
        "up": report.up,
        "disks": disks,
        "memory": memory,
        "containers": containers,
        "failed_units": units,
        "logs": logs,
        "probes": probes,
        "issues": issues,
        "alerted": alerted,
        "containers_seen": seen,
        "failures": state.failures,
    }


def _str(value: JsonValue) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _time(value: JsonValue) -> datetime | None:
    text = _str(value)
    if text is None:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _objects(value: JsonValue) -> list[dict[str, JsonValue]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _times(value: JsonValue) -> dict[str, datetime]:
    if not isinstance(value, dict):
        return {}
    return {key: at for key, raw in value.items() if (at := _time(raw)) is not None}


def _issues(value: JsonValue) -> tuple[Issue, ...]:
    known = {kind.value for kind in IssueKind}
    issues = [
        Issue(IssueKind(kind), _str(item.get("subject")) or "", since, _str(item.get("value")) or "")
        for item in _objects(value)
        if (kind := _str(item.get("kind"))) in known and (since := _time(item.get("since"))) is not None
    ]
    return _ordered(issues)


def _load(value: JsonValue) -> tuple[float, float, float] | None:
    if not isinstance(value, list) or len(value) != 3:
        return None
    numbers = [float(item) for item in value if isinstance(item, (int, float)) and not isinstance(item, bool)]
    return (numbers[0], numbers[1], numbers[2]) if len(numbers) == 3 else None


def _disks(value: JsonValue) -> tuple[DiskUse, ...]:
    return tuple(
        DiskUse(mount=mount, used_pct=pct, size=_str(item.get("size")) or "", avail=_str(item.get("avail")) or "")
        for item in _objects(value)
        if (mount := _str(item.get("mount"))) is not None and (pct := _int(item.get("used_pct"))) is not None
    )


def _memory(value: JsonValue) -> MemoryUse | None:
    if not isinstance(value, dict):
        return None
    total, available, pct = (_int(value.get(name)) for name in ("total_mb", "available_mb", "used_pct"))
    if total is None or available is None or pct is None:
        return None
    return MemoryUse(total_mb=total, available_mb=available, used_pct=pct)


def _containers(value: JsonValue) -> tuple[Container, ...] | None:
    if not isinstance(value, list):
        return None
    states = {state.value for state in ContainerState}
    return tuple(
        Container(name=name, status=_str(item.get("status")) or "", state=ContainerState(state))
        for item in _objects(value)
        if (name := _str(item.get("name"))) is not None and (state := _str(item.get("state"))) in states
    )


def _strings(value: JsonValue) -> tuple[str, ...] | None:
    return tuple(item for item in value if isinstance(item, str)) if isinstance(value, list) else None


def _logs(value: JsonValue) -> tuple[LogErrors, ...]:
    return tuple(
        LogErrors(path=path, count=count, lines=_strings(item.get("lines")) or ())
        for item in _objects(value)
        if (path := _str(item.get("path"))) is not None and (count := _int(item.get("count"))) is not None
    )


def _probes(value: JsonValue) -> tuple[ProbeRecord, ...]:
    statuses = {status.value for status in ProbeStatus}
    return tuple(
        ProbeRecord(
            key=key,
            status=ProbeStatus(status),
            exit_code=_int(item.get("exit_code")),
            error_code=_str(item.get("error_code")),
            excerpt=_str(item.get("excerpt")) or "",
        )
        for item in _objects(value)
        if (key := _str(item.get("key"))) is not None and (status := _str(item.get("status"))) in statuses
    )


def read_summary(host: str, raw: JsonValue) -> tuple[HostReport | None, CheckState]:
    """The inverse of `summary_json`. A malformed or unknown summary reads as "no report" and an empty state, so
    the next check starts over rather than failing."""
    if not isinstance(raw, dict) or raw.get("v") != SUMMARY_VERSION:
        return None, NO_STATE
    report = HostReport(
        host=host,
        reachable=raw.get("reachable") is True,
        load=_load(raw.get("load")),
        up=_str(raw.get("up")),
        disks=_disks(raw.get("disks")),
        memory=_memory(raw.get("memory")),
        containers=_containers(raw.get("containers")),
        failed_units=_strings(raw.get("failed_units")),
        logs=_logs(raw.get("logs")),
        probes=_probes(raw.get("probes")),
        error_code=_str(raw.get("error_code")),
    )
    state = CheckState(
        issues=_issues(raw.get("issues")),
        alerted=_times(raw.get("alerted")),
        containers_seen=_times(raw.get("containers_seen")),
        failures=_int(raw.get("failures")) or 0,
    )
    return report, state
