"""Digest: the fast report path, the digest refresh and the handoff note (SPEC §3.2.4, §4.2 `digests`; UC-15, D10,
NFR-SPD-01).

How the pieces fit:
- **Reports.** `answer_report(deps, state, question)` answers `report_project`, `report_all` and `server_status` (T34:
  after `turns.route_turn` wrote no delivery). It reads the `digests` rows, the live turn rows
  (`turns.list_recent_turns`, `turns.list_active_turns`) and the pending decisions: database reads only. It never
  enters the scheduler, never touches a session or the executor, and never waits on a turn or a refresh. A plain
  question gets the template; a free-form one the warm `report` Assistant within what is left of the report budget
  (the template on timeout). Every report ends with the freshness line. A stale digest (`digest.stale_after_min`) is
  refreshed from git first, within 1 s, and the line says so.
- **A fresh look.** A question the digest cannot answer ("nega test yiqilyapti?", "serverda nima bo'lyapti?", or
  routing's `needs_fresh`) gets "Tekshiryapman (~3 daqiqa)" in the same unit of work that queues a read-only
  `investigate` turn (`turns.submit`); that turn's result follows (≤ 3 min, the turns' investigation wall).
- **Refresh.** `refresh_after_turn` is turns' `turn_ended` hook. `run_digest_poll` reads git cheaply (`git status`)
  every `schedule.digest_active_poll_s` for projects active within 7 days and every `schedule.digest_idle_poll_min`
  for the rest; a change refreshes fully (`git log -10 --since=7.days`, today's files). Git is read outside any unit
  of work and the result written in one short one (A3). One refresh per project at a time (`DigestState.lock_of`); a
  report never waits for that lock.
- **Handoff note.** The latest finished turn's note (`turns.latest_handoff`) is stored as it is. One without the
  executor's `<handoff>` block is written by the report Assistant from the previous note, the last 20 progress lines
  and the git facts (`synthesized`), only in background refreshes. While it writes, a free-form report gets the
  template, so a report never queues behind it on the warm client.
- **Servers** (T31): `DigestState.server_checks` reads the latest checks; reports show them, and `server_status` is
  answered from them while they are fresh (else `ReportAnswer.needs_server_check`: `dev/servers` checks on demand).

Logs carry ids, codes, counts and durations, never text.
"""

import asyncio
import secrets
import time
from collections import Counter, defaultdict
from collections.abc import Awaitable, Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from yordamchi import prompts
from yordamchi.core import json_value, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import CallTimedOut, ExternalError, ProjectNotFound
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText, join
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.digest import domain, store
from yordamchi.dev.digest.domain import (
    Digest,
    Freshness,
    GitRead,
    GitState,
    HandoffState,
    ProjectState,
    ReportMode,
    ReportPlan,
    ReportQuestion,
    ReportScope,
    ServerCheck,
)
from yordamchi.dev.digest.models import Digest as DigestRow
from yordamchi.dev.gate import service as gate
from yordamchi.dev.projects import service as projects
from yordamchi.dev.projects.domain import Project
from yordamchi.dev.turns import domain as turns_domain
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns.domain import HandoffNote, TurnKind, TurnRequest, TurnSummary
from yordamchi.dev.turns.service import Submitted, TurnsState
from yordamchi.ports.assistant import AssistantReply, AssistantRequest
from yordamchi.ports.git import Commit
from yordamchi.shared.cost import domain as cost_domain
from yordamchi.shared.cost import service as cost
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import About, Kind, Thread
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)

SYSTEM_PROMPT: Final = "dev/report_system"
CONTEXT_PROMPT: Final = "dev/report_context"
FALLBACK_PROMPT: Final = "dev/digest_handoff_fallback"
_PROVIDERS: Final = {"claude": Provider.ANTHROPIC_PLAN, "gemini": Provider.GOOGLE, "openrouter": Provider.OPENROUTER}

# `dev/servers` (T31): the latest read-only check of each host in `servers.hosts`.
type ServerChecksReader = Callable[[DevDeps], Coroutine[object, object, Sequence[ServerCheck]]]


@dataclass(slots=True)
class DigestState:
    """Built once in `bots/dev/app.py`, next to the `TurnsState` it queues investigations on.

    `server_checks` is how `dev/servers` (T31) hands the latest checks in. `synthesizing` counts handoff notes the
    report Assistant is writing now: free-form reports take the template meanwhile."""

    turns: TurnsState
    server_checks: ServerChecksReader | None = None
    locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    synthesizing: int = 0

    def lock_of(self, project_id: str) -> asyncio.Lock:
        """One refresh of a project at a time; a report only checks `locked()` and never waits for it."""
        return self.locks.setdefault(project_id, asyncio.Lock())


@dataclass(frozen=True, slots=True)
class ReportAnswer:
    """What `answer_report` did. `text` is the message queued (None: nothing was sent, see `needs_server_check`)."""

    plan: ReportPlan
    text: BotText | None = None
    provider: str | None = None  # the Assistant that answered; None: the template
    refreshed: bool = False  # a stale digest was refreshed first
    investigation: Submitted | None = None  # the read-only turn queued behind "Tekshiryapman"
    needs_server_check: bool = False  # `server_status` and no fresh check: `dev/servers` checks on demand (T31)


class _OutOfTime(Exception):  # never leaves this module
    """A report's budget for refreshing a stale digest ran out: nothing is written, the old digest answers."""


@dataclass(frozen=True, slots=True)
class _Refreshed:
    digest: Digest
    previous: Digest | None


# --- rows -----------------------------------------------------------------------------------------------------------
def _digest(row: DigestRow) -> Digest:
    source = domain.handoff_source(row.handoff_note_source)
    handoff = (
        HandoffState(row.handoff_note, row.handoff_note_at, source, row.handoff_turn_id)
        if row.handoff_note and row.handoff_note_at is not None and source is not None
        else None
    )
    git = GitState(
        branch=row.git_branch,
        head=row.git_head,
        head_subject=row.git_head_subject,
        head_at=row.git_head_at,
        upstream=row.git_upstream,
        ahead=row.ahead,
        behind=row.behind,
        dirty=row.dirty_count,
        untracked=row.untracked_count,
        error_code=row.git_error_code,
    )
    return Digest(
        project_id=row.project_id,
        checked_at=row.checked_at,
        updated_at=row.updated_at,
        git=git,
        commits=domain.commits_from_json(row.recent_commits_json),
        files=domain.files_from_json(row.recent_files_json),
        files_total=row.recent_files_count,
        results=domain.results_from_json(row.results_json),
        open_questions=row.open_questions,
        pending_decisions=row.pending_decisions,
        queued=row.queued,
        parked=row.parked,
        handoff=handoff,
        executor=row.executor,
        session_id=row.session_id,
    )


def _values(digest: Digest) -> dict[str, object]:
    """The row's values; every ✱ column (SPEC §4.2) passes `core/redact` once more on the way in."""
    scrub = redact.scrubbed
    last, handoff, git = digest.last_done, digest.handoff, digest.git
    return {
        "updated_at": digest.updated_at,
        "checked_at": digest.checked_at,
        "git_branch": git.branch,
        "git_head": git.head,
        "git_head_subject": scrub(git.head_subject) if git.head_subject else None,
        "git_head_at": git.head_at,
        "git_upstream": git.upstream,
        "ahead": git.ahead,
        "behind": git.behind,
        "dirty_count": git.dirty,
        "untracked_count": git.untracked,
        "git_error_code": git.error_code,
        "recent_commits_json": json_value.map_strings(domain.commits_json(digest.commits), scrub),
        "recent_files_json": json_value.map_strings(domain.files_json(digest.files), scrub),
        "recent_files_count": digest.files_total,
        "results_json": json_value.map_strings(domain.results_json(digest.results), scrub),
        "last_natija": scrub(last.natija) if last is not None and last.natija else None,
        "last_natija_at": last.at if last is not None else None,
        "last_turn_id": digest.last_turn_id,
        "open_questions": digest.open_questions,
        "pending_decisions": digest.pending_decisions,
        "queued": digest.queued,
        "parked": digest.parked,
        "handoff_note": scrub(handoff.text) if handoff is not None else None,
        "handoff_note_at": handoff.at if handoff is not None else None,
        "handoff_note_source": handoff.source if handoff is not None else None,
        "handoff_turn_id": handoff.turn_id if handoff is not None else None,
        "executor": digest.executor,
        "session_id": digest.session_id,
    }


async def _load(deps: DevDeps, project_id: str) -> Digest | None:
    async with deps.db.read() as s:
        row = await store.get_digest(s, project_id)
    return None if row is None else _digest(row)


async def _load_all(deps: DevDeps) -> dict[str, Digest]:
    async with deps.db.read() as s:
        rows = await store.list_digests(s)
    return {row.project_id: _digest(row) for row in rows}


async def get_digest(deps: DevDeps, project_id: str) -> Digest | None:
    """The project's stored digest (for `holat?`, T32), or None before its first refresh."""
    return await _load(deps, project_id)


async def list_digests(deps: DevDeps) -> tuple[Digest, ...]:
    """Every stored digest, by project id."""
    return tuple((await _load_all(deps)).values())


# --- the report Assistant (outside any transaction) -----------------------------------------------------------------
def _instructions(snap: Snapshot) -> str:
    """`prompts/dev/report_context.md`: the owner-editable block, sent with every ask (SPEC §5.4)."""
    reply = snap[defaults.LANG_REPLY]
    return prompts.render(
        CONTEXT_PROMPT,
        language=turns_domain.LANGUAGE_NAMES.get(reply, reply),
        tone=snap[defaults.PERSONA_DEV_TONE],
    )


def _request(snap: Snapshot, *, prompt: str) -> AssistantRequest:
    """Answers and handoff notes share one profile (system prompt, effort, no schema), so neither rotates the warm
    client the other uses."""
    return AssistantRequest(
        kind="report",
        system_prompt=prompts.render(SYSTEM_PROMPT),
        prompt=prompt,
        model=snap[defaults.MODELS_REPORT],
        settings_version=snap.version,
        effort=snap[defaults.MODELS_REPORT_EFFORT],
        instructions=_instructions(snap),
        prompt_version=snap.version,
    )


async def warm_report(deps: DevDeps) -> None:
    """Start the warm report client (startup, after a settings change; T34) with exactly the profile every ask uses,
    so the first report is warm."""
    snap = await settings.snapshot(deps)
    try:
        await deps.assistant.warm(_request(snap, prompt="").profile, timeout=snap[defaults.TIMEOUTS_SESSION_OPEN_S])
    except ExternalError as error:  # the first ask then starts it cold (≈ 2 s, still within the report budget)
        log.warning("report_warm_failed", error_code=error.code.value)


def _usage(reply: AssistantReply, project_id: str | None) -> Usage | None:
    """The ask as a `usage_records` row; None for an unknown provider or a reply that cannot be recorded."""
    provider = _PROVIDERS.get(reply.provider)
    if provider is None:
        return None
    usage = Usage(
        source="report",
        provider=provider,
        model=reply.model,
        is_paid_api=provider in cost_domain.PAYABLE and reply.cost_usd_micros > 0,
        cost_usd_micros=reply.cost_usd_micros,
        duration_ms=reply.duration_ms,
        units=dict(reply.usage),
        project_id=project_id,
    )
    return None if cost_domain.problems(usage) else usage


async def _ask(deps: DevDeps, snap: Snapshot, *, prompt: str, deadline: datetime) -> AssistantReply | None:
    """One answer ask within `timeouts.report_s` and what is left of the report budget; None → the template."""
    left = (deadline - deps.clock.now()).total_seconds()
    timeout = min(snap[defaults.TIMEOUTS_REPORT_S], left)
    if timeout < domain.MIN_ASK_S:
        log.info("report_assistant_skipped", left_ms=round(left * 1000))
        return None
    try:
        return await deps.assistant.ask(_request(snap, prompt=prompt), timeout=timeout)
    except ExternalError as error:
        log.warning("report_assistant_failed", error_code=error.code.value)
        return None


# --- reading git (outside any transaction) --------------------------------------------------------------------------
def _timeout(deps: DevDeps, deadline: datetime | None) -> float:
    if deadline is None:
        return domain.GIT_TIMEOUT_S
    left = (deadline - deps.clock.now()).total_seconds()
    if left < domain.MIN_GIT_TIMEOUT_S:
        raise _OutOfTime
    return left


async def _git[T](deps: DevDeps, deadline: datetime | None, call: Callable[[float], Awaitable[T]]) -> T:
    """One git call bounded by the refresh's timeout; inside a report a timeout ends the refresh (`_OutOfTime`)."""
    try:
        return await call(_timeout(deps, deadline))
    except CallTimedOut:
        if deadline is not None:
            raise _OutOfTime from None
        raise


async def _changed_files(deps: DevDeps, root: Path, base: str, state: GitState, deadline: datetime | None) -> list[str]:
    """Today's changed files: tracked ones against `base`, then the untracked ones."""
    changes = await _git(deps, deadline, lambda timeout: deps.git.changed_files(root, base, timeout=timeout))
    paths = [change.path for change in changes]
    if state.untracked:
        paths += await _git(deps, deadline, lambda timeout: deps.git.untracked_files(root, timeout=timeout))
    return list(dict.fromkeys(paths))


async def _read_git(deps: DevDeps, project: Project, *, deadline: datetime | None, timezone: str) -> GitRead:
    """Status, the latest commits (7 days, ≤ 10), the head commit and today's files. An unreadable repository is a
    state (`error_code`), not a failure; inside a report a timeout raises `_OutOfTime`."""
    root = Path(project.path)
    scrub = redact.scrubbed
    try:
        status = await _git(deps, deadline, lambda timeout: deps.git.status(root, timeout=timeout))
        commits: list[Commit] = []
        head: Commit | None = None
        if status.head is not None:
            commits = await _git(
                deps,
                deadline,
                lambda timeout: deps.git.log(
                    root, since_days=domain.RECENT_COMMIT_DAYS, limit=domain.RECENT_COMMITS_MAX, timeout=timeout
                ),
            )
            head = commits[0] if commits and commits[0].sha == status.head else None
            if head is None:
                head = await _git(deps, deadline, lambda timeout: deps.git.find_commit(root, "HEAD", timeout=timeout))
    except ExternalError as error:
        log.warning("digest_git_unreadable", project=project.id, error_code=error.code.value)
        return GitRead(GitState(error_code=error.code.value))
    state = domain.git_state_of(status, head)
    if state.head_subject is not None:
        state = replace(state, head_subject=scrub(state.head_subject))
    lines = domain.commit_lines(commits, scrub)
    base = domain.files_base(lines, state, now=deps.clock.now(), timezone=timezone)
    files: list[str] = []
    if base is not None:
        try:
            files = await _changed_files(deps, root, base, state, deadline)
        except ExternalError as error:  # e.g. today's first commit is the root commit: it has no parent
            log.info("digest_files_unreadable", project=project.id, error_code=error.code.value)
    return GitRead(state, lines, tuple(scrub(path) for path in files))


async def _decisions(deps: DevDeps) -> Counter[str]:
    """Pending gate decisions per project (one read)."""
    return Counter(
        item.project_id for item in await gate.pending_items(deps) if item.kind == "decision" and item.project_id
    )


# --- refresh --------------------------------------------------------------------------------------------------------
async def _refresh(
    deps: DevDeps, state: DigestState, project: Project, *, reason: str, deadline: datetime | None, synthesize: bool
) -> _Refreshed | None:
    """Read git, the project's turns, its decisions and its latest handoff note, then write the row in one short
    unit of work. Background refreshes (`synthesize`) also have a note without its executor block written. None:
    a report's budget ran out (nothing written)."""
    async with state.lock_of(project.id):
        snap = await settings.snapshot(deps)
        previous = await _load(deps, project.id)
        try:
            git = await _read_git(deps, project, deadline=deadline, timezone=snap[defaults.UI_TIMEZONE])
        except _OutOfTime:
            log.info("digest_refresh_out_of_time", project=project.id, reason=reason)
            return None
        summaries = await turns.list_recent_turns(deps, project.id, limit=domain.RECENT_TURNS_READ)
        decisions = (await _decisions(deps))[project.id]
        note = await turns.latest_handoff(deps, project.id)
        before = previous.handoff if previous is not None else None
        step = domain.handoff_step(before, note)
        handoff, reply = step.handoff, None
        if synthesize and step.synthesize is not None:
            handoff, reply = await _synthesize(deps, state, snap, project.id, step.synthesize, before, git, summaries)
        digest = domain.build_digest(
            project.id,
            at=deps.clock.now(),
            git=git,
            summaries=summaries,
            decisions=decisions,
            handoff=handoff,
            executor=project.executor,
            previous=previous,
            scrub=redact.scrubbed,
        )
        usage = _usage(reply, project.id) if reply is not None else None
        async with deps.db.uow() as s:
            await store.set_digest(s, project.id, _values(digest))
            if usage is not None:
                await cost.record_usage(s, bot=deps.bot, at=digest.checked_at, usage=usage)
    log.info(
        "digest_refreshed",
        project=project.id,
        reason=reason,
        changed=previous is None or digest.updated_at != previous.updated_at,
        git_error=digest.git.error_code,
        commits=len(digest.commits),
        handoff=digest.handoff.source if digest.handoff is not None else None,
    )
    return _Refreshed(digest, previous)


async def _synthesize(
    deps: DevDeps,
    state: DigestState,
    snap: Snapshot,
    project_id: str,
    note: HandoffNote,
    previous: HandoffState | None,
    git: GitRead,
    summaries: Sequence[TurnSummary],
) -> tuple[HandoffState, AssistantReply | None]:
    """The report Assistant writes the four sections of a note whose turn left no `<handoff>` block (SPEC §3.2.4);
    Yordamchi adds the files and commits. On failure the note holds the last steps (`digest_handoff_fallback`)."""
    title = next((turn.title for turn in summaries if turn.turn_id == note.turn_id), "")
    prompt = domain.synthesis_prompt(project_id, title, note, previous, git.state, redact.scrubbed)
    reply: AssistantReply | None = None
    state.synthesizing += 1
    try:
        reply = await deps.assistant.ask(_request(snap, prompt=prompt), timeout=domain.SYNTHESIS_TIMEOUT_S)
    except ExternalError as error:
        log.warning(
            "digest_handoff_synthesis_failed", project=project_id, turn_id=note.turn_id, error_code=error.code.value
        )
    finally:
        state.synthesizing -= 1
    fallback = prompts.render(FALLBACK_PROMPT, title=title or "unknown", steps=domain.steps_text(note.progress))
    text = domain.synthesized_note(reply.text if reply is not None else None, note, fallback=fallback)
    log.info("digest_handoff_synthesized", project=project_id, turn_id=note.turn_id, by_model=reply is not None)
    return HandoffState(redact.scrubbed(text), note.at, "synthesized", note.turn_id), reply


async def refresh_digest(deps: DevDeps, state: DigestState, project_id: str, *, reason: str = "demand") -> Digest:
    """A full refresh now (git, turns, decisions, the handoff note, synthesized if needed). `ProjectNotFound` for an
    unknown project."""
    project = await projects.get_project(deps, project_id)
    refreshed = await _refresh(deps, state, project, reason=reason, deadline=None, synthesize=True)
    if refreshed is None:  # only a report's budget runs out, and this is no report
        msg = f"refresh of {project_id} without a deadline ran out of time"
        raise RuntimeError(msg)
    return refreshed.digest


async def refresh_after_turn(deps: DevDeps, project_id: str, *, state: DigestState) -> None:
    """turns' `turn_ended` hook (T34: `turns_state.turn_ended = partial(digest.refresh_after_turn, state=…)`): the
    project's digest after each finished, failed or stopped turn, its handoff note included. A turn of a project
    that is not registered (a smoke turn) has no digest."""
    try:
        await refresh_digest(deps, state, project_id, reason="turn_end")
    except ProjectNotFound:
        log.info("digest_project_unknown", project=project_id)


async def poll_digests(deps: DevDeps, state: DigestState) -> int:
    """One poll pass (SPEC §3.2.4): every project that is due gets one cheap `git status`; a change (or no digest
    yet) refreshes it fully, otherwise only its `checked_at` moves. Returns how many were refreshed."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    registry = [project for project in await projects.list_projects(deps) if not project.missing]
    stored = await _load_all(deps)
    busy = {turn.project_id for turn in await turns.list_active_turns(deps)}
    unchanged: list[str] = []
    refreshed = 0
    for project in registry:
        digest = stored.get(project.id)
        active = domain.is_active(project, digest, has_turn=project.id in busy, now=now)
        due = domain.poll_due(
            digest,
            active=active,
            now=now,
            active_poll_s=snap[defaults.SCHEDULE_DIGEST_ACTIVE_POLL_S],
            idle_poll_min=snap[defaults.SCHEDULE_DIGEST_IDLE_POLL_MIN],
        )
        if not due or state.lock_of(project.id).locked():
            continue
        if digest is not None and digest.git.readable:
            try:
                status = await deps.git.status(Path(project.path), timeout=domain.GIT_TIMEOUT_S)
            except ExternalError as error:
                log.info("digest_poll_unreadable", project=project.id, error_code=error.code.value)
            else:
                if not domain.git_changed(digest.git, status):
                    unchanged.append(project.id)
                    continue
        await _refresh(deps, state, project, reason="poll", deadline=None, synthesize=True)
        refreshed += 1
    if unchanged:
        async with deps.db.uow() as s:
            await store.set_digests_checked(s, unchanged, at=deps.clock.now())
    log.info("digest_polled", projects=len(registry), refreshed=refreshed, unchanged=len(unchanged))
    return refreshed


async def run_digest_poll(deps: DevDeps, state: DigestState) -> None:
    """The long-lived loop: `aio.forever("digest", lambda: digest.run_digest_poll(deps, digest_state))`. The first
    pass builds every missing digest."""
    while True:
        await poll_digests(deps, state)
        snap = await settings.snapshot(deps)
        await deps.clock.sleep(snap[defaults.SCHEDULE_DIGEST_ACTIVE_POLL_S])


# --- reports --------------------------------------------------------------------------------------------------------
def _project(registry: Sequence[Project], project_id: str) -> Project:
    found = next((project for project in registry if project.id == project_id), None)
    if found is None:
        raise ProjectNotFound(name=project_id)
    return found


def _ref(question: ReportQuestion, kind: str) -> str:
    """The outbox key: a redelivered burst is answered once."""
    return f"{kind}-{question.burst_id}" if question.burst_id is not None else f"{kind}-{secrets.token_hex(6)}"


async def _server_checks(deps: DevDeps, state: DigestState) -> Sequence[ServerCheck]:
    return () if state.server_checks is None else await state.server_checks(deps)


async def _send(
    deps: DevDeps,
    snap: Snapshot,
    text: BotText,
    *,
    ref: str,
    project_id: str | None,
    thread_id: int | None,
    reply: AssistantReply | None = None,
) -> None:
    """The answer, queued with its usage row in one short unit of work, in the forum topic the question was asked in
    (`thread_id`; None: General): a report answers where it was asked, also about a project that has a topic."""
    now = deps.clock.now()
    usage = _usage(reply, project_id) if reply is not None else None
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=snap[defaults.ACCESS_DEV_GROUP_ID],
            text=text,
            kind=Kind.REPORT,
            ref=ref,
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            about=About(project_id=project_id),
            thread=Thread(thread_id),
        )
        if usage is not None:
            await cost.record_usage(s, bot=deps.bot, at=now, usage=usage)


async def _refresh_on_demand(
    deps: DevDeps, state: DigestState, project: Project, digest: Digest | None, *, deadline: datetime
) -> tuple[Digest | None, Freshness]:
    """A stale digest inside a report: refreshed from git within `deadline`, or left as it is when another refresh
    holds the project (a report never waits) or the budget ran out."""
    old = Freshness(digest.checked_at if digest is not None else None, failed=True)
    if project.missing or state.lock_of(project.id).locked():
        return digest, old
    refreshed = await _refresh(deps, state, project, reason="stale", deadline=deadline, synthesize=False)
    if refreshed is None:
        return digest, old
    previous = refreshed.previous.checked_at if refreshed.previous is not None else None
    return refreshed.digest, Freshness(refreshed.digest.checked_at, refreshed=True, previous_at=previous)


async def _investigate(
    deps: DevDeps, state: DigestState, question: ReportQuestion, project: Project, plan: ReportPlan
) -> ReportAnswer:
    """UC-15 F: "Tekshiryapman (~3 daqiqa)" and a read-only investigation turn, in one unit of work (never one
    without the other), with no git, model or turn awaited first: the owner sees it within 2 s. In a forum both go
    to the project's topic (opened now on its first work: one Telegram call), and where the owner asked gets one line
    pointing there."""
    snap = await settings.snapshot(deps)
    topic = await turns.open_topic(deps, project.id, snap=snap)
    now = deps.clock.now()
    text = S.CHECKING(project=project.id, minutes=domain.INVESTIGATION_MINUTES)
    request = TurnRequest(
        project_id=project.id,
        text=question.text,
        kind=TurnKind.INVESTIGATE,
        burst_id=question.burst_id,
        route_id=question.route_id,
    )
    async with deps.db.uow() as s:
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=snap[defaults.ACCESS_DEV_GROUP_ID],
            text=text,
            kind=Kind.REPORT,
            ref=_ref(question, "checking"),
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            priority=outbox_domain.HIGHEST_PRIORITY,
            about=About(project_id=project.id),
        )
        await turns.point_to_topic(
            s,
            deps,
            snap,
            project_id=project.id,
            topic=topic,
            from_thread=question.thread_id,
            ref=_ref(question, "checking-pointer"),
        )
        submitted = await turns.submit(s, request=request, at=now)
    state.turns.poke()
    return ReportAnswer(plan, text=text, investigation=submitted)


async def _answer_project(
    deps: DevDeps,
    state: DigestState,
    question: ReportQuestion,
    project: Project,
    plan: ReportPlan,
    *,
    deadline: datetime,
) -> ReportAnswer:
    snap = await settings.snapshot(deps)
    timezone = snap[defaults.UI_TIMEZONE]
    scrub = redact.scrubbed
    digest = await _load(deps, project.id)
    fresh = Freshness(digest.checked_at if digest is not None else None)
    now = deps.clock.now()
    if domain.is_stale(digest, now=now, stale_after_min=snap[defaults.DIGEST_STALE_AFTER_MIN]):
        refresh_by = min(deadline, now + domain.ON_DEMAND_REFRESH)
        digest, fresh = await _refresh_on_demand(deps, state, project, digest, deadline=refresh_by)
    summaries = await turns.list_recent_turns(deps, project.id, limit=domain.RECENT_TURNS_READ)
    decisions = (await _decisions(deps))[project.id]
    view = ProjectState(project.id, digest, summaries, decisions, project.description)
    body: BotText | None = None
    reply: AssistantReply | None = None
    if plan.mode is ReportMode.ASSISTANT and state.synthesizing == 0:
        servers = await _server_checks(deps, state)
        prompt = domain.answer_prompt(
            question.text, [view], servers, now=deps.clock.now(), timezone=timezone, scrub=scrub
        )
        reply = await _ask(deps, snap, prompt=prompt, deadline=deadline)
        if reply is not None:
            body = domain.assistant_report(view, reply.text, now=deps.clock.now(), scrub=scrub)
    provider = reply.provider if reply is not None and body is not None else None
    if body is None:
        body = domain.project_report(view, now=deps.clock.now(), timezone=timezone, scrub=scrub)
    text = _with_freshness(snap, body, fresh, now=deps.clock.now())
    await _send(
        deps, snap, text, ref=_ref(question, "report"), project_id=project.id, thread_id=question.thread_id, reply=reply
    )
    return ReportAnswer(plan, text=text, provider=provider, refreshed=fresh.refreshed)


async def _answer_all(
    deps: DevDeps,
    state: DigestState,
    question: ReportQuestion,
    registry: Sequence[Project],
    plan: ReportPlan,
    *,
    deadline: datetime,
) -> ReportAnswer:
    """ "Bugun qaysi loyihalarda nima o'zgardi?": every digest at once (one read), the live turns (one read), the
    decisions (one read). Stale digests of active projects are refreshed together within the refresh budget."""
    snap = await settings.snapshot(deps)
    timezone = snap[defaults.UI_TIMEZONE]
    scrub = redact.scrubbed
    now = deps.clock.now()
    present = [project for project in registry if not project.missing]
    stored = await _load_all(deps)
    by_project: defaultdict[str, list[TurnSummary]] = defaultdict(list)
    for turn in await turns.list_active_turns(deps):
        by_project[turn.project_id].append(turn)
    active = [
        project
        for project in present
        if domain.is_active(project, stored.get(project.id), has_turn=project.id in by_project, now=now)
    ]
    stale = [
        project
        for project in active
        if domain.is_stale(stored.get(project.id), now=now, stale_after_min=snap[defaults.DIGEST_STALE_AFTER_MIN])
    ]
    freshness: dict[str, Freshness] = {}
    if stale:
        refresh_by = min(deadline, now + domain.ON_DEMAND_REFRESH)
        async with asyncio.TaskGroup() as group:
            tasks = {
                project.id: group.create_task(
                    _refresh_on_demand(deps, state, project, stored.get(project.id), deadline=refresh_by)
                )
                for project in stale
            }
        for project_id, task in tasks.items():
            digest, freshness[project_id] = task.result()
            if digest is not None:
                stored[project_id] = digest
    decisions = await _decisions(deps)
    views = [
        ProjectState(project.id, stored.get(project.id), tuple(by_project[project.id]), decisions[project.id])
        for project in present
    ]
    servers = await _server_checks(deps, state)
    body: BotText | None = None
    reply: AssistantReply | None = None
    if plan.mode is ReportMode.ASSISTANT and state.synthesizing == 0:
        prompt = domain.answer_prompt(question.text, views, servers, now=now, timezone=timezone, scrub=scrub)
        reply = await _ask(deps, snap, prompt=prompt, deadline=deadline)
        if reply is not None and (chat := domain.chat_reply(reply.text, scrub)) is not None:
            ref = _ref(question, "report-all")
            await _send(deps, snap, chat, ref=ref, project_id=None, thread_id=question.thread_id, reply=reply)
            return ReportAnswer(plan, text=chat, provider=reply.provider)
        if reply is not None:
            body = domain.all_assistant_report(reply.text, scrub)
    provider = reply.provider if reply is not None and body is not None else None
    if body is None:
        body = domain.all_report(views, servers, now=deps.clock.now(), timezone=timezone, scrub=scrub)
    fresh = _all_freshness((stored[p.id] for p in (active or present) if p.id in stored), freshness.values())
    text = _with_freshness(snap, body, fresh, now=deps.clock.now())
    ref = _ref(question, "report-all")
    await _send(deps, snap, text, ref=ref, project_id=None, thread_id=question.thread_id, reply=reply)
    refreshed = any(item.refreshed for item in freshness.values())
    return ReportAnswer(plan, text=text, provider=provider, refreshed=refreshed)


def _with_freshness(snap: Snapshot, body: BotText, fresh: Freshness, *, now: datetime) -> BotText:
    """`body`, then the freshness line when `report.freshness_line` and the data's age call for it."""
    note = domain.freshness_note(
        fresh,
        now=now,
        stale_after=timedelta(minutes=snap[defaults.DIGEST_STALE_AFTER_MIN]),
        always=snap[defaults.REPORT_FRESHNESS_LINE] == "always",
    )
    return body if note is None else join([body, note])


def _all_freshness(digests: Iterable[Digest], refreshes: Iterable[Freshness]) -> Freshness:
    """The cross-project line counts from the oldest digest of the active projects; it says "yangiladim" when this
    report refreshed stale ones, and "yangilab bo'lmadi" when one of them could not be."""
    done = list(refreshes)
    oldest = min((digest.checked_at for digest in digests), default=None)
    if any(item.failed for item in done):
        return Freshness(oldest, failed=True)
    if done:
        previous = [item.previous_at for item in done if item.previous_at is not None]
        stale_since = min(previous, default=None)
        return Freshness(oldest, refreshed=True, previous_at=stale_since)
    return Freshness(oldest)


async def _answer_servers(
    deps: DevDeps, state: DigestState, question: ReportQuestion, plan: ReportPlan
) -> ReportAnswer:
    """`server_status` with no project: the latest checks while they are fresh (SPEC §3.2.4 step 5). Otherwise
    nothing is sent here: `dev/servers` (T31) sends "Tekshiryapman" and checks on demand."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    checks = await _server_checks(deps, state)
    if not domain.servers_fresh(checks, now=now, stale_after_min=snap[defaults.DIGEST_STALE_AFTER_MIN]):
        return ReportAnswer(plan, needs_server_check=True)
    oldest = min(check.at for check in checks)
    text = _with_freshness(snap, domain.servers_line(checks, now=now), Freshness(oldest), now=now)
    await _send(deps, snap, text, ref=_ref(question, "servers"), project_id=None, thread_id=question.thread_id)
    return ReportAnswer(plan, text=text)


async def answer_report(deps: DevDeps, state: DigestState, question: ReportQuestion) -> ReportAnswer:
    """Answer one report question from stored state (UC-15): ≤ 4 s, including "hozir: <title>, N daq", even while
    the project's coding turn runs. It never touches the executor, a session or the turn scheduler; a deep question
    gets "Tekshiryapman" and a read-only investigation turn instead. `ProjectNotFound` for an unknown project."""
    started = time.monotonic()
    deadline = deps.clock.now() + domain.REPORT_BUDGET
    registry = await projects.list_projects(deps)
    plan = domain.plan_report(question, domain.known_names(registry))
    if plan.scope is ReportScope.SERVERS:
        answer = await _answer_servers(deps, state, question, plan)
    elif plan.scope is ReportScope.ALL or question.project_id is None:
        answer = await _answer_all(deps, state, question, registry, plan, deadline=deadline)
    elif plan.mode is ReportMode.INVESTIGATE:
        answer = await _investigate(deps, state, question, _project(registry, question.project_id), plan)
    else:
        project = _project(registry, question.project_id)
        answer = await _answer_project(deps, state, question, project, plan, deadline=deadline)
    log.info(
        "report_answered",
        project=question.project_id,
        burst_id=question.burst_id,
        scope=plan.scope.value,
        mode=plan.mode.value,
        reason=plan.reason,
        provider=answer.provider,
        refreshed=answer.refreshed,
        investigation=answer.investigation.turn_id if answer.investigation is not None else None,
        needs_server_check=answer.needs_server_check,
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return answer
