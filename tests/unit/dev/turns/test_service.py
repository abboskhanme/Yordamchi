"""turns.service on a real SQLite file, `FakeAgentRuntime` scripts and `FakeClock` (SPEC §3.2.1, §3.2.2, §3.2.5,
§3.2.6, §2.6, §2.7, §5.1–5.6; UC-02, UC-03, UC-10 F, UC-11, NFR-CAP-01, NFR-COST-03, NFR-REL-05/07, NFR-SEC-11).

Every turn state is reached through the executor's events, as the Claude adapter emits them: a `Pause` in a script
is a turn still working, so a follow-up merges into it and a stop interrupts it.
"""

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    Fakes,
    add_setting_row,
    make_burst_row,
    make_closed_burst,
    make_decision,
    make_owner_origin,
    make_part,
    make_project,
    make_project_folder,
    make_turn,
    make_turn_request,
    make_voice_part,
    make_window_settings,
    projects_root_of,
    routing_reply,
    send_outbox,
    write_transcript,
)
from tests.fakes.agent import FakeAgentRuntime, Pause, ToolStep, done_result
from tests.fakes.mac_host import GIB, FakeMacHost
from tests.owner_text import shown
from yordamchi.core import strings as S
from yordamchi.core.errors import CallTimedOut, GitFailed, ProjectNotFound, TurnNotFound
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.domain import Policy
from yordamchi.dev.gate.service import AnswerKind, DecisionAnswer
from yordamchi.dev.turns import domain, service, store
from yordamchi.dev.turns.domain import DeliveryMode, PendingQuestion, QueueReason, TurnKind, TurnStatus
from yordamchi.dev.turns.models import AgentSession, Delivery, Turn
from yordamchi.dev.turns.service import RevertOutcome, RevertReply, TurnsState
from yordamchi.ports.agent import (
    AgentInput,
    ModelUsage,
    Progress,
    QuestionAsked,
    RuntimeFailed,
    SessionClosed,
    SessionHandle,
    ToolCall,
    TurnResult,
    UsageLimited,
)
from yordamchi.ports.agent import DeliveryMode as DeliveryModeLiteral
from yordamchi.ports.git import Commit, FileChange, GitStatus
from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState
from yordamchi.shared.burst.domain import CloseReason, RouteHint, TurnInput, decide_dev_follow_up, turn_input
from yordamchi.shared.cost import store as cost_store
from yordamchi.shared.outbox import service as outbox_service
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.models import OutboxMessage

PROJECT = "BonviHR"
OTHER = "HRMentor"
START_HEAD = "0000000aaaa"
GRACE = 1.5  # turns.result_grace_s
FINAL = (
    "Tayyor.\nNatija: Login sahifasidagi xato tuzatildi.\nTestlar: o'tdi (42 passed)\n"
    "<handoff>\nGoal: fix login\nDecisions: none\nState: pushed\nNext: nothing\n</handoff>"
)


def result(text: str = FINAL, **changes: object) -> TurnResult:
    base = done_result(text)
    values = {
        "text": base.text,
        "subtype": base.subtype,
        "is_error": base.is_error,
        "stop_reason": base.stop_reason,
        "cost_usd_micros": 12_345,
        "model_usage": {"claude-opus": ModelUsage(input_tokens=100, output_tokens=40)},
        "num_turns": 3,
        "duration_ms": 9_000,
    }
    values.update(changes)
    return TurnResult(**values)  # type: ignore[arg-type]


# --- fixtures and helpers -------------------------------------------------------------------------------------------
@pytest.fixture
def root(paths: Paths) -> Path:
    return projects_root_of(paths)


@pytest.fixture
async def folders(dev_deps: DevDeps, fakes: Fakes, root: Path) -> dict[str, Path]:
    """Two registered projects whose git starts clean at `START_HEAD`, pushing to GitHub."""
    found: dict[str, Path] = {}
    for name in (PROJECT, OTHER):
        folder = make_project_folder(root, name, git_dir=True)
        async with dev_deps.db.uow() as s:
            await make_project(s, name, at=fakes.clock.now(), path=folder)
        fakes.git.heads[folder] = START_HEAD
        fakes.git.remotes[(folder, "origin")] = f"git@github.com:abboskhanme/{name}.git"
        found[name] = folder
    return found


@pytest.fixture
async def state(dev_deps: DevDeps) -> AsyncIterator[TurnsState]:
    state = TurnsState()
    yield state
    await service.shutdown_turns(dev_deps, state)


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
        row = await store.get_turn(s, turn_id)
    assert row is not None
    return row


def status_is(deps: DevDeps, turn_id: int, status: TurnStatus) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await turn(deps, turn_id)).status == status.value

    return check


async def deliveries(deps: DevDeps, turn_id: int) -> list[Delivery]:
    async with deps.db.read() as s:
        return await store.list_deliveries(s, turn_id)


async def sessions(deps: DevDeps) -> list[AgentSession]:
    async with deps.db.read() as s:
        return await store.list_current_sessions(s, statuses=[status.value for status in domain.SessionStatus])


async def messages(deps: DevDeps) -> list[OutboxMessage]:
    async with deps.db.read() as s:
        return await outbox_store.list_outbox_messages(s, deps.bot.value)


def text_of(row: OutboxMessage) -> str:
    payload = row.payload_json
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


async def texts(deps: DevDeps, kind: str | None = None) -> list[str]:
    return [text_of(row) for row in await messages(deps) if kind is None or row.kind == kind]


async def start(deps: DevDeps, state: TurnsState, request: domain.TurnRequest | None = None) -> int:
    """Submit, run a scheduler pass, and wait until the turn started and its input reached the executor."""
    submitted = await service.submit_turn(deps, state, request or make_turn_request())
    await service.schedule_turns(deps, state)

    async def delivered() -> bool:
        row = await turn(deps, submitted.turn_id)
        sent = all(item.status == "delivered" for item in await deliveries(deps, submitted.turn_id))
        return row.status not in {"queued", "starting"} and sent

    await until(delivered, what="turn started and delivered")
    return submitted.turn_id


async def finish(deps: DevDeps, fakes: Fakes, turn_id: int, *, to: TurnStatus = TurnStatus.DONE) -> Turn:
    """The result came: after `turns.result_grace_s` of silence the turn completes."""
    await until(lambda: fakes.clock.pending_sleepers > 0, what="result grace armed")
    await fakes.clock.advance(GRACE)
    await until(status_is(deps, turn_id, to), what=f"turn {to.value}")
    return await turn(deps, turn_id)


def session_of(fakes: Fakes, index: int = -1) -> str:
    return fakes.agents.sends[index][0]


# --- a turn from submit to result (UC-11, NFR-SEC-11, SPEC §6.2) ----------------------------------------------------
async def test_a_turn_runs_and_ends_with_the_golden_result_committed_and_pushed_without_a_question(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    folder = folders[PROJECT]
    fakes.agents.script_turn(
        ToolStep("Bash", {"command": "git add -A && git commit -m 'fix login'"}, tool_use_id="t1"),
        ToolStep("Bash", {"command": "git push origin main"}, tool_use_id="t2"),
        result(),
    )
    turn_id = await start(dev_deps, state)
    await until(lambda: fakes.clock.pending_sleepers > 0, what="result")
    await send_outbox(dev_deps)  # the progress line goes out; the result replies to it
    fakes.git.statuses[folder] = GitStatus("main", "a1b2c3d4e5", "origin/main", 0, 0, 0, 0)
    fakes.git.logs[folder] = [Commit("a1b2c3d4e5", "fix login", fakes.clock.now())]
    fakes.git.changes[folder] = [FileChange("M", "src/app/login.py"), FileChange("M", "tests/test_login.py")]

    row = await finish(dev_deps, fakes, turn_id)

    assert [verdict.allow for _, verdict in fakes.agents.guard_calls] == [True, True]
    assert await gate.pending_items(dev_deps) == ()  # NFR-SEC-11: commit and push asked nothing
    kinds = [message.kind for message in await messages(dev_deps)]
    assert "decision" not in kinds
    assert "question" not in kinds
    results = await texts(dev_deps, "result")
    assert [shown(text).splitlines() for text in results] == [
        [
            "BonviHR · Natija: Login sahifasidagi xato tuzatildi.",
            "Fayllar: 2 ta (login.py, test_login.py)",
            "Testlar: o'tdi (42 passed)",
            "Commit: a1b2c3d · push: bajarildi (origin/main)",
            "Vaqt: 0 daq",
        ]
    ]
    result_row = next(message for message in await messages(dev_deps) if message.kind == "result")
    assert result_row.reply_to_message_id is not None  # threads under the project's progress message
    assert row.natija == "Login sahifasidagi xato tuzatildi."
    assert row.tests_status == "passed"
    assert row.pushed is True
    assert row.commits_json == ["a1b2c3d4e5"]
    assert row.start_head == START_HEAD
    assert row.steps == 2
    assert row.cost_usd_micros == 12_345
    assert [item.status for item in await deliveries(dev_deps, turn_id)] == ["delivered"]
    note = await service.latest_handoff(dev_deps, PROJECT)
    assert note is not None
    assert note.block is not None
    assert note.block.startswith("Goal: fix login")
    assert note.files == ("src/app/login.py", "tests/test_login.py")
    async with dev_deps.db.read() as s:
        usage = await cost_store.list_usage_records(s)
    assert [(item.source, item.turn_id, item.cost_usd_micros, item.is_paid_api) for item in usage] == [
        ("turn", turn_id, 12_345, False)
    ]


async def test_every_project_message_starts_with_the_project_name(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), result())
    turn_id = await start(dev_deps, state)
    await finish(dev_deps, fakes, turn_id)
    assert all(shown(text).startswith(PROJECT) for text in await texts(dev_deps))  # as the owner reads it


async def test_progress_is_one_message_per_turn_edited_at_most_every_five_seconds(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    steps = [Progress(kind="tool_start", tool_name="Bash", summary=f"step {n}") for n in range(4)]
    fakes.agents.script_turn(*steps, Pause())
    await start(dev_deps, state)

    async def four_steps() -> bool:
        return any("4 qadam" in text for text in await texts(dev_deps, "progress"))

    await until(four_steps, what="progress at 4 steps")
    progress = [message for message in await messages(dev_deps) if message.kind == "progress"]
    sends = [message for message in progress if message.op == "send_text"]
    assert len(sends) == 1
    assert sends[0].dedupe_key == "progress:1:0"
    assert shown(text_of(sends[0])).startswith("BonviHR · ishlayapti 0 daq · 4 qadam")
    assert "• Bash: step 3" in text_of(sends[0])
    edits = [message for message in progress if message.op == "edit_text" and message.status == "pending"]
    assert len(edits) <= 1  # older pending edits are superseded (coalesced)


async def test_the_session_follows_the_coding_options_of_spec_5_3(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState, paths: Paths
) -> None:
    fakes.agents.script_turn(Pause())
    await start(dev_deps, state)
    spec, resume_id = fakes.agents.opened[0]
    assert resume_id is None
    assert spec.kind == "coding"
    assert spec.cwd == folders[PROJECT]
    assert spec.setting_sources == ("user", "project", "local")
    assert spec.permission_mode == "bypassPermissions"
    assert spec.tools is None
    assert spec.model == "opus"
    assert spec.effort == "xhigh"
    assert spec.max_turns == 200
    assert spec.hook_timeout_s == 15.0
    assert spec.question_hold_s == 30 * 60
    assert spec.question_parked_message.startswith("[Yordamchi] The owner has not answered yet")
    assert set(spec.env) == {"YORDAMCHI_SOCK", "YORDAMCHI_SESSION_TOKEN", "YORDAMCHI_PY"}
    assert spec.env["YORDAMCHI_SOCK"] == str(paths.socket(dev_deps.bot))
    assert spec.env["YORDAMCHI_PY"] == str(paths.current_release / ".venv" / "bin" / "python")
    assert f"Read(/{paths.data}/**)" in spec.disallowed_tools
    # SPEC §5.4: the frozen prompt holds only the fixed parts; NFR-SEC-11 overrides "push — ask first".
    assert "commit, push, branches and creating private GitHub repos" in spec.system_append
    assert "git push — ask first" in spec.system_append
    assert "Never commit `.env`" in spec.system_append
    assert "Natija:" in spec.system_append
    assert "qisqa, aniq, texnik" not in spec.system_append  # the tone is an owner instruction: per turn only
    assert spec.context_provider is not None
    context = spec.context_provider()
    assert context.startswith("Current instructions from the owner:")
    assert "qisqa, aniq, texnik" in context
    assert "Uzbek (Latin script)" in context


async def test_the_gate_guard_scopes_the_session_and_the_turn(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState, paths: Paths
) -> None:
    fakes.agents.script_turn(Pause())
    turn_id = await start(dev_deps, state)
    token = fakes.agents.opened[0][0].env["YORDAMCHI_SESSION_TOKEN"]
    scope = state.scope_for(token)
    assert scope is not None
    assert scope.project_id == PROJECT
    assert scope.turn_id == turn_id
    assert scope.policy is Policy.FULL
    assert scope.dirty_before_turn is False
    assert scope.protected_pids
    assert state.scope_for("unknown") is None
    spec = fakes.agents.opened[0][0]
    denied = await spec.guard(ToolCall("Bash", {"command": f"cat {paths.config}/master.key"}, "t", None, "s"))
    assert not denied.allow


async def test_a_selfupdate_turn_in_a_reused_coding_session_runs_under_the_self_update_policy(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    """UC-19: the policy is the turn's. A `selfupdate` turn after an instruction in the same coding session must
    still forbid pushing main; the instruction after it is FULL again."""
    fakes.agents.script_turn(result())
    first = await start(dev_deps, state)
    await finish(dev_deps, fakes, first)
    token = fakes.agents.opened[0][0].env["YORDAMCHI_SESSION_TOKEN"]
    fakes.agents.script_turn(Pause())
    await start(dev_deps, state, make_turn_request(PROJECT, "O'zingga yangi buyruq qo'sh", kind=TurnKind.SELFUPDATE))
    assert len(fakes.agents.opened) == 1  # the same session
    scope = state.scope_for(token)
    assert scope is not None
    assert scope.policy is Policy.SELF_UPDATE


async def test_an_investigation_runs_read_only_in_its_own_session_outside_the_cap(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await add_setting_row(dev_deps.db, "turns.max_concurrent", 1, at=fakes.clock.now())
    fakes.agents.script_turn(Pause())
    fakes.agents.script_turn(Pause())
    await start(dev_deps, state)
    investigation = await start(dev_deps, state, make_turn_request(text="xatoni tekshir", kind=TurnKind.INVESTIGATE))
    spec = fakes.agents.opened[1][0]
    assert spec.kind == "investigation"
    assert spec.permission_mode == "dontAsk"
    assert spec.tools == ("Read", "Glob", "Grep", "Bash", "WebFetch", "WebSearch")
    assert spec.allowed_tools == ("Read", "Glob", "Grep", "WebFetch", "WebSearch")
    assert spec.max_turns == 60
    assert "read-only" in spec.system_append
    token = spec.env["YORDAMCHI_SESSION_TOKEN"]
    scope = state.scope_for(token)
    assert scope is not None
    assert scope.policy is Policy.READ_ONLY
    assert scope.turn_id == investigation


async def test_each_delivery_carries_the_current_owner_instructions_and_records_the_version(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(result())
    first = await start(dev_deps, state)
    await finish(dev_deps, fakes, first)
    changed = await add_setting_row(dev_deps.db, "persona.dev_tone", "juda qisqa", at=fakes.clock.now())
    fakes.agents.script_turn(Pause())
    await start(dev_deps, state, make_turn_request(text="endi testlarni yoz"))
    spec = fakes.agents.opened[0][0]
    assert spec.context_provider is not None
    assert "juda qisqa" in spec.context_provider()  # the same warm session, the newest instructions (SPEC §5.4)
    assert len(fakes.agents.opened) == 1
    session = (await sessions(dev_deps))[0]
    assert session.prompt_version_seen == changed.id


# --- routing → turns, in one unit of work (SPEC §3.2.1 step 7; the T20 interface) ----------------------------------
async def test_a_routed_burst_is_recorded_acked_and_delivered_atomically(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_part(1, text="BonviHR'da login sahifasini tuzat"))
    fakes.agents.script_turn(Pause())

    routed = await service.route_turn(dev_deps, state, item)

    assert routed.routed is not None
    assert routed.submitted is not None
    assert not routed.submitted.merged
    rows = await messages(dev_deps)
    assert [(row.kind, text_of(row)) for row in rows] == [("echo", S.ROUTED(project=PROJECT))]
    [delivery] = await deliveries(dev_deps, routed.submitted.turn_id)
    assert (delivery.burst_id, delivery.text) == (item.burst_id, "BonviHR'da login sahifasini tuzat")
    assert (await service.route_turn(dev_deps, state, item)).routed is None  # never routed or delivered twice
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 1)


async def test_a_routed_follow_up_of_the_working_turn_merges_as_next(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause(), result())
    first = await start(dev_deps, state, make_turn_request(message_ids=(5001,), last_part_at=fakes.clock.now()))
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_part(2, text="tugmani ham", reply_to_message_id=5001))

    routed = await service.route_turn(dev_deps, state, item)

    assert routed.submitted is not None
    assert (routed.submitted.turn_id, routed.submitted.merged) == (first, True)
    assert S.ROUTED_MERGED(project=PROJECT) in await texts(dev_deps, "echo")
    await service.schedule_turns(dev_deps, state)
    await until(lambda: [mode for _, _, mode in fakes.agents.sends] == ["new_turn", "next"])


async def test_a_question_routing_asks_writes_no_delivery(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_part(1, text="BonviHR yoki HRMentor, qaysi biri bo'lsa ham"))
    routed = await service.route_turn(dev_deps, state, item)
    assert routed.routed is not None
    assert routed.submitted is None
    async with dev_deps.db.read() as s:
        assert await store.list_turns_with_status(s, [status.value for status in TurnStatus]) == []


async def test_the_gate_knows_when_the_turn_started_and_whether_the_tree_was_dirty(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.git.untracked[folders[PROJECT]] = ["scratch.txt"]
    started = fakes.clock.now()
    fakes.agents.script_turn(Pause())
    turn_id = await start(dev_deps, state)
    scope = state.scope_for(fakes.agents.opened[0][0].env["YORDAMCHI_SESSION_TOKEN"])
    assert scope is not None
    assert scope.dirty_before_turn is True
    assert scope.turn_started_at == started
    row = await turn(dev_deps, turn_id)
    assert row.start_dirty is True
    assert row.start_files_json == {"dirty": [], "untracked": ["scratch.txt"]}


# --- the scheduler: cap, pressure, disk, Mac attach (SPEC §5.6, NFR-CAP-01) ----------------------------------------
async def test_a_turn_over_the_cap_queues_with_its_position_and_starts_when_a_slot_frees(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await add_setting_row(dev_deps.db, "turns.max_concurrent", 1, at=fakes.clock.now())
    fakes.agents.script_turn(result())
    first = await start(dev_deps, state)
    second = await service.submit_turn(dev_deps, state, make_turn_request(OTHER, "README yangila"))
    await service.schedule_turns(dev_deps, state)

    queued = await turn(dev_deps, second.turn_id)
    assert queued.status == "queued"
    assert queued.queue_reason == QueueReason.CAP.value
    assert S.QUEUED(project=OTHER, position=1, reason=S.QUEUE_REASONS["cap"]()) in await texts(dev_deps)
    await service.schedule_turns(dev_deps, state)
    assert len([text for text in await texts(dev_deps) if text.startswith(f"{OTHER}: navbat")]) == 1  # told once

    await finish(dev_deps, fakes, first)
    await service.schedule_turns(dev_deps, state)
    await until(status_is(dev_deps, second.turn_id, TurnStatus.RUNNING), what="second starts")


@pytest.mark.parametrize(("level", "queues"), [(2, False), (3, True), (4, True)])
async def test_memory_pressure_above_the_setting_queues(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState, level: int, *, queues: bool
) -> None:
    fakes.mac_host.pressure_level = level
    fakes.agents.script_turn(Pause())
    submitted = await service.submit_turn(dev_deps, state, make_turn_request())
    await service.schedule_turns(dev_deps, state)
    if not queues:
        await until(status_is(dev_deps, submitted.turn_id, TurnStatus.RUNNING))
        return
    row = await turn(dev_deps, submitted.turn_id)
    assert (row.status, row.queue_reason) == ("queued", "pressure")
    fakes.mac_host.pressure_level = 1
    await service.schedule_turns(dev_deps, state)
    await until(status_is(dev_deps, submitted.turn_id, TurnStatus.RUNNING))


class _BlindMac(FakeMacHost):
    async def memory_pressure_level(self, *, timeout: float) -> int:
        raise CallTimedOut(operation="sysctl")


async def test_an_unreadable_pressure_level_queues_and_logs(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    from dataclasses import replace

    deps = replace(dev_deps, mac_host=_BlindMac())
    submitted = await service.submit_turn(deps, state, make_turn_request())
    assert await service.schedule_turns(deps, state) == 0
    row = await turn(deps, submitted.turn_id)
    assert (row.status, row.queue_reason) == ("queued", "pressure")


async def test_low_disk_queues_coding_turns(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.mac_host.free_bytes = 2 * GIB
    submitted = await service.submit_turn(dev_deps, state, make_turn_request())
    await service.schedule_turns(dev_deps, state)
    row = await turn(dev_deps, submitted.turn_id)
    assert (row.status, row.queue_reason) == ("queued", "disk")
    assert any(text.endswith(f"— {S.QUEUE_REASONS['disk']()}.") for text in await texts(dev_deps))


async def test_a_project_open_at_the_mac_queues(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    from yordamchi.ports.mac_host import InteractiveSession

    fakes.agents.script_turn(result())
    first = await start(dev_deps, state)
    await finish(dev_deps, fakes, first)
    session_id = session_of(fakes)
    fakes.mac_host.sessions = [InteractiveSession(pid=4242, session_id=session_id, kind="interactive")]
    submitted = await service.submit_turn(dev_deps, state, make_turn_request(text="davom"))
    await service.schedule_turns(dev_deps, state)
    row = await turn(dev_deps, submitted.turn_id)
    assert (row.status, row.queue_reason) == ("queued", "mac_attached")


async def test_a_second_instruction_waits_behind_the_projects_working_turn(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause())
    await start(dev_deps, state)
    second = await service.submit_turn(dev_deps, state, make_turn_request(text="boshqa ish"))
    await service.schedule_turns(dev_deps, state)
    row = await turn(dev_deps, second.turn_id)
    assert (row.status, row.queue_reason) == ("queued", "busy")


# --- follow-ups (UC-02) ---------------------------------------------------------------------------------------------
async def test_a_follow_up_merges_into_the_working_turn_as_next_with_one_result(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(ToolStep("Bash", {"command": "make test"}), Pause(), result())
    started_at = fakes.clock.now()
    first = await start(dev_deps, state, make_turn_request(message_ids=(5001,), last_part_at=started_at))

    running = await service.running_turns(dev_deps)
    assert [item.project_id for item in running] == [PROJECT]
    assert 5001 in running[0].message_ids
    async with dev_deps.db.uow() as s:
        burst_id = await make_burst_row(s)
    follow = turn_input(
        burst_id,
        DEV_GROUP_ID,
        (make_part(2, text="va testlarini ham", reply_to_message_id=5001, start=started_at),),
        opened_at=started_at,
        closed_at=started_at,
        reason=CloseReason.QUIET,
    )
    decision = decide_dev_follow_up(
        follow, running[0], route=RouteHint(None, 0.0), settings=make_window_settings(), min_confidence=0.85
    )
    assert decision.merge
    request = domain.request_from_input(follow, PROJECT, merge=decision.merge)
    merged = await service.submit_turn(dev_deps, state, request)
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2, what="follow-up sent")

    assert merged.turn_id == first
    assert merged.merged
    assert [mode for _, _, mode in fakes.agents.sends] == ["new_turn", "next"]
    assert fakes.agents.sends[1][1].text == "va testlarini ham"
    await fakes.agents.release(session_of(fakes))
    await finish(dev_deps, fakes, first)
    assert len(await texts(dev_deps, "result")) == 1
    assert [(item.mode, item.status) for item in await deliveries(dev_deps, first)] == [
        ("new_turn", "delivered"),
        ("next", "delivered"),
    ]


async def test_a_follow_up_after_the_result_but_inside_the_grace_starts_the_next_cli_turn(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(result("Natija: birinchi."))
    fakes.agents.script_turn(result("Natija: ikkinchisi ham."))
    first = await start(dev_deps, state)
    await until(lambda: fakes.clock.pending_sleepers > 0)
    await service.submit_turn(dev_deps, state, make_turn_request(text="yana bitta", merge=True))
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2)
    assert fakes.agents.sends[1][2] == "new_turn"  # the executor was idle: the same Yordamchi turn goes on
    # Both results armed a grace: the first is stale (the follow-up went in), only the second may end the turn.
    await until(lambda: fakes.clock.pending_sleepers == 2, what="the second result's grace")
    await fakes.clock.advance(GRACE)
    await until(status_is(dev_deps, first, TurnStatus.DONE))
    row = await turn(dev_deps, first)
    assert row.natija == "ikkinchisi ham."
    assert len(await texts(dev_deps, "result")) == 1


async def test_a_merge_request_whose_turn_is_gone_becomes_a_turn_of_its_own(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    submitted = await service.submit_turn(dev_deps, state, make_turn_request(merge=True))
    assert not submitted.merged
    assert (await turn(dev_deps, submitted.turn_id)).status == "queued"


async def test_a_burst_is_delivered_once(dev_deps: DevDeps, folders: dict[str, Path], state: TurnsState) -> None:
    first = await service.submit_turn(dev_deps, state, make_turn_request(burst_id=None))
    again = await service.submit_turn(dev_deps, state, make_turn_request(burst_id=None))
    assert first.turn_id != again.turn_id
    async with dev_deps.db.uow() as s:
        burst_id = await make_burst_row(s)
    one = await service.submit_turn(dev_deps, state, make_turn_request(burst_id=burst_id))
    two = await service.submit_turn(dev_deps, state, make_turn_request(burst_id=burst_id))
    assert one == two


# --- stop, redirect, stop all (UC-03) -------------------------------------------------------------------------------
async def test_stop_closes_the_guard_tells_the_owner_and_interrupts_within_three_seconds(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(ToolStep("Bash", {"command": "make build"}), Pause(), ToolStep("Bash", {"command": "x"}))
    turn_id = await start(dev_deps, state)
    await service.submit_turn(dev_deps, state, make_turn_request(text="keyin buni ham", merge=True))
    spec = fakes.agents.opened[0][0]

    began = time.monotonic()
    stopped = await service.stop_turn(dev_deps, state, PROJECT)
    elapsed = time.monotonic() - began

    assert elapsed < 3.0
    assert stopped.stopped == (PROJECT,)
    assert stopped.reply is None
    assert fakes.agents.interrupts == [session_of(fakes)]
    row = await turn(dev_deps, turn_id)
    assert row.status == "interrupted"
    notice = next(message for message in await messages(dev_deps) if text_of(message) == S.STOPPED(project=PROJECT))
    assert notice.priority == 0
    # No tool call from that turn starts after the confirmation.
    verdict = await spec.guard(ToolCall("Bash", {"command": "echo hi"}, "late", None, session_of(fakes)))
    assert not verdict.allow
    assert verdict.reason_code == service.STOPPING_REASON
    assert len(fakes.agents.guard_calls) == 1
    assert [item.status for item in await deliveries(dev_deps, turn_id)] == ["delivered", "superseded"]


async def test_stop_with_nothing_running_says_so(
    dev_deps: DevDeps, folders: dict[str, Path], state: TurnsState
) -> None:
    assert (await service.stop_turn(dev_deps, state, PROJECT)).reply == S.STOP_NOTHING(project=PROJECT)
    assert (await service.stop_all(dev_deps, state)).reply == S.STOP_ALL_NOTHING()


async def test_stop_cancels_a_queued_turn(dev_deps: DevDeps, folders: dict[str, Path], state: TurnsState) -> None:
    submitted = await service.submit_turn(dev_deps, state, make_turn_request())
    stopped = await service.stop_turn(dev_deps, state, PROJECT)
    assert stopped.stopped == (PROJECT,)
    assert (await turn(dev_deps, submitted.turn_id)).status == "cancelled"


async def test_a_stop_the_executor_cannot_honour_is_killed_and_the_session_needs_resume(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState, paths: Paths
) -> None:
    fakes.agents.kill_on_interrupt = True
    fakes.agents.script_turn(Pause())
    turn_id = await start(dev_deps, state)
    session_id = session_of(fakes)
    await service.stop_turn(dev_deps, state, PROJECT)

    async def resumable() -> bool:
        return [(row.session_id, row.status) for row in await sessions(dev_deps)] == [(session_id, "idle")]

    await until(resumable, what="session idle: it needs a resume")
    assert (await turn(dev_deps, turn_id)).status == "interrupted"
    assert state.live == {}

    write_transcript(paths.home, session_id)
    fakes.agents.script_turn(Pause())
    await start(dev_deps, state, make_turn_request(text="qaytadan"))
    assert fakes.agents.opened[-1][1] == session_id  # resumed by its id, context kept


async def test_a_redirect_stops_and_delivers_the_new_instruction_in_the_same_session_within_five_seconds(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause())
    fakes.agents.script_turn(Pause())
    first = await start(dev_deps, state)
    began = time.monotonic()
    redirected = await service.redirect_turn(dev_deps, state, make_turn_request(text="boshqacha: API orqali qil"))
    await service.schedule_turns(dev_deps, state)
    await until(status_is(dev_deps, redirected.turn_id, TurnStatus.RUNNING))
    await until(lambda: len(fakes.agents.sends) == 2)
    assert time.monotonic() - began < 5.0
    assert (await turn(dev_deps, first)).status == "interrupted"
    assert len(fakes.agents.opened) == 1  # the same session: prior context kept
    assert fakes.agents.sends[1][1].text == "boshqacha: API orqali qil"
    assert fakes.agents.sends[1][2] == "new_turn"


async def test_stop_all_interrupts_every_working_turn_within_five_seconds(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause())
    fakes.agents.script_turn(Pause())
    first = await start(dev_deps, state)
    second = await start(dev_deps, state, make_turn_request(OTHER, "README"))
    began = time.monotonic()
    stopped = await service.stop_all(dev_deps, state)
    assert time.monotonic() - began < 5.0
    assert stopped.stopped == (PROJECT, OTHER) or set(stopped.stopped) == {PROJECT, OTHER}
    assert {(await turn(dev_deps, first)).status, (await turn(dev_deps, second)).status} == {"interrupted"}
    assert len(fakes.agents.interrupts) == 2


# --- questions (SPEC §3.2.2) ----------------------------------------------------------------------------------------
async def test_a_question_pauses_the_turn_frees_its_slot_and_its_answer_resumes_it(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    asked: list[PendingQuestion] = []

    async def hook(deps: DevDeps, question: PendingQuestion) -> None:
        del deps
        asked.append(question)

    state.questions = hook
    await add_setting_row(dev_deps.db, "turns.max_concurrent", 1, at=fakes.clock.now())
    fakes.agents.script_turn(QuestionAsked("q1", ({"question": "Qaysi baza?"},)), Pause(), result())
    fakes.agents.script_turn(Pause())
    first = await start(dev_deps, state)
    await until(status_is(dev_deps, first, TurnStatus.WAITING_QUESTION))
    await until(lambda: asked)
    assert asked == [PendingQuestion(first, PROJECT, "q1", ({"question": "Qaysi baza?"},))]

    other = await start(dev_deps, state, make_turn_request(OTHER, "README"))  # the waiting turn holds no slot
    assert (await turn(dev_deps, other)).status == "running"

    held = await service.answer_question(
        dev_deps, state, turn_id=first, question_id="q1", answers={"Qaysi baza?": "Postgres"}, fallback_text="x"
    )
    assert held
    assert fakes.agents.answers == [(session_of(fakes, 0), "q1", {"Qaysi baza?": "Postgres"})]
    assert (await turn(dev_deps, first)).status == "running"
    await fakes.agents.release(session_of(fakes, 0))
    await finish(dev_deps, fakes, first)


async def test_an_answer_whose_callback_is_gone_arrives_as_the_turns_next_input(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(QuestionAsked("q1", ({"question": "Qaysi baza?"},)), result("Natija: javob kutyapman."))
    first = await start(dev_deps, state)
    await until(status_is(dev_deps, first, TurnStatus.WAITING_QUESTION))
    await until(lambda: not state.live_of_turn(first), what="turn detached after the parked result")

    fakes.agents.script_turn(result())
    held = await service.answer_question(
        dev_deps, state, turn_id=first, question_id="q1", answers={}, fallback_text="[Yordamchi] Owner: Postgres"
    )
    assert not held
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2)
    assert fakes.agents.sends[1][1].text == "[Yordamchi] Owner: Postgres"
    await finish(dev_deps, fakes, first)
    with pytest.raises(TurnNotFound):
        await service.answer_question(dev_deps, state, turn_id=999, question_id="q", answers={}, fallback_text="x")


async def test_an_answer_queued_while_the_executor_still_finishes_does_not_strand_the_session(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(QuestionAsked("q1", ({"question": "Qaysi?"},)), Pause(), result("Natija: kutyapman."))
    turn_id = await start(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.WAITING_QUESTION))
    held = await service.answer_question(
        dev_deps, state, turn_id=turn_id, question_id="gone", answers={}, fallback_text="[Yordamchi] Owner: A"
    )
    assert not held
    assert (await turn(dev_deps, turn_id)).status == "queued"

    fakes.agents.script_turn(result())
    await fakes.agents.release(session_of(fakes))  # the parked turn's own result arrives now
    await until(lambda: state.live_of_turn(turn_id) is None, what="session lets go of the turn")
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2, what="the answer goes in")
    assert fakes.agents.sends[1][1].text == "[Yordamchi] Owner: A"
    await finish(dev_deps, fakes, turn_id)


# --- usage limit (UC-11 F, SPEC §5.6) -------------------------------------------------------------------------------
async def test_a_usage_limit_waits_once_and_continues_where_it_stopped(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    resets_at = fakes.clock.now() + timedelta(minutes=30)
    fakes.agents.script_turn(
        UsageLimited(resets_at=resets_at, limit_type="five_hour"),
        result("", is_error=True, subtype="error_during_execution"),
    )
    fakes.agents.script_turn(result())
    turn_id = await start(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.WAITING_LIMIT))
    await until(lambda: not state.live_of_turn(turn_id))
    notices = [text for text in await texts(dev_deps) if "limiti tugadi" in text]
    assert notices == [S.USAGE_LIMIT(project=PROJECT, time="14:30")]  # Asia/Tashkent

    await fakes.clock.advance(5 * 60)
    await service.schedule_turns(dev_deps, state)
    assert (await turn(dev_deps, turn_id)).status == "waiting_limit"

    await fakes.clock.advance(5 * 60)  # every 10 minutes
    await service.schedule_turns(dev_deps, state)
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2, what="retry delivered")
    assert "a usage limit interrupted you" in fakes.agents.sends[1][1].text
    row = await finish(dev_deps, fakes, turn_id)
    assert row.status == "done"
    assert [(item.mode, item.status) for item in await deliveries(dev_deps, turn_id)] == [
        ("new_turn", "delivered"),
        ("resume", "delivered"),
    ]


async def test_a_claude_manager_rotation_retries_after_forty_seconds(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    def manager(active: str) -> ClaudeManagerState:
        account = ClaudeAccount(active, "ok", 10.0, 10.0, None, None, is_active=True)
        return ClaudeManagerState(active_id=active, auto_switch=True, accounts=(account,))

    fakes.mac_host.manager_state = manager("a")
    fakes.agents.script_turn(UsageLimited(resets_at=None, limit_type="five_hour"), result("", is_error=True))
    turn_id = await start(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.WAITING_LIMIT))
    assert S.USAGE_LIMIT_UNKNOWN(project=PROJECT) in await texts(dev_deps)
    await service.schedule_turns(dev_deps, state)  # sees account "a"
    fakes.mac_host.manager_state = manager("b")
    await fakes.clock.advance(1)
    await service.schedule_turns(dev_deps, state)  # the switch is noticed
    assert (await turn(dev_deps, turn_id)).status == "waiting_limit"
    await fakes.clock.advance(40)
    await service.schedule_turns(dev_deps, state)
    assert (await turn(dev_deps, turn_id)).status in {"queued", "starting", "running"}


# --- process death, runaway, nudge, failures ------------------------------------------------------------------------
async def test_a_dead_process_interrupts_the_turn_asks_and_the_session_resumes_on_continue(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState, paths: Paths
) -> None:
    fakes.agents.script_turn(ToolStep("Bash", {"command": "make"}), Pause())
    turn_id = await start(dev_deps, state)
    session_id = session_of(fakes)
    fakes.agents.emit(session_id, RuntimeFailed(code="process_died", retryable=True), SessionClosed("process_died"))
    await until(status_is(dev_deps, turn_id, TurnStatus.INTERRUPTED))
    row = await turn(dev_deps, turn_id)
    assert row.error_code == "process_died"
    assert S.TURN_PROCESS_DIED(project=PROJECT) in await texts(dev_deps)
    assert [(item.session_id, item.status) for item in await sessions(dev_deps)] == [(session_id, "idle")]

    write_transcript(paths.home, session_id)
    fakes.agents.script_turn(result())
    assert await service.continue_turn(dev_deps, state, PROJECT) == S.TURN_CONTINUED(project=PROJECT)
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2)
    assert fakes.agents.opened[-1][1] == session_id
    assert fakes.agents.sends[1][1].text.startswith("[Yordamchi] The owner says: continue")
    await finish(dev_deps, fakes, turn_id)
    assert await service.continue_turn(dev_deps, state, PROJECT) == S.TURN_NOTHING_TO_CONTINUE(project=PROJECT)


async def test_a_turn_the_executor_aborted_by_itself_is_interrupted_and_asks(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    from tests.fakes.agent import interrupted_result

    fakes.agents.script_turn(interrupted_result())
    turn_id = await start(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.INTERRUPTED))
    assert (await turn(dev_deps, turn_id)).error_code == "aborted"
    assert S.TURN_PROCESS_DIED(project=PROJECT) in await texts(dev_deps)
    assert state.live_of_turn(turn_id) is None


async def test_a_turn_over_its_steps_pauses_asks_and_continues_with_a_fresh_budget(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await add_setting_row(dev_deps.db, "turns.max_steps", 10, at=fakes.clock.now())
    fakes.agents.script_turn(*(ToolStep("Bash", {"command": f"try {n}"}) for n in range(11)), Pause())
    turn_id = await start(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.PAUSED_RUNAWAY))
    await until(lambda: not state.live_of_turn(turn_id))
    expected = S.TURN_RUNAWAY(project=PROJECT, why=S.RUNAWAY_STEPS(steps=10))
    assert expected in await texts(dev_deps)
    assert fakes.agents.interrupts

    fakes.agents.script_turn(result())
    await service.continue_turn(dev_deps, state, PROJECT)
    await service.schedule_turns(dev_deps, state)
    row = await finish(dev_deps, fakes, turn_id)
    assert row.steps == 11


async def test_a_turn_past_its_wall_clock_pauses(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause())
    turn_id = await start(dev_deps, state)
    await fakes.clock.advance(61 * 60)
    await service.schedule_turns(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.PAUSED_RUNAWAY))
    assert S.TURN_RUNAWAY(project=PROJECT, why=S.RUNAWAY_WALL(minutes=60)) in await texts(dev_deps)


async def test_the_same_failing_call_five_times_pauses(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    steps = [
        item
        for _ in range(5)
        for item in (
            Progress(kind="tool_start", tool_name="Bash", summary="make test"),
            Progress(kind="tool_end", tool_name="Bash", summary="", is_error=True),
        )
    ]
    fakes.agents.script_turn(*steps, Pause())
    turn_id = await start(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.PAUSED_RUNAWAY))
    assert S.TURN_RUNAWAY(project=PROJECT, why=S.RUNAWAY_SAME_FAILURE(count=5)) in await texts(dev_deps)


async def test_unpushed_work_gets_one_nudge_and_a_second_miss_shows_in_the_result(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    folder = folders[PROJECT]
    fakes.agents.script_turn(result("Natija: qildim."))
    fakes.agents.script_turn(result("Natija: qildim, lekin push o'tmadi."))
    turn_id = await start(dev_deps, state)
    fakes.git.statuses[folder] = GitStatus("main", "beef0001", "origin/main", 1, 0, 0, 0)
    fakes.git.logs[folder] = [Commit("beef0001", "wip", fakes.clock.now())]
    await until(lambda: fakes.clock.pending_sleepers > 0)
    await fakes.clock.advance(GRACE)

    async def nudged() -> bool:
        return [item.mode for item in await deliveries(dev_deps, turn_id)] == ["new_turn", "nudge"]

    await until(nudged, what="nudge written")  # it goes in on the next scheduler pass
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2)
    assert fakes.agents.sends[1][1].text.startswith("[Yordamchi] Commit and push your work now (pre-authorized")
    row = await finish(dev_deps, fakes, turn_id)
    assert row.pushed is False
    result_text = (await texts(dev_deps, "result"))[0]
    assert "Commit: commit/push qilinmadi" in shown(result_text)
    assert [item.mode for item in await deliveries(dev_deps, turn_id)] == ["new_turn", "nudge"]


async def test_a_pending_gate_decision_holds_the_turn_until_the_owners_answer(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(result("Natija: deploy qarorini kutyapman."))
    turn_id = await start(dev_deps, state)
    async with dev_deps.db.uow() as s:
        decision = await make_decision(s, at=fakes.clock.now())
    await until(lambda: fakes.clock.pending_sleepers > 0)
    await fakes.clock.advance(GRACE)
    await until(status_is(dev_deps, turn_id, TurnStatus.WAITING_DECISION))
    assert await texts(dev_deps, "result") == []

    fakes.agents.script_turn(result())
    answer = DecisionAnswer(
        AnswerKind.APPROVED, decision.id, PROJECT, turn_id, delivery="[Yordamchi] Owner approved #1. Run: x"
    )
    async with dev_deps.db.uow() as s:  # the owner's "ha" (gate.answer_decision) approved it
        await gate_store.mark_decision_resolved(s, decision.id, status="approved", at=fakes.clock.now(), update_id=9)
    submitted = await service.deliver_decision(dev_deps, state, answer)
    assert submitted is not None
    assert submitted.turn_id == turn_id
    await service.schedule_turns(dev_deps, state)
    await until(lambda: len(fakes.agents.sends) == 2)
    assert fakes.agents.sends[1][1].text.startswith("[Yordamchi] Owner approved #1")
    await finish(dev_deps, fakes, turn_id)


async def test_an_executor_error_fails_the_turn_and_says_why(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(result("", is_error=True, subtype="error_during_execution"))
    turn_id = await start(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.FAILED))
    assert S.TURN_FAILED(project=PROJECT, reason="error_during_execution") in await texts(dev_deps)


class _FailingSend(FakeAgentRuntime):
    async def send(self, h: SessionHandle, msg: AgentInput, *, mode: DeliveryModeLiteral, timeout: float) -> None:
        raise CallTimedOut(operation="claude_send")


async def test_a_send_that_failed_is_never_resent_and_the_owner_is_asked(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    from dataclasses import replace

    deps = replace(dev_deps, agents=_FailingSend())
    submitted = await service.submit_turn(deps, state, make_turn_request())
    await service.schedule_turns(deps, state)
    await until(status_is(deps, submitted.turn_id, TurnStatus.INTERRUPTED))
    assert [item.status for item in await deliveries(deps, submitted.turn_id)] == ["failed"]
    assert S.DELIVERY_UNCERTAIN(project=PROJECT) in await texts(deps)
    await service.schedule_turns(deps, state)
    assert [item.status for item in await deliveries(deps, submitted.turn_id)] == ["failed"]
    await service.shutdown_turns(deps, state)  # its sessions live in this runtime, not the fixture's


async def test_a_refused_login_fails_the_turn_with_its_own_message(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(RuntimeFailed(code="auth_failed", retryable=False), Pause())
    turn_id = await start(dev_deps, state)
    await until(status_is(dev_deps, turn_id, TurnStatus.FAILED))
    assert S.TURN_AUTH_FAILED(project=PROJECT) in await texts(dev_deps)


async def test_an_approval_for_a_working_turn_steers_it_and_one_for_a_finished_turn_starts_a_new_one(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause(), result())
    turn_id = await start(dev_deps, state)
    approved = DecisionAnswer(AnswerKind.APPROVED, 1, PROJECT, turn_id, delivery="[Yordamchi] Owner approved #1.")
    steered = await service.deliver_decision(dev_deps, state, approved)
    assert steered is not None
    assert (steered.turn_id, steered.merged) == (turn_id, True)
    await service.schedule_turns(dev_deps, state)
    await until(lambda: [mode for _, _, mode in fakes.agents.sends] == ["new_turn", "next"])
    await fakes.agents.release(session_of(fakes))
    await finish(dev_deps, fakes, turn_id)

    declined = DecisionAnswer(AnswerKind.DECLINED, 1, PROJECT, turn_id, delivery="[Yordamchi] Owner declined #1.")
    fresh = await service.deliver_decision(dev_deps, state, declined)
    assert fresh is not None
    assert fresh.turn_id != turn_id
    assert await service.deliver_decision(dev_deps, state, DecisionAnswer(AnswerKind.AMBIGUOUS)) is None


async def test_a_refused_credential_in_the_projects_claude_settings_never_opens_a_session(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    settings_dir = folders[PROJECT] / ".claude"
    settings_dir.mkdir()
    (settings_dir / "settings.json").write_text('{"env": {"ANTHROPIC_API_KEY": "x"}}')
    submitted = await service.submit_turn(dev_deps, state, make_turn_request())
    await service.schedule_turns(dev_deps, state)
    await until(status_is(dev_deps, submitted.turn_id, TurnStatus.INTERRUPTED))
    assert fakes.agents.opened == []
    assert (await turn(dev_deps, submitted.turn_id)).error_code == "credentials_refused"


async def test_a_late_background_result_is_a_note_owned_by_no_turn(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(result())
    turn_id = await start(dev_deps, state)
    await finish(dev_deps, fakes, turn_id)
    fakes.agents.emit(session_of(fakes), result("Fon testlari ham o'tdi.", background=True))
    note = S.TURN_LATE_NOTE(project=PROJECT, text="Fon testlari ham o'tdi.")
    await until(lambda: _contains(texts(dev_deps), note), what="late note")
    assert (await turn(dev_deps, turn_id)).status == "done"


async def _contains(found: Awaitable[list[str]], text: str) -> bool:
    return text in await found


# --- sessions: idle close, renewal, model and effort changes (SPEC §5.4, §5.6, §3.2.7) ------------------------------
async def test_an_idle_session_closes_after_sessions_idle_close_min(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(result())
    turn_id = await start(dev_deps, state)
    await finish(dev_deps, fakes, turn_id)
    await fakes.clock.advance(9 * 60)
    await service.schedule_turns(dev_deps, state)
    assert fakes.agents.closed == []
    await fakes.clock.advance(60)
    await service.schedule_turns(dev_deps, state)
    await until(lambda: fakes.agents.closed == [session_of(fakes)])
    assert [item.status for item in await sessions(dev_deps)] == ["idle"]


async def test_a_session_whose_transcript_is_gone_is_renewed_with_the_handoff_note(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(result())
    first = await start(dev_deps, state)
    await finish(dev_deps, fakes, first)
    old_session = session_of(fakes)
    await fakes.clock.advance(10 * 60)
    await service.schedule_turns(dev_deps, state)
    await until(lambda: not state.live)

    fakes.agents.script_turn(Pause())
    await start(dev_deps, state, make_turn_request(text="davom etamiz"))
    assert fakes.agents.opened[-1][1] is None  # no transcript under ~/.claude/projects: a new session
    assert S.SESSION_RENEWED(project=PROJECT) in await texts(dev_deps)
    seeded = fakes.agents.sends[-1][1].text
    assert seeded.startswith("[Yordamchi] This is a new session")
    assert "Goal: fix login" in seeded
    assert seeded.rstrip().endswith("davom etamiz")
    current = await sessions(dev_deps)
    assert [item.session_id for item in current] != [old_session]


async def test_a_model_change_applies_before_the_next_send_and_an_effort_change_resumes_the_session(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState, paths: Paths
) -> None:
    fakes.agents.script_turn(result())
    first = await start(dev_deps, state)
    await finish(dev_deps, fakes, first)
    session_id = session_of(fakes)
    await add_setting_row(dev_deps.db, "models.coding", "sonnet", at=fakes.clock.now())
    fakes.agents.script_turn(result())
    second = await start(dev_deps, state, make_turn_request(text="ikkinchi"))
    assert fakes.agents.models == [(session_id, "sonnet")]
    await finish(dev_deps, fakes, second)

    write_transcript(paths.home, session_id)
    await add_setting_row(dev_deps.db, "models.coding_effort", "high", at=fakes.clock.now())
    fakes.agents.script_turn(Pause())
    await start(dev_deps, state, make_turn_request(text="uchinchi"))
    assert fakes.agents.closed == [session_id]
    spec, resume_id = fakes.agents.opened[-1]
    assert (resume_id, spec.effort, spec.model) == (session_id, "high", "sonnet")


# --- crash recovery (SPEC §2.7, NFR-REL-05/07) ----------------------------------------------------------------------
async def test_recovery_gives_each_row_state_exactly_one_outcome(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    now = fakes.clock.now()
    async with dev_deps.db.uow() as s:
        uncertain = await make_turn(s, PROJECT, status=TurnStatus.RUNNING, delivery_status="sending", text="A")
        running = await make_turn(s, OTHER, status=TurnStatus.RUNNING, delivery_status="delivered", text="B")
        follow_up = await store.add_delivery(
            s,
            turn_id=running.id,
            mode=DeliveryMode.NEXT.value,
            source="owner",
            text="B2",
            media={},
            burst_id=None,
            at=now,
        )
        never_sent = await make_turn(s, PROJECT, status=TurnStatus.STARTING, text="C")
        stopping = await make_turn(s, OTHER, status=TurnStatus.STOPPING, delivery_status="delivered", text="D")
        queued = await make_turn(s, PROJECT, status=TurnStatus.QUEUED, text="E")
        waiting = await make_turn(s, OTHER, status=TurnStatus.WAITING_QUESTION, delivery_status="delivered")
        live = await store.add_agent_session(
            s,
            project_id=PROJECT,
            executor="claude",
            kind="coding",
            session_id="sess-live",
            status="live",
            policy="FULL",
            model="opus",
            at=now,
        )
    assert follow_up is not None

    recovered = await service.recover_turns(dev_deps)

    assert recovered.uncertain == (PROJECT,)
    assert set(recovered.interrupted) == {PROJECT, OTHER}
    assert (await deliveries(dev_deps, uncertain.id))[0].status == "failed"  # never resent
    assert S.DELIVERY_UNCERTAIN(project=PROJECT) in await texts(dev_deps)
    statuses = {row_id: (await turn(dev_deps, row_id)).status for row_id in (uncertain.id, running.id, stopping.id)}
    assert set(statuses.values()) == {"interrupted"}  # never re-run by itself
    assert (await turn(dev_deps, never_sent.id)).status == "queued"  # it never reached the executor
    assert (await turn(dev_deps, queued.id)).status == "queued"
    assert (await turn(dev_deps, waiting.id)).status == "waiting_question"
    moved = [item for item in await deliveries(dev_deps, running.id) if item.text == "B2"]
    assert moved == []  # the undelivered follow-up waits in a new queued turn of the project
    async with dev_deps.db.read() as s:
        fresh = await store.get_delivery(s, follow_up.id)
        session = await store.get_agent_session(s, live.id)
    assert fresh is not None
    assert fresh.status == "pending"
    assert (await turn(dev_deps, fresh.turn_id)).status == "queued"
    assert session is not None
    assert session.status == "idle"

    fakes.agents.script_turn(Pause())
    assert await service.retry_delivery(dev_deps, state, PROJECT) == S.DELIVERY_RESENT(project=PROJECT)
    assert await service.retry_delivery(dev_deps, state, PROJECT) == S.DELIVERY_NOTHING_TO_RETRY(project=PROJECT)


async def test_a_turn_cut_off_by_a_start_that_died_before_telling_is_listed_by_the_next_start(
    dev_deps: DevDeps, folders: dict[str, Path]
) -> None:
    async with dev_deps.db.uow() as s:
        cut = await make_turn(s, PROJECT, status=TurnStatus.RUNNING, delivery_status="delivered", text="A")

    first = await service.recover_turns(dev_deps)  # … and the process died before its restart notice
    assert (first.interrupted, first.interrupted_turns) == ((PROJECT,), (cut.id,))
    assert (await turn(dev_deps, cut.id)).error_code == domain.RESTART_UNTOLD

    second = await service.recover_turns(dev_deps)
    assert (second.interrupted, second.interrupted_turns) == ((PROJECT,), (cut.id,))  # listed again
    assert await service.mark_restart_told(dev_deps, second.interrupted_turns) == 1

    third = await service.recover_turns(dev_deps)
    assert (third.interrupted, third.interrupted_turns) == ((), ())  # told once: never again
    told = await turn(dev_deps, cut.id)
    assert (told.status, told.error_code) == (TurnStatus.INTERRUPTED.value, domain.RESTART_TOLD)


async def test_shutdown_interrupts_working_turns_and_asks_to_continue(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path]
) -> None:
    state = TurnsState()
    fakes.agents.script_turn(Pause())
    turn_id = await start(dev_deps, state)
    assert await service.shutdown_turns(dev_deps, state) == 1
    assert (await turn(dev_deps, turn_id)).status == "interrupted"
    assert S.TURN_SHUTDOWN(project=PROJECT) in await texts(dev_deps)
    assert fakes.agents.closed == [session_of(fakes)]
    assert state.live == {}


# --- wrong target (UC-10 F) -----------------------------------------------------------------------------------------
async def _wrong_turn(dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState) -> tuple[int, Path]:
    folder = folders[PROJECT]
    fakes.git.changes[folder] = [FileChange("M", "notes.md")]  # the owner's own edit, there before the turn
    fakes.agents.script_turn(ToolStep("Bash", {"command": "edit"}), Pause())
    turn_id = await start(dev_deps, state, make_turn_request(text="HRMentor uchun: login"))
    created = folder / "tmp" / "made.txt"
    created.parent.mkdir()
    created.write_text("x")
    fakes.git.changes[folder] = [FileChange("M", "notes.md"), FileChange("M", "src/a.py")]
    fakes.git.untracked[folder] = ["tmp/made.txt"]
    fakes.git.logs[folder] = [Commit("c0ffee1", "wrong", fakes.clock.now())]
    return turn_id, folder


async def test_wrong_target_stops_lists_files_redelivers_and_reverts_only_after_ha(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    turn_id, folder = await _wrong_turn(dev_deps, fakes, folders, state)
    fakes.agents.script_turn(Pause())

    began = time.monotonic()
    corrected = await service.correct_wrong_target(
        dev_deps, state, from_project=PROJECT, to_project=OTHER, burst_id=None
    )
    assert time.monotonic() - began < 3.0
    assert (await turn(dev_deps, turn_id)).status == "interrupted"
    assert corrected.offered
    assert set(corrected.files) == {"notes.md", "src/a.py", "tmp/made.txt"}
    assert corrected.redelivered is not None
    redelivered = await turn(dev_deps, corrected.redelivered.turn_id)
    assert redelivered.project_id == OTHER
    assert (await deliveries(dev_deps, redelivered.id))[0].text == "HRMentor uchun: login"
    offer = next(message for message in await messages(dev_deps) if message.kind == "decision")
    assert text_of(offer).startswith(f"{PROJECT}: shu topshiriq o'zgartirgan fayllar (3)")
    assert not [call for call in fakes.git.calls if call[0] in {"checkout_paths", "revert"}]  # nothing before "ha"

    answer = await service.answer_revert(dev_deps, make_owner_origin(update_id=77), RevertReply("ha"))

    assert answer.outcome is RevertOutcome.REVERTED
    assert answer.reply == S.ROUTING_WRONG_TARGET_REVERTED(project=PROJECT, count=3)
    assert ("checkout_paths", folder, ("HEAD", "src/a.py")) in fakes.git.calls  # notes.md is the owner's: kept
    assert ("revert", folder, ("c0ffee1",)) in fakes.git.calls
    assert not (folder / "tmp" / "made.txt").exists()
    assert (await turn(dev_deps, turn_id)).revert_status == "reverted"
    again = await service.answer_revert(dev_deps, make_owner_origin(update_id=78), RevertReply("ha"))
    assert again.outcome is RevertOutcome.NOT_FOR_ME  # one revert, once


async def test_wrong_target_no_keeps_everything(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    turn_id, _ = await _wrong_turn(dev_deps, fakes, folders, state)
    await service.correct_wrong_target(dev_deps, state, from_project=PROJECT, to_project=OTHER, burst_id=None)
    answer = await service.answer_revert(dev_deps, make_owner_origin(), RevertReply("yo'q"))
    assert answer.outcome is RevertOutcome.KEPT
    assert answer.reply == S.ROUTING_WRONG_TARGET_KEPT(project=PROJECT)
    assert not [call for call in fakes.git.calls if call[0] in {"checkout_paths", "revert"}]
    assert (await turn(dev_deps, turn_id)).revert_status == "declined"


async def test_a_bare_ha_with_other_items_open_or_a_gate_number_is_not_a_revert_answer(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await _wrong_turn(dev_deps, fakes, folders, state)
    await service.correct_wrong_target(dev_deps, state, from_project=PROJECT, to_project=OTHER, burst_id=None)
    origin = make_owner_origin()
    assert (await service.answer_revert(dev_deps, origin, RevertReply("ha", others_pending=True))).outcome is (
        RevertOutcome.NOT_FOR_ME
    )
    assert (await service.answer_revert(dev_deps, origin, RevertReply("ha #3"))).outcome is RevertOutcome.NOT_FOR_ME
    assert (await service.answer_revert(dev_deps, origin, RevertReply("salom"))).outcome is RevertOutcome.NOT_FOR_ME
    named = await service.answer_revert(
        dev_deps, origin, RevertReply("ha", mentioned_projects=(PROJECT,), others_pending=True)
    )
    assert named.outcome is RevertOutcome.REVERTED


async def test_a_failed_revert_says_so_and_leaves_the_files(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    turn_id, _ = await _wrong_turn(dev_deps, fakes, folders, state)
    await service.correct_wrong_target(dev_deps, state, from_project=PROJECT, to_project=OTHER, burst_id=None)
    fakes.git.failures["revert"] = GitFailed(operation="revert", reason="conflict")
    answer = await service.answer_revert(dev_deps, make_owner_origin(), RevertReply("ha"))
    assert answer.outcome is RevertOutcome.FAILED
    assert answer.reply == S.WRONG_TARGET_REVERT_FAILED(project=PROJECT, operation="revert")
    assert (await turn(dev_deps, turn_id)).revert_status == "failed"


async def test_a_wrong_turn_that_changed_nothing_says_so(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause())
    fakes.agents.script_turn(Pause())
    await start(dev_deps, state)
    corrected = await service.correct_wrong_target(
        dev_deps, state, from_project=PROJECT, to_project=OTHER, burst_id=None
    )
    assert not corrected.offered
    assert S.ROUTING_WRONG_TARGET_CLEAN(project=PROJECT) in await texts(dev_deps)


# --- retention (SPEC §4.2) ------------------------------------------------------------------------------------------
async def test_retention_deletes_old_finished_turns_and_keeps_open_ones(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path]
) -> None:
    old = fakes.clock.now() - timedelta(days=401)
    async with dev_deps.db.uow() as s:
        done = await make_turn(s, PROJECT, at=old, status=TurnStatus.DONE, delivery_status="delivered")
        queued = await make_turn(s, PROJECT, at=old, status=TurnStatus.QUEUED)
        await store.add_progress(s, turn_id=queued.id, at=old, kind="tool", summary="Bash: x", keep=50)
    assert await service.apply_retention(dev_deps) >= 2
    async with dev_deps.db.read() as s:
        assert await store.get_turn(s, done.id) is None
        kept = await store.get_turn(s, queued.id)
        assert kept is not None
        assert await store.list_progress(s, queued.id, limit=10) == []
        assert [item.text for item in await store.list_deliveries(s, queued.id)] == ["Login sahifasini tuzat"]


# --- save_images and route_turn(images=) (T34: the screenshot reaches the agent) ---------------------------------
async def test_a_bursts_photo_is_saved_privately_and_rides_with_its_delivery(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    from tests.factories import make_part
    from yordamchi.core.errors import ProviderUnavailable
    from yordamchi.shared.inbox.domain import UpdateKind

    lost = make_part(1, kind=UpdateKind.PHOTO)  # its download fails
    photo = make_part(2, kind=UpdateKind.PHOTO)
    voice = make_part(3, kind=UpdateKind.VOICE, duration_s=2)  # not an image: never downloaded here
    words = make_part(4, text="BonviHR'da shu xatoni tuzat")
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, lost, photo, voice, words)
    fakes.messenger.fail_next("download", ProviderUnavailable(provider="telegram"))
    fakes.messenger.files["file-2"] = b"PNG"
    images = await service.save_images(dev_deps, item)
    assert len(images) == 1  # the lost one is left out; its "[image]" label still tells the agent
    saved = Path(images[0])
    assert await asyncio.to_thread(saved.read_bytes) == b"PNG"
    assert (await asyncio.to_thread(saved.stat)).st_mode & 0o777 == 0o600
    assert saved.parent.parent == dev_deps.paths.media
    routed = await service.route_turn(dev_deps, state, item, images=images)
    assert routed.submitted is not None
    [delivery] = await deliveries(dev_deps, routed.submitted.turn_id)
    assert domain.media_from_json(delivery.media_json).images == images
    assert await service.save_images(dev_deps, await _text_only(dev_deps)) == ()


async def _text_only(deps: DevDeps) -> TurnInput:
    from tests.factories import make_part

    async with deps.db.uow() as s:
        return await make_closed_burst(s, make_part(9, text="faqat matn"))


# --- an investigation routed to a project says "Tekshiryapman" first (T34b, UC-15 F) ------------------------------
async def test_an_investigation_says_tekshiryapman_at_once_in_place_of_a_bare_route(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.assistant.reply_with("routing", routing_reply(PROJECT, 0.95, intent="investigate"))
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_part(1, text="BonviHR'da login nega ishlamayapti?"))
    fakes.agents.script_turn(Pause())

    routed = await service.route_turn(dev_deps, state, item)

    assert routed.submitted is not None
    assert (await turn(dev_deps, routed.submitted.turn_id)).kind == TurnKind.INVESTIGATE.value
    [checking] = await messages(dev_deps)  # queued with the delivery, in one unit of work: no bare "→ BonviHR"
    assert text_of(checking) == "BonviHR · Tekshiryapman (~3 daqiqa)"
    assert (checking.kind, checking.priority) == ("report", 0)


async def test_a_spoken_investigation_keeps_its_voice_echo_then_says_tekshiryapman(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.assistant.reply_with("routing", routing_reply(PROJECT, 0.95, intent="investigate"))
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_voice_part(1, at_s=0, transcript="BonviHR'da xato nega chiqyapti"))
    fakes.agents.script_turn(Pause())

    await service.route_turn(dev_deps, state, item)

    echo, checking = await messages(dev_deps)
    assert text_of(echo).startswith("Ovoz: «BonviHR'da xato nega chiqyapti» → BonviHR")
    assert text_of(checking) == domain.checking_text(PROJECT)
    assert checking.priority == echo.priority  # the echo first: it shows what was heard


# --- "yozmoqda…": quick answers keep it, long work does not (T34b, owner 2026-10-24) -----------------------------
async def _typing(deps: DevDeps) -> list[OutboxMessage]:
    return [row for row in await messages(deps) if row.op == "chat_action"]


async def _intake_typing(deps: DevDeps, fakes: Fakes, *, sending: bool = False) -> None:
    """The inbox's "typing…" for the message, as intake queues it; `sending`: the sender has it in flight."""
    async with deps.db.uow() as s:
        await outbox_service.request_typing(s, bot=deps.bot, chat_id=DEV_GROUP_ID, ref="update-1", at=fakes.clock.now())
    if sending:
        [row] = await _typing(deps)
        async with deps.db.uow() as s:
            assert await outbox_store.claim_message(s, row.id, at=fakes.clock.now()) is not None


@pytest.mark.parametrize("sending", [False, True], ids=["pending", "in_flight"])
async def test_a_burst_routed_to_a_turn_stops_the_intake_typing(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState, sending: bool
) -> None:
    await _intake_typing(dev_deps, fakes, sending=sending)
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_part(1, text="BonviHR'da login sahifasini tuzat"))
    fakes.agents.script_turn(Pause())

    await service.route_turn(dev_deps, state, item)

    [typing] = await _typing(dev_deps)
    assert (typing.status, typing.last_error_code) == ("cancelled", "typing_ended")  # never re-armed, even in flight


async def test_a_report_question_routes_without_stopping_the_typing(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await _intake_typing(dev_deps, fakes)
    fakes.assistant.reply_with("routing", routing_reply(PROJECT, 0.95, intent="report_project"))
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_part(1, text="BonviHR qanday?"))

    routed = await service.route_turn(dev_deps, state, item)

    assert routed.submitted is None  # the digest answers it; its reply ends the typing when it is sent
    [typing] = await _typing(dev_deps)
    assert typing.status == "pending"


async def test_with_typing_for_work_on_the_typing_stays_and_each_step_renews_it(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    await add_setting_row(dev_deps.db, "ack.typing_for_work", True, at=fakes.clock.now())
    await _intake_typing(dev_deps, fakes)
    async with dev_deps.db.uow() as s:
        item = await make_closed_burst(s, make_part(1, text="BonviHR'da login sahifasini tuzat"))
    fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), Pause())

    await service.route_turn(dev_deps, state, item)
    assert [row.status for row in await _typing(dev_deps)] == ["pending"]  # not stopped at routing
    await service.schedule_turns(dev_deps, state)

    async def renewed() -> bool:
        return any(row.dedupe_key.startswith("ack:turn-") for row in await _typing(dev_deps))

    await until(renewed, what="typing renewed by the step")


async def test_with_typing_for_work_off_a_working_turn_never_asks_for_typing(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), ToolStep("Grep", {"pattern": "x"}), Pause())
    turn_id = await start(dev_deps, state)

    async def two_steps() -> bool:
        return (await turn(dev_deps, turn_id)).steps == 2

    await until(two_steps, what="two steps recorded")
    assert await _typing(dev_deps) == []


# --- "X yangi sessiya" (SPEC §6, T34b) --------------------------------------------------------------------------
async def test_yangi_sessiya_closes_the_idle_session_and_the_next_turn_starts_fresh_with_the_note(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(result())
    first = await start(dev_deps, state)
    await finish(dev_deps, fakes, first)
    old_session = session_of(fakes)

    reply = await service.new_session(dev_deps, state, PROJECT)

    assert reply == S.NEW_SESSION_READY(project=PROJECT)
    await until(lambda: fakes.agents.closed == [old_session], what="the old process closed")
    async with dev_deps.db.read() as s:
        old = await store.find_latest_session(s, project_id=PROJECT, executor=domain.EXECUTOR, kind="coding")
    assert old is not None
    assert (old.is_current, old.status) == (False, "closed")  # never resumed again
    write_transcript(dev_deps.paths.home, old_session)  # even with its transcript still on disk

    fakes.agents.script_turn(Pause())
    await start(dev_deps, state, make_turn_request(text="endi testlarni yoz"))
    assert fakes.agents.opened[-1][1] is None  # a new session, not a resume
    assert session_of(fakes) != old_session
    seeded = fakes.agents.sends[-1][1].text
    assert seeded.startswith("[Yordamchi] This is a new session: the owner asked to start afresh")
    assert "Goal: fix login" in seeded
    assert seeded.rstrip().endswith("endi testlarni yoz")
    assert S.SESSION_RENEWED(project=PROJECT) not in await texts(dev_deps)  # the owner was told once, by the reply


async def test_yangi_sessiya_lets_a_working_turn_finish_in_the_old_session(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    fakes.agents.script_turn(Pause(), result())
    first = await start(dev_deps, state)
    old_session = session_of(fakes)

    assert await service.new_session(dev_deps, state, PROJECT) == S.NEW_SESSION_AFTER_TURN(project=PROJECT)
    assert fakes.agents.closed == []  # the work is never cut off
    assert [row.is_current for row in await sessions(dev_deps)] == [True]  # it waits for the turn to end
    await fakes.agents.release(old_session)
    await finish(dev_deps, fakes, first)
    await service.schedule_turns(dev_deps, state)
    await until(lambda: fakes.agents.closed == [old_session], what="closed once its turn ended")

    fakes.agents.script_turn(Pause())
    await start(dev_deps, state, make_turn_request(text="keyingisi"))
    assert fakes.agents.opened[-1][1] is None
    assert session_of(fakes) != old_session


async def test_yangi_sessiya_waits_for_a_turn_waiting_on_the_owner(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], state: TurnsState
) -> None:
    """A turn waiting for a decision continues in the session that holds its context: the renewal waits for it."""
    fakes.agents.script_turn(result())
    first = await start(dev_deps, state)
    await finish(dev_deps, fakes, first)
    async with dev_deps.db.uow() as s:
        await make_turn(s, PROJECT, status=TurnStatus.WAITING_DECISION, at=fakes.clock.now())

    assert await service.new_session(dev_deps, state, PROJECT) == S.NEW_SESSION_AFTER_TURN(project=PROJECT)
    assert fakes.agents.closed == []
    assert [row.is_current for row in await sessions(dev_deps)] == [True]
    assert state.renewing == {PROJECT}


async def test_yangi_sessiya_with_no_session_yet_says_the_next_turn_starts_one(
    dev_deps: DevDeps, folders: dict[str, Path], state: TurnsState
) -> None:
    assert await service.new_session(dev_deps, state, PROJECT) == S.NEW_SESSION_READY_NO_NOTE(project=PROJECT)


async def test_yangi_sessiya_for_an_unknown_project_raises(dev_deps: DevDeps, state: TurnsState) -> None:
    with pytest.raises(ProjectNotFound):
        await service.new_session(dev_deps, state, "NoSuchProject")
