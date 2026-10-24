"""T37 load: 15 projects × 10 minutes of virtual time (NFR-CAP-01/02, NFR-SPD-01 report path, UC-02, UC-03, UC-11,
UC-12, UC-15).

Fifteen projects work at once, up to the concurrency cap (`turns.max_concurrent` = 15), every turn reporting its
progress into the one dev group, while the owner adds follow-ups, asks "X qanday?", answers the agents' questions and
stops some turns. Pass criteria:
- 0 lost messages, every result delivered (each turn that ended done has its "X · Natija: …", each stopped one its
  "X: to'xtatdim.");
- Telegram's limits respected: 1 message/s per chat, 20/min per group (edits count), and progress edits coalesced to
  at most one per `outbox.progress_edit_s` per progress message;
- the report path still answers within 4 s at p90 (≥ 20 samples) while 15 turns work: measured from the owner's
  message to the report reaching Telegram, plus the burst's own quiet window after a complete question, as
  `tests/integration/test_dev_bot.py` measures it.
"""

import itertools
import json
import math
import random
import re
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Final

import pytest
from sqlalchemy import func, select

from tests.factories import agent_question
from tests.fakes.agent import Pause, ScriptItem, ToolStep, done_result
from tests.fault.harness import (
    World,
    delivered_to,
    delivery_counts,
    drain,
    outbox_ledger,
    record,
    release_paused,
    tokens_in,
    world,
)
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.dev.turns.domain import ACTIVE, TurnStatus
from yordamchi.dev.turns.models import Turn
from yordamchi.ports.agent import AgentInput, QuestionAsked
from yordamchi.ports.assistant import AssistantReply, AssistantRequest
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

PROJECTS: Final = (
    "BonviHR",
    "HRMentor",
    "Markaz",
    "OvozYoz",
    "AgentsRoom",
    "MegaPlus",
    "Wunderkind",
    "BonviCall",
    "Kassa",
    "Ombor",
    "Talaba",
    "Darslik",
    "Xarita",
    "Navbat",
    "Sayohat",
)
ASKING: Final = ("Kassa", "Talaba")  # their first turn asks the owner a question
STOPPING: Final = ("Ombor", "Xarita")  # the owner stops their first turn
SEED: Final = 37
RUN_S: Final = 600.0  # 10 minutes of virtual time
TICK_S: Final = 5.0
STEPS: Final = 3  # tool steps per turn, each followed by a pause the agent "works" through
REPORT_EVERY_S: Final = 25.0
REPORT_BUDGET_S: Final = 4.0  # NFR-SPD-01: project report from the digest, p90
REPORT_SAMPLES_MIN: Final = 20
CAP: Final = 15
MEASURE_STEP_S: Final = 0.1  # virtual seconds per step while a report's latency is measured
_NAME: Final = re.compile(r"^(\w+)")


def project_of(text: str) -> str:
    found = _NAME.match(text)
    assert found is not None, text
    return found.group(1)


def working(message: AgentInput) -> tuple[ScriptItem, ...]:
    """A turn of `STEPS` tool calls with the agent busy after each, finishing with the messages it worked on. The
    first turn of an `ASKING` project asks a question first."""
    names = " ".join(tokens_in(message.text)) or "ish"
    project = project_of(message.text)
    items: list[ScriptItem] = []
    if project in ASKING and "#q" in message.text:
        question = agent_question("Qaysi bazadan foydalanamiz?", [("SQLite", "oddiy"), ("Postgres", "server")])
        items += [QuestionAsked(f"q-{project}", (question,)), Pause()]
    for step in range(STEPS):
        items += [ToolStep("Edit", {"file_path": f"src/step_{step}.py"}, tool_use_id=f"{project}-{step}"), Pause()]
    return (*items, done_result(f"Natija: {names} bajarildi.\nTestlar: o'tdi (5)"))


def routes(request: AssistantRequest) -> AssistantReply:
    """The routing model: "X qanday?" is a report on X; anything else is work for the project it names."""
    structured: dict[str, JsonValue] | None = None
    if request.kind == "routing":
        prompt = json.loads(request.prompt)
        named = prompt.get("named") or []
        text = " ".join(str(part.get("text") or "") for part in prompt.get("message", []))
        intent = "report_project" if text.rstrip().endswith("qanday?") else "instruct_project"
        project = named[0] if named else None
        structured = {
            "intent": intent,
            "project_id": project,
            "candidates": [],
            "confidence": 0.95 if project else 0.0,
            "needs_fresh": False,
            "is_new_project": False,
        }
    text = json.dumps(structured) if structured is not None else "Hammasi joyida."
    return AssistantReply(
        text=text,
        provider="claude",
        model=request.model,
        duration_ms=1,
        structured=structured,
        prompt_version=request.prompt_version,
    )


async def statuses(here: World) -> dict[str, int]:
    async with here.read() as s:
        rows = (await s.execute(select(Turn.status, func.count()).group_by(Turn.status))).all()
    return {status: int(count) for status, count in rows}


async def active_now(here: World) -> int:
    """Turns working or waiting for the owner (a waiting turn holds no slot of the cap, SPEC §5.6)."""
    found = await statuses(here)
    return sum(found.get(status.value, 0) for status in ACTIVE)


def p90(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, math.ceil(0.9 * len(ordered)) - 1)]


async def test_fifteen_projects_for_ten_minutes_lose_nothing_and_keep_telegrams_limits(  # noqa: PLR0915
    request: pytest.FixtureRequest,
) -> None:
    rng = random.Random(SEED)  # noqa: S311  # a seeded simulation, so a failure replays; not a secret
    async with world(*PROJECTS, script=working) as here:
        here.fakes.assistant.responder = routes
        bot = await here.start()
        snap = await settings.snapshot(bot.deps)
        await here.advance(3.0)
        tokens: dict[str, str] = {}
        busy: set[str] = set()

        def instruct(project: str, extra: str = "") -> None:
            token = f"#{len(tokens):02d}"
            tokens[token] = project
            busy.add(project)
            here.say(f"{project}'da hisobot sahifasini qo'sh {token}{extra}")

        # --- all fifteen start within the first minute --------------------------------------------------------------
        for project in PROJECTS:
            instruct(project, " #q" if project in ASKING else "")
            await here.advance(3.0)
        await here.within(60, lambda: _at_least(here, CAP), what=f"{CAP} turns at once")
        assert (await statuses(here)).get(TurnStatus.QUEUED.value, 0) == 0  # none had to wait for a slot

        reports: list[float] = []
        asked: set[str] = set()
        stopped: list[str] = []
        started = here.clock.now()
        next_report = started
        # The owner does one thing per tick (5 s, more than a burst's quiet window), so his messages about different
        # projects never fall into one burst. Each tick the agents also work on (some sessions go on one step).
        while here.clock.now() - started < timedelta(seconds=RUN_S):
            now = here.clock.now()
            elapsed = (now - started).total_seconds()
            await release_paused(here, chance=0.35, rng=rng)
            ended = [project for project in PROJECTS if project in busy and _ended(here, project, tokens)]
            waiting = [p for p in ASKING if p not in asked and here.telegram.posted(f"{p} · Savol:")]
            if waiting:  # an agent's question, answered by project name and option number
                asked.add(waiting[0])
                here.say(f"{waiting[0]} 2")
            elif elapsed >= 120 and len(stopped) < len(STOPPING) and await _running(here, STOPPING[len(stopped)]):
                project = STOPPING[len(stopped)]  # two working turns are stopped, from two minutes in
                stopped.append(project)
                here.say(f"{project}'ni to'xtat")
            elif now >= next_report:  # "X qanday?" while all this runs: the report path's latency
                next_report = now + timedelta(seconds=REPORT_EVERY_S)
                reports.append(await _report_latency(here, rng.choice(PROJECTS)))
                continue
            elif ended:  # a project whose turn ended gets the next instruction, so the cap stays full
                busy.discard(ended[0])
                instruct(ended[0])
            elif rng.random() < 0.3:  # a follow-up into a working turn
                project = rng.choice([p for p in PROJECTS if p not in STOPPING])
                token = f"#{len(tokens):02d}"
                tokens[token] = project
                here.say(f"{project}: testlarini ham yoz {token}.")
            await here.advance(TICK_S)
        await drain(here)

        budget = REPORT_BUDGET_S + snap[defaults.BURST_AFTER_COMPLETE_TEXT_S]  # + the burst's own quiet window
        record(request, "report_samples", len(reports))
        record(request, "report_p90_s", p90(reports))
        record(request, "report_max_s", max(reports))
        assert len(reports) >= REPORT_SAMPLES_MIN
        assert p90(reports) <= budget, f"report p90 {p90(reports):.2f} s > {budget} s: {sorted(reports)}"

        # --- every instruction ran once, in its own project; every result reached the owner ------------------------
        counts = delivery_counts(here, tokens)
        assert all(count <= 1 for count in counts.values()), counts
        assert {t: found for t, found in delivered_to(here).items() if found != {tokens[t]}} == {}
        async with here.read() as s:
            turns = (await s.scalars(select(Turn))).all()
        done = [turn for turn in turns if turn.status == TurnStatus.DONE.value]
        assert len(done) >= 2 * len(PROJECTS)  # each project finished at least two turns in the ten minutes
        for turn in done:
            assert here.telegram.posted(f"{turn.project_id} · Natija:"), turn.project_id
        assert stopped == list(STOPPING)
        for project in STOPPING:
            assert here.telegram.posted(S.STOPPED(project=project))
        undelivered = [t for t, n in counts.items() if n == 0]
        assert undelivered == [], undelivered
        assert [answer for _, _, answer in here.fakes.agents.answers] == [
            {"Qaysi bazadan foydalanamiz?": "Postgres"}
        ] * len(ASKING)

        # --- 0 lost; Telegram's limits; progress edits coalesced (NFR-CAP-02, UC-11) -----------------------------
        ledger = await outbox_ledger(here)
        record(request, "turns_done", len(done))
        record(request, "instructions", len(tokens))
        record(request, "posts", ledger.posted)
        record(request, "edits", len(here.telegram.edits))
        assert (ledger.lost, ledger.pending, ledger.failed) == ((), (), ())
        assert ledger.duplicates == ()
        assert here.telegram.violations == []
        edit_every = timedelta(seconds=snap[defaults.OUTBOX_PROGRESS_EDIT_S])
        by_message: defaultdict[int, list[datetime]] = defaultdict(list)
        for edit in here.telegram.edits:
            if " · " in edit.text and "Onlayn" not in edit.text:
                by_message[edit.message_id].append(edit.at)
        assert by_message
        for times in by_message.values():
            assert all(later - earlier >= edit_every for earlier, later in itertools.pairwise(times))


async def _running(here: World, project: str) -> bool:
    async with here.read() as s:
        found = await s.scalar(
            select(func.count())
            .select_from(Turn)
            .where(Turn.project_id == project, Turn.status == TurnStatus.RUNNING.value)
        )
    return bool(found)


async def _at_least(here: World, count: int) -> bool:
    return await active_now(here) >= count


def _ended(here: World, project: str, tokens: dict[str, str]) -> bool:
    """The project's latest instruction has its result (or its stop) at Telegram."""
    latest = max((t for t, p in tokens.items() if p == project), default=None)
    if latest is None:
        return False
    return bool(here.telegram.posted(f"{project} · Natija:")) and any(
        latest in post.text for post in here.telegram.posted(f"{project} · Natija:")
    )


async def _report_latency(here: World, project: str) -> float:
    """The owner asks "X qanday?" alone (a quiet tick after his last message): the virtual seconds until the report
    reaches Telegram. The next tick comes after the report. Meanwhile the clock moves in small steps, each only once
    the bot stopped reacting, so a latency measures the bot's path (burst window, routing, digest, outbox, Telegram's
    limits), not how fast this machine ran fifteen turns' DB work."""
    before = len(here.telegram.posted(f"{project} · hozir"))
    asked = here.clock.now()
    here.say(f"{project} qanday?")

    def answered() -> bool:
        return len(here.telegram.posted(f"{project} · hozir")) > before

    await here.within(60, answered, what=f"{project} report", step=MEASURE_STEP_S, idle=True)
    return (here.telegram.posted(f"{project} · hozir")[before].at - asked).total_seconds()
