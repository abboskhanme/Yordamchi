"""The interrupt backstop's kill list (SPEC §3.2.5 step 3; docs/SPIKES.md S2).

The SDK spawns the CLI without `setsid`, so the CLI shares the bot's process group; each Bash tool call leads its own
group. Killing "the CLI's group" would kill the bot. So the backstop never calls `killpg(getpgid(cli_pid))`: it
lists the CLI's descendants and signals
- every process group led by a descendant (a tool's shell and everything it started), and
- by pid, the CLI itself and any descendant still in the bot's group.

Decision (T10): keep the shared group rather than spawning the CLI in its own session. launchd kills a job's whole
process group when the bot exits (STACK §4.6), so a crashed bot still takes its `claude` children with it
(NFR-REL-07); a CLI in its own session would outlive it. A command that calls `setsid` itself escapes both.
"""

import contextlib
import os
import signal
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from yordamchi.core import proc

PS_ARGV: Final = ("/bin/ps", "-A", "-o", "pid=,ppid=,pgid=")


@dataclass(frozen=True, slots=True)
class ProcRow:
    pid: int
    ppid: int
    pgid: int


@dataclass(frozen=True, slots=True)
class KillPlan:
    groups: tuple[int, ...]  # killpg targets, never the bot's own group
    pids: tuple[int, ...]  # kill targets: the CLI and descendants left in the bot's group

    @property
    def empty(self) -> bool:
        return not self.groups and not self.pids


def parse_ps(text: str) -> list[ProcRow]:
    rows: list[ProcRow] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            continue
        rows.append(ProcRow(pid=int(parts[0]), ppid=int(parts[1]), pgid=int(parts[2])))
    return rows


def descendants(rows: Iterable[ProcRow], root: int) -> list[ProcRow]:
    children: dict[int, list[ProcRow]] = {}
    for row in rows:
        children.setdefault(row.ppid, []).append(row)
    found: list[ProcRow] = []
    stack = [root]
    seen = {root}
    while stack:
        for child in children.get(stack.pop(), ()):
            if child.pid not in seen:
                seen.add(child.pid)
                found.append(child)
                stack.append(child.pid)
    return found


def kill_plan(rows: Sequence[ProcRow], *, root: int, own_pgid: int, own_pid: int) -> KillPlan:
    tree = descendants(rows, root)
    tree_pids = {row.pid for row in tree}
    # Only groups whose leader is in the tree: a group the tree merely joined is not ours to kill.
    groups = sorted({row.pgid for row in tree if row.pgid in tree_pids and row.pgid not in {own_pgid, 0, 1}})
    loose = [row.pid for row in tree if row.pgid not in groups]
    pids = [pid for pid in dict.fromkeys([root, *loose]) if pid not in {own_pid, 0, 1}]
    return KillPlan(groups=tuple(groups), pids=tuple(pids))


def send(plan: KillPlan, sig: signal.Signals, *, own_pgid: int) -> int:
    """Signal the plan; returns how many targets were still there. Never touches the bot's own group."""
    hit = 0
    for group in plan.groups:
        if group == own_pgid:
            continue
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, sig)
            hit += 1
    for pid in plan.pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, sig)
            hit += 1
    return hit


async def list_processes(*, env: Mapping[str, str], timeout: float) -> list[ProcRow]:
    result = await proc.run(PS_ARGV, env=env, timeout=timeout)
    return parse_ps(result.stdout.decode("ascii", errors="replace")) if result.returncode == 0 else []
