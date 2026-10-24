"""digest.service on a real SQLite file, `FakeClock`, `FakeAssistant`, `FakeGit` and a running `FakeAgentRuntime`
turn (SPEC §3.2.4; UC-15, D10, NFR-SPD-01).

The report path is held to its two promises: an answer within 4 s (virtual time, plus a wall-clock bound) even while
the project's coding turn runs, and no use of the executor at all — the report's deps carry an executor that fails
the test on any attribute access, while the running turn keeps the real fake.
"""

import asyncio
import inspect
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from typing import cast

import pytest

from tests.factories import (
    FAKE_KEYS,
    Fakes,
    make_burst_row,
    make_digest_state,
    make_project,
    make_project_folder,
    make_report_question,
    make_server_check,
    make_turn,
    make_turn_request,
    projects_root_of,
    script_git,
)
from tests.fakes.agent import Pause, ToolStep, done_result
from tests.fakes.assistant import FakeAssistant
from tests.fakes.clock import FakeClock
from tests.owner_text import shown
from yordamchi import prompts
from yordamchi.core.errors import CallTimedOut, GitFailed, ProjectNotFound, ProviderUnavailable
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.digest import service
from yordamchi.dev.digest import store as digest_store
from yordamchi.dev.digest.domain import Digest, ReportMode, ReportScope, ServerCheck
from yordamchi.dev.digest.service import DigestState
from yordamchi.dev.routing.domain import Intent
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns.domain import TurnKind, TurnStatus, TurnSummary
from yordamchi.dev.turns.service import TurnsState
from yordamchi.ports.agent import AgentRuntime, TurnResult
from yordamchi.ports.assistant import AssistantReply, AssistantRequest
from yordamchi.ports.git import Commit, FileChange
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.models import OutboxMessage

PROJECT = "BonviHR"
OTHER = "HRMentor"
HEAD = "a1b2c3d4e5f6"
NEW_HEAD = "f6e5d4c3b2a1"
GRACE = 1.5  # turns.result_grace_s
FINAL = (
    "Tayyor.\nNatija: Login sahifasidagi xato tuzatildi.\nTestlar: o'tdi (42 passed)\n"
    "<handoff>\nGoal: fix login\nDecisions: none\nState: pushed\nNext: nothing\n</handoff>"
)
NOTE = "Goal: fix login\nDecisions: none\nState: fixed and pushed\nNext: nothing"


class ExecutorTripwire:
    """The report path's executor: any use of it fails the test (UC-15: reports never touch a session)."""

    def __getattr__(self, name: str) -> object:
        msg = f"the report path touched the executor: {name}"
        raise AssertionError(msg)


class SlowAssistant(FakeAssistant):
    """A warm report client that takes `latency_s` of virtual time, cut at the ask's own timeout as the adapter
    does (`CallTimedOut`)."""

    def __init__(self, clock: FakeClock, latency_s: float) -> None:
        super().__init__()
        self.clock = clock
        self.latency_s = latency_s
        self.timeouts: list[float] = []

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        self.timeouts.append(timeout)
        self.clock.set(self.clock.now() + timedelta(seconds=min(self.latency_s, timeout)))
        if self.latency_s > timeout:
            raise CallTimedOut(operation="assistant_report")
        return await super().ask(request, timeout=timeout)


# --- fixtures and helpers -------------------------------------------------------------------------------------------
@pytest.fixture
def root(paths: Paths) -> Path:
    return projects_root_of(paths)


@pytest.fixture
async def folders(dev_deps: DevDeps, fakes: Fakes, root: Path) -> dict[str, Path]:
    """Two registered projects; BonviHR has one commit an hour ago and is pushed."""
    found: dict[str, Path] = {}
    for name in (PROJECT, OTHER):
        folder = make_project_folder(root, name, git_dir=True)
        async with dev_deps.db.uow() as s:
            await make_project(s, name, at=fakes.clock.now(), path=folder)
        found[name] = folder
    hour_ago = fakes.clock.now() - timedelta(hours=1)
    script_git(fakes.git, found[PROJECT], head=HEAD, commits=(Commit(HEAD, "fix login", hour_ago),))
    old = fakes.clock.now() - timedelta(days=10)
    script_git(fakes.git, found[OTHER], head="0ld0ld0ld", commits=(), upstream=None)
    fakes.git.commits[(found[OTHER], "HEAD")] = Commit("0ld0ld0ld", "init", old)
    return found


@pytest.fixture
async def turns_state(dev_deps: DevDeps) -> AsyncIterator[TurnsState]:
    state = TurnsState()
    yield state
    await turns.shutdown_turns(dev_deps, state)


@pytest.fixture
def digest_state(turns_state: TurnsState) -> DigestState:
    return make_digest_state(turns_state)


def report_deps(deps: DevDeps, assistant: FakeAssistant | None = None) -> DevDeps:
    """The deps the report path gets: the same DB and clock, an executor that must never be touched."""
    tripwire = cast(AgentRuntime, ExecutorTripwire())
    return replace(deps, agents=tripwire) if assistant is None else replace(deps, agents=tripwire, assistant=assistant)


async def until(check: Callable[[], object], *, timeout: float = 3.0, what: str = "condition") -> None:
    """Wait (real time) for background tasks to reach a state."""
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


async def project_turns(deps: DevDeps, project: str = PROJECT) -> tuple[TurnSummary, ...]:
    return await turns.list_recent_turns(deps, project, limit=10)


async def turn_status(deps: DevDeps, turn_id: int) -> TurnStatus:
    return next(turn.status for turn in await project_turns(deps) if turn.turn_id == turn_id)


async def start_turn(deps: DevDeps, state: TurnsState) -> int:
    """Submit, run a scheduler pass, and wait until the turn runs with its input delivered."""
    submitted = await turns.submit_turn(deps, state, make_turn_request())
    await turns.schedule_turns(deps, state)

    async def running() -> bool:
        return await turn_status(deps, submitted.turn_id) is TurnStatus.RUNNING

    await until(running, what="turn running")
    return submitted.turn_id


async def finish_turn(deps: DevDeps, fakes: Fakes, turn_id: int) -> None:
    await until(lambda: fakes.clock.pending_sleepers > 0, what="result grace armed")
    await fakes.clock.advance(GRACE)

    async def done() -> bool:
        return await turn_status(deps, turn_id) is TurnStatus.DONE

    await until(done, what="turn done")


def result(text: str = FINAL) -> TurnResult:
    return replace(done_result(text), cost_usd_micros=1_000)


def executor_calls(fakes: Fakes) -> list[list[object]]:
    agents = fakes.agents
    lists: list[list[object]] = [
        list(agents.opened),
        list(agents.sends),
        list(agents.answers),
        list(agents.interrupts),
        list(agents.models),
        list(agents.closed),
        list(agents.guard_calls),
    ]
    return lists


async def outbox_rows(deps: DevDeps) -> list[OutboxMessage]:
    async with deps.db.read() as s:
        return await outbox_store.list_outbox_messages(s, deps.bot.value)


def text_of(row: OutboxMessage) -> str:
    payload = row.payload_json
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


async def stored(deps: DevDeps, project: str = PROJECT) -> Digest:
    digest = await service.get_digest(deps, project)
    assert digest is not None
    return digest


def later(fakes: Fakes, **delta: float) -> None:
    """Move virtual time without waking anyone (the running turn's own timers stay put)."""
    fakes.clock.set(fakes.clock.now() + timedelta(**delta))


# --- the fast path: ≤ 4 s while a turn runs, never the executor (UC-15 AC, NFR-SPD-01) ------------------------------
async def test_a_free_form_report_answers_within_4_s_while_the_turn_runs_and_never_touches_the_executor(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], turns_state: TurnsState, digest_state: DigestState
) -> None:
    fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), Pause())
    turn_id = await start_turn(dev_deps, turns_state)
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    later(fakes, minutes=12)
    before = executor_calls(fakes)
    slow = SlowAssistant(fakes.clock, latency_s=2.2)  # a warm ask (T12 measured 1–3 s; haiku 1.7–4.6 s, 2026-10-24)
    slow.reply_with("report", "Login ustida ishlayapti, testlar hali ishga tushmagan.")
    question = make_report_question("BonviHR'da login qachon tugaydi?")

    started, wall = fakes.clock.now(), time.monotonic()
    answer = await service.answer_report(report_deps(dev_deps, slow), digest_state, question)

    assert fakes.clock.now() - started <= timedelta(seconds=4)
    assert time.monotonic() - wall < 4
    assert executor_calls(fakes) == before  # nothing reached the executor
    assert fakes.agents.busy(fakes.agents.sends[0][0])  # and the turn is still working
    assert await turn_status(dev_deps, turn_id) is TurnStatus.RUNNING
    assert answer.plan.mode is ReportMode.ASSISTANT
    assert answer.provider == "claude"
    assert slow.timeouts == [3.5]  # timeouts.report_s, within the 3.5 s budget
    assert answer.text is not None
    assert shown(answer.text).splitlines() == [
        "BonviHR · hozir: «Login sahifasini tuzat», ishlayapti, 12 daq",
        "Login ustida ishlayapti, testlar hali ishga tushmagan.",
        "Ma'lumot: 12 daqiqa oldin",
    ]
    request = slow.requests[0]
    assert (request.kind, request.model, request.effort) == ("report", "haiku", "low")
    assert request.system_prompt == prompts.render("dev/report_system")
    assert request.instructions.startswith("Current instructions from the owner:")
    assert json.loads(request.prompt)["projects"][0]["now_running"][0]["state"] == "running"
    reports = [row for row in await outbox_rows(dev_deps) if row.kind == "report"]
    assert [text_of(row) for row in reports] == [answer.text]
    assert reports[0].priority == 1


async def test_a_plain_status_question_gets_the_template_at_once_while_the_turn_runs(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], turns_state: TurnsState, digest_state: DigestState
) -> None:
    fakes.agents.script_turn(Pause())
    await start_turn(dev_deps, turns_state)
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    later(fakes, minutes=4)
    before = executor_calls(fakes)

    started = fakes.clock.now()
    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("BonviHR qanday?"))

    assert fakes.clock.now() == started
    assert executor_calls(fakes) == before
    assert fakes.assistant.requests == []
    assert answer.text is not None
    assert shown(answer.text).splitlines() == [
        "BonviHR · hozir: «Login sahifasini tuzat», ishlayapti, 4 daq",
        "Hali natija yo'q",
        "Git: main · origin/main bilan bir xil · toza",
        "Bugun: 1 commit — «fix login»",
        "Ma'lumot: 4 daqiqa oldin",
    ]


async def test_a_slow_assistant_is_cut_at_the_budget_and_the_template_answers_within_4_s(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    slow = SlowAssistant(fakes.clock, latency_s=9.0)
    started = fakes.clock.now()

    answer = await service.answer_report(
        report_deps(dev_deps, slow), digest_state, make_report_question("BonviHR'da login qachon tugaydi?")
    )

    assert fakes.clock.now() - started <= timedelta(seconds=4)
    assert answer.provider is None
    assert answer.text is not None
    assert shown(answer.text).splitlines()[0] == "BonviHR · hozir ish yo'q"
    assert "Git: main · origin/main bilan bir xil · toza" in shown(answer.text).splitlines()


async def test_an_assistant_failure_answers_with_the_template(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    fakes.assistant.reply_with("report", ProviderUnavailable(provider="claude"))

    answer = await service.answer_report(
        report_deps(dev_deps), digest_state, make_report_question("login qachon tugaydi?")
    )

    assert (answer.plan.mode, answer.provider) == (ReportMode.ASSISTANT, None)
    assert answer.text is not None
    assert shown(answer.text).splitlines()[-1] == "Ma'lumot: hozirgina"


async def test_while_a_handoff_note_is_being_written_free_form_reports_take_the_template(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    digest_state.synthesizing = 1  # the warm report client is busy: an ask would queue behind it

    answer = await service.answer_report(
        report_deps(dev_deps), digest_state, make_report_question("login qachon tugaydi?")
    )

    assert fakes.assistant.requests == []
    assert answer.provider is None


async def test_the_assistant_is_warmed_with_the_profile_every_ask_uses(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.warm_report(dev_deps)
    await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("login qachon tugaydi?"))

    assert fakes.assistant.warmed == [fakes.assistant.requests[0].profile]


async def test_a_report_on_an_unknown_project_raises_project_not_found(
    dev_deps: DevDeps, folders: dict[str, Path], digest_state: DigestState
) -> None:
    with pytest.raises(ProjectNotFound):
        await service.answer_report(dev_deps, digest_state, make_report_question("Yoq qanday?", "Yoq"))


async def test_the_turn_end_hook_of_an_unregistered_project_writes_nothing(
    dev_deps: DevDeps, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.refresh_after_turn(dev_deps, "smoke", state=digest_state)

    assert await service.get_digest(dev_deps, "smoke") is None


# --- freshness and staleness ----------------------------------------------------------------------------------------
async def test_every_report_ends_with_how_fresh_its_data_is(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    later(fakes, minutes=3)

    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("holat"))

    assert answer.text is not None
    assert shown(answer.text).splitlines()[-1] == "Ma'lumot: 3 daqiqa oldin"
    assert not answer.refreshed


async def test_a_stale_digest_is_refreshed_first_and_the_report_says_so(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    later(fakes, minutes=45)  # past digest.stale_after_min = 30
    now = fakes.clock.now()
    commits = (
        Commit(NEW_HEAD, "add report", now - timedelta(minutes=5)),
        Commit(HEAD, "fix login", now - timedelta(minutes=46)),
    )
    script_git(fakes.git, folders[PROJECT], head=NEW_HEAD, commits=commits, ahead=1)
    fakes.git.calls.clear()

    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("BonviHR qanday?"))

    assert answer.refreshed
    assert [call[0] for call in fakes.git.calls][:2] == ["status", "log"]
    assert answer.text is not None
    lines = shown(answer.text).splitlines()
    assert "Git: main · 1 commit push qilinmagan · toza" in lines
    assert "Bugun: 2 commit — «add report»; «fix login»" in lines
    assert lines[-1] == "Ma'lumot: eskirgan edi (45 daqiqa oldin) — hozir yangiladim"
    digest = await stored(dev_deps)
    assert (digest.checked_at, digest.git.head) == (now, NEW_HEAD)


async def test_a_first_report_collects_the_digest_and_says_so(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("holat"))

    assert answer.text is not None
    assert shown(answer.text).splitlines()[-1] == "Ma'lumot: hozir yig'dim"


async def test_a_stale_digest_whose_refresh_times_out_answers_from_the_old_data_and_says_so(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    checked = fakes.clock.now()
    later(fakes, minutes=45)
    fakes.git.failures["status"] = CallTimedOut(operation="git_status")

    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("holat"))

    assert not answer.refreshed
    assert answer.text is not None
    assert shown(answer.text).splitlines()[-1] == "Ma'lumot: 45 daqiqa oldin — yangilab bo'lmadi"
    assert (await stored(dev_deps)).checked_at == checked  # nothing was written


async def test_a_report_never_waits_for_a_refresh_already_running(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    later(fakes, minutes=45)

    async with digest_state.lock_of(PROJECT):  # a background refresh (say, writing a handoff note) holds it
        answer = await asyncio.wait_for(
            service.answer_report(report_deps(dev_deps), digest_state, make_report_question("holat")), timeout=2
        )

    assert answer.text is not None
    assert shown(answer.text).splitlines()[-1] == "Ma'lumot: 45 daqiqa oldin — yangilab bo'lmadi"


async def test_an_unreadable_repository_is_reported_as_such(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    fakes.git.failures["status"] = GitFailed(operation="git_status", reason="not_a_repo")

    digest = await service.refresh_digest(dev_deps, digest_state, PROJECT)
    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("holat"))

    assert digest.git.error_code == "git_failed"
    assert answer.text is not None
    assert "Git: o'qib bo'lmadi" in shown(answer.text).splitlines()


async def test_keys_in_commit_subjects_never_reach_the_row_or_the_owner(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    key = FAKE_KEYS["github_token"]
    script_git(fakes.git, folders[PROJECT], head=HEAD, commits=(Commit(HEAD, f"set token {key}", fakes.clock.now()),))

    await service.refresh_digest(dev_deps, digest_state, PROJECT)
    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("holat"))

    async with dev_deps.db.read() as s:
        row = await digest_store.get_digest(s, PROJECT)
    assert row is not None
    assert key not in json.dumps(row.recent_commits_json)
    assert row.git_head_subject is not None
    assert key not in row.git_head_subject
    assert answer.text is not None
    assert key not in answer.text


# --- refresh after a turn, the handoff note -------------------------------------------------------------------------
async def test_a_finished_turn_refreshes_its_project_digest(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], turns_state: TurnsState, digest_state: DigestState
) -> None:
    turns_state.turn_ended = partial(service.refresh_after_turn, state=digest_state)
    fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), result())
    turn_id = await start_turn(dev_deps, turns_state)
    now = fakes.clock.now()
    script_git(fakes.git, folders[PROJECT], head=NEW_HEAD, commits=(Commit(NEW_HEAD, "fix login page", now),))
    fakes.git.changes[folders[PROJECT]] = [FileChange("M", "src/app/login.py")]

    await finish_turn(dev_deps, fakes, turn_id)

    async def refreshed() -> bool:
        digest = await service.get_digest(dev_deps, PROJECT)
        return digest is not None and digest.last_turn_id == turn_id

    await until(refreshed, what="digest refreshed after the turn")
    digest = await stored(dev_deps)
    assert digest.last_done is not None
    assert (digest.last_done.natija, digest.last_done.tests) == ("Login sahifasidagi xato tuzatildi.", "passed")
    assert digest.git.head == NEW_HEAD
    assert [commit.subject for commit in digest.commits] == ["fix login page"]
    assert digest.files == ("src/app/login.py",)
    assert digest.handoff is not None
    assert (digest.handoff.source, digest.handoff.turn_id) == ("executor", turn_id)
    assert digest.handoff.text.startswith("Goal: fix login\nDecisions: none\nState: pushed\nNext: nothing")
    assert "Files touched: src/app/login.py" in digest.handoff.text
    assert fakes.assistant.requests == []  # the executor wrote the note: nothing to synthesize
    async with dev_deps.db.read() as s:
        row = await digest_store.get_digest(s, PROJECT)
    assert row is not None
    assert (row.last_natija, row.last_turn_id, row.handoff_note_source) == (
        "Login sahifasidagi xato tuzatildi.",
        turn_id,
        "executor",
    )


async def test_a_stopped_turn_refreshes_its_project_digest_too(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], turns_state: TurnsState, digest_state: DigestState
) -> None:
    turns_state.turn_ended = partial(service.refresh_after_turn, state=digest_state)
    fakes.agents.script_turn(Pause())
    turn_id = await start_turn(dev_deps, turns_state)

    await turns.stop_turn(dev_deps, turns_state, PROJECT)

    async def refreshed() -> bool:
        digest = await service.get_digest(dev_deps, PROJECT)
        return digest is not None and digest.last_turn_id == turn_id

    await until(refreshed, what="digest refreshed after the stop")
    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("holat"))
    assert answer.text is not None
    assert shown(answer.text).splitlines()[:2] == [
        "BonviHR · hozir ish yo'q",
        "Oxirgi topshiriq (14:00) to'xtatilgan: «Login sahifasini tuzat»",
    ]


async def test_a_failed_turn_refreshes_its_project_digest_too(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], turns_state: TurnsState, digest_state: DigestState
) -> None:
    turns_state.turn_ended = partial(service.refresh_after_turn, state=digest_state)
    fakes.agents.script_turn(replace(done_result("xato"), is_error=True, subtype="error_during_execution"))
    turn_id = await start_turn(dev_deps, turns_state)

    async def refreshed() -> bool:
        digest = await service.get_digest(dev_deps, PROJECT)
        return digest is not None and digest.last_turn_id == turn_id

    await until(refreshed, what="digest refreshed after the failure")
    digest = await stored(dev_deps)
    assert [(result.turn_id, result.status) for result in digest.results] == [(turn_id, TurnStatus.FAILED)]
    assert digest.last_done is None


async def test_a_turn_without_its_handoff_block_gets_a_note_written_by_the_report_assistant(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], turns_state: TurnsState, digest_state: DigestState
) -> None:
    turns_state.turn_ended = partial(service.refresh_after_turn, state=digest_state)
    fakes.assistant.reply_with("report", NOTE)
    fakes.agents.script_turn(
        ToolStep("Read", {"file_path": "README.md"}, tool_use_id="t1"),
        ToolStep("Grep", {"pattern": "login"}, tool_use_id="t2"),
        result("Tayyor.\nNatija: Login tuzatildi.\nTestlar: o'tdi (3 passed)"),
    )
    turn_id = await start_turn(dev_deps, turns_state)
    now = fakes.clock.now()
    script_git(fakes.git, folders[PROJECT], head=NEW_HEAD, commits=(Commit(NEW_HEAD, "fix login", now),))
    fakes.git.changes[folders[PROJECT]] = [FileChange("M", "src/app/login.py")]

    await finish_turn(dev_deps, fakes, turn_id)

    async def synthesized() -> bool:
        digest = await service.get_digest(dev_deps, PROJECT)
        return digest is not None and digest.handoff is not None and digest.handoff.turn_id == turn_id

    await until(synthesized, what="handoff note synthesized")
    digest = await stored(dev_deps)
    assert digest.handoff is not None
    assert digest.handoff.source == "synthesized"
    assert digest.handoff.text == f"{NOTE}\n\nFiles touched: src/app/login.py\n\nCommits: {NEW_HEAD[:7]}"
    request = fakes.assistant.requests[0]
    assert (request.kind, request.system_prompt) == ("report", prompts.render("dev/report_system"))
    payload = json.loads(request.prompt)
    assert payload["task"] == "handoff_note"
    assert payload["progress"] == ["Read", "Grep"]  # the turn's last progress lines
    assert payload["title"] == "Login sahifasini tuzat"
    assert digest_state.synthesizing == 0


async def test_a_failed_synthesis_keeps_the_last_steps_in_the_note(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], turns_state: TurnsState, digest_state: DigestState
) -> None:
    turns_state.turn_ended = partial(service.refresh_after_turn, state=digest_state)
    fakes.assistant.reply_with("report", ProviderUnavailable(provider="claude"))
    fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), result("Natija: Bajarildi."))
    turn_id = await start_turn(dev_deps, turns_state)

    await finish_turn(dev_deps, fakes, turn_id)

    async def written() -> bool:
        digest = await service.get_digest(dev_deps, PROJECT)
        return digest is not None and digest.handoff is not None and digest.handoff.turn_id == turn_id

    await until(written, what="fallback note written")
    digest = await stored(dev_deps)
    assert digest.handoff is not None
    assert digest.handoff.source == "synthesized"
    assert digest.handoff.text.startswith(
        "Goal: Login sahifasini tuzat\nDecisions: unknown (the task ended without a handoff block)\n"
        "State: unknown; the task's last steps were:\n- Read\n"
    )
    assert digest_state.synthesizing == 0


# --- a fresh look: "Tekshiryapman" ≤ 2 s, then an investigation (UC-15 F) -------------------------------------------
async def test_a_deep_question_says_tekshiryapman_at_once_and_queues_a_read_only_investigation(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    started, wall = fakes.clock.now(), time.monotonic()

    answer = await service.answer_report(
        report_deps(dev_deps), digest_state, make_report_question("nega test yiqilyapti?")
    )

    assert fakes.clock.now() == started
    assert time.monotonic() - wall < 2
    assert (answer.plan.mode, answer.plan.reason) == (ReportMode.INVESTIGATE, "deep_words")
    assert answer.text == "BonviHR · Tekshiryapman (~3 daqiqa)"
    checking = [row for row in await outbox_rows(dev_deps) if text_of(row) == answer.text]
    assert [(row.kind, row.priority) for row in checking] == [("report", 0)]
    assert answer.investigation is not None
    investigation = next(t for t in await project_turns(dev_deps) if t.turn_id == answer.investigation.turn_id)
    assert (investigation.kind, investigation.status, investigation.title) == (
        TurnKind.INVESTIGATE,
        TurnStatus.QUEUED,
        "nega test yiqilyapti?",
    )
    assert digest_state.turns.wake.is_set()  # the scheduler starts it now
    assert fakes.assistant.requests == []
    assert fakes.git.calls == []


async def test_routing_needs_fresh_sends_even_a_status_question_to_an_investigation(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    async with dev_deps.db.uow() as s:
        burst_id = await make_burst_row(s, at=fakes.clock.now())
    question = make_report_question("BonviHR qanday?", needs_fresh=True, burst_id=burst_id)

    first = await service.answer_report(report_deps(dev_deps), digest_state, question)
    again = await service.answer_report(report_deps(dev_deps), digest_state, question)  # a redelivered burst

    assert first.investigation is not None
    assert again.investigation is not None
    assert again.investigation.turn_id == first.investigation.turn_id
    checking = [row for row in await outbox_rows(dev_deps) if text_of(row) == "BonviHR · Tekshiryapman (~3 daqiqa)"]
    assert len(checking) == 1


# --- every project at once ------------------------------------------------------------------------------------------
@pytest.fixture
async def three_projects(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], root: Path, digest_state: DigestState
) -> dict[str, Path]:
    """BonviHR (a commit today, a running turn), HRMentor (two commits today), Wunderkind (a commit 3 days ago)."""
    now = fakes.clock.now()
    today = (
        Commit("b2b2b2", "add tests", now - timedelta(minutes=30)),
        Commit("b1b1b1", "init", now - timedelta(minutes=50)),
    )
    script_git(fakes.git, folders[OTHER], head="b2b2b2", commits=today)
    third = make_project_folder(root, "Wunderkind", git_dir=True)
    async with dev_deps.db.uow() as s:
        await make_project(s, "Wunderkind", at=now, path=third)
        await make_turn(s, PROJECT, at=now - timedelta(minutes=7), status=TurnStatus.RUNNING)
    script_git(fakes.git, third, head="c3c3c3", commits=(Commit("c3c3c3", "docs", now - timedelta(days=3)),))
    assert await service.poll_digests(dev_deps, digest_state) == 3
    return {**folders, "Wunderkind": third}


async def test_a_cross_project_question_is_answered_from_every_digest_at_once(
    dev_deps: DevDeps, fakes: Fakes, three_projects: dict[str, Path], digest_state: DigestState
) -> None:
    later(fakes, minutes=5)
    fakes.git.calls.clear()

    answer = await service.answer_report(
        report_deps(dev_deps), digest_state, make_report_question("bugun qaysi loyihalarda nima o'zgardi?", None)
    )

    assert (answer.plan.scope, answer.plan.mode) == (ReportScope.ALL, ReportMode.TEMPLATE)
    assert fakes.git.calls == []  # fresh digests: no git, no model
    assert fakes.assistant.requests == []
    assert answer.text is not None
    assert shown(answer.text).splitlines() == [
        "Bugun o'zgargan loyihalar: 2 ta",
        "• BonviHR: 1 commit (oxirgisi: «fix login»)",
        "• HRMentor: 2 commit (oxirgisi: «add tests»)",
        "Hozir ishlayapti: BonviHR («Login sahifasini tuzat», 12 daq)",
        "Qolgan 1 ta loyihada bugun o'zgarish yo'q",
        "Ma'lumot: 5 daqiqa oldin",
    ]
    rows = [row for row in await outbox_rows(dev_deps) if row.kind == "report"]
    assert [text_of(row) for row in rows] == [answer.text]


async def test_a_free_form_cross_project_question_gives_the_assistant_every_digest(
    dev_deps: DevDeps, fakes: Fakes, three_projects: dict[str, Path], digest_state: DigestState
) -> None:
    fakes.assistant.reply_with("report", "Eng ko'p ish HRMentor'da: bugun 2 commit.")

    answer = await service.answer_report(
        report_deps(dev_deps), digest_state, make_report_question("qaysi loyihada eng ko'p ish bo'ldi?", None)
    )

    assert answer.text == "Eng ko'p ish HRMentor'da: bugun 2 commit.\nMa'lumot: hozirgina"
    payload = json.loads(fakes.assistant.requests[0].prompt)
    assert [project["project"] for project in payload["projects"]] == [PROJECT, OTHER, "Wunderkind"]
    assert payload["projects"][0]["now_running"][0]["title"] == "Login sahifasini tuzat"


async def test_small_talk_routed_to_a_report_is_answered_alone_without_status(
    dev_deps: DevDeps, fakes: Fakes, three_projects: dict[str, Path], digest_state: DigestState
) -> None:
    """Owner, 2026-10-24: "oddiy gap uchun ham loyihalar haqida ma'lumot yoki holat haqida hisobotni tiqishtirmasin"."""
    fakes.assistant.reply_with("report", "[chat]\nVa alaykum assalom! Nima qilamiz?")

    answer = await service.answer_report(report_deps(dev_deps), digest_state, make_report_question("Salomlarim", None))

    assert answer.text == "Va alaykum assalom! Nima qilamiz?"  # no project, no "Ma'lumot: …" line
    assert answer.provider == "claude"


async def test_stale_digests_of_active_projects_are_refreshed_for_a_cross_project_answer(
    dev_deps: DevDeps, fakes: Fakes, three_projects: dict[str, Path], digest_state: DigestState
) -> None:
    later(fakes, minutes=40)

    answer = await service.answer_report(
        report_deps(dev_deps), digest_state, make_report_question("hammasi qanday?", None)
    )

    assert answer.refreshed
    assert answer.text is not None
    assert shown(answer.text).splitlines()[-1] == "Ma'lumot: eskirgan edi (40 daqiqa oldin) — hozir yangiladim"


# --- polling git ----------------------------------------------------------------------------------------------------
def status_calls(fakes: Fakes, folder: Path) -> int:
    return sum(1 for method, repo, _ in fakes.git.calls if method == "status" and repo == folder)


async def test_the_poll_builds_every_digest_then_refreshes_only_on_a_git_change(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    assert await service.poll_digests(dev_deps, digest_state) == 2
    fakes.git.calls.clear()

    later(fakes, seconds=60)  # not due yet (schedule.digest_active_poll_s = 120)
    assert await service.poll_digests(dev_deps, digest_state) == 0
    assert fakes.git.calls == []

    later(fakes, seconds=60)  # BonviHR (active) is due; HRMentor (idle 10 days) is not
    assert await service.poll_digests(dev_deps, digest_state) == 0
    assert (status_calls(fakes, folders[PROJECT]), status_calls(fakes, folders[OTHER])) == (1, 0)
    assert [call[0] for call in fakes.git.calls] == ["status"]  # the cheap poll only
    assert (await stored(dev_deps)).checked_at == fakes.clock.now()

    script_git(
        fakes.git, folders[PROJECT], head=HEAD, commits=(Commit(HEAD, "fix login", fakes.clock.now()),), changed=2
    )
    later(fakes, seconds=120)
    assert await service.poll_digests(dev_deps, digest_state) == 1
    assert (await stored(dev_deps)).git.dirty == 2

    later(fakes, minutes=60)  # schedule.digest_idle_poll_min = 60
    fakes.git.calls.clear()
    await service.poll_digests(dev_deps, digest_state)
    assert status_calls(fakes, folders[OTHER]) == 1


async def test_the_poll_loop_sleeps_digest_active_poll_s_between_passes(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    async with asyncio.TaskGroup() as group:
        loop = group.create_task(service.run_digest_poll(dev_deps, digest_state))
        await until(lambda: fakes.clock.pending_sleepers > 0, what="first pass done")
        assert fakes.clock.sleeps == [120]
        assert {digest.project_id for digest in await service.list_digests(dev_deps)} == {PROJECT, OTHER}
        loop.cancel()


# --- servers (T31 hands the checks in) ------------------------------------------------------------------------------
async def test_server_status_without_fresh_checks_is_left_to_the_on_demand_check(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    question = make_report_question("serverlar qanday?", None, intent=Intent.SERVER_STATUS)

    assert (await service.answer_report(dev_deps, digest_state, question)).needs_server_check

    old = [make_server_check("web", at=fakes.clock.now() - timedelta(minutes=45))]

    async def stale(deps: DevDeps) -> list[ServerCheck]:
        return old

    digest_state.server_checks = stale
    answer = await service.answer_report(dev_deps, digest_state, question)
    assert (answer.needs_server_check, answer.text) == (True, None)
    assert [row for row in await outbox_rows(dev_deps) if row.kind == "report"] == []


async def test_server_status_is_answered_from_fresh_checks(
    dev_deps: DevDeps, fakes: Fakes, folders: dict[str, Path], digest_state: DigestState
) -> None:
    checks = [
        make_server_check("web", at=fakes.clock.now() - timedelta(minutes=5)),
        make_server_check("db", at=fakes.clock.now() - timedelta(minutes=12), ok=False, summary="disk 95%"),
    ]

    async def latest(deps: DevDeps) -> list[ServerCheck]:
        return checks

    digest_state.server_checks = latest
    answer = await service.answer_report(
        dev_deps, digest_state, make_report_question("serverlar qanday?", None, intent=Intent.SERVER_STATUS)
    )

    assert answer.text is not None
    assert shown(answer.text) == (
        "Serverlar: db ✗ disk 95% (12 daqiqa oldin), web ✓ (5 daqiqa oldin)\nMa'lumot: 12 daqiqa oldin"
    )
