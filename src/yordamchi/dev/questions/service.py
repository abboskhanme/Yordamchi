"""Agent questions ↔ owner answers (SPEC §3.2.2, §2.7, §4.2 `questions`; UC-12; spike S3).

- **Asking.** `open_question(deps, question)` is T24's `TurnsState.questions` hook (`bots/dev/app.py` sets it). When
  an agent holds its turn on `AskUserQuestion`, the row and the owner's message (`S.AGENT_QUESTION…`: the project
  first, the options numbered) are written in one unit of work as an outbox priority-0 row about the question, so
  the sender posts it within seconds and `message_links` ties the posted message to the question.
- **Answering.** `answer_owner(deps, state, origin, item)` is the router's step 3, after pre-parse and routing's
  `answer_clarification`. One owner burst binds to at most one open item:
  1. the gate decides first (`gate.answer_decision`, with the open questions as pending items, so a bare "ha" with
     a question open approves nothing); a decision it binds is delivered to its turn (`turns.deliver_decision`);
  2. otherwise `domain.bind_answer` picks the question, and its answer goes to the held callback through
     `turns.answer_question(…, fallback_text=)`. When the callback is gone — the hold elapsed and the agent was told
     to go on (the question stays open meanwhile), or the bot restarted — the answer goes in as the turn's next
     input instead (`answer_mode=message`);
  3. an answer that fits questions of two projects is never guessed: one routing clarification asks which (method
     `binding`, intent `answer`), and routing's resolution comes back through `answer_clarified`.

  Whatever binds, the burst is recorded as routed (`routing.record_route`) in the same unit of work as the answer,
  so it is handled exactly once. None means the burst answered nothing: route it as usual.
- **Closed questions.** A question whose turn ended (stopped, failed, gone) can take no answer: it is cancelled when
  next read, and a reply to it routes as an ordinary message.
- **Restart** (`recover_questions`, SPEC §2.7): an answer recorded but not yet handed to its turn goes in now.

Ports are awaited only outside units of work (A3). Logs carry ids, counts and outcomes, never question or answer text.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.core import strings as S
from yordamchi.core.errors import DecisionNotFound, TurnNotFound
from yordamchi.core.log import get_log
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate.domain import PendingItem
from yordamchi.dev.gate.service import AnswerKind, DecisionAnswer, DecisionReply
from yordamchi.dev.projects import service as projects
from yordamchi.dev.projects.domain import Project
from yordamchi.dev.questions import domain, store
from yordamchi.dev.questions.domain import Ambiguous, Answer, AnswerMode, OpenQuestion, OutOfRange, QuestionStatus
from yordamchi.dev.questions.models import Question
from yordamchi.dev.routing import domain as routing_domain
from yordamchi.dev.routing import service as routing
from yordamchi.dev.routing.domain import Candidate, Intent, Method, Outcome, RouteDecision
from yordamchi.dev.routing.service import Planned
from yordamchi.dev.turns import domain as turns_domain
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns.domain import PendingQuestion, TurnStatus
from yordamchi.dev.turns.service import TurnsState
from yordamchi.persistence import bulk
from yordamchi.persistence.uow import Session
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.burst.domain import TurnInput
from yordamchi.shared.outbox import domain as outbox_domain
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import About, Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot

log = get_log(__name__)

OPEN_READ_LIMIT: Final = 200  # open questions read per pass; one per waiting turn, so far fewer in practice


class _AlreadyHandled(Exception):  # rolls one unit of work back; never leaves this module
    """The burst was no longer `closed`, or the question no longer `open`: another pass got there first."""


class AnswerOutcome(StrEnum):
    DECISION = "decision"  # the gate took it and told the owner itself: `decision`
    ANSWERED = "answered"  # an agent question took it: `held` says whether into the callback or as a message
    ASKED = "asked"  # it fits questions of several projects: the which-project question is queued
    OUT_OF_RANGE = "out_of_range"  # "5" for three options: told; the question stays open
    NOT_OPEN = "not_open"  # the question closed meanwhile: told, nothing delivered
    DUPLICATE = "duplicate"  # another pass handled this burst or this question first


@dataclass(frozen=True, slots=True)
class OwnerAnswer:
    """What one owner burst did to the open items. The owner's replies are already queued."""

    outcome: AnswerOutcome
    project_id: str | None = None
    question_id: int | None = None
    turn_id: int | None = None
    held: bool | None = None  # ANSWERED: True = into the held callback, False = as the turn's next input
    decision: DecisionAnswer | None = None
    clarification_id: int | None = None  # ASKED: `routing.set_clarification_message` once `message_key` is sent
    message_key: str | None = None  # ASKED: the outbox key of the which-project question


# --- rows -------------------------------------------------------------------------------------------------------
def _open(row: Question) -> OpenQuestion:
    return OpenQuestion(
        id=row.id,
        project_id=row.project_id,
        turn_id=row.turn_id,
        tool_use_id=row.tool_use_id,
        asked_at=row.asked_at,
        questions=domain.questions_from_json(row.questions_json),
    )


def _ended(status: TurnStatus | None) -> bool:
    return status is None or status in turns_domain.TERMINAL


async def _open_questions(deps: DevDeps) -> list[OpenQuestion]:
    """The questions still waiting, oldest first. Those whose turn ended are cancelled here: nothing can take their
    answer any more."""
    async with deps.db.read() as s:
        rows = await store.list_questions_with_status(s, QuestionStatus.OPEN.value, limit=OPEN_READ_LIMIT)
    if not rows:
        return []
    statuses = await turns.turn_statuses(deps, {row.turn_id for row in rows})
    ended = {row.id for row in rows if _ended(statuses.get(row.turn_id))}
    if ended:
        async with deps.db.uow() as s:
            cancelled = await store.set_questions_status(
                s, ended, from_status=QuestionStatus.OPEN.value, to_status=QuestionStatus.CANCELLED.value
            )
        log.info("questions_cancelled", count=cancelled)
    return [_open(row) for row in rows if row.id not in ended]


async def open_items(deps: DevDeps) -> tuple[PendingItem, ...]:
    """The open questions as the gate counts pending items (`holat?`, `RevertReply.others_pending`)."""
    return tuple(PendingItem("question", question.id, question.project_id) for question in await _open_questions(deps))


async def waiting_projects(deps: DevDeps) -> tuple[str, ...]:
    """The projects an agent question waits in, oldest first (`RouteFacts.open_questions`)."""
    return tuple(dict.fromkeys(question.project_id for question in await _open_questions(deps)))


# --- small helpers ----------------------------------------------------------------------------------------------
def _chat(snap: Snapshot) -> int:
    return snap[defaults.ACCESS_DEV_GROUP_ID]


async def _tell(
    s: Session,
    deps: DevDeps,
    snap: Snapshot,
    text: BotText,
    *,
    ref: str,
    question: OpenQuestion | None = None,
    project_id: str | None = None,
    kind: Kind = Kind.NOTICE,
) -> None:
    """One owner message, queued in the caller's transaction. About a question, it links to it."""
    about = (
        About(project_id=question.project_id, turn_id=question.turn_id, question_id=question.id)
        if question is not None
        else About(project_id=project_id)
    )
    await outbox.enqueue_text(
        s,
        bot=deps.bot,
        chat_id=_chat(snap),
        text=text,
        kind=kind,
        ref=ref,
        at=deps.clock.now(),
        max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        about=about,
    )


def _spellings(mentioned: Sequence[str], registry: Sequence[Project]) -> tuple[str, ...]:
    """How the named projects may be written: their ids and aliases."""
    named = set(mentioned)
    return tuple(dict.fromkeys(name for p in registry if p.id in named for name in (p.id, *p.aliases)))


async def _names_in(deps: DevDeps, text: str) -> tuple[tuple[str, ...], tuple[str, ...], frozenset[str]]:
    """The projects `text` names, their spellings, and the registered ids."""
    registry = await projects.list_projects(deps)
    mentioned = routing_domain.find_mentions(text, registry).named
    return mentioned, _spellings(mentioned, registry), frozenset(project.id for project in registry)


async def _reply_target(deps: DevDeps, chat_id: int, message_id: int | None) -> domain.ReplyTarget | None:
    if message_id is None:
        return None
    link = await outbox.find_message_link(deps, chat_id=chat_id, message_id=message_id)
    if link is None:
        return None
    return domain.ReplyTarget(question_id=link.about.question_id, project_id=link.about.project_id)


async def _record_route(s: Session, deps: DevDeps, item: TurnInput, *, project_id: str | None, reason: str) -> bool:
    """In the caller's transaction: the burst was itself the answer (method `binding`), so it is routed and needs no
    turn of its own. False when another pass handled it already."""
    decision = RouteDecision(
        outcome=Outcome.ROUTE if project_id is not None else Outcome.NO_PROJECT,
        method=Method.BINDING,
        intent=Intent.ANSWER,
        project_id=project_id,
        confidence=routing_domain.EXPLICIT_CONFIDENCE if project_id is not None else 0.0,
        candidates=(Candidate(project_id, routing_domain.EXPLICIT_CONFIDENCE),) if project_id is not None else (),
        reason=reason,
    )
    planned = Planned(item=item, decision=decision, reply=None, model=None, latency_ms=0)
    return await routing.record_route(s, bot=deps.bot, planned=planned, at=deps.clock.now()) is not None


# --- asking (the T24 hook) --------------------------------------------------------------------------------------
async def open_question(deps: DevDeps, question: PendingQuestion) -> None:
    """`TurnsState.questions`: the agent holds its turn on `AskUserQuestion` (SPEC §3.2.2 step 3). The row and its
    message (outbox priority 0, linked to the question) commit together; the same question is never posted twice."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    asked = domain.parse_questions(question.questions, redact.scrubbed)
    text = domain.question_text(question.project_id, asked)
    async with deps.db.uow() as s:
        question_id = await store.add_question(
            s,
            project_id=question.project_id,
            turn_id=question.turn_id,
            tool_use_id=question.question_id,
            questions=domain.questions_json(asked),
            status=QuestionStatus.OPEN.value,
            asked_at=now,
        )
        if question_id is not None:
            about = About(project_id=question.project_id, turn_id=question.turn_id, question_id=question_id)
            await outbox.enqueue_text(
                s,
                bot=deps.bot,
                chat_id=_chat(snap),
                text=text,
                kind=Kind.QUESTION,
                ref=f"question-{question_id}",
                at=now,
                max_parts=snap[defaults.OUTBOX_MAX_PARTS],
                about=about,
            )
    if question_id is None:
        log.info("question_already_open", project=question.project_id, turn_id=question.turn_id)
        return
    log.info(
        "question_opened",
        project=question.project_id,
        turn_id=question.turn_id,
        question_id=question_id,
        questions=len(asked),
        options=sum(len(item.options) for item in asked),
    )


# --- answering --------------------------------------------------------------------------------------------------
async def answer_owner(deps: DevDeps, state: TurnsState, origin: OwnerOrigin, item: TurnInput) -> OwnerAnswer | None:
    """The router's step 3 for one closed burst: a gate decision or an agent question takes it (module docstring), or
    it asks which project's question it answers. None: it answers nothing open — route it as usual. An unknown
    `#id` raises `DecisionNotFound` once the burst is recorded, so `guarded()` tells the owner and it is not re-read.
    Anything but this bot's `OwnerOrigin` raises before anything is read (`require_owner_origin`).
    """
    origin = require_owner_origin(origin, deps.bot)
    routable = item.interrupt is None and not item.synthetic  # pre-parse takes those (SPEC §6.1)
    text = domain.answer_text(item) if routable else ""
    if not text:
        return None
    mentioned, names, known = await _names_in(deps, text)
    reply_to = domain.reply_to_of(item)
    waiting = await _open_questions(deps)
    reply = DecisionReply(
        text,
        reply_to_message_id=reply_to,
        mentioned_projects=mentioned,
        names=names,
        open_questions=tuple(PendingItem("question", question.id, question.project_id) for question in waiting),
    )
    try:
        decided = await gate.answer_decision(deps, origin, reply)
    except DecisionNotFound:
        async with deps.db.uow() as s:
            await _record_route(s, deps, item, project_id=None, reason="decision_not_found")
        raise
    if decided.kind is not AnswerKind.NOT_AN_APPROVAL:
        return await _decided(deps, state, item, decided, known)
    if not waiting:
        return None
    binding = domain.bind_answer(
        text,
        waiting,
        reply=await _reply_target(deps, item.chat_id, reply_to),
        mentioned=mentioned,
        names=names,
        decisions_pending=bool(await gate.pending_items(deps)),
    )
    if isinstance(binding, domain.NotAnAnswer):
        return None
    if isinstance(binding, Ambiguous):
        return await _ask_which(deps, item, binding.projects, text)
    return await _answer(
        deps,
        state,
        binding.question,
        text=text,
        names=names,
        spoken=domain.spoken_text(item),
        burst=item,
        ref=str(item.burst_id),
    )


async def answer_clarified(
    deps: DevDeps, state: TurnsState, item: TurnInput, *, asked: TurnInput, project_id: str
) -> OwnerAnswer:
    """Routing resolved the which-project question `answer_owner` asked (a `Resolution` whose intent is
    `Intent.ANSWER`): `asked` is the burst that was ambiguous, `project_id` the pick, `item` the owner's reply that
    picked it. The answer `asked` held goes to that project's newest open question — unless `item` replied to a
    question itself: the reply is the stronger signal, and `item` is then the answer. Both bursts are routed already
    (routing recorded them); `item` is recorded here only when it became the answer and routing left it."""
    target = await _reply_target(deps, item.chat_id, domain.reply_to_of(item))
    waiting = await _open_questions(deps)
    replied = [question for question in waiting if target is not None and question.id == target.question_id]
    if replied and (text := domain.answer_text(item)):
        _mentioned, names, _known = await _names_in(deps, text)
        return await _answer(
            deps,
            state,
            replied[0],
            text=text,
            names=names,
            spoken=domain.spoken_text(item),
            burst=item,
            strict=False,
            ref=str(item.burst_id),
        )
    theirs = [question for question in waiting if question.project_id == project_id]
    text = domain.answer_text(asked)
    if not theirs or not text:
        snap = await settings.snapshot(deps)
        async with deps.db.uow() as s:
            await _tell(s, deps, snap, S.QUESTION_NOT_OPEN(project=project_id), ref=f"closed-{item.burst_id}")
        log.info("question_answer_not_open", project=project_id, burst_id=asked.burst_id)
        return OwnerAnswer(AnswerOutcome.NOT_OPEN, project_id=project_id)
    _mentioned, names, _known = await _names_in(deps, text)
    return await _answer(
        deps,
        state,
        domain.newest(theirs),
        text=text,
        names=names,
        spoken=domain.spoken_text(asked),
        burst=None,
        ref=str(item.burst_id),
    )


async def _decided(
    deps: DevDeps, state: TurnsState, item: TurnInput, decided: DecisionAnswer, known: frozenset[str]
) -> OwnerAnswer:
    """The gate bound the burst (and told the owner): record it as routed, then hand the agent its note."""
    project_id = decided.project_id if decided.project_id in known else None  # a decision may name an unknown folder
    async with deps.db.uow() as s:
        await _record_route(s, deps, item, project_id=project_id, reason="decision_answer")
    if decided.delivery:
        await turns.deliver_decision(deps, state, decided)
    log.info("owner_reply_decided", burst_id=item.burst_id, decision_id=decided.decision_id, kind=decided.kind.value)
    return OwnerAnswer(AnswerOutcome.DECISION, project_id=decided.project_id, turn_id=decided.turn_id, decision=decided)


async def _ask_which(deps: DevDeps, item: TurnInput, candidates: Sequence[str], text: str) -> OwnerAnswer:
    """UC-12 F: one short question naming the projects, as a routing clarification on this burst."""
    snap = await settings.snapshot(deps)
    now = deps.clock.now()
    question = domain.which_question(candidates, redact.scrubbed(text))
    decision = RouteDecision(
        outcome=Outcome.ASK,
        method=Method.BINDING,
        intent=Intent.ANSWER,
        project_id=None,
        confidence=0.0,
        candidates=tuple(Candidate(project, 0.0) for project in candidates),
        reason="question_which_project",
    )
    planned = Planned(item=item, decision=decision, reply=question, model=None, latency_ms=0)
    ref = f"which-{item.burst_id}"
    async with deps.db.uow() as s:
        routed = await routing.record_route(s, bot=deps.bot, planned=planned, at=now)
        if routed is not None:
            await _tell(s, deps, snap, question, ref=ref, kind=Kind.QUESTION)
    if routed is None:
        log.info("question_answer_duplicate", burst_id=item.burst_id)
        return OwnerAnswer(AnswerOutcome.DUPLICATE)
    log.info(
        "question_answer_asked_which",
        burst_id=item.burst_id,
        projects=len(candidates),
        clarification_id=routed.clarification_id,
    )
    return OwnerAnswer(
        AnswerOutcome.ASKED,
        clarification_id=routed.clarification_id,
        message_key=outbox_domain.dedupe_key(Kind.QUESTION, ref, 0),
    )


async def _answer(
    deps: DevDeps,
    state: TurnsState,
    question: OpenQuestion,
    *,
    text: str,
    names: Sequence[str],
    spoken: str,
    burst: TurnInput | None,
    ref: str,
    strict: bool = True,
) -> OwnerAnswer:
    """Record the answer (and the burst as routed) in one unit of work, then hand it to the turn. `strict`: the burst
    must still be unrouted, else another pass had it."""
    snap = await settings.snapshot(deps)
    picked = domain.answer_of(text, question.questions, names=names)
    now = deps.clock.now()
    try:
        async with deps.db.uow() as s:
            if burst is not None:
                recorded = await _record_route(s, deps, burst, project_id=question.project_id, reason="question")
                if strict and not recorded:
                    raise _AlreadyHandled
            if isinstance(picked, OutOfRange):
                notice = S.QUESTION_OUT_OF_RANGE(project=question.project_id, count=picked.count)
                await _tell(s, deps, snap, notice, ref=f"range-{ref}", question=question)
            elif not await store.mark_question_answered(
                s,
                question.id,
                from_status=QuestionStatus.OPEN.value,
                to_status=QuestionStatus.ANSWERED.value,
                answer=domain.answer_json(picked),
                at=now,
            ):
                raise _AlreadyHandled
    except _AlreadyHandled:
        log.info("question_answer_duplicate", question_id=question.id, burst_id=burst.burst_id if burst else None)
        return OwnerAnswer(AnswerOutcome.DUPLICATE, project_id=question.project_id, question_id=question.id)
    if isinstance(picked, OutOfRange):
        log.info("question_answer_out_of_range", question_id=question.id, options=picked.count)
        return OwnerAnswer(
            AnswerOutcome.OUT_OF_RANGE,
            project_id=question.project_id,
            question_id=question.id,
            turn_id=question.turn_id,
        )
    return await _hand_over(deps, state, question, picked, spoken=spoken)


async def _hand_over(
    deps: DevDeps, state: TurnsState, question: OpenQuestion, answer: Answer, *, spoken: str
) -> OwnerAnswer:
    """The recorded answer → the held callback, or the turn's next input once the callback is gone (SPEC §3.2.2
    step 5). Then its mode, and the owner's confirmation."""
    fallback = prompts.render("dev/questions_answer", answers=domain.answers_block(answer))
    snap = await settings.snapshot(deps)
    try:
        held = await turns.answer_question(
            deps,
            state,
            turn_id=question.turn_id,
            question_id=question.tool_use_id,
            answers=dict(answer.answers),
            fallback_text=fallback,
        )
    except TurnNotFound:  # deleted between the read and now: nothing can take the answer
        async with deps.db.uow() as s:
            await store.set_questions_status(
                s, [question.id], from_status=QuestionStatus.ANSWERED.value, to_status=QuestionStatus.CANCELLED.value
            )
            notice = S.QUESTION_NOT_OPEN(project=question.project_id)
            await _tell(s, deps, snap, notice, ref=f"gone-{question.id}", question=question)
        log.warning("question_turn_gone", question_id=question.id, turn_id=question.turn_id)
        return OwnerAnswer(AnswerOutcome.NOT_OPEN, project_id=question.project_id, question_id=question.id)
    mode = AnswerMode.CALLBACK if held else AnswerMode.MESSAGE
    confirmation = domain.answered_text(question.project_id, answer, spoken=spoken, held=held)
    async with deps.db.uow() as s:
        await store.set_answer_mode(s, question.id, mode=mode.value)
        await _tell(s, deps, snap, confirmation, ref=f"answer-{question.id}", question=question, kind=Kind.ECHO)
    log.info(
        "question_answered",
        project=question.project_id,
        turn_id=question.turn_id,
        question_id=question.id,
        mode=mode.value,
        chose=answer.chose,
    )
    return OwnerAnswer(
        AnswerOutcome.ANSWERED,
        project_id=question.project_id,
        question_id=question.id,
        turn_id=question.turn_id,
        held=held,
    )


# --- restart (SPEC §2.7) ----------------------------------------------------------------------------------------
async def recover_questions(deps: DevDeps, state: TurnsState) -> int:
    """Startup, after `turns.recover_turns` and before the scheduler: an answer recorded but never handed to its turn
    (a crash between the two) goes in now — as a message, since no callback outlives a restart. Open questions need
    nothing: they stay open, and their answer will arrive as a message. Returns how many were handed over."""
    async with deps.db.read() as s:
        rows = await store.list_undelivered_answers(s, QuestionStatus.ANSWERED.value)
    for row in rows:
        await _hand_over(deps, state, _open(row), domain.answer_from_json(row.answer_json), spoken="")
    log.info("questions_recovered", handed_over=len(rows))
    return len(rows)


# --- retention (SPEC §4.2) --------------------------------------------------------------------------------------
async def apply_retention(deps: DevDeps) -> int:
    """Finished questions are deleted 90 days after they were asked, in batches; an open one is never touched.
    Returns how many rows were deleted."""
    cutoff = domain.question_cutoff(deps.clock.now())
    finished = sorted(status.value for status in domain.TERMINAL)
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_questions_before(s, cutoff, statuses=finished, limit=limit)
    )
    log.info("questions_retention_applied", rows_deleted=deleted)
    return deleted
