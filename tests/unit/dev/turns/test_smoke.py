"""The two reads turns gives `shared/health` (T32): `work_status` for `holat?`, and `run_smoke_turn`, the CLI smoke
test after a `claude` update (SPEC §5.6, §6.2; NFR-REL-08). The smoke turn runs through the normal turn path in the
hidden `.smoke` project and tells the owner nothing; `shared/health` reports the outcome."""

import asyncio
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.factories import Fakes, make_decision, make_project, make_turn, send_outbox
from tests.fakes.agent import Pause, done_result
from tests.fakes.clock import DEFAULT_START
from yordamchi.core import aio
from yordamchi.core.errors import GitFailed
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.projects import service as projects
from yordamchi.dev.projects.domain import SMOKE_PROJECT_ID
from yordamchi.dev.turns import domain, service, store
from yordamchi.dev.turns.domain import TurnKind, TurnStatus
from yordamchi.dev.turns.service import TurnsState
from yordamchi.ports.git import GitStatus
from yordamchi.shared.health.domain import QueuedWork, SmokeOutcome, WorkStatus
from yordamchi.shared.outbox import store as outbox_store

NOW = datetime(2026, 10, 23, 9, 0, 7, tzinfo=UTC)
SMOKE_FILE = domain.smoke_file_name(DEFAULT_START)  # the runner starts at the fake clock's start
COMMITTED = GitStatus(branch="main", head="abc1234def", upstream=None, ahead=0, behind=0, changed=0, untracked=0)


# --- domain ---------------------------------------------------------------------------------------------------------
def test_work_status_lists_each_working_project_once_and_numbers_the_queue() -> None:
    status = domain.work_status(["BonviHR", "HRMentor", "BonviHR"], ["Wunderkind", "LC"], decisions=2, questions=1)
    assert status == WorkStatus(
        running=("BonviHR", "HRMentor"),
        queued=(QueuedWork("Wunderkind", 1), QueuedWork("LC", 2)),
        decisions=2,
        questions=1,
    )


def test_each_smoke_run_writes_a_file_of_its_own() -> None:
    assert domain.smoke_file_name(NOW) == "ok-20261023-090007.txt"


@pytest.mark.parametrize(
    ("status", "error_code", "committed", "file_ok", "outcome"),
    [
        (TurnStatus.DONE, None, True, True, SmokeOutcome(ok=True)),
        (TurnStatus.DONE, None, False, True, SmokeOutcome(ok=False, step="commit")),
        (TurnStatus.DONE, None, True, False, SmokeOutcome(ok=False, step="file")),
        (TurnStatus.RUNNING, None, False, False, SmokeOutcome(ok=False, step="timeout")),
        (TurnStatus.PAUSED_RUNAWAY, None, False, False, SmokeOutcome(ok=False, step="timeout")),
        (TurnStatus.FAILED, "auth_failed", False, False, SmokeOutcome(ok=False, step="auth_failed")),
        (TurnStatus.FAILED, None, False, False, SmokeOutcome(ok=False, step="failed")),
        (
            TurnStatus.INTERRUPTED,
            "credentials_refused",
            False,
            False,
            SmokeOutcome(ok=False, step="credentials_refused"),
        ),
        (TurnStatus.INTERRUPTED, None, False, False, SmokeOutcome(ok=False, step="start")),
        (TurnStatus.QUEUED, None, False, False, SmokeOutcome(ok=False, step="queued")),
        (TurnStatus.WAITING_LIMIT, None, False, False, SmokeOutcome(ok=False, step="waiting_limit")),
    ],
)
def test_the_smoke_verdict_passes_only_a_finished_committed_run_with_its_file(
    status: TurnStatus, error_code: str | None, committed: bool, file_ok: bool, outcome: SmokeOutcome
) -> None:
    assert domain.smoke_verdict(status, error_code=error_code, committed=committed, file_ok=file_ok) == outcome


# --- service --------------------------------------------------------------------------------------------------------
@pytest.fixture
async def state(dev_deps: DevDeps) -> AsyncIterator[TurnsState]:
    state = TurnsState()
    yield state
    await service.shutdown_turns(dev_deps, state)


async def until(check: Callable[[], object], *, timeout: float = 3.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while True:
        outcome = check()
        if asyncio.iscoroutine(outcome):
            outcome = await outcome
        if outcome:
            return
        if time.monotonic() > deadline:
            msg = f"{what} not reached"
            raise AssertionError(msg)
        await asyncio.sleep(0.005)


async def smoke_turn(deps: DevDeps) -> int | None:
    async with deps.db.read() as s:
        rows = await store.list_project_turns(s, SMOKE_PROJECT_ID, limit=5)
    return rows[0].id if rows else None


async def run_to_end(
    deps: DevDeps,
    fakes: Fakes,
    state: TurnsState,
    *,
    write: str | None,
    git: GitStatus | None,
    sleepers: int = 2,
) -> SmokeOutcome:
    """Run the smoke test as the health checks would, play the scheduler, and stand in for the agent's work."""
    root = deps.paths.session_dir("smoke")
    runner = aio.spawn("smoke_test", service.run_smoke_turn(deps, state, "2.1.310"))
    await until(lambda: smoke_turn(deps), what="smoke turn submitted")
    await service.schedule_turns(deps, state)
    # The runner's poll, plus the result grace once the agent answered (a still-working agent has none).
    await until(lambda: fakes.clock.pending_sleepers >= sleepers, what="the turn under way")
    if write is not None:
        (root / SMOKE_FILE).write_text(write, encoding="utf-8")
    if git is not None:
        fakes.git.statuses[root] = git
    for _ in range(200):
        if runner.done():
            break
        await fakes.clock.advance(domain.SMOKE_POLL_S)
        await asyncio.sleep(0.01)
    return await asyncio.wait_for(runner, timeout=3)


async def test_a_smoke_turn_that_writes_and_commits_its_file_passes_and_says_nothing(
    dev_deps: DevDeps, fakes: Fakes, state: TurnsState
) -> None:
    outcome = await run_to_end(dev_deps, fakes, state, write="ok\n", git=COMMITTED)

    assert outcome == SmokeOutcome(ok=True)
    root = dev_deps.paths.session_dir("smoke")
    assert ("init", root, ("main",)) in fakes.git.calls  # the scratch repo is made on first use
    [(spec, _resume)] = fakes.agents.opened
    assert (spec.kind, spec.model, spec.cwd, spec.strict_mcp) == ("smoke", "haiku", root, True)
    [(_session, sent, _mode)] = fakes.agents.sends
    assert SMOKE_FILE in sent.text
    turn_id = await smoke_turn(dev_deps)
    assert turn_id is not None
    async with dev_deps.db.read() as s:
        row = await store.get_turn(s, turn_id)
        queued = await outbox_store.list_outbox_messages(s, dev_deps.bot.value)
    assert row is not None
    assert (row.kind, row.status) == (TurnKind.SMOKE.value, TurnStatus.DONE.value)
    assert queued == []  # no progress line, no result: the owner hears only about a failure
    await send_outbox(dev_deps)
    assert fakes.messenger.requests == []


async def test_a_smoke_turn_that_did_not_commit_fails(dev_deps: DevDeps, fakes: Fakes, state: TurnsState) -> None:
    outcome = await run_to_end(dev_deps, fakes, state, write="ok", git=None)
    assert outcome == SmokeOutcome(ok=False, step="commit")


async def test_a_smoke_turn_without_its_file_fails(dev_deps: DevDeps, fakes: Fakes, state: TurnsState) -> None:
    outcome = await run_to_end(dev_deps, fakes, state, write="not ok", git=COMMITTED)
    assert outcome == SmokeOutcome(ok=False, step="file")


async def test_a_smoke_turn_still_working_at_the_deadline_is_stopped_and_fails(
    dev_deps: DevDeps, fakes: Fakes, state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause(), done_result())
    outcome = await run_to_end(dev_deps, fakes, state, write=None, git=None, sleepers=1)

    assert outcome == SmokeOutcome(ok=False, step="timeout")
    assert fakes.agents.interrupts  # the stop reached the executor
    async with dev_deps.db.read() as s:
        queued = await outbox_store.list_outbox_messages(s, dev_deps.bot.value)
    assert queued == []  # not even "to'xtatdim"


async def test_a_scratch_repo_that_cannot_be_made_fails_without_a_turn(
    dev_deps: DevDeps, fakes: Fakes, state: TurnsState
) -> None:
    fakes.git.failures["init"] = GitFailed(operation="init", reason="failed")
    assert await service.run_smoke_turn(dev_deps, state, "2.1.310") == SmokeOutcome(ok=False, step="repo")
    assert await smoke_turn(dev_deps) is None


async def test_work_status_reads_turns_and_decisions_and_leaves_the_smoke_project_out(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    at = fakes.clock.now()
    async with dev_deps.db.uow() as s:
        for name in ("BonviHR", "HRMentor", "Wunderkind", "LC"):
            await make_project(s, name, at=at, path=Path("/projects") / name)
        await make_turn(s, "BonviHR", at=at, status=TurnStatus.RUNNING, delivery_status="delivered")
        await make_turn(s, "HRMentor", at=at, status=TurnStatus.WAITING_QUESTION, delivery_status="delivered")
        await make_turn(s, "Wunderkind", at=at)
        await make_turn(s, "LC", at=at)
        await make_decision(s, at=at, project="BonviHR")
    await projects.ensure_smoke_project(dev_deps)
    async with dev_deps.db.uow() as s:
        await make_turn(s, SMOKE_PROJECT_ID, at=at, status=TurnStatus.RUNNING, kind=TurnKind.SMOKE)

    assert await service.work_status(dev_deps) == WorkStatus(
        running=("BonviHR",),
        queued=(QueuedWork("Wunderkind", 1), QueuedWork("LC", 2)),
        decisions=1,
        questions=1,
    )


async def test_the_restart_notice_never_lists_the_smoke_project(dev_deps: DevDeps, fakes: Fakes) -> None:
    await projects.ensure_smoke_project(dev_deps)
    at = fakes.clock.now()
    async with dev_deps.db.uow() as s:
        await make_project(s, "BonviHR", at=at, path=Path("/projects/BonviHR"))
        await make_turn(s, "BonviHR", at=at, status=TurnStatus.RUNNING, delivery_status="delivered")
        await make_turn(
            s, SMOKE_PROJECT_ID, at=at, status=TurnStatus.RUNNING, kind=TurnKind.SMOKE, delivery_status="sending"
        )

    recovered = await service.recover_turns(dev_deps)

    assert recovered.interrupted == ("BonviHR",)
    assert recovered.uncertain == ()
    async with dev_deps.db.read() as s:
        queued = await outbox_store.list_outbox_messages(s, dev_deps.bot.value)
    assert queued == []  # no "bajarilmay qoldi" about the smoke turn
