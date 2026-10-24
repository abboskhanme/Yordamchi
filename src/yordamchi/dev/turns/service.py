"""Turns: a routed burst becomes deliveries into the project's executor session (SPEC §3.2.1 steps 7–8, §3.2.2,
§3.2.5, §3.2.6 wrong target, §2.6, §2.7, §5.1–5.6; UC-02, UC-03, UC-10 F, UC-11, NFR-CAP-01, NFR-COST-03,
NFR-REL-05/07, NFR-SEC-11).

How the pieces fit:
- **Submit.** `submit(s, …)` joins routing's unit of work, so a routed burst and its delivery commit together (a
  crash cannot keep one and lose the other). A `merge` request steers the project's working turn (`deliveries.mode =
  next`); anything else queues a new turn. `submit_turn(deps, state, …)` is the same in its own unit of work.
- **Scheduler.** `run_scheduler` (under `core.aio.forever`) runs `schedule_turns` on every `state.poke()` and every
  `SCHEDULER_TICK_S`: it delivers waiting inputs into working turns, retries usage-limited turns, pauses runaways by
  the wall clock, closes idle sessions and starts the queued turns `domain.plan_starts` admits (cap, memory pressure,
  disk, Mac attach). A turn that must wait gets one `S.QUEUED` per reason.
- **Sessions.** One live executor session per (project, kind), held in `TurnsState` (memory) and `agent_sessions`
  (its id, so it resumes after an idle close or a crash). Its `SessionSpec` follows SPEC §5.3; its guard is the
  gate's (`gate.service.make_guard`), closed while the turn stops; its `context_provider` returns the current owner
  instructions (`prompts/dev/turn_context.md`), recomputed before every delivery (SPEC §5.4).
- **Events.** One pump per live session reads `AgentRuntime.events` and drives the turn: progress (one line per
  project, coalesced by the outbox), questions, usage limits, the result (complete after `turns.result_grace_s` of
  silence with no input left, SPEC §5.1) and failures. A finished turn gets the finish nudge once if work is left
  uncommitted or unpushed, then its result (SPEC §6.2), its cost and its handoff note (for `dev/digest`).
- **Stop.** `stop_turn` closes the guard first, queues `S.STOPPED`, then interrupts (≤ 2 s, then the adapter kills
  the process group): ≤ 3 s in total. A redirect is a stop plus a new turn in the same session.
- **Recovery** (`recover_turns`, SPEC §2.7): an uncertain delivery is never resent and an interrupted turn is never
  re-run; the owner is asked ("qayta yuboraymi?", "davom ettiraymi?").

Ports are awaited only outside units of work (A3). Logs carry ids, codes, counts and durations, never text.
"""

import asyncio
import hashlib
import os
import re
import secrets
from collections import defaultdict
from collections.abc import Callable, Collection, Coroutine, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from yordamchi import prompts
from yordamchi.core import aio, json_value, proc, redact, timefmt
from yordamchi.core import strings as S
from yordamchi.core.config import SESSION_TOKEN_VAR, SOCK_VAR
from yordamchi.core.errors import CallTimedOut, ExternalError, ProjectNotFound, TurnNotFound
from yordamchi.core.json_value import JsonValue
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import domain as gate_domain
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate.domain import OwnerVerdict, Policy
from yordamchi.dev.gate.service import DecisionAnswer, GateScope
from yordamchi.dev.projects import service as projects
from yordamchi.dev.projects.domain import Project, is_hidden
from yordamchi.dev.routing import service as routing
from yordamchi.dev.routing.domain import NO_FACTS, Intent, Outcome, RouteFacts
from yordamchi.dev.routing.service import Routed
from yordamchi.dev.turns import domain, store
from yordamchi.dev.turns.domain import (
    ACTIVE,
    CONTINUABLE,
    TERMINAL,
    WAITING,
    WORKING,
    DeliveryMode,
    DeliverySource,
    DeliveryStatus,
    HandoffNote,
    HostState,
    PendingQuestion,
    ProgressKind,
    QueuedTurn,
    RevertStatus,
    RunawayLimits,
    RunawayReason,
    SchedulerLimits,
    SessionStatus,
    StartFiles,
    TurnKind,
    TurnRequest,
    TurnStatus,
    TurnSummary,
    WorkFacts,
)
from yordamchi.dev.turns.models import Turn
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.agent import (
    MAX_INPUT_IMAGES,
    AgentEvent,
    AgentInput,
    Effort,
    GuardVerdict,
    Progress,
    QuestionAsked,
    RuntimeFailed,
    SessionClosed,
    SessionHandle,
    SessionKind,
    SessionSpec,
    ToolCall,
    ToolDenied,
    TurnResult,
    UsageLimited,
    UsageWindow,
)
from yordamchi.ports.clock import Clock
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.audit import service as audit
from yordamchi.shared.audit.domain import AuditActor, AuditKind
from yordamchi.shared.burst.domain import RunningTurn, TurnInput
from yordamchi.shared.cost import service as cost
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.health.domain import SmokeOutcome, WorkStatus
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import About, Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)

STOPPING_REASON: Final = "turn_stopping"
# A session whose process is gone: its turn cannot go on in it (SPEC §3.2.5 step 3, §5.1 `RuntimeFailed`).
_SESSION_FAILURES: Final = frozenset({"process_died", "session_not_found", "cli_missing", "protocol"})
_GATE_NUMBER: Final = re.compile(r"#\d+")  # "ha #17" answers a gate decision, never a revert offer
_RUNNING: Final = (TurnStatus.STARTING.value, TurnStatus.RUNNING.value)
_WAITING_OR_QUEUED: Final = (*(status.value for status in WAITING), TurnStatus.QUEUED.value)

type QuestionHook = Callable[[DevDeps, PendingQuestion], Coroutine[object, object, None]]
# `dev/digest` (T30): a project's turn ended (done, failed or stopped) and its row is committed; the hook refreshes
# that project's digest. It runs in the background, so a slow refresh never holds a turn up.
type TurnEndHook = Callable[[DevDeps, str], Coroutine[object, object, None]]


# --- in-memory state ------------------------------------------------------------------------------------------------
@dataclass(eq=False, slots=True)
class _Live:
    """One open executor session, in memory only. Its `agent_sessions` row survives a restart; this does not."""

    agent_session_id: int
    project_id: str
    kind: SessionKind
    policy: Policy
    project_root: Path
    token: str
    model: str
    effort: Effort | None
    handle: SessionHandle | None = None
    stop_message: str = ""
    context: str = ""  # the current owner instructions, returned by the UserPromptSubmit hook (SPEC §5.4)
    snap: Snapshot | None = None  # the settings of the latest delivery
    turn_id: int | None = None  # the turn this session works on now
    turn_kind: TurnKind = TurnKind.INSTRUCT
    busy: bool = False  # the executor is inside a turn: an input now steers it (`next`)
    stopping: bool = False  # the guard denies every call until the next turn starts
    dirty: bool | None = None  # `dirty_before_turn` for the gate
    turn_started_at: datetime | None = None  # the turn's first start: the gate tells the owner's stashes by it
    started_at: datetime | None = None  # this run of the turn: runaway steps and wall clock count from here
    run_steps: int = 0
    total_steps: int = 0
    failures: defaultdict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_tool: dict[str, str] = field(default_factory=dict)
    recent: list[str] = field(default_factory=list)
    questions: set[str] = field(default_factory=set)
    pending_result: TurnResult | None = None
    seen: int = 0  # events read; the result grace ends the turn only if nothing came after its result
    seed_note: str | None = None  # a renewed session's first input carries the last handoff note
    last_active: datetime | None = None
    failure: str | None = None
    closing: bool = False
    gone: bool = False
    sending: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class TurnsState:
    """The dev bot's live sessions and the scheduler's wake-up. Built once in `bots/dev/app.py`.

    `questions` is how `dev/questions` (T29) learns that an agent asked something: it posts the question and answers
    it later through `answer_question`. `turn_ended` is how `dev/digest` (T30) learns that a project's turn ended."""

    wake: asyncio.Event = field(default_factory=asyncio.Event)
    live: dict[int, _Live] = field(default_factory=dict)  # agent_sessions.id → live session
    tokens: dict[str, int] = field(default_factory=dict)  # YORDAMCHI_SESSION_TOKEN → agent_sessions.id
    starting: set[int] = field(default_factory=set)  # turns whose session is being opened
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    account_id: str | None = None  # ClaudeManager's active account, as last seen
    account_changed_at: datetime | None = None
    questions: QuestionHook | None = None
    turn_ended: TurnEndHook | None = None
    tasks: set[asyncio.Task[None]] = field(default_factory=set)  # this feature's background work, for shutdown

    def spawn(self, name: str, coro: Coroutine[object, object, None]) -> None:
        """`core.aio.spawn`, remembered until done, so `shutdown_turns` leaves nothing running."""
        task = aio.spawn(name, coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def poke(self) -> None:
        """Run a scheduler pass now (after a submit, an answer, a stop)."""
        self.wake.set()

    def ended(self, deps: DevDeps, project_id: str, turn_id: int) -> None:
        """A turn's end is committed: `turn_ended` (T30's digest refresh) runs in the background."""
        if self.turn_ended is not None:
            self.spawn(f"turn_ended_{turn_id}", self.turn_ended(deps, project_id))

    def scope_for(self, token: str) -> GateScope | None:
        """The gate scope of the session holding `token` (the control socket's `gate_check` and `secret_blocked`).
        None for an unknown or closed session: the gate refuses it."""
        session_row_id = self.tokens.get(token)
        live = self.live.get(session_row_id) if session_row_id is not None else None
        return None if live is None or live.gone else _scope(live)

    def live_of(self, project_id: str, kind: SessionKind) -> _Live | None:
        return next(
            (live for live in self.live.values() if live.project_id == project_id and live.kind == kind),
            None,
        )

    def live_of_turn(self, turn_id: int) -> _Live | None:
        return next((live for live in self.live.values() if live.turn_id == turn_id), None)


def _scope(live: _Live) -> GateScope:
    return GateScope(
        project_id=live.project_id,
        project_root=live.project_root,
        session_key=str(live.agent_session_id),
        policy=live.policy,
        turn_id=live.turn_id,
        dirty_before_turn=live.dirty,
        turn_started_at=live.turn_started_at,
        protected_pids=frozenset({os.getpid()}),
    )


def _detach(live: _Live, now: datetime) -> None:
    """The session has no turn any more (finished, stopped, waiting); it closes after `sessions.idle_close_min`."""
    live.turn_id = None
    live.busy = False
    live.pending_result = None
    live.questions.clear()
    live.last_active = now


# --- results handed back to callers ---------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Submitted:
    turn_id: int
    delivery_id: int | None
    merged: bool  # steers the working turn (`next`); False = a turn of its own


@dataclass(frozen=True, slots=True)
class StopResult:
    """`S.STOPPED` is already queued for each project in `stopped`; `reply` is set only when nothing was stopped."""

    stopped: tuple[str, ...] = ()
    reply: BotText | None = None


@dataclass(frozen=True, slots=True)
class Recovered:
    """The startup pass (SPEC §2.7). `interrupted` feeds `S.HEALTH_RESTARTED(interrupted=…)` (T32); each uncertain
    delivery was already told as `S.DELIVERY_UNCERTAIN`."""

    interrupted: tuple[str, ...] = ()
    uncertain: tuple[str, ...] = ()
    requeued: int = 0


@dataclass(frozen=True, slots=True)
class WrongTarget:
    """ "bu X uchun edi": the wrong turn was stopped, its input re-delivered to the right project, and either the
    revert offered (`S.ROUTING_WRONG_TARGET_FILES`, queued) or the owner told nothing changed (queued)."""

    turn_id: int | None
    files: tuple[str, ...] = ()
    offered: bool = False
    redelivered: Submitted | None = None


@dataclass(frozen=True, slots=True)
class RevertReply:
    """An owner message that may answer a revert offer. `others_pending`: a gate decision or an agent question is
    open too, so a bare "ha" names nothing (R-18)."""

    text: str
    reply_to_message_id: int | None = None
    mentioned_projects: tuple[str, ...] = ()
    others_pending: bool = False

    @classmethod
    def of(
        cls, item: TurnInput, *, mentioned_projects: tuple[str, ...] = (), others_pending: bool = False
    ) -> "RevertReply":
        """The reply a closed burst makes, in the owner's own words only: a forwarded "ha" reverts nothing."""
        return cls(
            text=item.own_text,
            reply_to_message_id=item.own_reply_to,
            mentioned_projects=mentioned_projects,
            others_pending=others_pending,
        )


class RevertOutcome(StrEnum):
    NOT_FOR_ME = "not_for_me"  # not an answer to a revert offer: gate binding or routing takes the message
    REVERTED = "reverted"
    KEPT = "kept"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class RevertAnswer:
    """What the owner's answer did; `reply` is for the caller to send."""

    outcome: RevertOutcome
    turn_id: int | None = None
    reply: BotText | None = None


# --- small helpers --------------------------------------------------------------------------------------------------
def _chat(snap: Snapshot) -> int:
    return snap[defaults.ACCESS_DEV_GROUP_ID]


async def _tell(
    s: Session,
    deps: DevDeps,
    snap: Snapshot,
    text: BotText,
    *,
    ref: str,
    project_id: str,
    turn_id: int | None,
    kind: Kind = Kind.NOTICE,
    priority: int | None = None,
    reply_to: int | None = None,
) -> None:
    """One owner message about a project, queued in the caller's transaction. It starts with the project name.
    Yordamchi's hidden smoke project tells the owner nothing: `shared/health` reports its outcome."""
    if is_hidden(project_id):
        return
    await outbox.enqueue_text(
        s,
        bot=deps.bot,
        chat_id=_chat(snap),
        text=text,
        kind=kind,
        ref=ref,
        at=deps.clock.now(),
        max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        priority=priority,
        reply_to=reply_to,
        about=About(project_id=project_id, turn_id=turn_id),
    )


async def _show(s: Session, deps: DevDeps, snap: Snapshot, *, turn_id: int, project_id: str, text: BotText) -> None:
    """The turn's one live progress message: posted once, then edited at most every `outbox.progress_edit_s`. None
    for the hidden smoke project."""
    if is_hidden(project_id):
        return
    await outbox.show_progress(
        s,
        bot=deps.bot,
        chat_id=_chat(snap),
        turn_id=turn_id,
        text=text,
        at=deps.clock.now(),
        every_s=snap[defaults.OUTBOX_PROGRESS_EDIT_S],
        project_id=project_id,
    )


def _seconds(start: datetime | None, now: datetime) -> float:
    return max(0.0, (now - start).total_seconds()) if start is not None else 0.0


def _minutes(start: datetime | None, now: datetime) -> int:
    return domain.minutes_of(_seconds(start, now))


def _strings(raw: JsonValue) -> tuple[str, ...]:
    return tuple(item for item in raw if isinstance(item, str)) if isinstance(raw, list) else ()


def _progress_key(turn_id: int) -> str:
    return outbox_domain.dedupe_key(Kind.PROGRESS, str(turn_id), 0)


def _limits(snap: Snapshot, kind: TurnKind) -> RunawayLimits:
    return domain.runaway_limits(
        kind,
        max_steps=snap[defaults.TURNS_MAX_STEPS],
        max_wall_min=snap[defaults.TURNS_MAX_WALL_MIN],
        max_same_failure=snap[defaults.TURNS_MAX_SAME_FAILURE],
    )


async def _sleep_or_wake(clock: Clock, seconds: float, wake: asyncio.Event) -> None:
    """Sleep on `clock` for `seconds`, or until `wake` is set, whichever comes first."""
    if wake.is_set():
        return
    async with asyncio.TaskGroup() as group:
        sleeping = group.create_task(clock.sleep(seconds))
        waking = group.create_task(wake.wait())
        await asyncio.wait({sleeping, waking}, return_when=asyncio.FIRST_COMPLETED)
        sleeping.cancel()
        waking.cancel()


async def _add_delivery(
    s: Session, turn_id: int, mode: DeliveryMode, text: str, *, at: datetime, source: DeliverySource
) -> int | None:
    """An input Yordamchi writes on its own or on the owner's word (a nudge, "continue", an approval)."""
    row = await store.add_delivery(
        s,
        turn_id=turn_id,
        mode=mode.value,
        source=source.value,
        text=text,
        media=domain.delivery_media(TurnRequest(project_id="", text=text)),
        burst_id=None,
        at=at,
    )
    return row.id if row is not None else None


# --- submit (the narrow interface to dev/routing) -------------------------------------------------------------------
async def _working_turn(s: Session, project_id: str, kind: SessionKind) -> Turn | None:
    rows = await store.list_project_turns(s, project_id, statuses=_RUNNING, limit=5)
    return next((row for row in rows if domain.session_kind_of(TurnKind(row.kind)) == kind), None)


async def submit(s: Session, *, request: TurnRequest, at: datetime) -> Submitted:
    """In the caller's transaction (routing's `record_route`): write one delivery. A `merge` request steers the
    project's working turn of the same session kind (`next`); otherwise, or when that turn is gone, a new turn is
    queued. A burst already delivered is not written twice (deliveries are keyed per `(turn_id, seq)`). Call
    `state.poke()` after the commit."""
    if request.burst_id is not None and (known := await store.find_delivery_by_burst(s, request.burst_id)):
        return Submitted(turn_id=known.turn_id, delivery_id=known.id, merged=known.mode == DeliveryMode.NEXT.value)
    media = domain.delivery_media(request)
    session_kind = domain.session_kind_of(request.kind)
    if request.merge and (target := await _working_turn(s, request.project_id, session_kind)) is not None:
        delivery = await store.add_delivery(
            s,
            turn_id=target.id,
            mode=DeliveryMode.NEXT.value,
            source=request.source.value,
            text=request.text,
            media=media,
            burst_id=request.burst_id,
            at=at,
        )
        return Submitted(turn_id=target.id, delivery_id=delivery.id if delivery else None, merged=True)
    turn = await store.add_turn(
        s,
        project_id=request.project_id,
        kind=request.kind.value,
        status=TurnStatus.QUEUED.value,
        title=redact.scrubbed(domain.title_of(request.text)),
        burst_id=request.burst_id,
        at=at,
    )
    delivery = await store.add_delivery(
        s,
        turn_id=turn.id,
        mode=DeliveryMode.NEW_TURN.value,
        source=request.source.value,
        text=request.text,
        media=media,
        burst_id=request.burst_id,
        at=at,
    )
    return Submitted(turn_id=turn.id, delivery_id=delivery.id if delivery else None, merged=False)


async def submit_turn(deps: DevDeps, state: TurnsState, request: TurnRequest) -> Submitted:
    """`submit` in its own unit of work, then a scheduler pass."""
    async with deps.db.uow() as s:
        submitted = await submit(s, request=request, at=deps.clock.now())
    log.info(
        "turn_submitted",
        project=request.project_id,
        turn_id=submitted.turn_id,
        merged=submitted.merged,
        burst_id=request.burst_id,
    )
    state.poke()
    return submitted


async def running_turns(deps: DevDeps) -> tuple[RunningTurn, ...]:
    """The coding turns working now, as routing's follow-up rule reads them (`RouteFacts.running`, UC-02): their
    owner and bot messages, albums and the time of their latest owner part."""
    async with deps.db.read() as s:
        rows = [
            row
            for row in await store.list_turns_with_status(s, _RUNNING)
            if domain.counts_toward_cap(TurnKind(row.kind))
        ]
        media = {row.id: await store.list_deliveries(s, row.id) for row in rows}
    found: list[RunningTurn] = []
    for row in rows:
        progress = await outbox.get_delivery(deps, _progress_key(row.id))
        bot_ids = [progress.message_id] if progress is not None and progress.message_id is not None else []
        found.append(
            domain.running_turn(
                row.project_id,
                (domain.media_from_json(delivery.media_json) for delivery in media[row.id]),
                bot_message_ids=bot_ids,
                started_at=row.started_at or row.queued_at,
            )
        )
    return tuple(found)


# The routed intents that are work in a project session; reports, server checks and drafts go elsewhere (UC-15).
_TURN_INTENTS: Final[dict[Intent, TurnKind]] = {
    Intent.INSTRUCT: TurnKind.INSTRUCT,
    Intent.INVESTIGATE: TurnKind.INVESTIGATE,
}


@dataclass(frozen=True, slots=True)
class RoutedTurn:
    """`route_turn`: routing's verdict and, when it names work for a project, the delivery written with it."""

    routed: Routed | None  # None: another pass handled the burst already
    submitted: Submitted | None = None


async def route_turn(deps: DevDeps, state: TurnsState, item: TurnInput, facts: RouteFacts = NO_FACTS) -> RoutedTurn:
    """A closed burst → routing → a delivery, atomically (SPEC §3.2.1 step 7, NFR-REL-05): `routing.record_route`,
    the first reply ("→ BonviHR", queued so it goes out before the progress line) and `submit` share one unit of
    work, so a burst is never routed without its delivery, nor delivered twice. Routing's follow-up rule reads this
    feature's `running_turns`. A question, a report or a projectless intent writes no delivery: the caller handles
    `routed` (send the question and `set_clarification_message`, answer the report, …)."""
    facts = replace(facts, running=await running_turns(deps))
    planned = await routing.decide_route(deps, item, facts)
    decision = planned.decision
    kind = _TURN_INTENTS.get(decision.intent)
    to_project = decision.project_id if decision.outcome in {Outcome.ROUTE, Outcome.MERGE} else None
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        routed = await routing.record_route(s, bot=deps.bot, planned=planned, at=now)
        submitted: Submitted | None = None
        if routed is not None and kind is not None and to_project is not None:
            if routed.reply is not None:
                await _tell(
                    s,
                    deps,
                    snap,
                    routed.reply,
                    ref=f"route-{routed.route_id}",
                    project_id=to_project,
                    turn_id=None,
                    kind=Kind.ECHO,
                )
            merge = decision.outcome is Outcome.MERGE
            request = domain.request_from_input(item, to_project, kind=kind, merge=merge, route_id=routed.route_id)
            submitted = await submit(s, request=request, at=now)
    if submitted is not None:
        state.poke()
        log.info("turn_routed", project=to_project, turn_id=submitted.turn_id, merged=submitted.merged)
    return RoutedTurn(routed=routed, submitted=submitted)


# --- the scheduler (SPEC §5.6) --------------------------------------------------------------------------------------
async def run_scheduler(deps: DevDeps, state: TurnsState) -> None:
    """The long-lived loop: `aio.forever("turns", lambda: turns.run_scheduler(deps, state))`, after
    `recover_turns`."""
    while True:
        state.wake.clear()
        await schedule_turns(deps, state)
        await _sleep_or_wake(deps.clock, domain.SCHEDULER_TICK_S, state.wake)


async def schedule_turns(deps: DevDeps, state: TurnsState) -> int:
    """One pass. Returns how many queued turns it started."""
    async with state.lock:
        snap = await settings.snapshot(deps)
        now = deps.clock.now()
        await _reconcile_sessions(deps, state)
        await _tend_working(deps, state, snap, now)
        await _retry_limits(deps, state, now)
        return await _start_queued(deps, state, snap)


async def _reconcile_sessions(deps: DevDeps, state: TurnsState) -> None:
    """A session row still `live` with no process in memory (its pump ended) can only be resumed: `idle`."""
    async with deps.db.read() as s:
        rows = await store.list_current_sessions(s, statuses=[SessionStatus.LIVE.value])
    stale = [row.id for row in rows if row.id not in state.live]
    if stale:
        async with deps.db.uow() as s:
            for row_id in stale:
                await store.set_agent_session(s, row_id, status=SessionStatus.IDLE.value)


async def _tend_working(deps: DevDeps, state: TurnsState, snap: Snapshot, now: datetime) -> None:
    """Deliver waiting inputs into working turns, pause runaways by the wall clock, close idle sessions."""
    async with deps.db.read() as s:
        running = await store.list_turns_with_status(s, [TurnStatus.RUNNING.value])
        pending = {
            row.id: await store.list_deliveries(s, row.id, statuses=[DeliveryStatus.PENDING.value]) for row in running
        }
    for row in running:
        live = state.live_of_turn(row.id)
        if live is None or live.stopping:
            continue
        limits = _limits(snap, TurnKind(row.kind))
        reason = domain.runaway_of(
            steps=live.run_steps,
            started_at=live.started_at or now,
            now=now,
            same_failures=max(live.failures.values(), default=0),
            limits=limits,
        )
        if reason is RunawayReason.WALL:
            state.spawn(f"turn_runaway_{row.id}", _pause_runaway(deps, state, live, row.id, reason, limits))
        elif pending[row.id]:
            state.spawn(f"turn_deliver_{row.id}", _deliver_pending(deps, state, live))
    idle_after = snap[defaults.SESSIONS_IDLE_CLOSE_MIN] * 60
    for live in list(state.live.values()):
        idle = live.turn_id is None and not live.closing and not live.gone
        if idle and _seconds(live.last_active or now, now) >= idle_after:
            state.spawn(f"turns_close_{live.agent_session_id}", _close_live(deps, state, live, reason="idle"))


async def _retry_limits(deps: DevDeps, state: TurnsState, now: datetime) -> None:
    """SPEC §5.6: a usage-limited turn goes back to the queue at `resets_at`, 40 s after ClaudeManager switched
    account, or every 10 min, with "continue where you stopped" as its next input. No instruction is lost."""
    async with deps.db.read() as s:
        waiting = await store.list_turns_with_status(s, [TurnStatus.WAITING_LIMIT.value])
    if not waiting:
        return
    try:
        manager = await deps.mac_host.claude_manager_state(timeout=domain.HOST_TIMEOUT_S)
    except (ExternalError, OSError) as error:
        log.warning("claude_manager_unreadable", error_type=type(error).__name__)
        manager = None
    if manager is not None and manager.active_id != state.account_id:
        if state.account_id is not None:
            state.account_changed_at = now
        state.account_id = manager.active_id
    text = prompts.render("dev/turns_limit_continue")
    for row in waiting:
        due = domain.limit_retry_due(
            now=now,
            waiting_since=row.waiting_since or row.queued_at,
            resets_at=row.limit_resets_at,
            account_changed_at=state.account_changed_at,
        )
        if not due:
            continue
        async with deps.db.uow() as s:
            moved = await store.move_turn(
                s,
                row.id,
                from_statuses=[TurnStatus.WAITING_LIMIT.value],
                to_status=TurnStatus.QUEUED.value,
                waiting_since=None,
                queue_reason=None,
            )
            if moved:
                await _add_delivery(s, row.id, DeliveryMode.RESUME, text, at=now, source=DeliverySource.SYSTEM)
        log.info("turn_limit_retried", project=row.project_id, turn_id=row.id)


async def _host_state(deps: DevDeps, *, check_attach: bool) -> HostState:
    """The Mac right now, each reading bounded. An unreadable pressure level queues (SPEC §5.6); an unreadable disk
    or session list does not block."""
    pressure: int | None
    try:
        pressure = await deps.mac_host.memory_pressure_level(timeout=domain.HOST_TIMEOUT_S)
    except (ExternalError, OSError, ValueError) as error:
        log.warning("pressure_unreadable", error_type=type(error).__name__)
        pressure = None
    free: int | None
    try:
        free = await deps.mac_host.free_disk_bytes(deps.paths.data, timeout=domain.HOST_TIMEOUT_S)
    except (ExternalError, OSError) as error:
        log.warning("disk_unreadable", error_type=type(error).__name__)
        free = None
    attached: frozenset[str] = frozenset()
    if check_attach:
        # The P1 stub of the Mac-attach check (P2, T54, does it fully): the owner's live interactive sessions.
        try:
            sessions = await deps.mac_host.interactive_sessions(timeout=domain.HOST_TIMEOUT_S)
            attached = frozenset(item.session_id for item in sessions)
        except (ExternalError, OSError) as error:
            log.warning("mac_sessions_unreadable", error_type=type(error).__name__)
    draining = await asyncio.to_thread(deps.paths.drain_flag.exists)
    return HostState(pressure_level=pressure, free_disk_bytes=free, attached_sessions=attached, draining=draining)


async def _start_queued(deps: DevDeps, state: TurnsState, snap: Snapshot) -> int:
    async with deps.db.read() as s:
        queued = await store.list_turns_with_status(s, [TurnStatus.QUEUED.value])
        active = await store.list_turns_with_status(s, [status.value for status in ACTIVE])
        sessions = {
            row.id: await store.get_current_session(
                s, project_id=row.project_id, executor=domain.EXECUTOR, kind=domain.session_kind_of(TurnKind(row.kind))
            )
            for row in queued
        }
    if not queued:
        return 0
    candidates = [
        QueuedTurn(
            turn_id=row.id,
            project_id=row.project_id,
            kind=TurnKind(row.kind),
            session_id=session.session_id if (session := sessions[row.id]) is not None else None,
        )
        for row in queued
    ]
    host = await _host_state(deps, check_attach=any(turn.session_id for turn in candidates))
    working = sum(
        1 for row in active if TurnStatus(row.status) in WORKING and domain.counts_toward_cap(TurnKind(row.kind))
    )
    busy = {(row.project_id, domain.session_kind_of(TurnKind(row.kind))) for row in active}
    busy |= {(live.project_id, live.kind) for live in state.live.values() if live.turn_id is not None}
    limits = SchedulerLimits(
        max_concurrent=snap[defaults.TURNS_MAX_CONCURRENT],
        max_pressure_level=snap[defaults.GUARD_MAX_PRESSURE_LEVEL],
        block_disk_gb=snap[defaults.GUARD_BLOCK_DISK_GB],
    )
    rows = {row.id: row for row in queued}
    started = 0
    for admission in domain.plan_starts(candidates, working=working, busy=busy, host=host, limits=limits):
        row = rows[admission.turn_id]
        if admission.start:
            async with deps.db.uow() as s:
                claimed = await store.move_turn(
                    s,
                    row.id,
                    from_statuses=[TurnStatus.QUEUED.value],
                    to_status=TurnStatus.STARTING.value,
                    queue_reason=None,
                )
            if claimed:
                started += 1
                state.starting.add(row.id)
                state.spawn(f"turn_start_{row.id}", _run_start(deps, state, row.id))
            continue
        reason = admission.reason
        if reason is None or row.queue_reason == reason.value:
            continue
        text = domain.queue_notice(row.project_id, admission.position, reason, pressure_level=host.pressure_level)
        async with deps.db.uow() as s:
            await store.set_turn(s, row.id, queue_reason=reason.value)
            await _tell(
                s, deps, snap, text, ref=f"queued-{row.id}-{reason.value}", project_id=row.project_id, turn_id=row.id
            )
        log.info(
            "turn_queued", project=row.project_id, turn_id=row.id, reason=reason.value, position=admission.position
        )
    return started


# --- starting a turn ------------------------------------------------------------------------------------------------
async def _run_start(deps: DevDeps, state: TurnsState, turn_id: int) -> None:
    try:
        await _start_turn(deps, state, turn_id)
    finally:
        state.starting.discard(turn_id)
        state.poke()


async def _start_files(deps: DevDeps, root: Path) -> tuple[str | None, StartFiles | None]:
    """`start_head` and what was already changed in the tree; None when git cannot say (the gate then asks)."""
    try:
        head = await deps.git.head(root, timeout=domain.GIT_TIMEOUT_S)
        dirty = await deps.git.changed_files(root, "HEAD", timeout=domain.GIT_TIMEOUT_S) if head else []
        untracked = await deps.git.untracked_files(root, timeout=domain.GIT_TIMEOUT_S)
    except ExternalError as error:
        log.warning("turn_git_unreadable", error_code=error.code.value, stage="start")
        return None, None
    return head, StartFiles(dirty=tuple(change.path for change in dirty), untracked=tuple(untracked))


async def _fail_start(deps: DevDeps, turn_id: int, project_id: str, text: BotText, *, code: str) -> None:
    snap = await settings.snapshot(deps)
    async with deps.db.uow() as s:
        moved = await store.move_turn(
            s,
            turn_id,
            from_statuses=[TurnStatus.STARTING.value],
            to_status=TurnStatus.INTERRUPTED.value,
            ended_at=deps.clock.now(),
            error_code=code,
        )
        if moved:
            await _tell(s, deps, snap, text, ref=f"start-failed-{turn_id}", project_id=project_id, turn_id=turn_id)
    log.warning("turn_start_failed", project=project_id, turn_id=turn_id, error_code=code)


async def _start_turn(deps: DevDeps, state: TurnsState, turn_id: int) -> None:
    async with deps.db.read() as s:
        row = await store.get_turn(s, turn_id)
    if row is None or row.status != TurnStatus.STARTING.value:
        return
    kind = TurnKind(row.kind)
    try:
        project = await projects.get_project(deps, row.project_id)
    except ProjectNotFound as error:
        await _fail_start(deps, turn_id, row.project_id, S.ERRORS[error.code](**error.params), code=error.code.value)
        return
    root = deps.paths.session_dir("smoke") if kind is TurnKind.SMOKE else Path(project.path)
    # SPEC §5.3 start check: the project's own Claude settings must not bring a credential source (§2.5 step 3).
    claude_files = [root / ".claude" / "settings.json", root / ".claude" / "settings.local.json"]
    refused = await asyncio.to_thread(proc.check_credentials, claude_files, environ={})
    if refused:
        text = S.TURN_CREDENTIALS_REFUSED(project=project.id, what=", ".join(refused))
        await _fail_start(deps, turn_id, project.id, text, code="credentials_refused")
        return
    dirty = row.start_dirty
    if row.started_at is None:
        head, files = await _start_files(deps, root)
        dirty = None if files is None else files.is_dirty
        async with deps.db.uow() as s:
            await store.set_turn(
                s,
                turn_id,
                start_head=head,
                start_dirty=dirty,
                start_files_json=None if files is None else domain.start_files_json(files),
            )
    snap = await settings.snapshot(deps)
    try:
        live = await _session_for(deps, state, snap, project, kind, root)
    except ExternalError as error:
        await _fail_start(deps, turn_id, project.id, S.TURN_START_FAILED(project=project.id), code=error.code.value)
        return
    now = deps.clock.now()
    live.turn_id = turn_id  # before the move, so a stop that lands now finds this session
    live.turn_kind = kind
    live.dirty = dirty
    live.turn_started_at = row.started_at or now
    live.started_at = now
    live.run_steps = 0
    live.total_steps = row.steps
    live.failures.clear()
    live.last_tool.clear()
    live.recent.clear()
    live.pending_result = None
    live.stopping = False
    live.snap = snap
    started_at = row.started_at or now
    async with deps.db.uow() as s:
        moved = await store.move_turn(
            s,
            turn_id,
            from_statuses=[TurnStatus.STARTING.value],
            to_status=TurnStatus.RUNNING.value,
            started_at=started_at,
            agent_session_id=live.agent_session_id,
        )
        if moved:
            text = domain.progress_text(project.id, seconds=_seconds(started_at, now), steps=row.steps, latest=())
            await _show(s, deps, snap, turn_id=turn_id, project_id=project.id, text=text)
    if not moved:  # stopped while its session opened
        if live.turn_id == turn_id:
            _detach(live, now)
        return
    log.info("turn_started", project=project.id, turn_id=turn_id, kind=kind.value, session=live.agent_session_id)
    await _deliver_pending(deps, state, live)


# --- sessions (SPEC §5.3, §5.4; the resume rule of §3.2.7) ----------------------------------------------------------
def _model_of(snap: Snapshot, kind: SessionKind) -> tuple[str, Effort | None]:
    if kind == "investigation":
        return snap[defaults.MODELS_INVESTIGATION], None
    if kind == "smoke":
        return domain.SMOKE_MODEL, None
    return snap[defaults.MODELS_CODING], domain.effort_of(snap[defaults.MODELS_CODING_EFFORT])


def _transcript_exists(home: Path, session_id: str) -> bool:
    """SPEC §3.2.7: `~/.claude/projects/*/<session_id>.jsonl`, by name only; never read or copied."""
    folder = home / ".claude" / "projects"
    return folder.is_dir() and any(folder.glob(f"*/{session_id}.jsonl"))


def _context_text(snap: Snapshot, project: Project) -> str:
    """`prompts/dev/turn_context.md`: the owner-editable instructions, sent with every turn (SPEC §5.4)."""
    reply = snap[defaults.LANG_REPLY]
    return prompts.render(
        "dev/turn_context",
        project=project.id,
        description=domain.description_slot(project.description),
        language=domain.LANGUAGE_NAMES.get(reply, reply),
        tone=snap[defaults.PERSONA_DEV_TONE],
        extra=snap[defaults.PROMPT_DEV_EXECUTOR_EXTRA].strip() or "none",
    )


def _spec(deps: DevDeps, snap: Snapshot, live: _Live, kind: TurnKind) -> SessionSpec:
    """The SPEC §5.3 table, per session kind; behaviour values come from `snap`."""
    gate_guard = gate.make_guard(deps, lambda: _scope(live))

    async def guard(call: ToolCall) -> GuardVerdict:
        if live.stopping:  # SPEC §3.2.5 step 3: closed before the interrupt
            return GuardVerdict(allow=False, message=live.stop_message, reason_code=STOPPING_REASON)
        return await gate_guard(call)

    def context() -> str:
        return live.context

    coding = SessionSpec(
        kind=live.kind,
        cwd=live.project_root,
        model=live.model,
        guard=guard,
        effort=live.effort,
        system_append=prompts.render("dev/executor_system"),
        context_provider=context,
        setting_sources=domain.SETTING_SOURCES,
        disallowed_tools=domain.forbidden_tool_rules(data_dir=str(deps.paths.data), config_dir=str(deps.paths.config)),
        permission_mode="bypassPermissions",
        strict_mcp=live.kind == "smoke",
        env={
            SOCK_VAR: str(deps.paths.socket(BotName.DEV)),
            SESSION_TOKEN_VAR: live.token,
            domain.PY_VAR: str(deps.paths.current_release / domain.RELEASE_PYTHON),
        },
        max_turns=None if kind is TurnKind.SMOKE else snap[defaults.TURNS_MAX_STEPS],
        hook_timeout_s=float(snap[defaults.GATE_HOOK_TIMEOUT_S]),
        question_hold_s=float(snap[defaults.QUESTIONS_HOLD_PROCESS_MIN] * 60),
        question_parked_message=prompts.render("dev/question_parked"),
    )
    if live.kind != "investigation":
        return coding
    return replace(
        coding,
        system_append=prompts.render("dev/investigate_system"),
        tools=domain.INVESTIGATION_TOOLS,
        allowed_tools=domain.INVESTIGATION_ALLOWED,
        permission_mode="dontAsk",
        max_turns=domain.INVESTIGATION_MAX_STEPS,
    )


async def _session_for(
    deps: DevDeps, state: TurnsState, snap: Snapshot, project: Project, kind: TurnKind, root: Path
) -> _Live:
    """The project's live session of this kind, opened or resumed if there is none. A model change is applied
    with `set_model`; an effort change (a launch option) closes the session and resumes it (SPEC §5.4)."""
    session_kind = domain.session_kind_of(kind)
    model, effort = _model_of(snap, session_kind)
    live = state.live_of(project.id, session_kind)
    if live is not None and live.effort != effort:
        await _close_live(deps, state, live, reason="effort_changed")
        live = None
    if live is None:
        return await _open_session(deps, state, snap, project, kind, root, model=model, effort=effort)
    if live.model != model and live.handle is not None:
        await deps.agents.set_model(live.handle, model, timeout=domain.CONTROL_TIMEOUT_S)
        live.model = model
        async with deps.db.uow() as s:
            await store.set_agent_session(s, live.agent_session_id, model=model)
    return live


async def _open_session(
    deps: DevDeps,
    state: TurnsState,
    snap: Snapshot,
    project: Project,
    kind: TurnKind,
    root: Path,
    *,
    model: str,
    effort: Effort | None,
) -> _Live:
    session_kind = domain.session_kind_of(kind)
    async with deps.db.read() as s:
        row = await store.get_current_session(s, project_id=project.id, executor=domain.EXECUTOR, kind=session_kind)
    resume_id: str | None = None
    renewed = False
    if row is not None:
        if await asyncio.to_thread(_transcript_exists, deps.paths.home, row.session_id):
            resume_id = row.session_id
        else:
            renewed = True  # the transcript is gone (30-day cleanup): a new session, seeded with the handoff note
    now = deps.clock.now()
    live = _Live(
        agent_session_id=0,
        project_id=project.id,
        kind=session_kind,
        policy=domain.policy_of(kind),
        project_root=root,
        token=secrets.token_urlsafe(32),
        model=model,
        effort=effort,
        stop_message=prompts.render("dev/turns_stopping"),
        context=_context_text(snap, project),
        snap=snap,
        last_active=now,
    )
    spec = _spec(deps, snap, live, kind)
    handle = await deps.agents.open(spec, resume_id=resume_id, timeout=snap[defaults.TIMEOUTS_SESSION_OPEN_S])
    live.handle = handle
    async with deps.db.uow() as s:
        if row is not None and resume_id is not None and handle.session_id == row.session_id:
            await store.set_agent_session(
                s, row.id, status=SessionStatus.LIVE.value, model=model, policy=live.policy.value, last_active_at=now
            )
            session_row_id = row.id
        else:
            if row is not None:
                retired = SessionStatus.LOST if renewed else SessionStatus.CLOSED
                await store.retire_agent_session(s, row.id, status=retired.value, at=now)
            added = await store.add_agent_session(
                s,
                project_id=project.id,
                executor=domain.EXECUTOR,
                kind=session_kind,
                session_id=handle.session_id,
                status=SessionStatus.LIVE.value,
                policy=live.policy.value,
                model=model,
                at=now,
            )
            session_row_id = added.id
        await audit.record_event(
            s,
            bot=deps.bot,
            at=now,
            actor=AuditActor.SYSTEM,
            kind=AuditKind.SESSION_OPENED,
            project_id=project.id,
            ref_type="agent_session",
            ref_id=str(session_row_id),
            data={"kind": session_kind, "model": model, "resumed": resume_id is not None, "renewed": renewed},
        )
        if renewed:
            await _tell(
                s,
                deps,
                snap,
                S.SESSION_RENEWED(project=project.id),
                ref=f"renewed-{session_row_id}",
                project_id=project.id,
                turn_id=None,
            )
    live.agent_session_id = session_row_id
    if renewed:
        note = await latest_handoff(deps, project.id)
        live.seed_note = note.text() if note is not None and note.text() else None
    state.live[session_row_id] = live
    state.tokens[live.token] = session_row_id
    idle_timeout = max(60.0, snap[defaults.SESSIONS_IDLE_CLOSE_MIN] * 60.0)
    state.spawn(f"turns_pump_{session_row_id}", _pump(deps, state, live, idle_timeout=idle_timeout))
    log.info(
        "turn_session_opened",
        project=project.id,
        session=session_row_id,
        kind=session_kind,
        resumed=resume_id is not None,
        renewed=renewed,
    )
    return live


async def _close_live(deps: DevDeps, state: TurnsState, live: _Live, *, reason: str) -> None:
    """Close a session's process; its row stays resumable (`idle`)."""
    if live.gone or live.closing:
        return
    live.closing = True
    if live.handle is not None:
        try:
            await deps.agents.close(live.handle, timeout=domain.CONTROL_TIMEOUT_S)
        except ExternalError as error:
            log.warning("turn_session_close_failed", session=live.agent_session_id, error_code=error.code.value)
    await _session_gone(deps, state, live, reason=reason)


async def _session_gone(deps: DevDeps, state: TurnsState, live: _Live, *, reason: str) -> None:
    """The session's process is gone (closed, died, lost). Its row becomes `idle` (resume by id next time) or
    `lost`; a turn that was working in it is interrupted and never re-run on its own (NFR-REL-07)."""
    if live.gone:
        return
    live.gone = True
    state.live.pop(live.agent_session_id, None)
    state.tokens.pop(live.token, None)
    turn_id = live.turn_id
    now = deps.clock.now()
    _detach(live, now)
    snap = live.snap or await settings.snapshot(deps)
    status = SessionStatus.LOST if reason == "session_not_found" else SessionStatus.IDLE
    async with deps.db.uow() as s:
        await store.set_agent_session(s, live.agent_session_id, status=status.value, last_active_at=now)
        await audit.record_event(
            s,
            bot=deps.bot,
            at=now,
            actor=AuditActor.SYSTEM,
            kind=AuditKind.SESSION_CLOSED,
            project_id=live.project_id,
            ref_type="agent_session",
            ref_id=str(live.agent_session_id),
            data={"reason": reason},
        )
        row = await store.get_turn(s, turn_id) if turn_id is not None else None
        if row is not None and row.status == TurnStatus.STOPPING.value:
            await _mark_interrupted(s, row.id, now=now, code=None)
        elif row is not None and row.status in _RUNNING:
            await _mark_interrupted(s, row.id, now=now, code=reason)
            text = (
                S.TURN_START_FAILED(project=row.project_id)
                if reason == "session_not_found"
                else S.TURN_PROCESS_DIED(project=row.project_id)
            )
            await _tell(s, deps, snap, text, ref=f"died-{row.id}", project_id=row.project_id, turn_id=row.id)
    state.poke()
    log.info(
        "turn_session_closed", project=live.project_id, session=live.agent_session_id, reason=reason, turn_id=turn_id
    )


async def _mark_interrupted(s: Session, turn_id: int, *, now: datetime, code: str | None) -> bool:
    moved = await store.move_turn(
        s,
        turn_id,
        from_statuses=[TurnStatus.STARTING.value, TurnStatus.RUNNING.value, TurnStatus.STOPPING.value],
        to_status=TurnStatus.INTERRUPTED.value,
        ended_at=now,
        error_code=code,
    )
    if moved:
        await store.move_turn_deliveries(
            s, turn_id, from_status=DeliveryStatus.PENDING.value, to_status=DeliveryStatus.SUPERSEDED.value
        )
    return moved


# --- deliveries (SPEC §4.6: `sending` is written before the port call) ---------------------------------------------
async def _deliver_pending(deps: DevDeps, state: TurnsState, live: _Live) -> None:
    """Send the working turn's pending inputs in `seq` order. While the executor is inside a turn an input steers it
    (`next`); otherwise it starts the next CLI turn of the same Yordamchi turn."""
    async with live.sending:
        while (turn_id := live.turn_id) is not None and not live.gone and live.handle is not None:
            async with deps.db.read() as s:
                row = await store.get_turn(s, turn_id)
                pending = await store.list_deliveries(s, turn_id, statuses=[DeliveryStatus.PENDING.value])
            if row is None or TurnStatus(row.status) in TERMINAL:
                _detach(live, deps.clock.now())
                return
            if not pending or row.status != TurnStatus.RUNNING.value or live.stopping:
                return
            if not await _deliver_one(deps, state, live, row, pending[0].id):
                return


async def _deliver_one(deps: DevDeps, state: TurnsState, live: _Live, row: Turn, delivery_id: int) -> bool:
    """One input into the session. False when it failed (the turn is then interrupted and the owner asked)."""
    handle = live.handle
    if handle is None:
        return False
    snap = await settings.snapshot(deps)
    try:
        live.context = _context_text(snap, await projects.get_project(deps, row.project_id))
    except ProjectNotFound:
        log.warning("turn_project_missing", project=row.project_id, turn_id=row.id)
    live.snap = snap
    model, _ = _model_of(snap, live.kind)
    if not live.busy and model != live.model:  # SPEC §5.4: a model change applies before the next send
        try:
            await deps.agents.set_model(handle, model, timeout=domain.CONTROL_TIMEOUT_S)
            live.model = model
        except ExternalError as error:  # the input still goes in, on the model the session has
            log.warning("turn_set_model_failed", project=row.project_id, turn_id=row.id, error_code=error.code.value)
    async with deps.db.read() as s:
        delivery = await store.get_delivery(s, delivery_id)
    if delivery is None:
        return True
    text = delivery.text or ""
    if live.seed_note is not None:
        text = prompts.render("dev/turns_renewed", note=live.seed_note, message=text)
        live.seed_note = None
    mode = domain.send_mode(cli_busy=live.busy)
    media = domain.media_from_json(delivery.media_json)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        claimed = await store.move_delivery(
            s, delivery.id, from_status=DeliveryStatus.PENDING.value, to_status=DeliveryStatus.SENDING.value
        )
        if claimed:
            await audit.record_event(
                s,
                bot=deps.bot,
                at=now,
                actor=AuditActor.OWNER if delivery.source == DeliverySource.OWNER.value else AuditActor.SYSTEM,
                kind=AuditKind.EXECUTOR_INPUT,
                project_id=row.project_id,
                ref_type="delivery",
                ref_id=str(delivery.id),
                data={
                    "turn_id": row.id,
                    "seq": delivery.seq,
                    "mode": delivery.mode,
                    "sent_as": mode,
                    "images": len(media.images),
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                },
            )
    if not claimed:
        return True
    images = tuple(Path(path) for path in media.images[:MAX_INPUT_IMAGES])
    try:
        await deps.agents.send(handle, AgentInput(text=text, images=images), mode=mode, timeout=domain.SEND_TIMEOUT_S)
    except ExternalError as error:
        await _delivery_failed(deps, live, row, delivery.id, code=error.code.value)
        return False
    live.busy = True
    live.pending_result = None
    live.last_active = now
    async with deps.db.uow() as s:
        await store.move_delivery(
            s,
            delivery.id,
            from_status=DeliveryStatus.SENDING.value,
            to_status=DeliveryStatus.DELIVERED.value,
            sent_at=deps.clock.now(),
        )
        await store.set_agent_session(s, live.agent_session_id, prompt_version_seen=snap.version, last_active_at=now)
    log.info(
        "turn_input_delivered",
        project=row.project_id,
        turn_id=row.id,
        seq=delivery.seq,
        mode=delivery.mode,
        sent_as=mode,
        settings_version=snap.version,
    )
    return True


async def _delivery_failed(deps: DevDeps, live: _Live, row: Turn, delivery_id: int, *, code: str) -> None:
    """Whether a failed send reached the executor is unknown: never resent, the owner is asked (NFR-REL-05)."""
    snap = live.snap or await settings.snapshot(deps)
    now = deps.clock.now()
    _detach(live, now)
    async with deps.db.uow() as s:
        await store.move_delivery(
            s, delivery_id, from_status=DeliveryStatus.SENDING.value, to_status=DeliveryStatus.FAILED.value
        )
        await _mark_interrupted(s, row.id, now=now, code=code)
        await _tell(
            s,
            deps,
            snap,
            S.DELIVERY_UNCERTAIN(project=row.project_id),
            ref=f"uncertain-{delivery_id}",
            project_id=row.project_id,
            turn_id=row.id,
        )
    log.warning("turn_input_failed", project=row.project_id, turn_id=row.id, error_code=code)


# --- the event pump -------------------------------------------------------------------------------------------------
def _ended(live: _Live) -> bool:
    return live.gone


async def _pump(deps: DevDeps, state: TurnsState, live: _Live, *, idle_timeout: float) -> None:
    """The one reader of a session's events, for the life of its process."""
    handle = live.handle
    if handle is None:
        return
    reason = "ended"
    try:
        while not live.gone:
            try:
                async for event in deps.agents.events(handle, idle_timeout=idle_timeout):
                    live.seen += 1
                    await _on_event(deps, state, live, event)
                    if isinstance(event, SessionClosed):
                        reason = event.reason
                        return
                    if _ended(live):  # the session failed while handling this event
                        return
                return
            except CallTimedOut:
                continue  # a quiet session; the scheduler closes it when idle
    finally:
        await _session_gone(deps, state, live, reason=live.failure or reason)


async def _on_event(deps: DevDeps, state: TurnsState, live: _Live, event: AgentEvent) -> None:
    if isinstance(event, Progress):
        await _on_progress(deps, state, live, event)
    elif isinstance(event, QuestionAsked):
        await _on_question(deps, state, live, event)
    elif isinstance(event, UsageLimited):
        await _on_limit(deps, live, event)
    elif isinstance(event, TurnResult):
        await _on_result(deps, state, live, event)
        return
    elif isinstance(event, RuntimeFailed):
        await _on_failed(deps, state, live, event)
    elif isinstance(event, ToolDenied):
        log.info("turn_tool_denied", project=live.project_id, turn_id=live.turn_id, reason=event.reason_code)
    elif isinstance(event, UsageWindow):
        log.info("usage_window", limit_type=event.limit_type, status=event.status, utilization=event.utilization)
    if live.pending_result is not None and not live.busy and live.turn_id is not None:
        _arm_grace(deps, state, live)  # a later event restarts the silence a result needs


async def _on_progress(deps: DevDeps, state: TurnsState, live: _Live, event: Progress) -> None:
    turn_id = live.turn_id
    if turn_id is None:
        return
    line: str | None = None
    if event.kind == "tool_start":
        live.run_steps += 1
        live.total_steps += 1
        live.last_tool[event.tool_name or "?"] = event.summary
        line = domain.progress_line(ProgressKind.TOOL, event.tool_name, event.summary)
    elif event.kind == "tool_end":
        if event.is_error:
            key = domain.failure_key(event.tool_name, live.last_tool.get(event.tool_name or "?", ""))
            live.failures[key] += 1
    else:
        line = domain.progress_line(ProgressKind(event.kind), event.tool_name, event.summary)
    if line:
        live.recent = [*live.recent, redact.scrubbed(line)][-domain.PROGRESS_LINES :]
    snap = live.snap or await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        if line:
            await store.add_progress(
                s,
                turn_id=turn_id,
                at=now,
                kind=ProgressKind.TOOL.value if event.kind.startswith("tool") else event.kind,
                summary=redact.scrubbed(line),
                keep=domain.PROGRESS_KEPT,
            )
        await store.set_turn(s, turn_id, steps=live.total_steps)
        text = domain.progress_text(
            live.project_id, seconds=_seconds(live.started_at, now), steps=live.total_steps, latest=live.recent
        )
        await _show(s, deps, snap, turn_id=turn_id, project_id=live.project_id, text=text)
    limits = _limits(snap, live.turn_kind)
    reason = domain.runaway_of(
        steps=live.run_steps,
        started_at=live.started_at or now,
        now=now,
        same_failures=max(live.failures.values(), default=0),
        limits=limits,
    )
    if reason is not None and not live.stopping:
        state.spawn(f"turn_runaway_{turn_id}", _pause_runaway(deps, state, live, turn_id, reason, limits))


async def _on_question(deps: DevDeps, state: TurnsState, live: _Live, event: QuestionAsked) -> None:
    """SPEC §3.2.2: the agent holds its turn for the owner's answer; the turn holds no slot meanwhile."""
    turn_id = live.turn_id
    if turn_id is None:
        return
    live.questions.add(event.question_id)
    snap = live.snap or await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        moved = await store.move_turn(
            s,
            turn_id,
            from_statuses=[TurnStatus.RUNNING.value],
            to_status=TurnStatus.WAITING_QUESTION.value,
            waiting_since=now,
        )
        if moved:
            text = S.TURN_PROGRESS_WAITING(
                project=live.project_id, minutes=_minutes(live.started_at, now), steps=live.total_steps
            )
            await _show(s, deps, snap, turn_id=turn_id, project_id=live.project_id, text=text)
    log.info("turn_question_asked", project=live.project_id, turn_id=turn_id, question_id=event.question_id)
    if state.questions is not None:
        question = PendingQuestion(turn_id, live.project_id, event.question_id, event.questions)
        state.spawn(f"turn_question_{turn_id}", state.questions(deps, question))
    state.poke()


async def _on_limit(deps: DevDeps, live: _Live, event: UsageLimited) -> None:
    """One `S.USAGE_LIMIT`, then the turn waits; `_retry_limits` continues it (SPEC §5.6, UC-11 F)."""
    turn_id = live.turn_id
    if turn_id is None:
        return
    snap = live.snap or await settings.snapshot(deps)
    now = deps.clock.now()
    if event.resets_at is not None:
        text = S.USAGE_LIMIT(project=live.project_id, time=timefmt.hhmm(event.resets_at, snap[defaults.UI_TIMEZONE]))
    else:
        text = S.USAGE_LIMIT_UNKNOWN(project=live.project_id)
    async with deps.db.uow() as s:
        moved = await store.move_turn(
            s,
            turn_id,
            from_statuses=list(_RUNNING),
            to_status=TurnStatus.WAITING_LIMIT.value,
            limit_resets_at=event.resets_at,
            waiting_since=now,
        )
        if moved:
            await _tell(s, deps, snap, text, ref=f"limit-{turn_id}", project_id=live.project_id, turn_id=turn_id)
    log.warning("turn_usage_limited", project=live.project_id, turn_id=turn_id, limit_type=event.limit_type)


async def _record_cost(deps: DevDeps, live: _Live, event: TurnResult, turn_id: int | None) -> None:
    """Every executor result is metered (SPEC §4.2 `usage_records`): the Claude plan, not money (`is_paid_api`)."""
    now = deps.clock.now()
    usage = Usage(
        source="turn",
        provider=Provider.ANTHROPIC_PLAN,
        model=domain.main_model(event.model_usage, live.model),
        is_paid_api=False,
        cost_usd_micros=max(0, event.cost_usd_micros),
        duration_ms=max(0, event.duration_ms),
        units=domain.usage_units(event.model_usage),
        project_id=live.project_id,
        turn_id=turn_id,
    )
    async with deps.db.uow() as s:
        await cost.record_usage(s, bot=deps.bot, at=now, usage=usage)
        if turn_id is not None:
            await store.add_turn_cost(s, turn_id, cost_usd_micros=usage.cost_usd_micros, duration_ms=usage.duration_ms)


async def _on_result(deps: DevDeps, state: TurnsState, live: _Live, event: TurnResult) -> None:
    turn_id = live.turn_id
    if turn_id is None or (event.background and not live.busy):  # no input of ours started it
        await _record_cost(deps, live, event, None)
        await _late_note(deps, live, event)
        return
    await _record_cost(deps, live, event, turn_id)
    live.busy = False
    async with deps.db.read() as s:
        row = await store.get_turn(s, turn_id)
    now = deps.clock.now()
    if row is None:
        _detach(live, now)
        return
    status = TurnStatus(row.status)
    if event.interrupted or status in {TurnStatus.STOPPING, TurnStatus.PAUSED_RUNAWAY}:
        await _end_stopped(deps, state, live, turn_id)
        return
    if status is not TurnStatus.RUNNING:
        # It waits (a usage limit, a question), was queued again (an answer as a message) or ended meanwhile: the
        # session lets go of it and closes when idle.
        _detach(live, now)
        state.poke()
        return
    snap = live.snap or await settings.snapshot(deps)
    if event.subtype == "error_max_turns":  # the executor's own step cap (`max_turns=turns.max_steps`)
        await _pause_runaway(deps, state, live, turn_id, RunawayReason.STEPS, _limits(snap, live.turn_kind))
        return
    if event.is_error:
        await _fail_turn(deps, state, live, row, code=event.subtype or "error")
        return
    live.pending_result = event
    _arm_grace(deps, state, live)


async def _late_note(deps: DevDeps, live: _Live, event: TurnResult) -> None:
    """A turn the executor started by itself after background work: shown as a short note, owned by no turn."""
    text = " ".join(event.text.split())
    if not text:
        return
    snap = live.snap or await settings.snapshot(deps)
    note = S.TURN_LATE_NOTE(project=live.project_id, text=redact.scrubbed(text[: domain.LATE_NOTE_MAX]))
    async with deps.db.uow() as s:
        await _tell(
            s,
            deps,
            snap,
            note,
            ref=f"late-{live.agent_session_id}-{live.seen}",
            project_id=live.project_id,
            turn_id=None,
        )
    log.info("turn_late_note", project=live.project_id, session=live.agent_session_id)


async def _on_failed(deps: DevDeps, state: TurnsState, live: _Live, event: RuntimeFailed) -> None:
    live.failure = event.code
    log.warning("turn_runtime_failed", project=live.project_id, turn_id=live.turn_id, code=event.code)
    if event.code == "auth_failed" and live.turn_id is not None:
        async with deps.db.read() as s:
            row = await store.get_turn(s, live.turn_id)
        if row is not None and row.status in _RUNNING:
            await _fail_turn(deps, state, live, row, code=event.code)
        return
    if event.code in _SESSION_FAILURES:
        # `process_died` after the backstop kill: the session needs a resume; its turn is interrupted.
        await _session_gone(deps, state, live, reason=event.code)


def _arm_grace(deps: DevDeps, state: TurnsState, live: _Live) -> None:
    if live.turn_id is not None:
        state.spawn(f"turn_grace_{live.turn_id}", _after_grace(deps, state, live, live.turn_id, live.seen))


async def _after_grace(deps: DevDeps, state: TurnsState, live: _Live, turn_id: int, seen: int) -> None:
    """SPEC §5.1: a turn completes on a result followed by `turns.result_grace_s` of silence with no input left."""
    snap = live.snap or await settings.snapshot(deps)
    await deps.clock.sleep(snap[defaults.TURNS_RESULT_GRACE_S])
    if live.turn_id != turn_id or live.seen != seen or live.busy or live.gone or live.pending_result is None:
        return
    async with deps.db.read() as s:
        pending = await store.list_deliveries(s, turn_id, statuses=[DeliveryStatus.PENDING.value])
    if pending:
        state.poke()  # a follow-up came in: it goes in as the next turn, and its result ends this one
        return
    result = live.pending_result
    live.pending_result = None
    await _finish_turn(deps, state, live, turn_id, result)


async def _work_facts(deps: DevDeps, root: Path, row: Turn) -> WorkFacts:
    """What the turn changed, read from git against `start_head`."""
    start = row.start_head
    try:
        status = await deps.git.status(root, timeout=domain.GIT_TIMEOUT_S)
        head = status.head
        commits = await deps.git.commits_since(root, start, timeout=domain.GIT_TIMEOUT_S) if start and head else []
        changed = await deps.git.changed_files(root, start, timeout=domain.GIT_TIMEOUT_S) if start else []
        untracked = await deps.git.untracked_files(root, timeout=domain.GIT_TIMEOUT_S)
    except ExternalError as error:
        log.warning("turn_git_unreadable", project=row.project_id, turn_id=row.id, error_code=error.code.value)
        return WorkFacts(end_head=None, known=False)
    before = domain.start_files_from_json(row.start_files_json)
    return WorkFacts(
        end_head=head,
        commits=tuple(commit.sha for commit in commits) if head != start else (),
        changed=tuple(change.path for change in changed),
        created=domain.created_files(untracked, before.untracked),
        uncommitted=status.changed,
        upstream=status.upstream,
        ahead=status.ahead,
    )


async def _finish_turn(deps: DevDeps, state: TurnsState, live: _Live, turn_id: int, result: TurnResult) -> None:
    async with deps.db.read() as s:
        row = await store.get_turn(s, turn_id)
        nudges = await store.count_deliveries(s, turn_id, mode=DeliveryMode.NUDGE.value)
        progress = [item.summary for item in await store.list_progress(s, turn_id, limit=domain.HANDOFF_PROGRESS_LINES)]
    if row is None or row.status != TurnStatus.RUNNING.value:
        if live.turn_id == turn_id:
            _detach(live, deps.clock.now())
        state.poke()
        return
    kind = TurnKind(row.kind)
    facts = await _work_facts(deps, live.project_root, row)
    decisions = [item for item in await gate.pending_items(deps) if item.project_id == row.project_id]
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    if decisions:  # SPEC §3.2.3: the turn waits for the owner's "ha"; `deliver_decision` goes on with it
        async with deps.db.uow() as s:
            moved = await store.move_turn(
                s,
                turn_id,
                from_statuses=[TurnStatus.RUNNING.value],
                to_status=TurnStatus.WAITING_DECISION.value,
                waiting_since=now,
            )
            if moved:
                text = S.TURN_PROGRESS_WAITING(
                    project=row.project_id, minutes=_minutes(row.started_at, now), steps=live.total_steps
                )
                await _show(s, deps, snap, turn_id=turn_id, project_id=row.project_id, text=text)
        _detach(live, now)
        state.poke()
        log.info("turn_waiting_decision", project=row.project_id, turn_id=turn_id, decisions=len(decisions))
        return
    if facts.unfinished and nudges == 0 and domain.counts_toward_cap(kind):
        # SPEC §5.5 finish nudge: once, before the result; commit and push are pre-authorized (NFR-SEC-11).
        async with deps.db.uow() as s:
            nudge = prompts.render("dev/turns_nudge")
            await _add_delivery(s, turn_id, DeliveryMode.NUDGE, nudge, at=now, source=DeliverySource.SYSTEM)
        state.poke()
        log.info("turn_nudged", project=row.project_id, turn_id=turn_id)
        return
    parsed = domain.parse_result(result.text)
    progress_message = await outbox.get_delivery(deps, _progress_key(turn_id))
    reply_to = progress_message.message_id if progress_message is not None else None
    note = HandoffNote(
        project_id=row.project_id,
        turn_id=turn_id,
        at=now,
        block=parsed.handoff,
        files=facts.files,
        commits=facts.commits,
        progress=tuple(progress),
    )
    seconds = _seconds(row.started_at, now)
    text = domain.result_text(
        row.project_id, parsed, facts, seconds=seconds, push_missed=nudges > 0, scrub=redact.scrubbed
    )
    async with deps.db.uow() as s:
        moved = await store.move_turn(
            s,
            turn_id,
            from_statuses=[TurnStatus.RUNNING.value],
            to_status=TurnStatus.DONE.value,
            ended_at=now,
            end_head=facts.end_head,
            natija=redact.scrubbed(parsed.natija),
            tests_status=parsed.tests.value,
            commits_json=list(facts.commits),
            pushed=facts.pushed if facts.known else None,
            changed_files_json=list(facts.files),
            handoff_json=json_value.map_strings(domain.handoff_json(note), redact.scrubbed),
            progress_message_id=reply_to,
        )
        if moved:
            await _tell(
                s,
                deps,
                snap,
                text,
                ref=f"turn-{turn_id}",
                project_id=row.project_id,
                turn_id=turn_id,
                kind=Kind.RESULT,
                reply_to=reply_to,
            )
            final = S.TURN_PROGRESS_FINISHED(
                project=row.project_id, minutes=domain.minutes_of(seconds), steps=live.total_steps
            )
            await _show(s, deps, snap, turn_id=turn_id, project_id=row.project_id, text=final)
            await outbox.stop_typing(s, bot=deps.bot, chat_id=_chat(snap))
    _detach(live, now)
    state.poke()
    if moved:
        state.ended(deps, row.project_id, turn_id)
    log.info(
        "turn_finished",
        project=row.project_id,
        turn_id=turn_id,
        duration_ms=round(seconds * 1000),
        steps=live.total_steps,
        tests=parsed.tests.value,
        commits=len(facts.commits),
        pushed=facts.pushed,
        handoff=note.source,
    )


async def _fail_turn(deps: DevDeps, state: TurnsState, live: _Live, row: Turn, *, code: str) -> None:
    snap = live.snap or await settings.snapshot(deps)
    now = deps.clock.now()
    text = (
        S.TURN_AUTH_FAILED(project=row.project_id)
        if code == "auth_failed"
        else S.TURN_FAILED(project=row.project_id, reason=code)
    )
    async with deps.db.uow() as s:
        moved = await store.move_turn(
            s, row.id, from_statuses=list(_RUNNING), to_status=TurnStatus.FAILED.value, ended_at=now, error_code=code
        )
        if moved:
            await _tell(s, deps, snap, text, ref=f"failed-{row.id}", project_id=row.project_id, turn_id=row.id)
    if live.turn_id == row.id:
        _detach(live, now)
    state.poke()
    if moved:
        state.ended(deps, row.project_id, row.id)
    log.warning("turn_failed", project=row.project_id, turn_id=row.id, error_code=code)


# --- stop, redirect, runaway (SPEC §3.2.5, §5.6) -------------------------------------------------------------------
async def _end_stopped(deps: DevDeps, state: TurnsState, live: _Live, turn_id: int) -> None:
    """The interrupted result came (or the interrupt returned): a stop ends as `interrupted`, a runaway pause stays
    `paused_runaway` with its inputs kept for "davom ettir". A turn nobody stopped that the executor aborted is
    interrupted too, and the owner is asked whether to continue (NFR-REL-07)."""
    if live.turn_id != turn_id:
        return
    now = deps.clock.now()
    steps = live.total_steps
    started = live.started_at
    _detach(live, now)
    snap = live.snap or await settings.snapshot(deps)
    async with deps.db.uow() as s:
        row = await store.get_turn(s, turn_id)
        if row is not None and row.status == TurnStatus.STOPPING.value:
            await _mark_interrupted(s, turn_id, now=now, code=None)
            text = S.TURN_PROGRESS_STOPPED(project=row.project_id, minutes=_minutes(started, now), steps=steps)
            await _show(s, deps, snap, turn_id=turn_id, project_id=row.project_id, text=text)
        elif row is not None and row.status == TurnStatus.PAUSED_RUNAWAY.value:
            text = S.TURN_PROGRESS_PAUSED(project=row.project_id, minutes=_minutes(started, now), steps=steps)
            await _show(s, deps, snap, turn_id=turn_id, project_id=row.project_id, text=text)
        elif row is not None and row.status in _RUNNING:
            await _mark_interrupted(s, turn_id, now=now, code="aborted")
            ask = S.TURN_PROCESS_DIED(project=row.project_id)
            await _tell(s, deps, snap, ask, ref=f"died-{turn_id}", project_id=row.project_id, turn_id=turn_id)
    state.poke()
    if row is not None:
        state.ended(deps, row.project_id, turn_id)
    log.info("turn_stopped", project=live.project_id, turn_id=turn_id)


async def _interrupt(deps: DevDeps, state: TurnsState, live: _Live, turn_id: int) -> None:
    """`interrupt()` bounded by 2 s; the adapter then kills the process group (SPEC §3.2.5 step 3)."""
    if live.handle is not None and live.busy:
        try:
            await deps.agents.interrupt(live.handle, timeout=domain.INTERRUPT_TIMEOUT_S)
        except ExternalError as error:
            log.warning("turn_interrupt_failed", project=live.project_id, turn_id=turn_id, error_code=error.code.value)
    await _end_stopped(deps, state, live, turn_id)


async def _stop_working(deps: DevDeps, state: TurnsState, snap: Snapshot, row: Turn, *, text: BotText) -> None:
    """SPEC §3.2.5 step 3, in this order: the guard denies every new call, the owner is told, then the interrupt."""
    live = state.live_of_turn(row.id)
    if live is not None:
        live.stopping = True
    async with deps.db.uow() as s:
        moved = await store.move_turn(s, row.id, from_statuses=list(_RUNNING), to_status=TurnStatus.STOPPING.value)
        if moved:
            await _tell(
                s,
                deps,
                snap,
                text,
                ref=f"stopped-{row.id}",
                project_id=row.project_id,
                turn_id=row.id,
                priority=outbox_domain.HIGHEST_PRIORITY,
            )
    if not moved:
        return
    log.info("turn_stop_requested", project=row.project_id, turn_id=row.id)
    if live is None:  # its session is still opening: the start sees `stopping` and gives up
        async with deps.db.uow() as s:
            await _mark_interrupted(s, row.id, now=deps.clock.now(), code=None)
        return
    await _interrupt(deps, state, live, row.id)


async def _cancel(s: Session, deps: DevDeps, snap: Snapshot, row: Turn, *, now: datetime) -> bool:
    """A turn that is not working (queued or waiting) simply ends; its unsent inputs are dropped."""
    moved = await store.move_turn(
        s, row.id, from_statuses=list(_WAITING_OR_QUEUED), to_status=TurnStatus.CANCELLED.value, ended_at=now
    )
    if moved:
        await store.move_turn_deliveries(
            s, row.id, from_status=DeliveryStatus.PENDING.value, to_status=DeliveryStatus.SUPERSEDED.value
        )
        await _tell(
            s,
            deps,
            snap,
            S.STOPPED(project=row.project_id),
            ref=f"stopped-{row.id}",
            project_id=row.project_id,
            turn_id=row.id,
            priority=outbox_domain.HIGHEST_PRIORITY,
        )
    return moved


async def stop_turn(deps: DevDeps, state: TurnsState, project_id: str) -> StopResult:
    """ "to'xta" (UC-03): the project's working turns stop within 3 s; with none working, its latest waiting or queued
    turn is cancelled. `S.STOPPED` is queued after the guard closes and before the interrupt completes."""
    snap = await settings.snapshot(deps)
    statuses = [*_RUNNING, TurnStatus.STOPPING.value, *_WAITING_OR_QUEUED]
    async with deps.db.read() as s:
        rows = await store.list_project_turns(s, project_id, statuses=statuses, limit=20)
    working = [row for row in rows if row.status in _RUNNING]
    if working:
        for row in working:
            await _stop_working(deps, state, snap, row, text=S.STOPPED(project=project_id))
        return StopResult(stopped=(project_id,))
    if any(row.status == TurnStatus.STOPPING.value for row in rows):
        return StopResult(stopped=(project_id,))
    if rows:
        async with deps.db.uow() as s:
            cancelled = await _cancel(s, deps, snap, rows[0], now=deps.clock.now())
        if cancelled:
            state.poke()
            return StopResult(stopped=(project_id,))
    return StopResult(reply=S.STOP_NOTHING(project=project_id))


async def stop_all(deps: DevDeps, state: TurnsState) -> StopResult:
    """ "hammasini to'xtat" (UC-03): every working turn, all at once (≤ 5 s)."""
    async with deps.db.read() as s:
        rows = await store.list_turns_with_status(s, _RUNNING)
    names = tuple(sorted({row.project_id for row in rows}))
    if not names:
        return StopResult(reply=S.STOP_ALL_NOTHING())
    async with asyncio.TaskGroup() as group:
        for name in names:
            group.create_task(stop_turn(deps, state, name))
    return StopResult(stopped=names)


async def redirect_turn(deps: DevDeps, state: TurnsState, request: TurnRequest) -> Submitted:
    """ "yo'q, boshqacha qil: X" (UC-03): stop, then X as a new turn in the same session (context kept), ≤ 5 s."""
    await stop_turn(deps, state, request.project_id)
    return await submit_turn(deps, state, replace(request, merge=False))


async def _pause_runaway(
    deps: DevDeps, state: TurnsState, live: _Live, turn_id: int, reason: RunawayReason, limits: RunawayLimits
) -> None:
    """NFR-COST-03: past the steps, the wall clock or the same failure → interrupt → `paused_runaway` + ask."""
    if live.turn_id != turn_id or live.stopping:
        return
    live.stopping = True
    snap = live.snap or await settings.snapshot(deps)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        moved = await store.move_turn(
            s,
            turn_id,
            from_statuses=[TurnStatus.RUNNING.value],
            to_status=TurnStatus.PAUSED_RUNAWAY.value,
            waiting_since=now,
        )
        if moved:
            text = domain.runaway_notice(live.project_id, reason, limits)
            ref = f"runaway-{turn_id}-{reason.value}-{live.total_steps}"
            await _tell(s, deps, snap, text, ref=ref, project_id=live.project_id, turn_id=turn_id)
    if not moved:
        live.stopping = False
        return
    log.warning("turn_runaway_paused", project=live.project_id, turn_id=turn_id, reason=reason.value)
    await _interrupt(deps, state, live, turn_id)


async def continue_turn(deps: DevDeps, state: TurnsState, project_id: str) -> BotText:
    """ "davom ettir" (NFR-COST-03, NFR-REL-07): the project's latest turn, if it stopped before its end, goes on in
    the same session with a fresh runaway budget. Only the owner's word does this; nothing re-runs by itself."""
    async with deps.db.read() as s:
        rows = await store.list_project_turns(s, project_id, limit=1)
    row = rows[0] if rows else None
    if row is None or TurnStatus(row.status) not in CONTINUABLE:
        return S.TURN_NOTHING_TO_CONTINUE(project=project_id)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        moved = await store.move_turn(
            s,
            row.id,
            from_statuses=[status.value for status in CONTINUABLE],
            to_status=TurnStatus.QUEUED.value,
            ended_at=None,
            error_code=None,
            queue_reason=None,
            waiting_since=None,
        )
        if moved:
            text = prompts.render("dev/turns_continue")
            await _add_delivery(s, row.id, DeliveryMode.RESUME, text, at=now, source=DeliverySource.OWNER)
    if not moved:
        return S.TURN_NOTHING_TO_CONTINUE(project=project_id)
    state.poke()
    log.info("turn_continued", project=project_id, turn_id=row.id)
    return S.TURN_CONTINUED(project=project_id)


async def retry_delivery(deps: DevDeps, state: TurnsState, project_id: str) -> BotText:
    """ "qayta yubor" (NFR-REL-05): the project's latest uncertain delivery goes in again, as a new turn, once."""
    async with deps.db.read() as s:
        rows = await store.list_project_turns(s, project_id, limit=10)
        failed = [
            (row, delivery)
            for row in rows
            for delivery in await store.list_deliveries(s, row.id, statuses=[DeliveryStatus.FAILED.value])
            if delivery.text
        ]
    if not failed:
        return S.DELIVERY_NOTHING_TO_RETRY(project=project_id)
    row, delivery = max(failed, key=lambda item: item[1].id)
    media = domain.media_from_json(delivery.media_json)
    request = TurnRequest(
        project_id=project_id,
        text=delivery.text or "",
        kind=TurnKind(row.kind),
        images=media.images,
        source=DeliverySource(delivery.source),
    )
    async with deps.db.uow() as s:
        retired = await store.move_delivery(
            s, delivery.id, from_status=DeliveryStatus.FAILED.value, to_status=DeliveryStatus.SUPERSEDED.value
        )
        submitted = await submit(s, request=request, at=deps.clock.now()) if retired else None
    if submitted is None:
        return S.DELIVERY_NOTHING_TO_RETRY(project=project_id)
    state.poke()
    log.info("turn_delivery_retried", project=project_id, turn_id=submitted.turn_id, failed_delivery=delivery.id)
    return S.DELIVERY_RESENT(project=project_id)


# --- answers from the owner (gate decisions, agent questions) -------------------------------------------------------
async def deliver_decision(deps: DevDeps, state: TurnsState, answer: DecisionAnswer) -> Submitted | None:
    """The gate's note on the owner's answer (SPEC §3.2.3 steps 6–7) goes to the agent: `next` into a working turn,
    else the waiting turn continues with it, else a new turn of the project."""
    if not answer.delivery or answer.project_id is None:
        return None
    async with deps.db.read() as s:
        row = await store.get_turn(s, answer.turn_id) if answer.turn_id is not None else None
    now = deps.clock.now()
    async with deps.db.uow() as s:
        if row is not None and row.status in _RUNNING:
            delivery_id = await _add_delivery(
                s, row.id, DeliveryMode.APPROVAL, answer.delivery, at=now, source=DeliverySource.OWNER
            )
            submitted = Submitted(turn_id=row.id, delivery_id=delivery_id, merged=True)
        elif row is not None and await store.move_turn(
            s,
            row.id,
            from_statuses=[TurnStatus.WAITING_DECISION.value],
            to_status=TurnStatus.QUEUED.value,
            waiting_since=None,
        ):
            delivery_id = await _add_delivery(
                s, row.id, DeliveryMode.APPROVAL, answer.delivery, at=now, source=DeliverySource.OWNER
            )
            submitted = Submitted(turn_id=row.id, delivery_id=delivery_id, merged=False)
        else:
            kind = TurnKind(row.kind) if row is not None else TurnKind.INSTRUCT
            request = TurnRequest(project_id=answer.project_id, text=answer.delivery, kind=kind)
            submitted = await submit(s, request=request, at=now)
    state.poke()
    log.info("turn_decision_delivered", project=answer.project_id, turn_id=submitted.turn_id, kind=answer.kind.value)
    return submitted


async def answer_question(
    deps: DevDeps,
    state: TurnsState,
    *,
    turn_id: int,
    question_id: str,
    answers: dict[str, str],
    fallback_text: str,
) -> bool:
    """The owner answered an agent question (SPEC §3.2.2 step 5): straight into the held callback if it is still
    there (True); otherwise `fallback_text` (the answer as a message) becomes the turn's next input (False)."""
    live = state.live_of_turn(turn_id)
    if live is not None and live.handle is not None and question_id in live.questions:
        try:
            held = await deps.agents.answer(live.handle, question_id, answers, timeout=domain.CONTROL_TIMEOUT_S)
        except ExternalError as error:
            log.warning("turn_answer_failed", turn_id=turn_id, error_code=error.code.value)
            held = False
        if held:
            live.questions.discard(question_id)
            async with deps.db.uow() as s:
                await store.move_turn(
                    s,
                    turn_id,
                    from_statuses=[TurnStatus.WAITING_QUESTION.value],
                    to_status=TurnStatus.RUNNING.value,
                    waiting_since=None,
                )
            log.info("turn_question_answered", turn_id=turn_id, question_id=question_id, held=True)
            return True
    async with deps.db.read() as s:
        row = await store.get_turn(s, turn_id)
    if row is None:
        raise TurnNotFound(turn_id=turn_id)
    now = deps.clock.now()
    async with deps.db.uow() as s:
        await _add_delivery(s, turn_id, DeliveryMode.ANSWER, fallback_text, at=now, source=DeliverySource.OWNER)
        await store.move_turn(
            s,
            turn_id,
            from_statuses=[TurnStatus.WAITING_QUESTION.value],
            to_status=TurnStatus.QUEUED.value,
            waiting_since=None,
        )
    state.poke()
    log.info("turn_question_answered", turn_id=turn_id, question_id=question_id, held=False)
    return False


# --- wrong target (UC-10 F) -----------------------------------------------------------------------------------------
async def _changed_since_start(deps: DevDeps, root: Path, row: Turn) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The turn's files (diff since `start_head` plus the untracked files it created) and its commits."""
    if row.start_head is None:
        return (), ()
    try:
        changed = await deps.git.changed_files(root, row.start_head, timeout=domain.GIT_TIMEOUT_S)
        commits = await deps.git.commits_since(root, row.start_head, timeout=domain.GIT_TIMEOUT_S)
        untracked = await deps.git.untracked_files(root, timeout=domain.GIT_TIMEOUT_S)
    except ExternalError as error:
        log.warning("turn_git_unreadable", project=row.project_id, turn_id=row.id, error_code=error.code.value)
        return _strings(row.changed_files_json), _strings(row.commits_json)
    before = domain.start_files_from_json(row.start_files_json)
    created = domain.created_files(untracked, before.untracked)
    files = tuple(dict.fromkeys((*(change.path for change in changed), *created)))
    return files, tuple(commit.sha for commit in commits)


async def correct_wrong_target(
    deps: DevDeps, state: TurnsState, *, from_project: str, to_project: str, burst_id: int | None
) -> WrongTarget:
    """ "bu HRMentor uchun edi" (routing's `Correction`, MOVED): stop the wrong turn (≤ 3 s), deliver its owner input
    to `to_project`, list the files it changed and offer to put them back. Nothing is reverted before "ha"."""
    async with deps.db.read() as s:
        row = await store.find_turn_by_burst(s, burst_id) if burst_id is not None else None
        if row is None:
            latest = await store.list_project_turns(s, from_project, limit=1)
            row = latest[0] if latest else None
    if row is None or row.project_id != from_project:
        return WrongTarget(turn_id=None)
    snap = await settings.snapshot(deps)
    if row.status in _RUNNING:
        await _stop_working(deps, state, snap, row, text=S.STOPPED(project=from_project))
    elif row.status in _WAITING_OR_QUEUED:
        async with deps.db.uow() as s:
            await _cancel(s, deps, snap, row, now=deps.clock.now())
    async with deps.db.read() as s:
        deliveries = await store.list_deliveries(s, row.id)
        row = await store.get_turn(s, row.id) or row
    owned = [
        item
        for item in deliveries
        if item.source == DeliverySource.OWNER.value and item.text and (burst_id is None or item.burst_id == burst_id)
    ]
    redelivered: Submitted | None = None
    if owned:
        media = [domain.media_from_json(item.media_json) for item in owned]
        request = TurnRequest(
            project_id=to_project,
            text="\n\n".join(item.text or "" for item in owned),
            images=tuple(path for item in media for path in item.images)[:MAX_INPUT_IMAGES],
            message_ids=tuple(mid for item in media for mid in item.message_ids),
        )
        redelivered = await submit_turn(deps, state, request)
    try:
        root = Path((await projects.get_project(deps, from_project)).path)
    except ProjectNotFound:
        return WrongTarget(turn_id=row.id, redelivered=redelivered)
    files, commits = await _changed_since_start(deps, root, row)
    async with deps.db.uow() as s:
        if files or commits:
            await store.set_turn(
                s,
                row.id,
                revert_status=RevertStatus.OFFERED.value,
                changed_files_json=list(files),
                commits_json=list(commits),
            )
            text = S.ROUTING_WRONG_TARGET_FILES(
                project=from_project, count=len(files), files=domain.revert_files_text(files)
            )
            await _tell(
                s, deps, snap, text, ref=f"revert-{row.id}", project_id=from_project, turn_id=row.id, kind=Kind.DECISION
            )
        else:
            text = S.ROUTING_WRONG_TARGET_CLEAN(project=from_project)
            await _tell(s, deps, snap, text, ref=f"revert-{row.id}", project_id=from_project, turn_id=row.id)
    log.info("turn_wrong_target", project=from_project, turn_id=row.id, files=len(files), commits=len(commits))
    return WrongTarget(turn_id=row.id, files=files, offered=bool(files or commits), redelivered=redelivered)


async def _bound_offer(deps: DevDeps, offers: Sequence[Turn], reply: RevertReply) -> Turn | None:
    """Which offer the answer is about: the replied-to one, the one of the named project, or the only open item."""
    if reply.reply_to_message_id is not None:
        snap = await settings.snapshot(deps)
        link = await outbox.find_message_link(deps, chat_id=_chat(snap), message_id=reply.reply_to_message_id)
        return next((offer for offer in offers if link is not None and link.about.turn_id == offer.id), None)
    if reply.mentioned_projects:
        named = [offer for offer in offers if offer.project_id in reply.mentioned_projects]
        return named[0] if len(named) == 1 else None
    if len(offers) == 1 and not reply.others_pending:
        return offers[0]
    return None


def _delete_created(root: Path, paths: Sequence[str]) -> int:
    """Delete the untracked files a wrong turn created; never a path outside the repo, never a directory."""
    base = root.resolve()
    deleted = 0
    for path in paths:
        relative = domain.safe_relative(path)
        if relative is None:
            continue
        target = root / relative
        if not target.parent.resolve().is_relative_to(base):
            continue
        if target.is_symlink() or target.is_file():
            target.unlink(missing_ok=True)
            deleted += 1
    return deleted


async def _revert(deps: DevDeps, root: Path, row: Turn) -> domain.RevertPlan:
    """SPEC §3.2.6 with Yordamchi's own git port: never a reset, never a force push (see `domain.RevertPlan`)."""
    uncommitted = await deps.git.changed_files(root, "HEAD", timeout=domain.GIT_TIMEOUT_S)
    untracked = await deps.git.untracked_files(root, timeout=domain.GIT_TIMEOUT_S)
    before = domain.start_files_from_json(row.start_files_json)
    plan = domain.revert_plan(
        uncommitted,
        created=domain.created_files(untracked, before.untracked),
        commits=_strings(row.commits_json),
        owner_dirty=frozenset(before.dirty),
    )
    if plan.restore:
        await deps.git.checkout_paths(root, "HEAD", plan.restore, timeout=domain.GIT_TIMEOUT_S)
    if plan.delete:
        await asyncio.to_thread(_delete_created, root, plan.delete)
    if plan.revert:
        await deps.git.revert(root, plan.revert, timeout=domain.GIT_TIMEOUT_S)
    return plan


async def answer_revert(deps: DevDeps, origin: OwnerOrigin, reply: RevertReply) -> RevertAnswer:
    """The owner's "ha" / "yo'q" to a wrong-target revert offer (UC-10 AC: only on "ha" are exactly those files
    reverted). Anything else, or an answer that names no single offer, is `NOT_FOR_ME`: gate binding or routing
    takes the message. Only an owner update answers (`origin`, checked at runtime: `require_owner_origin`)."""
    origin = require_owner_origin(origin, deps.bot)
    async with deps.db.read() as s:
        offers = await store.list_turns_with_revert(s, RevertStatus.OFFERED.value)
    verdict = gate_domain.reply_verdict(reply.text, names=reply.mentioned_projects)
    if not offers or verdict not in {OwnerVerdict.APPROVE, OwnerVerdict.DECLINE} or _GATE_NUMBER.search(reply.text):
        return RevertAnswer(RevertOutcome.NOT_FOR_ME)
    row = await _bound_offer(deps, offers, reply)
    if row is None:
        return RevertAnswer(RevertOutcome.NOT_FOR_ME)
    approve = verdict is OwnerVerdict.APPROVE
    status = RevertStatus.REVERTED if approve else RevertStatus.DECLINED
    now = deps.clock.now()
    async with deps.db.uow() as s:
        claimed = await store.move_turn_revert(
            s, row.id, from_status=RevertStatus.OFFERED.value, to_status=status.value
        )
        if claimed:
            await audit.record_event(
                s,
                bot=deps.bot,
                at=now,
                actor=AuditActor.OWNER,
                kind=AuditKind.DECISION_RESOLVED,
                project_id=row.project_id,
                ref_type="wrong_target_revert",
                ref_id=str(row.id),
                data={"status": status.value, "update_id": origin.update_id},
            )
    if not claimed:
        return RevertAnswer(RevertOutcome.NOT_FOR_ME)
    if not approve:
        log.info("turn_revert_declined", project=row.project_id, turn_id=row.id)
        return RevertAnswer(RevertOutcome.KEPT, row.id, S.ROUTING_WRONG_TARGET_KEPT(project=row.project_id))
    try:
        root = Path((await projects.get_project(deps, row.project_id)).path)
        plan = await _revert(deps, root, row)
    except (ExternalError, ProjectNotFound, OSError) as error:
        operation = str(error.params.get("operation", error.code.value)) if isinstance(error, ExternalError) else "fs"
        async with deps.db.uow() as s:
            await store.set_turn(s, row.id, revert_status=RevertStatus.FAILED.value)
        log.warning("turn_revert_failed", project=row.project_id, turn_id=row.id, error_type=type(error).__name__)
        reply_text = S.WRONG_TARGET_REVERT_FAILED(project=row.project_id, operation=operation)
        return RevertAnswer(RevertOutcome.FAILED, row.id, reply_text)
    count = len(_strings(row.changed_files_json))
    log.info(
        "turn_reverted",
        project=row.project_id,
        turn_id=row.id,
        restored=len(plan.restore),
        deleted=len(plan.delete),
        commits=len(plan.revert),
        kept=len(plan.kept),
    )
    return RevertAnswer(
        RevertOutcome.REVERTED, row.id, S.ROUTING_WRONG_TARGET_REVERTED(project=row.project_id, count=count)
    )


# --- crash recovery and shutdown (SPEC §2.6, §2.7) ------------------------------------------------------------------
async def recover_turns(deps: DevDeps) -> Recovered:
    """The startup pass, before the scheduler runs. Each row state has exactly one outcome:
    - `deliveries.sending`: it may or may not have arrived → `failed` and `S.DELIVERY_UNCERTAIN`; never resent;
    - `turns.starting` with nothing delivered yet: it never reached the executor → `queued` (the normal path);
    - other working turns (`starting`, `running`, `stopping`) → `interrupted`, listed for "Qayta ishga tushdim…";
      never re-run (NFR-REL-07). Their undelivered inputs move to a new queued turn of the project;
    - `deliveries.pending`, queued and waiting turns: kept; `live` sessions → `idle` (resumed by id)."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    interrupted: list[str] = []
    uncertain: list[str] = []
    requeued = 0
    async with deps.db.uow() as s:
        for delivery in await store.list_deliveries_with_status(s, DeliveryStatus.SENDING.value):
            await store.move_delivery(
                s, delivery.id, from_status=DeliveryStatus.SENDING.value, to_status=DeliveryStatus.FAILED.value
            )
            turn = await store.get_turn(s, delivery.turn_id)
            if turn is not None and not is_hidden(turn.project_id):
                uncertain.append(turn.project_id)
                await _tell(
                    s,
                    deps,
                    snap,
                    S.DELIVERY_UNCERTAIN(project=turn.project_id),
                    ref=f"uncertain-{delivery.id}",
                    project_id=turn.project_id,
                    turn_id=turn.id,
                )
        for row in await store.list_turns_with_status(s, [status.value for status in WORKING]):
            deliveries = await store.list_deliveries(s, row.id)
            reached = any(item.status != DeliveryStatus.PENDING.value for item in deliveries)
            if row.status == TurnStatus.STARTING.value and not reached:
                await store.move_turn(
                    s, row.id, from_statuses=[TurnStatus.STARTING.value], to_status=TurnStatus.QUEUED.value
                )
                requeued += 1
                continue
            await store.move_turn(
                s,
                row.id,
                from_statuses=[row.status],
                to_status=TurnStatus.INTERRUPTED.value,
                ended_at=now,
                error_code="restart",
            )
            if not is_hidden(row.project_id):  # the restart notice lists the owner's projects only
                interrupted.append(row.project_id)
            waiting = [item.id for item in deliveries if item.status == DeliveryStatus.PENDING.value]
            if waiting:
                fresh = await store.add_turn(
                    s,
                    project_id=row.project_id,
                    kind=row.kind,
                    status=TurnStatus.QUEUED.value,
                    title=row.title,
                    burst_id=None,
                    at=now,
                )
                await store.reassign_deliveries(s, waiting, turn_id=fresh.id)
                requeued += 1
        await store.set_sessions_status(s, from_status=SessionStatus.LIVE.value, to_status=SessionStatus.IDLE.value)
    log.info("turns_recovered", interrupted=len(interrupted), uncertain=len(uncertain), requeued=requeued)
    return Recovered(
        interrupted=tuple(dict.fromkeys(interrupted)), uncertain=tuple(dict.fromkeys(uncertain)), requeued=requeued
    )


async def shutdown_turns(deps: DevDeps, state: TurnsState) -> int:
    """SIGTERM (SPEC §2.6): every working turn's guard closes, it is interrupted (each bounded, all at once) and ends
    `interrupted`; the owner is asked whether to continue. Then every session closes. Returns how many stopped."""
    snap = await settings.snapshot(deps)
    async with deps.db.read() as s:
        rows = await store.list_turns_with_status(s, _RUNNING)
    async with asyncio.TaskGroup() as group:
        for row in rows:
            group.create_task(_stop_working(deps, state, snap, row, text=S.TURN_SHUTDOWN(project=row.project_id)))
    for live in list(state.live.values()):
        await _close_live(deps, state, live, reason="shutdown")
    leftover = [task for task in state.tasks if not task.done() and task is not asyncio.current_task()]
    for task in leftover:  # result graces and pumps of sessions that are gone: nothing they do is needed now
        task.cancel()
    await asyncio.gather(*leftover, return_exceptions=True)
    log.info("turns_shut_down", stopped=len(rows), cancelled=len(leftover))
    return len(rows)


# --- reads for other features ---------------------------------------------------------------------------------------
async def work_status(deps: DevDeps) -> WorkStatus:
    """`holat?`'s work lines: `shared/health` takes `functools.partial(work_status, deps)` as its `WorkProbe`. The
    projects working now, the queue in start order, the gate decisions waiting for the owner and the agent questions
    (turns in `waiting_question`). The hidden smoke project is left out."""
    async with deps.db.read() as s:
        working = await store.list_turns_with_status(s, [status.value for status in WORKING])
        queued = await store.list_turns_with_status(s, [TurnStatus.QUEUED.value])
        asking = await store.list_turns_with_status(s, [TurnStatus.WAITING_QUESTION.value])
    decisions = await gate.pending_items(deps)
    return domain.work_status(
        [row.project_id for row in working if not is_hidden(row.project_id)],
        [row.project_id for row in queued if not is_hidden(row.project_id)],
        decisions=len(decisions),
        questions=sum(1 for row in asking if not is_hidden(row.project_id)),
    )


async def _prepare_smoke_repo(deps: DevDeps, root: Path) -> None:
    """The scratch repo of the smoke test (SPEC §2.2 `sessions/smoke/`): a private folder, a git repo from the first
    run on."""
    await asyncio.to_thread(root.mkdir, mode=0o700, parents=True, exist_ok=True)
    if not await asyncio.to_thread((root / ".git").exists):
        await deps.git.init(root, branch=domain.SMOKE_BRANCH, timeout=domain.GIT_TIMEOUT_S)


def _smoke_file_ok(path: Path) -> bool:
    try:
        with path.open("rb") as file:
            return file.read(domain.SMOKE_FILE_MAX).strip() == b"ok"
    except OSError:
        return False


async def _smoke_turn_settled(deps: DevDeps, turn_id: int, *, deadline: datetime) -> Turn | None:
    """The smoke turn once it ended, waits for something, or the deadline passed."""
    while True:
        async with deps.db.read() as s:
            row = await store.get_turn(s, turn_id)
        if row is None or TurnStatus(row.status) in TERMINAL | WAITING or deps.clock.now() >= deadline:
            return row
        await deps.clock.sleep(domain.SMOKE_POLL_S)


async def run_smoke_turn(deps: DevDeps, state: TurnsState, version: str) -> SmokeOutcome:
    """The CLI smoke test (SPEC §5.6, NFR-REL-08): one tiny turn — haiku, wall 5 min — in the scratch repo, which
    must write a file and commit it. `shared/health` runs it after a `claude --version` change, as its `SmokeRunner`
    (`functools.partial(run_smoke_turn, deps, state)`), and tells the owner only about a failure; the turn itself
    tells the owner nothing. A turn not finished by the deadline is stopped. Never raises for a failed run."""
    project = await projects.ensure_smoke_project(deps)
    root = Path(project.path)
    try:
        await _prepare_smoke_repo(deps, root)
    except (ExternalError, OSError) as error:
        log.warning("smoke_repo_failed", error_type=type(error).__name__)
        return SmokeOutcome(ok=False, step="repo")
    started = deps.clock.now()
    file_name = domain.smoke_file_name(started)
    request = TurnRequest(
        project_id=project.id,
        text=prompts.render("dev/turns_smoke", file=file_name),
        kind=TurnKind.SMOKE,
        source=DeliverySource.SYSTEM,
    )
    submitted = await submit_turn(deps, state, request)
    deadline = started + domain.SMOKE_WALL + domain.SMOKE_START_BUDGET
    row = await _smoke_turn_settled(deps, submitted.turn_id, deadline=deadline)
    if row is None:
        return SmokeOutcome(ok=False, step="lost")
    status = TurnStatus(row.status)
    if status not in TERMINAL:
        await stop_turn(deps, state, project.id)
    file_ok = status is TurnStatus.DONE and await asyncio.to_thread(_smoke_file_ok, root / file_name)
    outcome = domain.smoke_verdict(
        status,
        error_code=row.error_code,
        committed=row.end_head is not None and row.end_head != row.start_head,
        file_ok=file_ok,
    )
    log.info(
        "smoke_turn_finished",
        cli_version=version,
        turn_id=row.id,
        status=status.value,
        ok=outcome.ok,
        step=outcome.step,
    )
    return outcome


async def latest_handoff(deps: DevDeps, project_id: str) -> HandoffNote | None:
    """The handoff note of the project's latest finished turn (for `dev/digest`, T30, which stores it in
    `digests.handoff_note`; `needs_synthesis` tells it to have the report Assistant write the four sections)."""
    async with deps.db.read() as s:
        row = await store.find_latest_handoff(s, project_id)
    if row is None:
        return None
    return domain.handoff_from_json(row.project_id, row.id, row.handoff_json)


def _summary(row: Turn) -> TurnSummary:
    return TurnSummary(
        turn_id=row.id,
        project_id=row.project_id,
        kind=TurnKind(row.kind),
        status=TurnStatus(row.status),
        title=row.title,
        queued_at=row.queued_at,
        started_at=row.started_at,
        ended_at=row.ended_at,
        natija=row.natija,
        tests=row.tests_status,
    )


async def list_active_turns(deps: DevDeps) -> tuple[TurnSummary, ...]:
    """Every queued, working or waiting turn of every project, oldest first (one read; no session is touched): the
    reports' "hozir: <title>, N daq" and queue counts (SPEC §3.2.4 step 2)."""
    statuses = sorted(status.value for status in (*ACTIVE, TurnStatus.QUEUED))
    async with deps.db.read() as s:
        rows = await store.list_turns_with_status(s, statuses)
    return tuple(_summary(row) for row in rows)


async def list_recent_turns(deps: DevDeps, project_id: str, *, limit: int) -> tuple[TurnSummary, ...]:
    """The project's latest `limit` turns, newest first, whatever their status: its live state and last results."""
    async with deps.db.read() as s:
        rows = await store.list_project_turns(s, project_id, limit=limit)
    return tuple(_summary(row) for row in rows)


async def turn_statuses(deps: DevDeps, turn_ids: Collection[int]) -> dict[int, TurnStatus]:
    """The status of each of these turns still stored; a missing id is a turn that is gone. For `dev/questions`
    (T29): a question whose turn ended can take no answer any more."""
    found: dict[int, TurnStatus] = {}
    async with deps.db.read() as s:
        for turn_id in sorted(set(turn_ids)):  # the few turns with an open question
            row = await store.get_turn(s, turn_id)
            if row is not None:
                found[turn_id] = TurnStatus(row.status)
    return found


# --- retention (SPEC §4.2) ------------------------------------------------------------------------------------------
async def apply_retention(deps: DevDeps) -> int:
    """Finished turns go after 400 days (with their deliveries and steps), delivery texts after 30, steps after 7.
    A turn that is not finished, and a delivery still pending, are never touched. Returns how many rows changed."""
    now = deps.clock.now()
    finished = sorted(status.value for status in TERMINAL)
    turn_cutoff = domain.turn_cutoff(now)
    text_cutoff = domain.delivery_text_cutoff(now)
    progress_cutoff = domain.progress_cutoff(now)
    turns = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_turns_before(s, turn_cutoff, statuses=finished, limit=limit)
    )
    texts = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.clear_delivery_texts_before(s, text_cutoff, limit=limit)
    )
    steps = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_progress_before(s, progress_cutoff, limit=limit)
    )
    log.info("turns_retention_applied", turns_deleted=turns, texts_cleared=texts, steps_deleted=steps)
    return turns + texts + steps
