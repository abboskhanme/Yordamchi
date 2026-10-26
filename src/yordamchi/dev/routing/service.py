"""Routing (SPEC §3.2.6, §6.1 step 3; UC-10, UC-02, UC-05): which project a closed burst is about.

The router (T24/T34) reads `burst.service.list_closed_bursts`, runs pre-parse (interrupts come first: SPEC §6.1),
then for each burst:
1. `answer_clarification`: a burst that answers the open "BonviHR'gami yoki HRMentor'gami?" resolves it (§6.1 step 3);
2. `correct_route`: "bu HRMentor uchun edi" moves the latest work to HRMentor (UC-10 F);
3. `route_burst` for everything else: `decide_route`, then `record_route` in one unit of work.

`decide_route` reads the registry, the settings and the recent decisions, asks the routing LLM outside any
transaction (A3), bounded by `timeouts.routing_s` (NFR-SPD: the decision takes ≤ 4 s), and decides; it writes
nothing. `record_route(s, …)` joins the caller's transaction: it writes the decision, the clarification and the usage
row and moves the burst `closed → routed | merged`, so T24 can queue the turn in the same commit. A burst that was
already handled records nothing and returns None.

Nothing here sends a message. Each result carries the owner's first reply — "→ BonviHR", the voice echo, or the one
question — which the router hands to the outbox before any delivery (SPEC §3.2.1 step 7). Logs carry ids, methods,
intents and counts, never message text.
"""

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import ExternalError
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.projects import service as projects
from yordamchi.dev.routing import domain, store
from yordamchi.dev.routing.domain import (
    NO_FACTS,
    ChoiceKind,
    ClarificationKind,
    ClarificationStatus,
    CorrectionKind,
    Intent,
    Method,
    OpenClarification,
    Outcome,
    PastRoute,
    RouteDecision,
    RouteFacts,
    RouteSettings,
)
from yordamchi.dev.routing.models import Clarification as ClarificationRow
from yordamchi.dev.routing.models import RouteDecision as RouteDecisionRow
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.ports.assistant import AssistantReply, AssistantRequest
from yordamchi.shared.burst import service as burst
from yordamchi.shared.burst.domain import TurnInput
from yordamchi.shared.cost import domain as cost_domain
from yordamchi.shared.cost import service as cost
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

log = get_log(__name__)

PAST_LIMIT: Final = 500  # recent decisions read for context and for a correction
_PROVIDERS: Final = {"claude": Provider.ANTHROPIC_PLAN, "gemini": Provider.GOOGLE, "openrouter": Provider.OPENROUTER}


class _AlreadyHandled(Exception):  # rolls one unit of work back; never leaves this module
    """The burst was no longer `closed`, or the clarification no longer `open`: another pass got there first."""


@dataclass(frozen=True, slots=True)
class Planned:
    """A decision not yet written: what `decide_route` hands to `record_route`."""

    item: TurnInput
    decision: RouteDecision
    reply: BotText | None  # the first reply: it names the target, or it is the one question
    model: str | None  # the routing LLM's model, when it answered
    latency_ms: int
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class Routed:
    route_id: int
    decision: RouteDecision
    reply: BotText | None
    clarification_id: int | None = None  # ASK: set its message id once the question is sent


@dataclass(frozen=True, slots=True)
class Resolution:
    """What the owner's answer to a clarification did."""

    choice: ChoiceKind
    reply: BotText
    clarification_id: int
    burst_id: int | None = None  # the burst that was asked about: deliver its input to `project_id` now
    project_id: str | None = None
    intent: Intent | None = None
    route_id: int | None = None  # that burst's decision, which now names the project
    consumed: bool = True  # False: the answer said more; route this burst on its own as well


@dataclass(frozen=True, slots=True)
class Expired:
    clarification_id: int
    burst_id: int | None
    reply: BotText


@dataclass(frozen=True, slots=True)
class Correction:
    """ "bu X uchun edi" (UC-10 F). MOVED: turns stops the wrong turn, lists its changed files, re-delivers the input
    of `burst_id` to `to_project` and asks `decisions(kind=wrong_target_revert)`; `reply` is the first line."""

    kind: CorrectionKind
    reply: BotText
    to_project: str
    from_project: str | None = None
    route_id: int | None = None  # the corrected decision
    burst_id: int | None = None  # its burst


# --- rows ---------------------------------------------------------------------------------------------------------
def _past(row: RouteDecisionRow) -> PastRoute:
    return PastRoute(
        route_id=row.id,
        burst_id=row.burst_id,
        at=row.at,
        method=domain.member(Method, row.method, Method.LLM),
        intent=domain.member(Intent, row.intent, Intent.INSTRUCT),
        project_id=row.project_id,
        corrected_to=row.corrected_to,
        asked=row.asked,
    )


def _open_clarification(row: ClarificationRow) -> OpenClarification:
    return OpenClarification(
        id=row.id,
        burst_id=row.burst_id,
        kind=domain.member(ClarificationKind, row.kind, ClarificationKind.WHICH_PROJECT),
        candidates=domain.ids_from_json(row.candidates_json),
        message_id=row.message_id,
        asked_at=row.asked_at,
    )


async def _recent_routes(deps: DevDeps, *, since: datetime) -> list[PastRoute]:
    async with deps.db.read() as s:
        rows = await store.list_route_decisions_since(s, since, limit=PAST_LIMIT)
    return [_past(row) for row in rows]


async def _open_clarifications(deps: DevDeps) -> list[OpenClarification]:
    async with deps.db.read() as s:
        rows = await store.list_clarifications_with_status(s, ClarificationStatus.OPEN.value)
    return [_open_clarification(row) for row in rows]


# --- the LLM (outside any transaction) ----------------------------------------------------------------------------
def _system_prompt() -> str:
    return prompts.render("dev/routing_system", **domain.prompt_slots())


def _request(cfg: RouteSettings, *, settings_version: int, prompt: str) -> AssistantRequest:
    return AssistantRequest(
        kind="routing",
        system_prompt=_system_prompt(),
        prompt=prompt,
        model=cfg.model,
        settings_version=settings_version,
        effort=domain.ROUTING_EFFORT,
        output_schema=domain.OUTPUT_SCHEMA,
    )


async def warm_routing(deps: DevDeps) -> None:
    """Start the warm routing client (startup, after a settings change; T34) with exactly the profile every ask
    uses, so the first ask is warm and no ask rotates the client for a changed prompt or schema."""
    snap = await settings.snapshot(deps)
    profile = _request(domain.route_settings(snap), settings_version=snap.version, prompt="").profile
    try:
        await deps.assistant.warm(profile, timeout=snap[defaults.TIMEOUTS_SESSION_OPEN_S])
    except ExternalError as error:  # the first ask then starts it cold; routing still works
        log.warning("routing_warm_failed", error_code=error.code.value)


async def _ask_llm(deps: DevDeps, cfg: RouteSettings, *, settings_version: int, prompt: str) -> AssistantReply | None:
    """One routing ask; None when it failed or timed out (the decision then falls back, SPEC §3.2.6 step 6)."""
    request = _request(cfg, settings_version=settings_version, prompt=prompt)
    try:
        return await deps.assistant.ask(request, timeout=cfg.timeout_s)
    except ExternalError as error:
        log.warning("routing_llm_failed", error_code=error.code.value)
        return None


def _usage(reply: AssistantReply, project_id: str | None) -> Usage | None:
    """The ask as a `usage_records` row; None for an unknown provider or a reply that cannot be recorded."""
    provider = _PROVIDERS.get(reply.provider)
    if provider is None:
        return None
    usage = Usage(
        source="routing",
        provider=provider,
        model=reply.model,
        is_paid_api=provider in cost_domain.PAYABLE and reply.cost_usd_micros > 0,
        cost_usd_micros=reply.cost_usd_micros,
        duration_ms=reply.duration_ms,
        units=dict(reply.usage),
        project_id=project_id,
    )
    return None if cost_domain.problems(usage) else usage


# --- routing ------------------------------------------------------------------------------------------------------
async def decide_route(deps: DevDeps, item: TurnInput, facts: RouteFacts = NO_FACTS) -> Planned:
    """Decide where `item` goes (SPEC §3.2.6): names, the reply, recent context, then the LLM; a follow-up of a
    running turn is checked before any question (UC-02). Writes nothing. An interrupt or the self-test is a wiring
    bug here and raises `ValueError` (pre-parse takes them first)."""
    domain.require_routable(item)
    started = time.monotonic()
    snap = await settings.snapshot(deps)
    cfg = domain.route_settings(snap)
    registry = await projects.list_projects(deps)
    now = deps.clock.now()
    activity = domain.spoken_activity(await _recent_routes(deps, since=now - cfg.context_window))
    plan = domain.plan_route(item, registry, facts, activity, now=now, window=cfg.context_window)
    prompt = domain.llm_prompt(item, plan, registry, facts, activity=activity, now=now, window=cfg.context_window)
    reply = await _ask_llm(deps, cfg, settings_version=snap.version, prompt=prompt)
    llm = None if reply is None else domain.parse_llm_route(reply.structured, registry)
    if reply is not None and llm is None:
        log.warning("routing_reply_unusable", burst_id=item.burst_id)
    decision = domain.with_follow_up(domain.decide(plan, llm, cfg), item, facts.running, cfg)
    latency_ms = round((time.monotonic() - started) * 1000)
    log.info(
        "route_decided",
        burst_id=item.burst_id,
        outcome=decision.outcome.value,
        method=decision.method.value,
        intent=decision.intent.value,
        reason=decision.reason,
        confidence_milli=domain.confidence_milli(decision.confidence),
        candidates=len(decision.candidates),
        llm=llm is not None,
        duration_ms=latency_ms,
    )
    return Planned(
        item=item,
        decision=decision,
        reply=domain.first_reply(decision, item, redact.scrubbed),
        model=reply.model if reply is not None else None,
        latency_ms=latency_ms,
        usage=None if reply is None else _usage(reply, decision.project_id),
    )


async def record_route(s: Session, *, bot: BotName, planned: Planned, at: datetime) -> Routed | None:
    """In the caller's transaction: the decision row, the burst `closed → routed | merged`, the clarification (ASK)
    and the usage row. None, with nothing written, when the burst was no longer `closed`."""
    decision, item = planned.decision, planned.item
    row = await store.add_route_decision(
        s,
        burst_id=item.burst_id,
        at=at,
        method=decision.method.value,
        intent=decision.intent.value,
        project_id=decision.project_id,
        candidates=domain.candidates_json(decision.candidates),
        confidence_milli=domain.confidence_milli(decision.confidence),
        asked=decision.asked,
        model=planned.model,
        latency_ms=planned.latency_ms,
    )
    if decision.outcome is Outcome.MERGE:
        moved = await burst.mark_burst_merged(s, bot=bot, burst_id=item.burst_id, route_id=row.id)
    else:
        moved = await burst.mark_burst_routed(s, bot=bot, burst_id=item.burst_id, route_id=row.id)
    if not moved:
        await store.delete_route_decision(s, row.id)
        log.info("route_already_handled", burst_id=item.burst_id)
        return None
    clarification_id: int | None = None
    if decision.asked:
        clarification = await store.add_clarification(
            s,
            burst_id=item.burst_id,
            kind=ClarificationKind.WHICH_PROJECT.value,
            candidates=domain.ids_json(decision.candidate_ids),
            status=ClarificationStatus.OPEN.value,
            asked_at=at,
        )
        clarification_id = clarification.id
    if planned.usage is not None:
        await cost.record_usage(s, bot=bot, at=at, usage=planned.usage)
    return Routed(route_id=row.id, decision=decision, reply=planned.reply, clarification_id=clarification_id)


async def plan_topic_route(deps: DevDeps, item: TurnInput, project_id: str, facts: RouteFacts = NO_FACTS) -> Planned:
    """A burst written in `project_id`'s own forum topic (D4 revised 2026-10-26): that project, method `topic`, with
    no model and no question; a follow-up of its working turn steers it (UC-02). Writes nothing: `record_route` does,
    like any other decision."""
    domain.require_routable(item)
    snap = await settings.snapshot(deps)
    cfg = domain.route_settings(snap)
    decision = domain.with_follow_up(domain.topic_decision(project_id), item, facts.running, cfg)
    log.info(
        "route_decided",
        burst_id=item.burst_id,
        outcome=decision.outcome.value,
        method=decision.method.value,
        intent=decision.intent.value,
        reason=decision.reason,
        confidence_milli=domain.confidence_milli(decision.confidence),
        candidates=len(decision.candidates),
        llm=False,
        duration_ms=0,
    )
    return Planned(
        item=item,
        decision=decision,
        reply=domain.topic_reply(decision, item, redact.scrubbed),
        model=None,
        latency_ms=0,
    )


async def route_burst(deps: DevDeps, item: TurnInput, facts: RouteFacts = NO_FACTS) -> Routed | None:
    """Route one closed burst and record it (SPEC §3.2.6). None when another pass already handled the burst."""
    planned = await decide_route(deps, item, facts)
    async with deps.db.uow() as s:
        return await record_route(s, bot=deps.bot, planned=planned, at=deps.clock.now())


# --- consuming a burst that needs no turn of its own --------------------------------------------------------------
async def _consume(
    s: Session, bot: BotName, item: TurnInput, *, at: datetime, project_id: str | None, intent: Intent, method: Method
) -> int:
    """Record the decision for a burst that was itself the routing (an answer, a correction) and mark it routed."""
    confidence = domain.EXPLICIT_CONFIDENCE if project_id is not None else 0.0
    candidates = (domain.Candidate(project_id, confidence),) if project_id is not None else ()
    row = await store.add_route_decision(
        s,
        burst_id=item.burst_id,
        at=at,
        method=method.value,
        intent=intent.value,
        project_id=project_id,
        candidates=domain.candidates_json(candidates),
        confidence_milli=domain.confidence_milli(confidence),
        asked=False,
        model=None,
        latency_ms=0,
    )
    if not await burst.mark_burst_routed(s, bot=bot, burst_id=item.burst_id, route_id=row.id):
        raise _AlreadyHandled
    return row.id


# --- clarifications (SPEC §6.1 step 3) ----------------------------------------------------------------------------
async def _resolve(
    s: Session, bot: BotName, item: TurnInput, asked: OpenClarification, choice: domain.Choice, *, at: datetime
) -> Resolution:
    if choice.kind is ChoiceKind.OUT_OF_RANGE:  # the question stays open; this burst was only the attempt
        await _consume(s, bot, item, at=at, project_id=None, intent=Intent.ANSWER, method=Method.BINDING)
        return Resolution(choice.kind, domain.out_of_range_text(asked.candidates), asked.id)
    closed = await store.set_clarification_status(
        s, asked.id, from_status=ClarificationStatus.OPEN.value, to_status=ClarificationStatus.RESOLVED.value, at=at
    )
    if closed == 0:
        raise _AlreadyHandled
    if choice.kind is ChoiceKind.CANCELLED or choice.project_id is None:
        await _consume(s, bot, item, at=at, project_id=None, intent=Intent.ANSWER, method=Method.BINDING)
        return Resolution(ChoiceKind.CANCELLED, S.ROUTING_CLARIFY_CANCELLED(), asked.id, burst_id=asked.burst_id)
    project = choice.project_id
    original = await store.find_asked_route(s, asked.burst_id) if asked.burst_id is not None else None
    if original is not None:
        await store.set_route_project(s, original.id, project_id=project)
    if choice.pure:
        await _consume(s, bot, item, at=at, project_id=project, intent=Intent.ANSWER, method=Method.BINDING)
    return Resolution(
        ChoiceKind.PICKED,
        S.ROUTED(project=project),
        asked.id,
        burst_id=asked.burst_id,
        project_id=project,
        intent=domain.member(Intent, original.intent, Intent.INSTRUCT) if original is not None else Intent.INSTRUCT,
        route_id=original.id if original is not None else None,
        consumed=choice.pure,
    )


async def answer_clarification(deps: DevDeps, item: TurnInput, facts: RouteFacts = NO_FACTS) -> Resolution | None:
    """The owner's answer to the open project question ("2", "ikkinchisi", "BonviHR'ga", or a voice note saying it).
    None when `item` answers nothing: route it as usual. A reply to a question binds to that question, else the
    newest one still open; "ha" picks the one candidate only when no other question or decision is pending."""
    now = deps.clock.now()
    replies = frozenset(part.reply_to_message_id for part in item.parts if part.reply_to_message_id is not None)
    asked = domain.live_clarification(await _open_clarifications(deps), replies_to=replies, now=now)
    if asked is None:
        return None
    registry = await projects.list_projects(deps)
    choice = domain.choose(domain.item_text(item), asked.candidates, registry, accept_yes=not facts.others_pending)
    if choice.kind is ChoiceKind.NONE:
        return None
    try:
        async with deps.db.uow() as s:
            resolution = await _resolve(s, deps.bot, item, asked, choice, at=now)
    except _AlreadyHandled:
        log.info("clarification_already_handled", clarification_id=asked.id, burst_id=item.burst_id)
        return None
    log.info(
        "clarification_answered",
        clarification_id=asked.id,
        burst_id=item.burst_id,
        asked_burst_id=asked.burst_id,
        choice=resolution.choice.value,
        consumed=resolution.consumed,
    )
    return resolution


async def set_clarification_message(deps: DevDeps, clarification_id: int, message_id: int) -> bool:
    """The question went out as `message_id` (outbox, T15): a reply to it now binds the answer to this question."""
    async with deps.db.uow() as s:
        changed = await store.set_clarification_message(s, clarification_id, message_id=message_id)
    return changed == 1


async def expire_clarifications(deps: DevDeps) -> tuple[Expired, ...]:
    """Questions open for 30 minutes expire; each comes back with the notice that its message went nowhere, so a
    burst is never dropped silently. Run by the scheduler (T34)."""
    now = deps.clock.now()
    found: list[Expired] = []
    async with deps.db.uow() as s:
        rows = await store.list_clarifications_with_status(s, ClarificationStatus.OPEN.value)
        for asked in domain.expired([_open_clarification(row) for row in rows], now=now):
            moved = await store.set_clarification_status(
                s,
                asked.id,
                from_status=ClarificationStatus.OPEN.value,
                to_status=ClarificationStatus.EXPIRED.value,
                at=now,
            )
            if moved:
                found.append(Expired(asked.id, asked.burst_id, domain.clarification_expired_text(asked)))
    for item in found:
        log.info("clarification_expired", clarification_id=item.clarification_id, burst_id=item.burst_id)
    return tuple(found)


# --- wrong target (UC-10 F) ---------------------------------------------------------------------------------------
async def correct_route(deps: DevDeps, item: TurnInput, facts: RouteFacts = NO_FACTS) -> Correction | None:
    """ "bu HRMentor uchun edi": mark the latest routed work (the replied-to project's, when it is a reply) as meant for
    HRMentor. None when `item` is not such a correction. The burst itself is recorded and needs no turn."""
    registry = await projects.list_projects(deps)
    target = domain.wrong_target_of(domain.item_text(item), registry)
    if target is None:
        return None
    now = deps.clock.now()
    past = await _recent_routes(deps, since=now - domain.CORRECTION_WINDOW)
    replied = domain.reply_project(item, facts.reply_projects, {project.id for project in registry})
    wrong = domain.route_to_correct(past, now=now, reply_project=replied, burst_id=item.burst_id)
    if wrong is None or wrong.project is None:
        hours = round(domain.CORRECTION_WINDOW.total_seconds() / 3600)
        correction = Correction(
            CorrectionKind.NOTHING, S.ROUTING_WRONG_TARGET_NONE(project=target, hours=hours), target
        )
    elif wrong.project == target:
        correction = Correction(CorrectionKind.SAME, S.ROUTING_WRONG_TARGET_SAME(project=target), target)
    else:
        correction = Correction(
            CorrectionKind.MOVED,
            S.ROUTING_WRONG_TARGET_MOVED(to_project=target, from_project=wrong.project),
            target,
            from_project=wrong.project,
            route_id=wrong.route_id,
            burst_id=wrong.burst_id,
        )
    try:
        async with deps.db.uow() as s:
            if correction.route_id is not None:
                await store.set_route_corrected(s, correction.route_id, corrected_to=target)
            await _consume(
                s, deps.bot, item, at=now, project_id=target, intent=Intent.WRONG_TARGET, method=Method.EXPLICIT
            )
    except _AlreadyHandled:
        log.info("correction_already_handled", burst_id=item.burst_id)
        return None
    log.info("route_corrected", burst_id=item.burst_id, kind=correction.kind.value, route_id=correction.route_id)
    return correction


# --- retention ----------------------------------------------------------------------------------------------------
async def apply_retention(deps: DevDeps) -> int:
    """SPEC §4.2: route decisions are deleted after 90 days; answered or expired clarifications 30 days after they were
    asked, in batches. An open clarification is never touched. Returns how many rows were deleted."""
    now = deps.clock.now()
    route_cutoff, clarification_cutoff = domain.route_cutoff(now), domain.clarification_cutoff(now)
    finished = [ClarificationStatus.RESOLVED.value, ClarificationStatus.EXPIRED.value]
    routes = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_route_decisions_before(s, route_cutoff, limit=limit)
    )
    clarifications = await bulk.run_in_batches(
        deps.db,
        lambda s, limit: store.delete_clarifications_before(s, clarification_cutoff, statuses=finished, limit=limit),
    )
    log.info("routing_retention_applied", routes_deleted=routes, clarifications_deleted=clarifications)
    return routes + clarifications
