"""turns.service: the gate policy and the wall limit belong to the turn, not to the session (SPEC §3.2.8 steps 4 and
6, §5.7; UC-14). A new project's plan (PLAN_ONLY) and its build (FULL) run in one coding session; the plan's wall
limit is `newproject.plan_max_min`."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest

from tests.factories import Fakes, make_project, make_project_folder, make_turn_request, projects_root_of
from tests.fakes.agent import Pause, ToolStep, done_result
from yordamchi.core import strings as S
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate.domain import Policy
from yordamchi.dev.turns import service, store
from yordamchi.dev.turns.domain import TurnKind, TurnStatus
from yordamchi.dev.turns.service import TurnsState
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.settings import defaults

PROJECT = "KitobBot"
GRACE = float(defaults.TURNS_RESULT_GRACE_S.default)  # type: ignore[arg-type]


@pytest.fixture
async def folder(dev_deps: DevDeps, fakes: Fakes, paths: Paths) -> Path:
    path = make_project_folder(projects_root_of(paths), PROJECT, git_dir=True)
    async with dev_deps.db.uow() as s:
        await make_project(s, PROJECT, at=fakes.clock.now(), path=path)
    return path


@pytest.fixture
async def state(dev_deps: DevDeps) -> AsyncIterator[TurnsState]:
    state = TurnsState()
    yield state
    await service.shutdown_turns(dev_deps, state)


async def until(check: Callable[[], object], *, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        outcome = check()
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if outcome:
            return
        if time.monotonic() > deadline:
            msg = "condition not reached"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


async def status_of(deps: DevDeps, turn_id: int) -> str:
    async with deps.db.read() as s:
        row = await store.get_turn(s, turn_id)
    assert row is not None
    return row.status


async def run(deps: DevDeps, fakes: Fakes, state: TurnsState, kind: TurnKind, *, wait_done: bool = True) -> int:
    submitted = await service.submit_turn(deps, state, make_turn_request(PROJECT, "ish", kind=kind))
    await service.schedule_turns(deps, state)
    if wait_done:
        await until(lambda: fakes.clock.pending_sleepers > 0)
        await fakes.clock.advance(GRACE)
        await until(lambda: _is(deps, submitted.turn_id, TurnStatus.DONE))
    return submitted.turn_id


async def _is(deps: DevDeps, turn_id: int, status: TurnStatus) -> bool:
    return await status_of(deps, turn_id) == status.value


async def test_each_turn_runs_under_its_own_policy_in_the_shared_session(
    dev_deps: DevDeps, fakes: Fakes, folder: Path, state: TurnsState
) -> None:
    code = {"file_path": str(folder / "src" / "app.py"), "content": "x"}
    fakes.agents.script_turn(ToolStep("Write", code), done_result())
    await run(dev_deps, fakes, state, TurnKind.PLAN)
    fakes.agents.script_turn(ToolStep("Write", code), done_result())
    await run(dev_deps, fakes, state, TurnKind.BUILD)
    fakes.agents.script_turn(ToolStep("Write", code), done_result())
    await run(dev_deps, fakes, state, TurnKind.PLAN)

    verdicts = [(verdict.allow, verdict.reason_code or None) for _, verdict in fakes.agents.guard_calls]
    assert verdicts == [(False, "plan_only_policy"), (True, None), (False, "plan_only_policy")]
    assert len(fakes.agents.opened) == 1  # one live session took all three turns
    live = state.live_of(PROJECT, "coding")
    assert live is not None
    assert live.policy is Policy.PLAN_ONLY  # the latest turn's policy


async def test_a_plan_turn_pauses_at_the_new_project_plan_limit(
    dev_deps: DevDeps, fakes: Fakes, folder: Path, state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause())
    turn_id = await run(dev_deps, fakes, state, TurnKind.PLAN, wait_done=False)
    await until(lambda: _is(dev_deps, turn_id, TurnStatus.RUNNING))
    await fakes.clock.advance(14 * 60 + 1)
    await service.schedule_turns(dev_deps, state)
    await until(lambda: _is(dev_deps, turn_id, TurnStatus.PAUSED_RUNAWAY))
    async with dev_deps.db.read() as s:
        sent = [row.payload_json for row in await outbox_store.list_outbox_messages(s, dev_deps.bot.value)]
    expected = S.TURN_RUNAWAY(project=PROJECT, why=S.RUNAWAY_WALL(minutes=14))
    assert any(isinstance(payload, dict) and payload.get("text") == expected for payload in sent)


async def test_a_coding_turn_keeps_the_general_wall_limit(
    dev_deps: DevDeps, fakes: Fakes, folder: Path, state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause())
    turn_id = await run(dev_deps, fakes, state, TurnKind.BUILD, wait_done=False)
    await until(lambda: _is(dev_deps, turn_id, TurnStatus.RUNNING))
    await fakes.clock.advance(14 * 60 + 1)
    await service.schedule_turns(dev_deps, state)
    assert await status_of(dev_deps, turn_id) == TurnStatus.RUNNING.value
