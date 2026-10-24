"""questions.service on a real SQLite file, `FakeAgentRuntime` scripts, `FakeMessenger` and `FakeClock` (SPEC §3.2.2,
§2.7; UC-12; spike S3).

Questions come the way the Claude adapter delivers them: a `QuestionAsked` in a turn's script reaches T24's turn
pump, which calls `state.questions` (here `service.open_question`, as `bots/dev/app.py` wires it). A `Pause` after it
is the agent holding its turn for the answer; the owner's answers are closed bursts, as the router hands them over.
"""

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import pytest

from tests.factories import (
    Fakes,
    agent_question,
    drain_outbox,
    make_closed_burst,
    make_decision,
    make_owner_origin,
    make_part,
    make_project,
    make_project_folder,
    make_question,
    make_turn,
    make_turn_request,
    make_voice_part,
    projects_root_of,
    send_outbox,
)
from tests.fakes.agent import Pause, ToolStep, done_result
from tests.fakes.clock import FakeClock
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate.service import AnswerKind
from yordamchi.dev.questions import domain, service, store
from yordamchi.dev.questions.domain import AnswerMode, QuestionStatus
from yordamchi.dev.questions.models import Question
from yordamchi.dev.questions.service import AnswerOutcome
from yordamchi.dev.routing import service as routing
from yordamchi.dev.routing.domain import ChoiceKind, Intent
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.domain import PendingQuestion, TurnStatus
from yordamchi.dev.turns.models import Delivery, Turn
from yordamchi.dev.turns.service import TurnsState
from yordamchi.ports.agent import QuestionAsked, ToolDenied, TurnResult
from yordamchi.shared.burst import service as burst
from yordamchi.shared.burst.domain import BurstPart, TurnInput
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.models import OutboxMessage

PROJECT = "BonviHR"
OTHER = "HRMentor"
START_HEAD = "0000000aaaa"
GRACE = 1.5  # turns.result_grace_s
DB_QUESTION = "Qaysi bazadan foydalanamiz?"
FINAL = "Natija: baza tanlandi.\nTestlar: o'tdi (3 passed)"


def result(text: str = FINAL) -> TurnResult:
    return done_result(text)


# --- fixtures and helpers -------------------------------------------------------------------------------------------
@pytest.fixture
async def folders(dev_deps: DevDeps, fakes: Fakes, paths: Paths) -> dict[str, Path]:
    """Two registered projects whose git starts clean at `START_HEAD`."""
    found: dict[str, Path] = {}
    for name in (PROJECT, OTHER):
        folder = make_project_folder(projects_root_of(paths), name, git_dir=True)
        async with dev_deps.db.uow() as s:
            await make_project(s, name, at=fakes.clock.now(), path=folder)
        fakes.git.heads[folder] = START_HEAD
        fakes.git.remotes[(folder, "origin")] = f"git@github.com:abboskhanme/{name}.git"
        found[name] = folder
    return found


@pytest.fixture
async def state(dev_deps: DevDeps) -> AsyncIterator[TurnsState]:
    """T24's state with this feature's hook, as `bots/dev/app.py` builds it."""
    state = TurnsState()
    state.questions = service.open_question
    yield state
    await turns.shutdown_turns(dev_deps, state)


async def until(check: Callable[[], object], *, timeout: float = 3.0, what: str = "condition") -> None:
    """Wait (real time) for background tasks to reach a state; the scripted clock never moves here."""
    deadline = time.monotonic() + timeout
    while True:
        outcome = check()
        if inspect.isawaitable(outcome):
            outcome = await outcome
        if outcome:
            return
        if time.monotonic() > deadline:
            msg = f"{what} not reached"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


async def turn(deps: DevDeps, turn_id: int) -> Turn:
    async with deps.db.read() as s:
        row = await turns_store.get_turn(s, turn_id)
    assert row is not None
    return row


def status_is(deps: DevDeps, turn_id: int, status: TurnStatus) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await turn(deps, turn_id)).status == status.value

    return check


async def questions(deps: DevDeps) -> list[Question]:
    async with deps.db.read() as s:
        return [row for status in QuestionStatus for row in await store.list_questions_with_status(s, status.value)]


async def question(deps: DevDeps, question_id: int) -> Question:
    async with deps.db.read() as s:
        row = await store.get_question(s, question_id)
    assert row is not None
    return row


async def messages(deps: DevDeps, kind: str | None = None) -> list[OutboxMessage]:
    async with deps.db.read() as s:
        rows = await outbox_store.list_outbox_messages(s, deps.bot.value)
    return [row for row in rows if kind is None or row.kind == kind]


def text_of(row: OutboxMessage) -> str:
    payload = row.payload_json
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


async def texts(deps: DevDeps, kind: str | None = None) -> list[str]:
    return [text_of(row) for row in await messages(deps, kind)]


async def deliveries(deps: DevDeps, turn_id: int) -> list[Delivery]:
    async with deps.db.read() as s:
        return await turns_store.list_deliveries(s, turn_id)


async def ask(
    deps: DevDeps,
    fakes: Fakes,
    state: TurnsState,
    project: str = PROJECT,
    *,
    tool_use_id: str = "toolu_q1",
) -> tuple[int, int]:
    """A turn of `project` whose agent asks and holds its turn for the answer: (turn id, question id)."""
    before = len(await questions(deps))
    fakes.agents.script_turn(QuestionAsked(tool_use_id, (agent_question(),)), Pause(), result())
    submitted = await turns.submit_turn(deps, state, make_turn_request(project, "Bazani sozla"))
    await turns.schedule_turns(deps, state)
    await until(status_is(deps, submitted.turn_id, TurnStatus.WAITING_QUESTION), what="turn waits for the owner")
    await until(lambda: _count_above(deps, before), what="question row written")
    rows = [row for row in await questions(deps) if row.turn_id == submitted.turn_id]
    return submitted.turn_id, rows[-1].id


async def _count_above(deps: DevDeps, before: int) -> bool:
    return len(await questions(deps)) > before


async def owner_says(deps: DevDeps, *parts: BurstPart) -> TurnInput:
    """A closed burst of the owner's parts, as the router reads it."""
    async with deps.db.uow() as s:
        return await make_closed_burst(s, *parts)


async def answer(deps: DevDeps, state: TurnsState, item: TurnInput) -> service.OwnerAnswer | None:
    return await service.answer_owner(deps, state, make_owner_origin(update_id=900 + item.burst_id), item)


async def posted_message_id(deps: DevDeps, question_id: int) -> int:
    """The Telegram message the question went out as (the outbox sent it)."""
    sent = await outbox.get_delivery(deps, f"question:question-{question_id}:0")
    assert sent is not None
    assert sent.message_id is not None
    return sent.message_id


async def unrouted(deps: DevDeps) -> set[int]:
    return {item.burst_id for item in await burst.list_closed_bursts(deps, limit=50)}


def session_of(fakes: Fakes, index: int = -1) -> str:
    return fakes.agents.sends[index][0]


# --- asking (SPEC §3.2.2 step 3) ------------------------------------------------------------------------------------
async def test_an_agent_question_reaches_the_owner_numbered_within_5_s_and_without_buttons(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    turn_id, question_id = await ask(dev_deps, fakes, state)
    await drain_outbox(dev_deps, fakes.clock, seconds=5.0)

    row = await question(dev_deps, question_id)
    assert (row.project_id, row.turn_id, row.tool_use_id, row.status) == (PROJECT, turn_id, "toolu_q1", "open")
    [queued] = await messages(dev_deps, "question")
    assert queued.priority == 0
    assert queued.sent_at is not None
    assert queued.sent_at - row.asked_at <= timedelta(seconds=5)
    [sent] = [
        request for request in fakes.messenger.sent() if str(request.payload["text"]).startswith(f"{PROJECT} · Savol")
    ]
    assert str(sent.payload["text"]).splitlines() == [
        "BonviHR · Savol: Qaysi bazadan foydalanamiz?",
        "1. PostgreSQL — ishonchli, production uchun",
        "2. SQLite — oddiy, bitta fayl",
        S.AGENT_QUESTION_HINT(),
    ]
    link = await outbox.find_message_link(
        dev_deps, chat_id=sent.chat_id, message_id=await posted_message_id(dev_deps, question_id)
    )
    assert link is not None
    assert (link.about.project_id, link.about.turn_id, link.about.question_id) == (PROJECT, turn_id, question_id)
    fakes.messenger.assert_no_reply_markup()


async def test_the_same_question_is_written_and_posted_once(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path]
) -> None:
    async with dev_deps.db.uow() as s:
        row = await make_turn(s, PROJECT, status=TurnStatus.WAITING_QUESTION, delivery_status="delivered")
    pending = PendingQuestion(row.id, PROJECT, "toolu_q1", (agent_question(),))
    await service.open_question(dev_deps, pending)
    await service.open_question(dev_deps, pending)
    assert len(await questions(dev_deps)) == 1
    assert len(await messages(dev_deps, "question")) == 1


# --- answering into the held callback -------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("said", "label"),
    [("2", "SQLite"), ("SQLite", "SQLite"), ("postgres bo'lsin", "PostgreSQL"), ("MySQL ishlating", "MySQL ishlating")],
)
async def test_a_number_an_option_or_own_words_answer_into_the_held_callback(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState, said: str, label: str
) -> None:
    turn_id, question_id = await ask(dev_deps, fakes, state)
    item = await owner_says(dev_deps, make_part(1, text=said))

    outcome = await answer(dev_deps, state, item)

    assert outcome == service.OwnerAnswer(
        AnswerOutcome.ANSWERED, project_id=PROJECT, question_id=question_id, turn_id=turn_id, held=True
    )
    assert fakes.agents.answers == [(session_of(fakes), "toolu_q1", {DB_QUESTION: label})]
    assert (await turn(dev_deps, turn_id)).status == "running"
    row = await question(dev_deps, question_id)
    assert (row.status, row.answer_mode) == ("answered", "callback")
    assert await texts(dev_deps, "echo") == [f"→ {PROJECT}: javob «{label}»"]
    assert item.burst_id not in await unrouted(dev_deps)  # handled once: the router does not route it again

    await fakes.agents.release(session_of(fakes))
    await until(lambda: fakes.clock.pending_sleepers > 0, what="result grace armed")
    await fakes.clock.advance(GRACE)
    await until(status_is(dev_deps, turn_id, TurnStatus.DONE))


async def test_a_voice_answer_is_bound_from_its_transcript_and_echoed(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await ask(dev_deps, fakes, state)
    item = await owner_says(dev_deps, make_voice_part(1, at_s=0, transcript="ikkinchisi"))

    outcome = await answer(dev_deps, state, item)

    assert outcome is not None
    assert outcome.held
    assert fakes.agents.answers[0][2] == {DB_QUESTION: "SQLite"}
    assert await texts(dev_deps, "echo") == [f"Ovoz: «ikkinchisi» → {PROJECT}: javob «SQLite»"]


async def test_a_number_past_the_options_is_told_and_the_question_stays_open(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    _turn_id, question_id = await ask(dev_deps, fakes, state)
    outcome = await answer(dev_deps, state, await owner_says(dev_deps, make_part(1, text="5")))
    assert outcome is not None
    assert outcome.outcome is AnswerOutcome.OUT_OF_RANGE
    assert await texts(dev_deps, "notice") == [S.QUESTION_OUT_OF_RANGE(project=PROJECT, count=2)]
    assert (await question(dev_deps, question_id)).status == "open"
    assert fakes.agents.answers == []


# --- the callback is gone: the answer goes in as a message ----------------------------------------------------------
async def test_when_answer_returns_false_the_answer_goes_to_the_turn_as_a_message(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause(), result("Natija: javobni kutyapman."))
    submitted = await turns.submit_turn(dev_deps, state, make_turn_request(PROJECT, "Bazani sozla"))
    await turns.schedule_turns(dev_deps, state)
    await until(status_is(dev_deps, submitted.turn_id, TurnStatus.RUNNING), what="turn running")
    await until(lambda: fakes.agents.sends, what="turn delivered")
    # The session moved on: the executor no longer holds this question, so `answer()` returns False.
    fakes.agents.emit(session_of(fakes), QuestionAsked("toolu_gone", (agent_question(),)))
    await until(status_is(dev_deps, submitted.turn_id, TurnStatus.WAITING_QUESTION))
    await until(lambda: _count_above(dev_deps, 0))

    outcome = await answer(dev_deps, state, await owner_says(dev_deps, make_part(1, text="1")))

    assert outcome is not None
    assert (outcome.outcome, outcome.held) == (AnswerOutcome.ANSWERED, False)
    [row] = await questions(dev_deps)
    assert (row.status, row.answer_mode) == ("answered", AnswerMode.MESSAGE.value)
    assert (await turn(dev_deps, submitted.turn_id)).status == "queued"
    fallback = [item for item in await deliveries(dev_deps, submitted.turn_id) if item.mode == "answer"]
    expected = (
        "[Yordamchi] The owner answered the question you asked earlier with AskUserQuestion:\n\n"
        f"«{DB_QUESTION}»: PostgreSQL\n\nContinue the task with this answer.\n"
    )
    assert [item.text for item in fallback] == [expected]
    assert await texts(dev_deps, "echo") == [f"→ {PROJECT}: javob «PostgreSQL» " + S.QUESTION_ANSWERED_AS_MESSAGE()]

    fakes.agents.script_turn(result())
    await fakes.agents.release(session_of(fakes))  # the old turn's own result; then the answer goes in
    await until(lambda: state.live_of_turn(submitted.turn_id) is None, what="session lets go of the turn")
    await turns.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2, what="the answer is the next input")
    assert fakes.agents.sends[1][1].text == fallback[0].text


async def test_a_parked_question_stays_open_and_its_later_answer_arrives_as_a_message(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    # The hold elapsed: the adapter denied the call with the parked message and the agent ended its turn.
    fakes.agents.script_turn(
        QuestionAsked("toolu_q1", (agent_question(),)),
        ToolDenied(tool_name="AskUserQuestion", reason_code="question_parked"),
        result("Natija: savolingizga javob kutyapman."),
    )
    submitted = await turns.submit_turn(dev_deps, state, make_turn_request(PROJECT, "Bazani sozla"))
    await turns.schedule_turns(dev_deps, state)
    await until(status_is(dev_deps, submitted.turn_id, TurnStatus.WAITING_QUESTION))
    await until(lambda: state.live_of_turn(submitted.turn_id) is None, what="turn detached after the parked result")
    await until(lambda: _count_above(dev_deps, 0), what="question row written")
    await fakes.clock.advance(31 * 60)
    [row] = await questions(dev_deps)
    assert row.status == "open"

    fakes.agents.script_turn(result())
    outcome = await answer(dev_deps, state, await owner_says(dev_deps, make_part(1, text="birinchisi")))

    assert outcome is not None
    assert (outcome.outcome, outcome.held) == (AnswerOutcome.ANSWERED, False)
    assert fakes.agents.answers == []  # no callback was there to take it
    await turns.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2, what="the answer is the next input")
    assert f"«{DB_QUESTION}»: PostgreSQL" in fakes.agents.sends[1][1].text
    assert (await question(dev_deps, row.id)).answer_mode == "message"


# --- which question (UC-12 F) ---------------------------------------------------------------------------------------
async def test_a_bare_number_with_two_projects_waiting_asks_one_question_naming_both(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await ask(dev_deps, fakes, state, PROJECT)
    other_turn, other_question = await ask(dev_deps, fakes, state, OTHER, tool_use_id="toolu_q2")
    first = await owner_says(dev_deps, make_part(1, text="2"))

    asked = await answer(dev_deps, state, first)

    assert asked is not None
    assert asked.outcome is AnswerOutcome.ASKED
    assert asked.clarification_id is not None
    assert fakes.agents.answers == []  # nothing guessed
    which = [text for text in await texts(dev_deps, "question") if text.startswith("«")]
    assert which == [f"«2» — {PROJECT} savoligami yoki {OTHER} savoligami?"]
    assert {row.status for row in await questions(dev_deps)} == {"open"}
    assert first.burst_id not in await unrouted(dev_deps)

    # "HRMentor" resolves routing's clarification; its resolution brings the "2" to HRMentor's question.
    second = await owner_says(dev_deps, make_part(2, at_s=5, text=OTHER))
    resolution = await routing.answer_clarification(dev_deps, second)
    assert resolution is not None
    assert (resolution.choice, resolution.intent, resolution.burst_id) == (
        ChoiceKind.PICKED,
        Intent.ANSWER,
        first.burst_id,
    )
    assert resolution.project_id == OTHER
    outcome = await service.answer_clarified(dev_deps, state, second, asked=first, project_id=resolution.project_id)

    assert (outcome.outcome, outcome.question_id, outcome.held) == (AnswerOutcome.ANSWERED, other_question, True)
    assert [(tool_use_id, answers) for _, tool_use_id, answers in fakes.agents.answers] == [
        ("toolu_q2", {DB_QUESTION: "SQLite"})
    ]
    assert (await turn(dev_deps, other_turn)).status == "running"


async def test_a_reply_binds_the_answer_to_the_question_replied_to(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    _first_turn, first_question = await ask(dev_deps, fakes, state, PROJECT)
    _other_turn, other_question = await ask(dev_deps, fakes, state, OTHER, tool_use_id="toolu_q2")
    await drain_outbox(dev_deps, fakes.clock, seconds=5.0)
    replied = await posted_message_id(dev_deps, first_question)

    outcome = await answer(
        dev_deps, state, await owner_says(dev_deps, make_part(1, text="2", reply_to_message_id=replied))
    )

    assert outcome is not None
    assert (outcome.outcome, outcome.question_id) == (AnswerOutcome.ANSWERED, first_question)
    assert [tool_use_id for _, tool_use_id, _ in fakes.agents.answers] == ["toolu_q1"]
    assert (await question(dev_deps, other_question)).status == "open"


async def test_a_reply_to_a_question_beats_the_pick_of_the_which_question(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    _first_turn, first_question = await ask(dev_deps, fakes, state, PROJECT)
    _other_turn, other_question = await ask(dev_deps, fakes, state, OTHER, tool_use_id="toolu_q2")
    first = await owner_says(dev_deps, make_part(1, text="2"))
    asked = await answer(dev_deps, state, first)
    assert asked is not None
    assert asked.outcome is AnswerOutcome.ASKED
    await drain_outbox(dev_deps, fakes.clock, seconds=5.0)
    replied = await posted_message_id(dev_deps, other_question)

    # "1" replying to HRMentor's question: routing reads it as "the first candidate" (BonviHR); the reply wins.
    second = await owner_says(dev_deps, make_part(2, at_s=8, text="1", reply_to_message_id=replied))
    resolution = await routing.answer_clarification(dev_deps, second)
    assert resolution is not None
    assert resolution.project_id == PROJECT
    outcome = await service.answer_clarified(dev_deps, state, second, asked=first, project_id=PROJECT)

    assert (outcome.outcome, outcome.question_id) == (AnswerOutcome.ANSWERED, other_question)
    assert [(tool_use_id, answers) for _, tool_use_id, answers in fakes.agents.answers] == [
        ("toolu_q2", {DB_QUESTION: "PostgreSQL"})
    ]
    assert (await question(dev_deps, first_question)).status == "open"


async def test_a_named_project_takes_the_answer_without_asking(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await ask(dev_deps, fakes, state, PROJECT)
    _other_turn, other_question = await ask(dev_deps, fakes, state, OTHER, tool_use_id="toolu_q2")
    outcome = await answer(dev_deps, state, await owner_says(dev_deps, make_part(1, text="HRMentor'ga 1")))
    assert outcome is not None
    assert (outcome.outcome, outcome.question_id) == (AnswerOutcome.ANSWERED, other_question)
    assert fakes.agents.answers[0][1:] == ("toolu_q2", {DB_QUESTION: "PostgreSQL"})


async def test_free_words_with_two_projects_waiting_are_left_to_routing(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await ask(dev_deps, fakes, state, PROJECT)
    await ask(dev_deps, fakes, state, OTHER, tool_use_id="toolu_q2")
    item = await owner_says(dev_deps, make_part(1, text="login sahifasini ham tuzatib qo'y"))
    assert await answer(dev_deps, state, item) is None
    assert item.burst_id in await unrouted(dev_deps)
    assert await service.waiting_projects(dev_deps) == (PROJECT, OTHER)
    assert [item.project_id for item in await service.open_items(dev_deps)] == [PROJECT, OTHER]


# --- a question asked inside a subagent (S3) ------------------------------------------------------------------------
async def test_a_subagent_question_reaches_the_owner_as_the_main_agents_question_in_the_same_form(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    asked: dict[str, JsonValue] = {"questions": [agent_question()]}
    fakes.agents.script_turn(
        # The subagent's call passes PreToolUse (the gate lets AskUserQuestion through), then `can_use_tool` denies
        # it with the "hand it to the main agent" message; the main agent asks it itself.
        ToolStep("AskUserQuestion", asked, tool_use_id="toolu_sub", agent_id="agent-7"),
        ToolDenied(tool_name="AskUserQuestion", reason_code="subagent_question"),
        QuestionAsked("toolu_main", (agent_question(),)),
        Pause(),
        result(),
    )
    submitted = await turns.submit_turn(dev_deps, state, make_turn_request(PROJECT, "Bazani sozla"))
    await turns.schedule_turns(dev_deps, state)
    await until(status_is(dev_deps, submitted.turn_id, TurnStatus.WAITING_QUESTION))
    await until(lambda: _count_above(dev_deps, 0))
    await send_outbox(dev_deps)

    [row] = await questions(dev_deps)
    assert row.tool_use_id == "toolu_main"
    [posted] = await texts(dev_deps, "question")
    assert posted == domain.question_text(PROJECT, domain.parse_questions((agent_question(),), str))
    outcome = await answer(dev_deps, state, await owner_says(dev_deps, make_part(1, text="2")))
    assert outcome is not None
    assert outcome.held
    assert fakes.agents.answers[0][1:] == ("toolu_main", {DB_QUESTION: "SQLite"})
    fakes.messenger.assert_no_reply_markup()


# --- gate decisions and agent questions share one binding (SPEC §3.2.2 step 4, §3.2.3 step 5) ------------------------
async def test_a_bare_yes_with_a_decision_and_a_question_open_binds_neither_and_a_number_answers_the_question(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    _turn_id, question_id = await ask(dev_deps, fakes, state, PROJECT)
    async with dev_deps.db.uow() as s:
        decision = await make_decision(s, at=fakes.clock.now(), project=OTHER)

    yes = await answer(dev_deps, state, await owner_says(dev_deps, make_part(1, text="ha")))
    assert yes is not None
    assert yes.outcome is AnswerOutcome.DECISION
    assert yes.decision is not None
    assert yes.decision.kind is AnswerKind.AMBIGUOUS
    assert fakes.agents.answers == []
    assert (await question(dev_deps, question_id)).status == "open"

    number = await answer(dev_deps, state, await owner_says(dev_deps, make_part(2, at_s=3, text="2")))
    assert number is not None
    assert (number.outcome, number.question_id) == (AnswerOutcome.ANSWERED, question_id)

    approved = await answer(
        dev_deps, state, await owner_says(dev_deps, make_part(3, at_s=6, text=f"#{decision.id} ha"))
    )
    assert approved is not None
    assert approved.decision is not None
    assert approved.decision.kind is AnswerKind.APPROVED
    assert await gate.pending_items(dev_deps) == ()


# --- closed questions, restart, retention ---------------------------------------------------------------------------
async def test_a_question_whose_turn_was_stopped_takes_no_answer(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    async with dev_deps.db.uow() as s:
        row = await make_turn(s, PROJECT, status=TurnStatus.WAITING_QUESTION, delivery_status="delivered")
        question_id = await make_question(s, turn_id=row.id, at=fakes.clock.now())
    await turns.stop_turn(dev_deps, state, PROJECT)  # "to'xta" while it waits: the turn is cancelled

    item = await owner_says(dev_deps, make_part(1, text="2"))
    assert await answer(dev_deps, state, item) is None
    assert (await question(dev_deps, question_id)).status == "cancelled"
    assert item.burst_id in await unrouted(dev_deps)


async def test_an_answer_recorded_before_a_crash_is_handed_over_at_startup(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    now = fakes.clock.now()
    async with dev_deps.db.uow() as s:
        row = await make_turn(s, PROJECT, status=TurnStatus.WAITING_QUESTION, delivery_status="delivered")
        question_id = await make_question(s, turn_id=row.id, at=now)
        answered = domain.answer_of("2", domain.parse_questions((agent_question(),), str))
        assert isinstance(answered, domain.Answer)
        await store.mark_question_answered(
            s, question_id, from_status="open", to_status="answered", answer=domain.answer_json(answered), at=now
        )

    assert await service.recover_questions(dev_deps, state) == 1

    assert (await question(dev_deps, question_id)).answer_mode == "message"
    assert [item.mode for item in await deliveries(dev_deps, row.id)] == ["new_turn", "answer"]
    assert (await turn(dev_deps, row.id)).status == "queued"
    assert await service.recover_questions(dev_deps, state) == 0


async def test_retention_deletes_finished_questions_after_90_days_and_never_an_open_one(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path]
) -> None:
    clock: FakeClock = fakes.clock
    old = clock.now() - timedelta(days=91)
    async with dev_deps.db.uow() as s:
        row = await make_turn(s, PROJECT, status=TurnStatus.DONE, delivery_status="delivered")
        await make_question(s, turn_id=row.id, tool_use_id="old-answered", at=old, status=QuestionStatus.ANSWERED)
        await make_question(s, turn_id=row.id, tool_use_id="old-open", at=old)
        recent = clock.now() - timedelta(days=89)
        await make_question(s, turn_id=row.id, tool_use_id="recent", at=recent, status=QuestionStatus.CANCELLED)

    assert await service.apply_retention(dev_deps) == 1
    assert sorted(row.tool_use_id for row in await questions(dev_deps)) == ["old-open", "recent"]


async def test_turns_report_the_status_of_each_stored_turn_and_leave_out_a_missing_one(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path]
) -> None:
    """`turns.turn_statuses`, the one read this feature takes from T24: a question whose turn ended takes no answer."""
    async with dev_deps.db.uow() as s:
        waiting = await make_turn(s, PROJECT, status=TurnStatus.WAITING_QUESTION, delivery_status="delivered")
        done = await make_turn(s, OTHER, status=TurnStatus.DONE, delivery_status="delivered")
    statuses = await turns.turn_statuses(dev_deps, [waiting.id, done.id, 999])
    assert statuses == {waiting.id: TurnStatus.WAITING_QUESTION, done.id: TurnStatus.DONE}
