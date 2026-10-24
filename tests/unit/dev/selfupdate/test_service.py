"""selfupdate.service on a real SQLite file, `FakeGit` and `FakeClock` (SPEC §3.2.12 bot side; UC-19, Q5): the branch
and the turn, `request.json` for the supervisor, the one protected-core question, R-32, following `status.json` to
one outcome message, and the supervisor self-switch watch. Nothing runs git, launchd or the supervisor."""

import json
import stat
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest

from tests.factories import (
    SELF_BASE,
    SELF_HEAD,
    Fakes,
    list_self_updates,
    make_owner_origin,
    make_self_project,
    write_update_status,
)
from yordamchi.core import strings as S
from yordamchi.core.errors import SelfUpdateBusy, SelfUpdateDirty, SelfUpdateNotOnMain
from yordamchi.core.json_value import JsonValue
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.service import SELFUPDATE_KIND, DecisionReply
from yordamchi.dev.selfupdate import service
from yordamchi.dev.selfupdate.domain import UpdateStatus
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.domain import TurnKind
from yordamchi.dev.turns.service import TurnsState
from yordamchi.ports.git import FileChange
from yordamchi.shared.outbox import store as outbox_store

REQUEST = "O'zingga ovoz sozlamasini qo'sh"
APP_FILE = "src/yordamchi/dev/servers/domain.py"
CORE_FILE = "src/yordamchi/dev/gate/domain.py"
DEFAULTS = "src/yordamchi/shared/settings/defaults.py"


@pytest.fixture
async def state(dev_deps: DevDeps) -> AsyncIterator[TurnsState]:
    state = TurnsState()
    yield state
    await turns.shutdown_turns(dev_deps, state)


@pytest.fixture
async def repo(dev_deps: DevDeps, fakes: Fakes) -> Path:
    return await make_self_project(dev_deps, fakes)


async def texts(deps: DevDeps) -> list[str]:
    async with deps.db.read() as s:
        rows = await outbox_store.list_outbox_messages(s, deps.bot.value)
    return [str(row.payload_json.get("text", "")) for row in rows if isinstance(row.payload_json, dict)]


async def finish_turn(deps: DevDeps, fakes: Fakes, repo: Path, turn_id: int, *files: str, status: str = "done") -> None:
    """The coding turn ended with commits on its branch that change `files`."""
    async with deps.db.uow() as s:
        await turns_store.set_turn(s, turn_id, status=status, natija="Ovoz sozlamasi qo'shildi.")
    fakes.git.heads[repo] = SELF_HEAD
    fakes.git.changes[repo] = [FileChange("M", path) for path in files]


async def started(deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState) -> service.Started:
    begun = await service.request_self_update(deps, state, text=REQUEST)
    fakes.git.branches[repo] = begun.branch
    return begun


def request_of(deps: DevDeps, update_id: str) -> dict[str, object]:
    raw = json.loads((deps.paths.updates / update_id / "request.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


# --- request (bot step 1) -------------------------------------------------------------------------------------------
async def test_a_request_cuts_a_branch_from_main_and_queues_a_selfupdate_turn(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await service.request_self_update(dev_deps, state, text=REQUEST)

    assert begun.branch == "selfupdate/1-ozingga-ovoz-sozlamasini-qosh"
    assert ("create_branch", repo, (begun.branch,)) in fakes.git.calls
    async with dev_deps.db.read() as s:
        row = await turns_store.get_turn(s, begun.turn_id)
    assert row is not None
    assert (row.project_id, row.kind) == ("Yordamchi", TurnKind.SELFUPDATE.value)
    (update,) = await list_self_updates(dev_deps.db)
    assert (update.status, update.base_commit, update.turn_id) == ("working", SELF_BASE, begun.turn_id)
    assert begun.update_id == "u000001"
    assert any(text.startswith("Yordamchi: o'zimni yangilashni boshladim") for text in await texts(dev_deps))


@pytest.mark.parametrize(
    ("branch", "changed", "error"),
    [("main", 2, SelfUpdateDirty), ("selfupdate/3-eski", 0, SelfUpdateNotOnMain)],
)
async def test_a_dirty_or_off_main_working_copy_is_refused(
    dev_deps: DevDeps, fakes: Fakes, state: TurnsState, branch: str, changed: int, error: type[Exception]
) -> None:
    await make_self_project(dev_deps, fakes, branch=branch, changed=changed)
    with pytest.raises(error):
        await service.request_self_update(dev_deps, state, text=REQUEST)
    assert await list_self_updates(dev_deps.db) == []


async def test_only_one_self_update_at_a_time(dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState) -> None:
    await service.request_self_update(dev_deps, state, text=REQUEST)
    with pytest.raises(SelfUpdateBusy):
        await service.request_self_update(dev_deps, state, text="yana bir narsa")


# --- the turn ends (bot steps 2–3) ----------------------------------------------------------------------------------
async def test_an_ordinary_change_writes_the_request_for_the_supervisor(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    assert await service.advance_self_updates(dev_deps) == 0  # the turn is still working
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, APP_FILE, "tests/unit/dev/servers/test_domain.py")

    assert await service.advance_self_updates(dev_deps) == 1

    path = dev_deps.paths.updates / begun.update_id / "request.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    body = request_of(dev_deps, begun.update_id)
    assert body["id"] == begun.update_id
    assert (body["kind"], body["commit"], body["branch"]) == ("app", SELF_HEAD, begun.branch)
    assert body["touches_core"] is False
    assert (body["health_timeout_s"], body["drain_max_min"]) == (120, 30)
    assert body["summary"] == "Ovoz sozlamasi qo'shildi."
    (update,) = await list_self_updates(dev_deps.db)
    assert update.status == UpdateStatus.REQUESTED.value
    assert any("o'zgarishlar tayyor (2 ta fayl)" in text for text in await texts(dev_deps))


async def test_a_protected_core_change_asks_exactly_once_and_ha_applies_it(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, APP_FILE, CORE_FILE)

    await service.advance_self_updates(dev_deps)
    await service.advance_self_updates(dev_deps)
    await service.advance_self_updates(dev_deps)

    questions = [r for r in fakes.messenger.sent() if "himoya qatlamiga tegadi" in str(r.payload.get("text"))]
    assert len(questions) == 1
    assert CORE_FILE in str(questions[0].payload["text"])
    assert "Ovoz sozlamasi qo'shildi." in str(questions[0].payload["text"])
    (update,) = await list_self_updates(dev_deps.db)
    assert update.status == UpdateStatus.AWAITING_CORE.value
    assert not (dev_deps.paths.updates / begun.update_id / "request.json").exists()

    answer = await gate.answer_decision(dev_deps, make_owner_origin(update_id=77), DecisionReply("ha"))
    assert answer.delivery == ""  # no agent waits on it
    await service.advance_self_updates(dev_deps)

    body = request_of(dev_deps, begun.update_id)
    assert (body["touches_core"], body["decision_id"]) == (True, update.decision_id)
    async with dev_deps.db.read() as s:
        decision = await gate_store.get_decision(s, update.decision_id or 0)
    assert decision is not None
    assert (decision.kind, decision.status) == (SELFUPDATE_KIND, "consumed")
    await service.advance_self_updates(dev_deps)
    asked = [r for r in fakes.messenger.sent() if "himoya qatlamiga tegadi" in str(r.payload.get("text"))]
    assert len(asked) == 1


async def test_yoq_to_a_core_change_refuses_it_and_delivers_nothing(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, CORE_FILE)
    await service.advance_self_updates(dev_deps)

    answer = await gate.answer_decision(dev_deps, make_owner_origin(update_id=78), DecisionReply("yo'q"))
    await service.advance_self_updates(dev_deps)

    assert answer.delivery == ""
    assert answer.reply is not None
    assert answer.reply.startswith("Yordamchi: yangilanishni qo'llamayman")
    (update,) = await list_self_updates(dev_deps.db)
    assert (update.status, update.result_code) == ("refused", "core_declined")
    assert not (dev_deps.paths.updates / begun.update_id / "request.json").exists()


async def test_a_changed_safeguard_default_asks_once(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    before = 'X = Setting("gate.secret_scan_enabled", bool, True, weakens=disables)\n'
    after = 'X = Setting("gate.secret_scan_enabled", bool, False, weakens=disables)\n'
    fakes.git.trees[(repo, SELF_BASE, "src/yordamchi/shared/settings")] = {"defaults.py": before.encode()}
    fakes.git.trees[(repo, SELF_HEAD, "src/yordamchi/shared/settings")] = {"defaults.py": after.encode()}
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, DEFAULTS)

    await service.advance_self_updates(dev_deps)

    (update,) = await list_self_updates(dev_deps.db)
    assert update.status == UpdateStatus.AWAITING_CORE.value
    (question,) = [r for r in fakes.messenger.sent() if "himoya qatlamiga tegadi" in str(r.payload.get("text"))]
    assert "sozlama gate.secret_scan_enabled" in str(question.payload["text"])


async def test_without_a_readable_protected_core_every_change_asks(
    dev_deps: DevDeps, fakes: Fakes, state: TurnsState
) -> None:
    repo = await make_self_project(dev_deps, fakes, protected=None)
    begun = await started(dev_deps, fakes, repo, state)
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, "README.md")
    await service.advance_self_updates(dev_deps)
    (update,) = await list_self_updates(dev_deps.db)
    assert update.status == UpdateStatus.AWAITING_CORE.value


async def test_the_supervisor_and_the_app_together_are_refused(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, APP_FILE, "supervisor/src/yordamchi_supervisor/main.py")

    await service.advance_self_updates(dev_deps)

    (update,) = await list_self_updates(dev_deps.db)
    assert (update.status, update.result_code) == ("refused", "split_needed")
    assert not (dev_deps.paths.updates / begun.update_id / "request.json").exists()
    assert any("ham supervisor'ga, ham botlarga tegadi" in text for text in await texts(dev_deps))


async def test_a_supervisor_only_change_is_a_supervisor_request_and_asks_once(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, "supervisor/src/yordamchi_supervisor/updater.py")
    await service.advance_self_updates(dev_deps)
    await gate.answer_decision(dev_deps, make_owner_origin(update_id=79), DecisionReply("ha"))
    await service.advance_self_updates(dev_deps)
    assert request_of(dev_deps, begun.update_id)["kind"] == "supervisor"


async def test_a_turn_without_commits_or_that_failed_applies_nothing(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, status="failed")
    await service.advance_self_updates(dev_deps)
    (update,) = await list_self_updates(dev_deps.db)
    assert (update.status, update.result_code) == ("failed", "turn_failed")
    assert any("yangilanish ishi oxiriga yetmadi" in text for text in await texts(dev_deps))


# --- follow the supervisor ----------------------------------------------------------------------------------------
async def requested(deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState) -> service.Started:
    begun = await started(deps, fakes, repo, state)
    await finish_turn(deps, fakes, repo, begun.turn_id, APP_FILE)
    await service.advance_self_updates(deps)
    return begun


async def test_the_final_outcome_is_told_once_with_version_tests_and_health(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await requested(dev_deps, fakes, repo, state)
    write_update_status(dev_deps.paths, begun.update_id, "probation")
    await service.advance_self_updates(dev_deps)
    (update,) = await list_self_updates(dev_deps.db)
    assert update.status == "probation"

    tests: dict[str, JsonValue] = {"app": 1301, "supervisor": 141}
    previous: dict[str, JsonValue] = {"app": 1290, "supervisor": 141}
    write_update_status(
        dev_deps.paths,
        begun.update_id,
        "merged",
        final=True,
        version="2026.10.25-bbbbbbb",
        tests=tests,
        previous_tests=previous,
    )
    await service.advance_self_updates(dev_deps)
    await service.advance_self_updates(dev_deps)

    done = [text for text in await texts(dev_deps) if text.startswith("Yordamchi: yangilandim")]
    assert done == [
        S.SELFUPDATE_DONE(
            project="Yordamchi",
            version="2026.10.25-bbbbbbb",
            changes="Ovoz sozlamasi qo'shildi.",
            tests="1301 ta (oldin 1290)",
        )
    ]
    (update,) = await list_self_updates(dev_deps.db)
    assert (update.status, update.version, update.tests_count) == ("merged", "2026.10.25-bbbbbbb", 1301)
    assert update.reported_at is not None


async def test_a_rollback_the_supervisor_already_alerted_is_not_told_twice(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await requested(dev_deps, fakes, repo, state)
    before = len(await texts(dev_deps))
    write_update_status(
        dev_deps.paths, begun.update_id, "rolled_back", final=True, alerted=True, result_code="crash_loop"
    )
    await service.advance_self_updates(dev_deps)
    assert len(await texts(dev_deps)) == before
    (update,) = await list_self_updates(dev_deps.db)
    assert (update.status, update.result_code) == ("rolled_back", "crash_loop")


async def test_a_refusal_names_the_reason_in_plain_uzbek(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await requested(dev_deps, fakes, repo, state)
    write_update_status(
        dev_deps.paths,
        begun.update_id,
        "refused",
        final=True,
        result_code="tests_failed",
        detail="tests/unit/x.py::test_y",
    )
    await service.advance_self_updates(dev_deps)
    assert S.SELFUPDATE_REFUSED(project="Yordamchi", reason="testlar o'tmadi: tests/unit/x.py::test_y") in (
        await texts(dev_deps)
    )


async def test_a_new_supervisor_without_a_heartbeat_is_switched_back_once(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, "supervisor/src/yordamchi_supervisor/updater.py")
    await service.advance_self_updates(dev_deps)
    await gate.answer_decision(dev_deps, make_owner_origin(update_id=80), DecisionReply("ha"))
    await service.advance_self_updates(dev_deps)
    releases = dev_deps.paths.supervisor / "releases"
    (releases / "2026.10.24-aaaaaaa").mkdir(parents=True)
    (releases / "2026.10.25-bbbbbbb").mkdir()
    link = dev_deps.paths.supervisor / "current"
    link.symlink_to(releases / "2026.10.25-bbbbbbb")
    switched = fakes.clock.now() - timedelta(seconds=61)
    write_update_status(
        dev_deps.paths,
        begun.update_id,
        "switching",
        version="2026.10.25-bbbbbbb",
        previous="2026.10.24-aaaaaaa",
        switched_at=switched.isoformat(),
    )
    kicks: list[int] = []

    async def kick() -> bool:
        kicks.append(1)
        return True

    await service.advance_self_updates(dev_deps, kick_supervisor=kick)
    await service.advance_self_updates(dev_deps, kick_supervisor=kick)

    assert link.readlink().name == "2026.10.24-aaaaaaa"
    assert kicks == [1]
    assert sum("yangi supervisor" in text for text in await texts(dev_deps)) == 1


async def test_a_new_supervisor_that_beats_in_time_is_left_alone(
    dev_deps: DevDeps, fakes: Fakes, repo: Path, state: TurnsState
) -> None:
    begun = await started(dev_deps, fakes, repo, state)
    await finish_turn(dev_deps, fakes, repo, begun.turn_id, "supervisor/src/yordamchi_supervisor/updater.py")
    await service.advance_self_updates(dev_deps)
    await gate.answer_decision(dev_deps, make_owner_origin(update_id=81), DecisionReply("ha"))
    await service.advance_self_updates(dev_deps)
    switched = fakes.clock.now() - timedelta(seconds=90)
    write_update_status(
        dev_deps.paths,
        begun.update_id,
        "switching",
        version="2026.10.25-bbbbbbb",
        previous="2026.10.24-aaaaaaa",
        switched_at=switched.isoformat(),
    )
    beat = {"release": "2026.10.25-bbbbbbb", "last_tick_at": fakes.clock.now().isoformat()}
    dev_deps.paths.run.mkdir(parents=True, exist_ok=True)
    (dev_deps.paths.run / "supervisor.heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")
    kicks: list[int] = []

    async def kick() -> bool:
        kicks.append(1)
        return True

    await service.advance_self_updates(dev_deps, kick_supervisor=kick)
    assert kicks == []
