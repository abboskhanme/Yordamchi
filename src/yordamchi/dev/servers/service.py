"""Server checks every 30 min and on demand, and read-only server investigations (SPEC §3.2.4, §4.2 `server_checks`,
§5.3 investigation, §5.7; UC-15; REQUIREMENTS Q3).

- **Periodic.** `run_server_checks(deps)` runs under `core.aio.forever`. Each pass (`check_due_servers`) checks every
  host of `servers.hosts` whose last check is `schedule.server_check_min` old, all hosts in parallel. An issue the
  owner was not told about yet is alerted once (`S.SERVERS_ALERT`), queued in the unit of work of the check's row.
- **Cache.** The newest `server_checks` row per host is the cache. `latest_statuses(deps)` reads it without SSH: the
  narrow interface for `dev/digest` and reports (`domain.status_line` renders one host).
- **On demand** ("serverda nima bo'lyapti?"): `answer_server_status` answers from checks at most
  `digest.stale_after_min` old. Otherwise it queues `S.SERVERS_CHECKING` before any SSH call, checks the stale hosts
  afresh (each within `timeouts.server_check_s`) and answers. Issues shown in that answer are not alerted again.
- **Investigation** (a question about why something on a server is wrong): `investigate_server` queues the same ack
  and a `TurnKind.INVESTIGATE` turn in the project's own read-only session. Its policy is READ_ONLY, so the gate
  denies (never asks) a write or a remote command off the read-only list; `turns` delivers the result.
- **Hosts by chat**: `add_server_host` / `remove_server_host`; the intent rows are `bots/dev`'s.

Every remote command is the gate's call (`domain.refused` → `dev/gate/domain.shim_verdict`): the same read-only list
and jump-only hosts the executor and the ssh shim are judged by. A jump-only host is never a target, even if listed.
SSH runs outside units of work (A3); each host's result is one short unit of work. Logs carry hosts, counts, codes and
durations, never command output.
"""

import asyncio
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from yordamchi import prompts
from yordamchi.core import json_value, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import ExternalError, ServerJumpOnly, ServerNotWatched
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate.domain import GateContext, GateRules, Policy
from yordamchi.dev.projects import service as projects
from yordamchi.dev.servers import domain, store
from yordamchi.dev.servers.domain import (
    NO_STATE,
    CheckState,
    HostStatus,
    Limits,
    Probe,
    ProbeOutcome,
    ProbeStatus,
)
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns.domain import INVESTIGATION_WALL, TurnKind, TurnRequest
from yordamchi.dev.turns.service import Submitted, TurnsState
from yordamchi.persistence import bulk
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)

INVESTIGATE_PROMPT = "dev/servers_investigate"


class Trigger(StrEnum):
    SCHEDULED = "scheduled"  # the 30-min loop: new issues are alerted
    ON_DEMAND = "on_demand"  # the owner asked: the answer shows the issues, so they are not alerted as well


@dataclass(frozen=True, slots=True)
class ChecksPass:
    """One pass of the loop: the hosts it checked, and how long to wait before the next pass."""

    checked: tuple[HostStatus, ...]
    next_in_s: float


@dataclass(frozen=True, slots=True)
class StatusAnswer:
    """What `answer_server_status` queued last. `checked`: the hosts checked afresh for it; () = from the cache."""

    reply: BotText
    checked: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Latest:
    status: HostStatus
    state: CheckState


# --- helpers --------------------------------------------------------------------------------------------------------
def _rules(snap: Snapshot) -> GateRules:
    """The gate's `gate.*` values of this snapshot: the one read-only list and jump-only list."""
    return GateRules.from_settings(
        ask_classes=snap[defaults.GATE_ASK_CLASSES],
        readonly_remote_commands=snap[defaults.GATE_READONLY_REMOTE_COMMANDS],
        jump_only_hosts=snap[defaults.GATE_JUMP_ONLY_HOSTS],
        git_hosts=snap[defaults.GATE_GIT_HOSTS],
        http_write_allow_hosts=snap[defaults.GATE_HTTP_WRITE_ALLOW_HOSTS],
        temp_roots=snap[defaults.GATE_TEMP_ROOTS],
    )


def _gate_context(deps: DevDeps) -> GateContext:
    """Yordamchi's own checks run in no project: only the policy matters to the ssh rule."""
    home = str(deps.paths.home)
    return GateContext(
        policy=Policy.READ_ONLY,
        home=home,
        cwd=home,
        project_root=home,
        data_dir=str(deps.paths.data),
        config_dir=str(deps.paths.config),
    )


def _limits(snap: Snapshot) -> Limits:
    return Limits(
        disk_warn_pct=snap[defaults.SERVERS_DISK_WARN_PCT], memory_warn_pct=snap[defaults.SERVERS_MEMORY_WARN_PCT]
    )


def _targets(hosts: tuple[str, ...], rules: GateRules) -> tuple[str, ...]:
    return tuple(host for host in hosts if not domain.is_jump_only(host, rules))


async def _latest(deps: DevDeps, hosts: tuple[str, ...]) -> dict[str, _Latest]:
    """The newest check of each host, one query."""
    async with deps.db.read() as s:
        rows = await store.list_latest_checks(s, hosts)
    latest: dict[str, _Latest] = {}
    for row in rows:
        report, state = domain.read_summary(row.host, row.summary_json)
        status = domain.status_of(row.host, checked_at=row.at, report=report, state=state, error_code=row.error_code)
        latest[row.host] = _Latest(status=status, state=state)
    return latest


async def _tell(
    deps: DevDeps,
    snap: Snapshot,
    text: BotText,
    *,
    chat_id: int,
    ref: str,
    priority: int | None = None,
    reply_to: int | None = None,
) -> None:
    """One reply to the owner, in its own unit of work (keyed by `ref`, so a retried request is not answered twice)."""
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=text,
            kind=Kind.REPORT,
            ref=f"servers-{ref}",
            at=deps.clock.now(),
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            priority=priority,
            reply_to=reply_to,
        )


# --- one check of one host ------------------------------------------------------------------------------------------
async def _run_probes(
    deps: DevDeps,
    host: str,
    probes: tuple[Probe, ...],
    *,
    rules: GateRules,
    command_s: float,
    check_s: float,
) -> list[ProbeOutcome]:
    """Run the probes the gate allows, one at a time, within the host's budget. If the first command cannot
    connect, the host is down: the rest are skipped instead of each waiting out its own timeout."""
    ctx = _gate_context(deps)
    deadline = deps.clock.now() + timedelta(seconds=check_s)
    outcomes: list[ProbeOutcome] = []
    answered: bool | None = None  # None: no command has run yet
    for probe in probes:
        now = deps.clock.now()
        if domain.refused(host, probe, rules=rules, ctx=ctx, now=now):
            outcomes.append(ProbeOutcome(probe, ProbeStatus.REFUSED))
            continue
        timeout = domain.command_timeout_s((deadline - now).total_seconds(), command_s)
        if timeout is None or answered is False:
            outcomes.append(ProbeOutcome(probe, ProbeStatus.SKIPPED))
            continue
        try:
            result = await deps.remote_shell.run(host, probe.command, timeout=timeout)
        except ExternalError as error:
            outcomes.append(ProbeOutcome(probe, ProbeStatus.ERROR, error_code=error.code.value))
            if answered is None:
                answered = False
            continue
        answered = True
        status = ProbeStatus.OK if result.exit_code == 0 else ProbeStatus.FAILED
        stdout = domain.kept_output(probe, result.stdout)
        outcomes.append(ProbeOutcome(probe, status, exit_code=result.exit_code, stdout=stdout))
    return outcomes


async def _check_host(
    deps: DevDeps, snap: Snapshot, host: str, previous: CheckState, *, trigger: Trigger
) -> HostStatus:
    started = time.monotonic()
    rules = _rules(snap)
    probes = domain.plan_probes(domain.log_paths_for(host, snap[defaults.SERVERS_LOG_PATHS]))
    outcomes = await _run_probes(
        deps,
        host,
        probes,
        rules=rules,
        command_s=snap[defaults.TIMEOUTS_SERVER_COMMAND_S],
        check_s=snap[defaults.TIMEOUTS_SERVER_CHECK_S],
    )
    now = deps.clock.now()
    evaluation = domain.evaluate(host, outcomes, previous, now=now, limits=_limits(snap), scrub=redact.scrubbed)
    alerts = evaluation.new_issues if trigger is Trigger.SCHEDULED else ()
    summary = json_value.map_strings(
        domain.summary_json(evaluation.report, evaluation.state, trigger=trigger.value), redact.scrubbed
    )
    duration_ms = round((time.monotonic() - started) * 1000)
    async with deps.db.uow() as s:
        row = await store.add_server_check(
            s,
            host=host,
            at=now,
            ok=evaluation.ok,
            summary=summary,
            error_code=evaluation.report.error_code,
            duration_ms=duration_ms,
        )
        if alerts:
            await outbox.enqueue_text(
                s,
                bot=deps.bot,
                chat_id=snap[defaults.ACCESS_DEV_GROUP_ID],
                text=domain.alert_text(host, alerts),
                kind=Kind.NOTICE,
                ref=f"server-check-{row.id}",
                at=now,
                max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            )
    log.info(
        "server_checked",
        host=host,
        trigger=trigger.value,
        ok=evaluation.ok,
        reachable=evaluation.report.reachable,
        issues=len(evaluation.state.issues),
        alerted=len(alerts),
        refused=sum(1 for item in outcomes if item.status is ProbeStatus.REFUSED),
        error_code=evaluation.report.error_code,
        duration_ms=duration_ms,
    )
    return domain.status_of(
        host, checked_at=now, report=evaluation.report, state=evaluation.state, error_code=evaluation.report.error_code
    )


async def _check_hosts(
    deps: DevDeps, snap: Snapshot, hosts: tuple[str, ...], latest: dict[str, _Latest], *, trigger: Trigger
) -> tuple[HostStatus, ...]:
    """Every host in parallel: one slow or unreachable server never delays the others."""
    if not hosts:
        return ()
    async with asyncio.TaskGroup() as group:
        tasks = [
            group.create_task(
                _check_host(deps, snap, host, latest[host].state if host in latest else NO_STATE, trigger=trigger)
            )
            for host in hosts
        ]
    return tuple(task.result() for task in tasks)


# --- the 30-min loop (SPEC §3.2.4 refresh triggers) -----------------------------------------------------------------
async def check_due_servers(deps: DevDeps) -> ChecksPass:
    """One pass: check the hosts that are due. A jump-only host in `servers.hosts` is never checked."""
    snap = await settings.snapshot(deps)
    hosts = _targets(tuple(snap[defaults.SERVERS_HOSTS]), _rules(snap))
    interval_min = snap[defaults.SCHEDULE_SERVER_CHECK_MIN]
    latest = await _latest(deps, hosts)
    checked_at = {host: item.status.checked_at for host, item in latest.items() if item.status.checked_at is not None}
    due = domain.due_hosts(hosts, checked_at, deps.clock.now(), interval_min=interval_min)
    checked = await _check_hosts(deps, snap, due, latest, trigger=Trigger.SCHEDULED)
    checked_at.update({status.host: status.checked_at for status in checked if status.checked_at is not None})
    wait_s = domain.next_pass_in_s(hosts, checked_at, deps.clock.now(), interval_min=interval_min)
    return ChecksPass(checked=checked, next_in_s=wait_s)


async def run_server_checks(deps: DevDeps) -> None:
    """The long-lived loop: `aio.forever("servers", lambda: servers.run_server_checks(deps))`. It re-reads the
    settings at least every `domain.CHECK_TICK_S`, so a host the owner adds is checked within a minute."""
    while True:
        done = await check_due_servers(deps)
        await deps.clock.sleep(done.next_in_s)


# --- the cache, for dev/digest and reports --------------------------------------------------------------------------
async def latest_statuses(deps: DevDeps) -> tuple[HostStatus, ...]:
    """One status per host of `servers.hosts`, in its order, from the newest check (`checked_at` None: not checked
    yet; `jump_only`: never checked). DB only, one query: safe on the ≤ 4 s report path."""
    snap = await settings.snapshot(deps)
    rules = _rules(snap)
    hosts = tuple(snap[defaults.SERVERS_HOSTS])
    latest = await _latest(deps, _targets(hosts, rules))
    statuses = {host: item.status for host, item in latest.items()}
    return tuple(_shown(host, statuses, rules) for host in hosts)


def _shown(host: str, statuses: Mapping[str, HostStatus], rules: GateRules) -> HostStatus:
    if domain.is_jump_only(host, rules):
        return HostStatus(host=host, jump_only=True)
    return statuses.get(host) or HostStatus(host=host)


# --- on demand (UC-15: cached ≤ 30 min, else "Tekshiryapman" ≤ 2 s and the answer ≤ 3 min) --------------------------
async def answer_server_status(
    deps: DevDeps, *, chat_id: int, ref: str, text: str = "", reply_to: int | None = None
) -> StatusAnswer:
    """ "serverda nima bo'lyapti?": one line per host the words name (every host if they name none), then how old the
    data is. Run it as a background job: a fresh check can take up to `timeouts.server_check_s`. `ref` keys the
    replies (e.g. `route-<id>`)."""
    snap = await settings.snapshot(deps)
    rules = _rules(snap)
    hosts = tuple(snap[defaults.SERVERS_HOSTS])
    if not hosts:
        reply = S.SERVERS_NONE()
        await _tell(deps, snap, reply, chat_id=chat_id, ref=ref, reply_to=reply_to)
        return StatusAnswer(reply=reply)
    wanted = domain.mentioned_hosts(text, hosts) or hosts
    targets = _targets(wanted, rules)
    latest = await _latest(deps, targets)
    statuses = {host: item.status for host, item in latest.items()}
    max_age_min = snap[defaults.DIGEST_STALE_AFTER_MIN]
    now = deps.clock.now()
    stale = tuple(
        host
        for host in targets
        if not domain.is_fresh(statuses[host].checked_at if host in statuses else None, now, max_age_min=max_age_min)
    )
    if stale:
        minutes = domain.checking_minutes(snap[defaults.TIMEOUTS_SERVER_CHECK_S])
        checking = S.SERVERS_CHECKING(minutes=minutes, hosts=", ".join(stale))
        await _tell(deps, snap, checking, chat_id=chat_id, ref=f"{ref}-checking", priority=0, reply_to=reply_to)
        statuses.update(
            {status.host: status for status in await _check_hosts(deps, snap, stale, latest, trigger=Trigger.ON_DEMAND)}
        )
    reply = domain.status_answer([_shown(host, statuses, rules) for host in wanted], deps.clock.now())
    await _tell(deps, snap, reply, chat_id=chat_id, ref=ref, reply_to=reply_to)
    log.info("server_status_answered", hosts=len(wanted), checked=len(stale))
    return StatusAnswer(reply=reply, checked=stale)


async def investigate_server(
    deps: DevDeps,
    state: TurnsState,
    *,
    project_id: str,
    question: str,
    chat_id: int,
    ref: str,
    burst_id: int | None = None,
) -> Submitted | None:
    """A read-only investigation of the servers the question names (all of them if it names none), in `project_id`'s
    investigation session: the ack and the turn are queued in one unit of work, then the scheduler is poked. None
    when no server is configured (the owner is told so). `ProjectNotFound` / `ServerJumpOnly` raise before anything
    is written."""
    snap = await settings.snapshot(deps)
    rules = _rules(snap)
    hosts = tuple(snap[defaults.SERVERS_HOSTS])
    if not hosts:
        await _tell(deps, snap, S.SERVERS_NONE(), chat_id=chat_id, ref=ref)
        return None
    named = domain.mentioned_hosts(question, hosts) or hosts
    targets = _targets(named, rules)
    if not targets:
        raise ServerJumpOnly(host=named[0])
    project = await projects.get_project(deps, project_id)
    statuses = {host: item.status for host, item in (await _latest(deps, targets)).items()}
    facts = "\n".join(domain.check_facts(_shown(host, statuses, rules)) for host in targets)
    text = prompts.render(
        INVESTIGATE_PROMPT,
        question=question.strip(),
        hosts=", ".join(targets),
        jump_hosts=", ".join(sorted(rules.jump_only_hosts)) or "none",
        commands="\n".join(f"- `{command}`" for command in rules.readonly_remote_commands),
        last_check=redact.scrubbed(facts),
    )
    request = TurnRequest(project_id=project.id, text=text, kind=TurnKind.INVESTIGATE, burst_id=burst_id)
    checking = S.SERVERS_CHECKING(
        minutes=domain.checking_minutes(INVESTIGATION_WALL.total_seconds()), hosts=", ".join(targets)
    )
    now = deps.clock.now()
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=checking,
            kind=Kind.REPORT,
            ref=f"servers-{ref}-checking",
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            priority=0,
        )
        submitted = await turns.submit(s, request=request, at=now)
    state.poke()
    log.info("server_investigation_submitted", project=project.id, turn_id=submitted.turn_id, hosts=len(targets))
    return submitted


# --- hosts by chat (`servers.hosts`; the intent rows are bots/dev's) ------------------------------------------------
async def add_server_host(deps: DevDeps, origin: OwnerOrigin, host: str) -> BotText:
    """ "bonvi-srv serverini kuzat": watch one more `~/.ssh/config` alias. A jump-only host is refused
    (`ServerJumpOnly`); a malformed alias raises `SettingInvalid`. The caller sends the reply."""
    snap = await settings.snapshot(deps)
    alias = host.strip()
    if domain.is_jump_only(alias, _rules(snap)):
        raise ServerJumpOnly(host=alias)
    hosts = list(snap[defaults.SERVERS_HOSTS])
    if alias.lower() in {known.lower() for known in hosts}:
        return S.SERVERS_HOST_KNOWN(host=alias)
    value = settings.as_json([*hosts, alias])
    await settings.record_version(deps, origin, defaults.SERVERS_HOSTS, value, reason="change")
    log.info("server_host_added", host=alias, hosts=len(hosts) + 1)
    return S.SERVERS_HOST_ADDED(host=alias, minutes=snap[defaults.SCHEDULE_SERVER_CHECK_MIN])


async def remove_server_host(deps: DevDeps, origin: OwnerOrigin, host: str) -> BotText:
    """ "bonvi-srv'ni kuzatma": stop watching an alias; its past checks age out with retention."""
    snap = await settings.snapshot(deps)
    hosts = list(snap[defaults.SERVERS_HOSTS])
    alias = host.strip()
    known = next((item for item in hosts if item.lower() == alias.lower()), None)
    if known is None:
        raise ServerNotWatched(host=alias, hosts=", ".join(hosts) or S.SERVERS_NONE_LISTED())
    remaining = [item for item in hosts if item != known]
    await settings.record_version(deps, origin, defaults.SERVERS_HOSTS, settings.as_json(remaining), reason="change")
    log.info("server_host_removed", host=known, hosts=len(remaining))
    return S.SERVERS_HOST_REMOVED(host=known)


# --- retention (SPEC §4.2: 30 d) ------------------------------------------------------------------------------------
async def apply_retention(deps: DevDeps) -> int:
    """Delete checks older than 30 days, in batches. Returns how many rows went."""
    cutoff = domain.retention_cutoff(deps.clock.now())
    deleted = await bulk.run_in_batches(deps.db, lambda s, limit: store.delete_checks_before(s, cutoff, limit=limit))
    log.info("server_checks_retention_applied", rows_deleted=deleted)
    return deleted
