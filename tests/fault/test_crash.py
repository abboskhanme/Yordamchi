"""T36 crash suite: 50 owner messages with `kill -9` at random points, a restart after each (NFR-REL-05/06/07,
UC-06; SPEC §2.5 step 8, §2.7).

The bot is the real wired dev bot with its real poller (`tests/fault/harness.py`); a kill cancels every task of the
process at once and blocks every DB, Telegram and executor call it would still make, so no shutdown pass runs and no
clean marker is written. Each kill lands at a random moment: after a random stretch of virtual time, a random slice of
real time and a random number of event-loop turns, so it hits whatever the bot is doing (intake, a burst window,
routing, a turn, the outbox). launchd brings the bot back after a few seconds; the owner sometimes writes while it is
down (Telegram holds the update), and some restarts die again inside their own start pass (a crash loop). The random
choices are seeded. `test_crash_points.py` kills at the exact transaction boundaries instead.

Pass criteria, checked against the DB and against what the fake Telegram and the fake executor really got:
- every message reached the inbox (Telegram redelivers what no process confirmed), and every one either reached its
  own project's executor exactly once or its delivery was told as "bajarilmay qoldi — qayta yuboraymi?": 0 silent
  drops, 0 double deliveries;
- every turn a restart cut off is listed in a "Qayta ishga tushdim … — davom ettiraymi?" and is never run again;
- every outbox message reached Telegram, duplicates ≤ 2 % of what was posted; Telegram's limits held;
- each process that lived a minute told "Qayta ishga tushdim" within 60 s of its start.

After a restart the owner's next message comes a quiet window later than the bot's start: updates Telegram held for
the dead bot all arrive at once, and the burst windows read the arrival time, so messages the owner sent far apart
would be joined (`test_network.py::test_messages_held_while_offline_stay_apart` covers that bug on its own).
"""

import asyncio
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

import pytest
from sqlalchemy import select

from tests.fakes.agent import Pause, ScriptItem, ToolStep, done_result
from tests.fault.harness import (
    OFFER_END,
    OFFER_START,
    BotProcess,
    OutboxLedger,
    Script,
    World,
    cut_off_turns,
    delivered_to,
    delivery_counts,
    drain,
    offered_projects,
    outbox_ledger,
    record,
    release_paused,
    tokens_in,
    uncertain_tokens,
    world,
)
from yordamchi.dev.turns.domain import RESTART_UNTOLD, TurnStatus
from yordamchi.ports.agent import AgentInput
from yordamchi.shared.inbox.models import InboundUpdate

PROJECTS: Final = ("BonviHR", "HRMentor", "Markaz", "OvozYoz", "AgentsRoom")
TASKS: Final = (
    "login sahifasini tuzat",
    "testlarini yoz",
    "README'ni yangila",
    "hisobot sahifasini qo'sh",
    "formadagi xatoni tuzat",
)
MESSAGES: Final = 50
KILLS: Final = 10
SEED: Final = 20261024
QUIET_GAP_S: Final = (3.0, 12.0)  # between instructions for different projects (the text window is 2 s)
FOLLOW_UP_GAP_S: Final = (0.3, 1.5)  # a quick second message for the same project
DOWNTIME_S: Final = (1.0, 12.0)  # launchd's restart (NFR-REL-01: back within 30 s)
START_PASS_UNITS: Final = 4  # a crash loop dies inside the start pass: recovery, questions, notice, told
MAX_DUPLICATE_RATE: Final = 0.02  # NFR-REL-06
RESTART_NOTICE_S: Final = 60.0  # UC-06


def coding_script(rng: random.Random) -> Script:
    """Each new CLI turn reads a file, then finishes; four in ten pause first (the agent is still working) until the
    test releases them. The result names the messages it worked on."""

    def script(message: AgentInput) -> tuple[ScriptItem, ...]:
        names = " ".join(tokens_in(message.text)) or "ish"
        result = done_result(f"Natija: {names} bajarildi.\nTestlar: o'tdi (3)")
        read = ToolStep("Read", {"file_path": "README.md"})
        return (read, Pause(), result) if rng.random() < 0.4 else (read, result)

    return script


@dataclass
class Scenario:
    here: World
    rng: random.Random
    tokens: dict[str, str] = field(default_factory=dict)  # "#07" → its project
    kills: list[str] = field(default_factory=list)  # when each process died, for the failure messages
    last_project: str = PROJECTS[0]

    def say(self, project: str, *, while_down: bool = False) -> str:
        if not while_down:  # the scenario means the bot to be up here; a death it did not plan is a test bug
            assert not self.here.bot.dead, "no bot is running"
        token = f"#{len(self.tokens):02d}"
        self.tokens[token] = project
        self.last_project = project
        self.here.say(f"{project}'da {self.rng.choice(TASKS)} {token}")
        return token

    def next_message(self) -> tuple[str, float]:
        """Mostly an instruction for some project a few seconds later; sometimes a quick follow-up for the same one."""
        if self.rng.random() < 0.25:
            return self.last_project, self.rng.uniform(*FOLLOW_UP_GAP_S)
        project = self.rng.choice(PROJECTS)
        gap = self.rng.uniform(*(QUIET_GAP_S if project != self.last_project else FOLLOW_UP_GAP_S))
        return project, gap

    async def kill(self) -> None:
        """`kill -9` at a random moment of whatever the bot is doing."""
        here, rng = self.here, self.rng
        await here.advance(rng.uniform(0.0, 6.0))
        await asyncio.sleep(rng.uniform(0.0, 0.012))  # real time: aiosqlite threads and tasks are mid-flight
        for _ in range(rng.randint(0, 80)):
            await asyncio.sleep(0)
        await here.crash()
        self.kills.append(f"{here.clock.now().time()}")

    async def restart(self) -> BotProcess:
        """launchd brings the bot back; the owner may write meanwhile (to the project he wrote last, see the module
        docstring); one start in five dies inside its own start pass. The next message comes a quiet window later."""
        here, rng = self.here, self.rng
        while True:
            await here.advance(rng.uniform(*DOWNTIME_S))
            if rng.random() < 0.5:
                self.say(self.last_project, while_down=True)
            crash_loop = rng.random() < 0.2
            kill_at = (rng.randint(1, START_PASS_UNITS), rng.random() < 0.5) if crash_loop else None
            process = await here.start(kill_at=kill_at)
            if not process.dead:
                await here.advance(rng.uniform(*QUIET_GAP_S))
                return process
            await process.reap()
            self.kills.append(f"{here.clock.now().time()} start pass")


@dataclass(frozen=True, slots=True)
class Outcome:
    delivered: dict[str, int]  # token → executor sends carrying it
    projects: dict[str, set[str]]  # token → projects whose session got it
    uncertain: frozenset[str]  # tokens whose delivery was told "bajarilmay qoldi — qayta yuboraymi?"
    cut_off: tuple[tuple[int, str, str, str | None], ...]  # (turn, project, status, code) of turns a restart cut off
    ledger: OutboxLedger


async def outcome_of(scenario: Scenario) -> Outcome:
    here = scenario.here
    return Outcome(
        delivered=delivery_counts(here, scenario.tokens),
        projects=delivered_to(here),
        uncertain=await uncertain_tokens(here),
        cut_off=tuple((row.id, row.project_id, row.status, row.error_code) for row in await cut_off_turns(here)),
        ledger=await outbox_ledger(here),
    )


def lifetime(process: BotProcess, following: BotProcess | None, now: datetime) -> timedelta:
    end = process.mortality.killed_at or (following.started_at if following is not None else now)
    return end - process.started_at


async def test_fifty_messages_survive_kill_9_at_random_points(request: pytest.FixtureRequest) -> None:  # noqa: PLR0915
    rng = random.Random(SEED)  # noqa: S311  # a seeded simulation, so a failure replays; not a secret
    agent_rng = random.Random(SEED + 1)  # noqa: S311  # likewise
    async with world(*PROJECTS, script=coding_script(agent_rng)) as here:
        scenario = Scenario(here, rng)
        await here.start()
        await here.advance(3.0)
        kill_after = sorted(rng.sample(range(2, MESSAGES - KILLS), KILLS))  # message counts a kill follows
        project, gap = PROJECTS[0], 0.0
        while len(scenario.tokens) < MESSAGES:
            scenario.say(project)
            project, gap = scenario.next_message()
            if kill_after and len(scenario.tokens) > kill_after[0]:
                kill_after.pop(0)
                await scenario.kill()
                await scenario.restart()
            else:
                await here.advance(gap)
            await release_paused(here, chance=0.3, rng=rng)
        await drain(here)
        result = await outcome_of(scenario)
        context = f"kills at {scenario.kills}"

        # --- every message reached the inbox: Telegram redelivered what no process had confirmed --------------------
        async with here.read() as s:
            stored = set((await s.scalars(select(InboundUpdate.update_id))).all())
        assert set(here.sent) <= stored, f"never stored: {sorted(set(here.sent) - stored)}; {context}"

        # --- 0 double deliveries, 0 silent drops, each to its own project (NFR-REL-05) ------------------------------
        doubled = {token: n for token, n in result.delivered.items() if n > 1}
        assert not doubled, f"delivered to the executor more than once: {doubled}; {context}"
        silent = sorted(t for t, n in result.delivered.items() if n == 0 and t not in result.uncertain)
        assert not silent, f"neither delivered nor told: {silent}; {context}"
        misrouted = {t: found for t, found in result.projects.items() if found != {scenario.tokens[t]}}
        assert not misrouted, f"delivered to another project: {misrouted}; {context}"

        # --- interrupted turns: offered with "davom ettiraymi?", never run again (NFR-REL-07) ------------------------
        offered = offered_projects(here)
        for process in here.processes:
            if process.app.pipeline.restarted:
                listed = f"{OFFER_START}{', '.join(process.app.pipeline.restarted)}{OFFER_END}"
                assert here.telegram.posted(listed), f"no notice lists {process.app.pipeline.restarted}; {context}"
        cut_projects = {project for _, project, _, _ in result.cut_off}
        assert cut_projects <= offered, f"cut off but never offered: {sorted(cut_projects - offered)}; {context}"
        rerun = [row for row in result.cut_off if row[2] != TurnStatus.INTERRUPTED.value]
        assert not rerun, f"a turn a restart cut off ran again: {rerun}"
        untold = [row for row in result.cut_off if row[3] == RESTART_UNTOLD]
        assert not untold, f"cut off and still untold: {untold}"

        # --- the outbox: nothing lost, duplicates ≤ 2 %, Telegram's limits held (NFR-REL-06, NFR-CAP-02) ------------
        ledger = result.ledger
        assert ledger.pending == ()
        assert ledger.lost == (), f"never reached Telegram: {ledger.lost}"
        assert ledger.failed == ()
        assert ledger.duplicate_rate <= MAX_DUPLICATE_RATE, (
            f"{len(ledger.duplicates)} duplicates of {ledger.posted} posts: {ledger.duplicates}; {context}"
        )
        assert here.telegram.violations == []

        # --- "Qayta ishga tushdim" within 60 s of each start that lived a minute (UC-06) ----------------------------
        notices = [post.at for post in here.telegram.posted("Qayta ishga tushdim")]
        followers: list[BotProcess | None] = [*here.processes[1:], None]
        for process, following in zip(here.processes, followers, strict=True):
            if lifetime(process, following, here.clock.now()) < timedelta(seconds=RESTART_NOTICE_S):
                continue
            deadline = process.started_at + timedelta(seconds=RESTART_NOTICE_S)
            assert any(process.started_at <= at <= deadline for at in notices), (
                f"no restart notice within 60 s of the start at {process.started_at.time()}; {context}"
            )
        record(request, "kills", len(scenario.kills))
        record(request, "processes", len(here.processes))
        record(request, "delivered_once", sum(n == 1 for n in result.delivered.values()))
        record(request, "told_uncertain", len(result.uncertain))
        record(request, "cut_off_turns", len(result.cut_off))
        record(request, "posts", ledger.posted)
        record(request, "duplicates", len(ledger.duplicates))
        assert len(scenario.kills) >= KILLS
        assert sum(n == 1 for n in result.delivered.values()) >= MESSAGES * 0.9  # the work really ran
