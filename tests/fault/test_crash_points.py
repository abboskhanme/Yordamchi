"""T36: `kill -9` at chosen points of the start pass and of one message's way through the bot (NFR-REL-05/07,
UC-06; SPEC §2.5 steps 6–8, §2.7).

The random suite (`test_crash.py`) kills at random moments; these kills land exactly on the boundaries between the
units of work, where a step that is not in the same transaction as the next one can be lost: just before a unit
begins (what the bot did since the last commit, a Telegram send or an executor delivery, happened; this unit never
will) and just after one commits (nothing after it happens).
"""

import pytest
from sqlalchemy import select

from tests.fakes.agent import Pause, ScriptItem, ToolStep, done_result
from tests.fault.harness import (
    cut_off_turns,
    delivery_counts,
    drain,
    offered_projects,
    outbox_ledger,
    release_paused,
    tokens_in,
    uncertain_tokens,
    world,
)
from yordamchi.dev.turns.domain import TurnStatus
from yordamchi.dev.turns.models import Turn
from yordamchi.ports.agent import AgentInput

PROJECT = "BonviHR"
TOKEN = "#01"
INSTRUCTION = f"{PROJECT}'da login sahifasini tuzat {TOKEN}"
RECOVERY_UNIT = 1  # the start pass's first unit of work: `turns.recover_turns`
FLOW_UNITS = 32  # one message, intake to its result and the progress line's last edit: about 30 units of work
FLOW_LIMIT_S = 60.0


def keeps_working(message: AgentInput) -> tuple[ScriptItem, ...]:
    return ToolStep("Read", {"file_path": "README.md"}), Pause(), done_result(f"Natija: {tokens_in(message.text)}.")


async def test_a_turn_cut_off_is_offered_even_if_the_next_start_dies_after_recovering_it() -> None:
    async with world(PROJECT, script=keeps_working) as here:
        await here.start()
        await here.advance(2.0)
        here.say(INSTRUCTION)
        await here.within(30, lambda: delivery_counts(here, [TOKEN])[TOKEN] == 1, what="the turn works")
        await here.advance(1.0)
        await here.crash()  # the turn is mid-work

        await here.advance(3.0)
        second = await here.start(kill_at=(RECOVERY_UNIT, False))  # dies right after the recovery committed
        assert second.dead
        await second.reap()
        async with here.read() as s:
            [turn] = (await s.scalars(select(Turn))).all()
        assert turn.status == TurnStatus.INTERRUPTED.value  # the recovery pass did run and commit
        assert not here.telegram.posted("davom ettiraymi?")

        await here.advance(3.0)
        await here.start()
        offer = f"Uzilib qolgan ishlar: {PROJECT} — davom ettiraymi?"
        took = await here.within(90, lambda: bool(here.telegram.posted(offer)), what="the cut-off turn offered")
        assert took <= 60
        async with here.read() as s:
            [turn] = (await s.scalars(select(Turn))).all()
        assert turn.status == TurnStatus.INTERRUPTED.value  # offered, never run again (NFR-REL-07)
        assert delivery_counts(here, [TOKEN]) == {TOKEN: 1}


async def _killed_at(offset: int, *, before: bool) -> list[str]:
    """One message; the process dies at the `offset`-th unit of work after it was sent; launchd restarts it. What
    went wrong, if anything."""
    where = f"{'before unit' if before else 'after commit'} +{offset}"
    async with world(PROJECT, script=keeps_working) as here:
        first = await here.start()
        await here.advance(2.0)
        first.db.arm(first.db.units + offset, before=before)
        here.say(INSTRUCTION)
        waited = 0.0
        while not first.dead and waited < FLOW_LIMIT_S:
            await release_paused(here)
            await here.advance(0.5)
            waited += 0.5
        if not first.dead:
            return []  # the message's way took fewer units than `offset`: nothing to kill there
        await here.advance(3.0)
        await here.start()
        await drain(here)

        problems: list[str] = []
        delivered = delivery_counts(here, [TOKEN])[TOKEN]
        cut = await cut_off_turns(here)
        if delivered > 1:
            problems.append(f"{where}: delivered {delivered} times")
        if delivered == 0 and TOKEN not in await uncertain_tokens(here):
            problems.append(f"{where}: neither delivered nor told")
        if any(row.project_id not in offered_projects(here) for row in cut):
            problems.append(f"{where}: cut off but never offered")
        if any(row.status != TurnStatus.INTERRUPTED.value for row in cut):
            problems.append(f"{where}: a cut-off turn ran again")
        if delivered == 1 and not cut and not here.telegram.posted(f"{PROJECT} · Natija"):
            problems.append(f"{where}: delivered, never cut off, but no result")
        ledger = await outbox_ledger(here)
        if ledger.lost or ledger.pending or ledger.failed:
            problems.append(f"{where}: outbox lost {ledger.lost}, pending {ledger.pending}, failed {ledger.failed}")
        if len(ledger.duplicates) > 1:  # one kill can leave at most the one message in flight to resend
            problems.append(f"{where}: {len(ledger.duplicates)} duplicates {ledger.duplicates}")
        if len(here.telegram.posted("Qayta ishga tushdim")) < len(here.processes):
            problems.append(f"{where}: a start without its restart notice")
        if here.telegram.violations:
            problems.append(f"{where}: {here.telegram.violations}")
        return problems


@pytest.mark.parametrize("before", [True, False], ids=["before_each_unit", "after_each_commit"])
async def test_one_message_survives_kill_9_at_every_unit_of_work_on_its_way(before: bool) -> None:
    problems: list[str] = []
    for offset in range(1, FLOW_UNITS + 1):
        problems.extend(await _killed_at(offset, before=before))
    assert not problems
