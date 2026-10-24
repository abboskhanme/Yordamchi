"""T36: a follow-up that lands while its turn is finishing is still delivered (NFR-REL-05: 0 silent drops; UC-02).

The turn's end is decided in two steps: after the result's grace the scheduler sees no input waiting, then it reads
git and the gate and only then commits `running → done`. A follow-up routed in between is merged into the turn that
is still `running` (a `next` delivery). If the turn then ends `done`, that delivery waits forever on a finished turn:
the owner's message never reaches the executor and nobody tells him.
"""

import asyncio
from pathlib import Path

from tests.fakes.agent import ScriptItem, ToolStep, done_result
from tests.fakes.git import FakeGit
from tests.fault.harness import delivery_counts, tokens_in, world
from yordamchi.ports.agent import AgentInput
from yordamchi.ports.git import GitStatus

PROJECT = "BonviHR"


class HeldStatus(FakeGit):
    """`FakeGit` whose `status` can be held: the moment the turn's end reads what changed."""

    def __init__(self, shared: FakeGit) -> None:
        super().__init__()
        self.__dict__.update(shared.__dict__)  # the same repositories, heads and remotes
        self.armed = False
        self.waiting = asyncio.Event()
        self.go = asyncio.Event()

    async def status(self, repo: Path, *, timeout: float) -> GitStatus:
        if self.armed:
            self.armed = False
            self.waiting.set()
            await self.go.wait()
        return await super().status(repo, timeout=timeout)


def finishes(message: AgentInput) -> tuple[ScriptItem, ...]:
    return ToolStep("Read", {"file_path": "README.md"}), done_result(f"Natija: {' '.join(tokens_in(message.text))}.")


async def test_a_follow_up_merged_while_the_turn_finishes_still_reaches_the_executor() -> None:
    async with world(PROJECT, script=finishes) as here:
        repos = HeldStatus(here.fakes.git)
        here.repos = repos
        await here.start()
        await here.advance(2.0)

        here.say(f"{PROJECT}'da login sahifasini tuzat #01")
        await here.within(30, lambda: delivery_counts(here, ["#01"])["#01"] == 1, what="the first instruction")
        repos.armed = True  # the next status read is the turn's end (`_work_facts`): hold it there
        await here.within(30, repos.waiting.is_set, what="the turn's end reading git")

        here.say(f"{PROJECT}'da testlarini ham yoz #02")  # the owner adds to it, as it finishes
        await here.within(30, lambda: bool(here.telegram.posted("ishlayotgan topshiriqqa qo'shdim")), what="merged")
        repos.go.set()

        await here.within(60, lambda: delivery_counts(here, ["#02"])["#02"] == 1, what="the follow-up delivered")
        await here.within(60, lambda: bool(here.telegram.posted("#02")), what="its result")
        assert delivery_counts(here, ["#01", "#02"]) == {"#01": 1, "#02": 1}
