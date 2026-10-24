"""newproject.service on a real SQLite file, `FakeAgentRuntime`, `FakeGit`, `FakeGitHub`, `FakeClock` and `tmp_path`
folders (SPEC §3.2.8; UC-14 and its ACs; NFR-SEC-11).

The plan and build turns run through the real `dev/turns` and the real gate: a `ToolStep` in a script goes through
the session's guard exactly as the Claude adapter's PreToolUse hook does, so PLAN_ONLY is the gate's own verdict.
"""

import asyncio
import inspect
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from tests.factories import (
    FAKE_KEYS,
    GITHUB_OWNER,
    Fakes,
    make_new_project_state,
    make_owner_burst,
    make_owner_origin,
    make_project,
    make_project_folder,
    projects_root_of,
)
from tests.fakes.agent import Pause, ToolStep, done_result
from yordamchi.core import strings as S
from yordamchi.core.errors import ProviderUnavailable
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.domain import PendingItem, Policy
from yordamchi.dev.newproject import domain, service, store
from yordamchi.dev.newproject.domain import DraftStatus
from yordamchi.dev.newproject.models import ProjectDraft
from yordamchi.dev.newproject.service import DraftAnswer, DraftOutcome, NewProjectState
from yordamchi.dev.projects import service as projects
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.domain import TurnKind, TurnStatus
from yordamchi.dev.turns.models import Turn
from yordamchi.dev.turns.service import TurnsState
from yordamchi.ports.agent import TurnResult
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.models import OutboxMessage
from yordamchi.shared.settings import defaults

ORIGIN = make_owner_origin()
NAME = "KitobBot"
OPENING = "Yangi loyiha: «KitobBot» — kutubxona uchun telegram bot."
SECOND = "Kitoblarni qidirish va bron qilish kerak."
PLAN_NATIJA = "Reja tayyor: Telegram bot, FastAPI + PostgreSQL, 3 bosqich."
GRACE = float(defaults.TURNS_RESULT_GRACE_S.default)  # type: ignore[arg-type]
ALL_STATUSES = [status.value for status in DraftStatus]


def plan_result(natija: str = PLAN_NATIJA) -> TurnResult:
    return done_result(f"Natija: {natija}\nTestlar: ishga tushirilmadi (faqat reja)")


# --- fixtures and helpers -------------------------------------------------------------------------------------------
@pytest.fixture
def root(paths: Paths) -> Path:
    return projects_root_of(paths)


@pytest.fixture
async def turns_state(dev_deps: DevDeps) -> AsyncIterator[TurnsState]:
    state = TurnsState()
    state.turn_ended = service.on_turn_ended
    yield state
    await turns.shutdown_turns(dev_deps, state)


@pytest.fixture
async def state(dev_deps: DevDeps, turns_state: TurnsState) -> AsyncIterator[NewProjectState]:
    state = make_new_project_state(turns_state)
    yield state
    await service.shutdown_drafts(dev_deps, state)


async def until(check: Callable[[], object], *, timeout: float = 5.0, what: str = "condition") -> None:
    """Wait (real time) for background tasks to reach a state; the virtual clock moves only when a test says so."""
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


_update_ids = iter(range(100, 10_000))


async def say(
    deps: DevDeps,
    state: NewProjectState,
    text: str | None,
    *,
    start: bool = False,
    transcript: str | None = None,
    reply_to: int | None = None,
) -> DraftAnswer:
    """One owner burst: `start` is routing's `new_project`; otherwise routing step 4 offers it to the drafts."""
    item = await make_owner_burst(
        deps, text, update_id=next(_update_ids), transcript=transcript, reply_to_message_id=reply_to
    )
    if start:
        return await service.start_draft(deps, state, ORIGIN, item)
    return await service.take_message(deps, state, ORIGIN, item)


async def all_drafts(deps: DevDeps) -> list[ProjectDraft]:
    async with deps.db.read() as s:
        return await store.list_drafts_with_status(s, ALL_STATUSES)


async def draft(deps: DevDeps, draft_id: int) -> ProjectDraft:
    async with deps.db.read() as s:
        row = await store.get_draft(s, draft_id)
    assert row is not None
    return row


def draft_is(deps: DevDeps, draft_id: int, status: DraftStatus) -> Callable[[], Awaitable[bool]]:
    async def check() -> bool:
        return (await draft(deps, draft_id)).status == status.value

    return check


async def turn(deps: DevDeps, turn_id: int) -> Turn:
    async with deps.db.read() as s:
        row = await turns_store.get_turn(s, turn_id)
    assert row is not None
    return row


async def delivery_texts(deps: DevDeps, turn_id: int) -> list[str]:
    async with deps.db.read() as s:
        return [row.text or "" for row in await turns_store.list_deliveries(s, turn_id)]


async def outbox_rows(deps: DevDeps) -> list[OutboxMessage]:
    async with deps.db.read() as s:
        return await outbox_store.list_outbox_messages(s, BotName.DEV.value)


def text_of(row: OutboxMessage) -> str:
    payload = row.payload_json
    return str(payload.get("text") or payload.get("caption") or "") if isinstance(payload, dict) else ""


async def texts(deps: DevDeps) -> list[str]:
    return [text_of(row) for row in await outbox_rows(deps) if row.op == "send_text"]


async def documents(deps: DevDeps) -> list[OutboxMessage]:
    return [row for row in await outbox_rows(deps) if row.op == "send_document"]


async def run_turn(deps: DevDeps, fakes: Fakes, turns_state: TurnsState, turn_id: int) -> Turn:
    """A scheduler pass starts the queued turn; its script plays; after the result grace it is done."""
    await turns.schedule_turns(deps, turns_state)

    async def delivered() -> bool:
        row = await turn(deps, turn_id)
        async with deps.db.read() as s:
            rows = await turns_store.list_deliveries(s, turn_id)
        return row.status not in {"queued", "starting"} and all(item.status == "delivered" for item in rows)

    await until(delivered, what=f"turn {turn_id} delivered")
    await until(lambda: fakes.clock.pending_sleepers > 0, what="result grace armed")
    await fakes.clock.advance(GRACE)
    await until(lambda: _done(deps, turn_id), what=f"turn {turn_id} done")
    return await turn(deps, turn_id)


async def _done(deps: DevDeps, turn_id: int) -> bool:
    return (await turn(deps, turn_id)).status == TurnStatus.DONE.value


def entries(folder: Path) -> list[str]:
    """The names in `folder`; none when it does not exist (sync: the disk is read outside the event loop's tasks)."""
    return sorted(entry.name for entry in folder.iterdir()) if folder.exists() else []


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def mode_of(path: Path) -> int:
    return path.stat().st_mode & 0o777


def write_plan_docs(folder: Path) -> None:
    """What the planning agent writes (the fake agent writes nothing itself). One document quotes a key."""
    docs = folder / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "REQUIREMENTS.md").write_text("# Requirements\nKutubxona boti.\n", encoding="utf-8")
    (docs / "SPEC.md").write_text(f"# SPEC\nBot token: {FAKE_KEYS['telegram_bot_token']}\n", encoding="utf-8")
    (docs / "STACK.md").write_text("# Stack\n1. FastAPI (tavsiya)\n", encoding="utf-8")


async def open_and_close(deps: DevDeps, state: NewProjectState) -> int:
    """ "yangi loyiha: …" in two messages, then "tamom": the folder is made and the plan turn queued."""
    opened = await say(deps, state, OPENING, start=True)
    assert opened.outcome is DraftOutcome.OPENED
    assert (await say(deps, state, SECOND)).outcome is DraftOutcome.ADDED
    assert (await say(deps, state, "tamom")).outcome is DraftOutcome.CLOSED
    assert opened.draft_id is not None
    await until(draft_is(deps, opened.draft_id, DraftStatus.PLANNING), what="planning")
    return opened.draft_id


async def reach_approval(deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path) -> tuple[int, int]:
    """Through the plan turn to "Rejani tasdiqlaysizmi?". Returns (draft id, plan turn id)."""
    draft_id = await open_and_close(deps, state)
    row = await draft(deps, draft_id)
    assert row.plan_turn_id is not None
    write_plan_docs(root / NAME)
    fakes.agents.script_turn(plan_result())
    await run_turn(deps, fakes, state.turns, row.plan_turn_id)
    await until(draft_is(deps, draft_id, DraftStatus.AWAITING_APPROVAL), what="awaiting approval")
    return draft_id, row.plan_turn_id


# --- the whole flow -------------------------------------------------------------------------------------------------
async def test_draft_plan_ha_build_end_to_end(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id, plan_turn_id = await reach_approval(dev_deps, fakes, state, root)
    folder = root / NAME
    assert ("init", folder, ("main",)) in fakes.git.calls
    assert str((await projects.get_project(dev_deps, NAME)).path) == str(folder)
    plan_turn = await turn(dev_deps, plan_turn_id)
    assert plan_turn.kind == TurnKind.PLAN.value
    [plan_prompt] = await delivery_texts(dev_deps, plan_turn_id)
    assert OPENING in plan_prompt
    assert SECOND in plan_prompt
    assert plan_prompt.index(OPENING) < plan_prompt.index(SECOND)
    assert "tamom" not in plan_prompt

    sent = await texts(dev_deps)
    assert domain.collecting_text(silence_s=120) in sent
    plan_message = domain.plan_ready_text(
        NAME,
        natija=PLAN_NATIJA,
        docs=["REQUIREMENTS.md", "SPEC.md", "STACK.md"],
        repo_url=f"https://github.com/{GITHUB_OWNER}/{NAME}",
    )
    assert plan_message in sent
    assert "tasdiqlaysizmi" in plan_message
    docs = await documents(dev_deps)
    assert [text_of(row) for row in docs] == [
        S.NEWPROJECT_DOC_CAPTION(project=NAME, name=name) for name in ("REQUIREMENTS.md", "SPEC.md", "STACK.md")
    ]
    assert all(row.priority == domain.PLAN_PRIORITY for row in docs)

    built = await say(dev_deps, state, "ha")
    assert built.outcome is DraftOutcome.BUILDING
    assert built.turn_id is not None
    assert (await turn(dev_deps, built.turn_id)).kind == TurnKind.BUILD.value
    assert S.NEWPROJECT_BUILDING(project=NAME) in await texts(dev_deps)
    fakes.agents.script_turn(
        ToolStep("Write", {"file_path": str(folder / "src" / "bot.py"), "content": "x"}), done_result()
    )
    await run_turn(dev_deps, fakes, state.turns, built.turn_id)
    await until(draft_is(dev_deps, draft_id, DraftStatus.DONE), what="done")
    row = await draft(dev_deps, draft_id)
    assert (row.project_id, row.plan_turn_id, row.build_turn_id) == (NAME, plan_turn_id, built.turn_id)
    assert row.closed_at is not None
    assert S.NEWPROJECT_DONE(project=NAME) in "\n".join(await texts(dev_deps))


async def test_planning_denies_a_code_write_and_the_build_allows_it(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id = await open_and_close(dev_deps, state)
    folder = root / NAME
    plan_turn_id = (await draft(dev_deps, draft_id)).plan_turn_id
    assert plan_turn_id is not None
    write_plan_docs(folder)
    fakes.agents.script_turn(
        ToolStep("Write", {"file_path": str(folder / "docs" / "SPEC.md"), "content": "# SPEC"}),
        ToolStep("Write", {"file_path": str(folder / "src" / "bot.py"), "content": "print()"}),
        ToolStep("Bash", {"command": "git add docs README.md && git commit -m plan && git push -u origin main"}),
        plan_result(),
    )
    await run_turn(dev_deps, fakes, state.turns, plan_turn_id)
    verdicts = [
        (call.tool_input.get("file_path") or call.tool_input.get("command"), v.allow, v.reason_code)
        for call, v in fakes.agents.guard_calls
    ]
    assert verdicts[0][1] is True
    assert verdicts[1] == (str(folder / "src" / "bot.py"), False, "plan_only_policy")
    assert verdicts[2][1] is True  # commit and push of the plan are allowed, never asked
    await until(draft_is(dev_deps, draft_id, DraftStatus.AWAITING_APPROVAL))
    assert not (folder / "src").exists()  # UC-14 AC: before "ha" the folder holds only plan docs

    built = await say(dev_deps, state, "ha")
    assert built.turn_id is not None
    fakes.agents.script_turn(
        ToolStep("Write", {"file_path": str(folder / "src" / "bot.py"), "content": "x"}), done_result()
    )
    await run_turn(dev_deps, fakes, state.turns, built.turn_id)
    call, verdict = fakes.agents.guard_calls[-1]
    assert (call.tool_input["file_path"], verdict.allow) == (str(folder / "src" / "bot.py"), True)
    assert len(fakes.agents.opened) == 1  # the plan's session took the build turn, under the build's policy
    live = state.turns.live_of(NAME, "coding")
    assert live is not None
    assert live.policy is Policy.FULL


async def test_the_repo_is_private_made_before_the_first_push_and_never_asked_about(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id = await open_and_close(dev_deps, state)
    folder = root / NAME
    assert fakes.github.created == [(GITHUB_OWNER, NAME, folder, True)]  # before the plan turn ran at all
    plan_turn_id = (await draft(dev_deps, draft_id)).plan_turn_id
    assert plan_turn_id is not None
    assert (await turn(dev_deps, plan_turn_id)).status == TurnStatus.QUEUED.value
    row = await draft(dev_deps, draft_id)
    assert row.repo_url == f"https://github.com/{GITHUB_OWNER}/{NAME}"
    planning = domain.planning_text(
        NAME,
        path=f"~/Documents/Projects/{NAME}",
        wanted=NAME,
        plan_max_min=14,
        repo_url=row.repo_url,
        repo_problem=None,
    )
    assert planning in await texts(dev_deps)

    write_plan_docs(folder)
    fakes.agents.script_turn(plan_result())
    await run_turn(dev_deps, fakes, state.turns, plan_turn_id)
    await until(draft_is(dev_deps, draft_id, DraftStatus.AWAITING_APPROVAL))
    built = await say(dev_deps, state, "ha")
    assert built.turn_id is not None
    await run_turn(dev_deps, fakes, state.turns, built.turn_id)
    assert len(fakes.github.created) == 1  # once, whatever turns followed
    async with dev_deps.db.read() as s:
        assert await gate_store.list_recent_decisions(s, limit=10) == []  # no question, no decision row
    assert not [text for text in await texts(dev_deps) if "Muhim qaror" in text or "push qilaymi" in text]


async def test_documents_are_scrubbed_copies_and_never_a_link_out_of_the_project(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path, tmp_path: Path
) -> None:
    draft_id = await open_and_close(dev_deps, state)
    folder = root / NAME
    write_plan_docs(folder)
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY", encoding="utf-8")
    (folder / "docs" / "KEY.md").symlink_to(secret)
    (folder / "docs" / "LINKED.md").hardlink_to(secret)
    plan_turn_id = (await draft(dev_deps, draft_id)).plan_turn_id
    assert plan_turn_id is not None
    fakes.agents.script_turn(plan_result())
    await run_turn(dev_deps, fakes, state.turns, plan_turn_id)
    await until(draft_is(dev_deps, draft_id, DraftStatus.AWAITING_APPROVAL))
    sent = await documents(dev_deps)
    names = [row.payload_json["filename"] for row in sent if isinstance(row.payload_json, dict)]
    assert names == ["REQUIREMENTS.md", "SPEC.md", "STACK.md"]
    for row in sent:
        assert isinstance(row.payload_json, dict)
        copy = Path(str(row.payload_json["path"]))
        assert copy.is_relative_to(dev_deps.paths.media)
        assert mode_of(copy) == 0o600
        content = read(copy)
        assert FAKE_KEYS["telegram_bot_token"] not in content
        assert "PRIVATE KEY" not in content


# --- revision ---------------------------------------------------------------------------------------------------------
async def test_a_revision_request_runs_another_plan_turn_and_asks_again(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id, first_plan = await reach_approval(dev_deps, fakes, state, root)
    revised = await say(dev_deps, state, "o'zgartir: mobil ilova ham bo'lsin")
    assert revised.outcome is DraftOutcome.REVISING
    assert revised.turn_id is not None
    assert revised.turn_id != first_plan
    assert (await draft(dev_deps, draft_id)).status == DraftStatus.REVISING.value
    revision = await turn(dev_deps, revised.turn_id)
    assert revision.kind == TurnKind.PLAN.value  # PLAN_ONLY again
    [prompt] = await delivery_texts(dev_deps, revised.turn_id)
    assert "mobil ilova ham bo'lsin" in prompt
    assert S.NEWPROJECT_REVISING(project=NAME) in await texts(dev_deps)

    fakes.agents.script_turn(plan_result("Reja: bot va mobil ilova, 4 bosqich."))
    await run_turn(dev_deps, fakes, state.turns, revised.turn_id)
    await until(draft_is(dev_deps, draft_id, DraftStatus.AWAITING_APPROVAL), what="asked again")
    asks = [text for text in await texts(dev_deps) if text.endswith(S.NEWPROJECT_PLAN_ASK())]
    assert len(asks) == 2
    assert "mobil ilova, 4 bosqich" in asks[-1]
    assert len(fakes.github.created) == 1

    assert (await say(dev_deps, state, "ha")).outcome is DraftOutcome.BUILDING


async def test_any_other_words_at_the_question_are_what_to_change(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id, _ = await reach_approval(dev_deps, fakes, state, root)
    answer = await say(dev_deps, state, "ha, lekin Django bo'lsin")
    assert answer.outcome is DraftOutcome.REVISING
    assert answer.turn_id is not None
    [prompt] = await delivery_texts(dev_deps, answer.turn_id)
    assert "ha, lekin Django bo'lsin" in prompt
    assert (await draft(dev_deps, draft_id)).build_turn_id is None


async def test_ozgartir_with_nothing_after_it_asks_what_to_change(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id, _ = await reach_approval(dev_deps, fakes, state, root)
    assert (await say(dev_deps, state, "o'zgartir")).outcome is DraftOutcome.ASKED
    assert S.NEWPROJECT_REVISE_WHAT(project=NAME) in await texts(dev_deps)
    assert (await draft(dev_deps, draft_id)).status == DraftStatus.AWAITING_APPROVAL.value


# --- the name ---------------------------------------------------------------------------------------------------------
async def test_a_name_collision_takes_the_first_free_name_and_never_touches_the_existing_folder(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    existing = make_project_folder(root, "kitobbot", readme="# Eski loyiha\n")  # another case: the same name on APFS
    fakes.github.add_repo(GITHUB_OWNER, "KitobBot2")  # free on disk, taken on GitHub
    draft_id = await open_and_close(dev_deps, state)
    row = await draft(dev_deps, draft_id)
    assert row.project_id == "KitobBot3"
    assert ("init", root / "KitobBot3", ("main",)) in fakes.git.calls
    assert [entry.name for entry in existing.iterdir()] == ["README.md"]
    assert (existing / "README.md").read_text(encoding="utf-8") == "# Eski loyiha\n"
    assert not (root / "KitobBot2").exists()
    assert S.NEWPROJECT_RENAMED(wanted=NAME, project="KitobBot3") in "\n".join(await texts(dev_deps))
    assert fakes.github.created == [(GITHUB_OWNER, "KitobBot3", root / "KitobBot3", True)]


async def test_a_registered_project_whose_folder_vanished_keeps_its_name(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    async with dev_deps.db.uow() as s:
        await make_project(s, NAME, at=fakes.clock.now(), path=root / NAME)  # registered, folder gone
    draft_id = await open_and_close(dev_deps, state)
    assert (await draft(dev_deps, draft_id)).project_id == "KitobBot2"


async def test_without_a_github_login_the_plan_still_starts_and_the_repo_is_tried_again(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    fakes.github.user = None
    draft_id = await open_and_close(dev_deps, state)
    assert fakes.github.created == []
    problem = S.NEWPROJECT_REPO_FAILED(why=S.NEWPROJECT_GITHUB_LOGGED_OUT())
    assert any(problem in text for text in await texts(dev_deps))
    assert (await draft(dev_deps, draft_id)).repo_url is None

    plan_turn_id = (await draft(dev_deps, draft_id)).plan_turn_id
    assert plan_turn_id is not None
    write_plan_docs(root / NAME)
    fakes.agents.script_turn(plan_result())
    await run_turn(dev_deps, fakes, state.turns, plan_turn_id)
    await until(draft_is(dev_deps, draft_id, DraftStatus.AWAITING_APPROVAL))
    fakes.github.user = GITHUB_OWNER
    assert (await say(dev_deps, state, "ha")).outcome is DraftOutcome.BUILDING
    assert fakes.github.created == [(GITHUB_OWNER, NAME, root / NAME, True)]  # before the build's first push


async def test_a_github_outage_at_creation_does_not_block_the_plan(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    fakes.github.failures["create_repo"] = ProviderUnavailable(provider="GitHub")
    draft_id = await open_and_close(dev_deps, state)
    why = S.ERRORS[ProviderUnavailable(provider="GitHub").code](provider="GitHub")
    assert any(S.NEWPROJECT_REPO_FAILED(why=why) in text for text in await texts(dev_deps))
    assert (await draft(dev_deps, draft_id)).status == DraftStatus.PLANNING.value


# --- several messages, silence, "tamom" ------------------------------------------------------------------------------
async def test_a_description_in_several_messages_closes_after_the_silence(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState
) -> None:
    opened = await say(dev_deps, state, "yangi loyiha: o'quv markazi uchun CRM", start=True)
    assert opened.draft_id is not None
    await fakes.clock.advance(60)
    assert (
        await say(dev_deps, state, None, transcript="o'quvchilar va to'lovlar hisobi")
    ).outcome is DraftOutcome.ADDED
    await fakes.clock.advance(60)
    assert (await say(dev_deps, state, "BonviHR kabi oddiy bo'lsin")).outcome is DraftOutcome.ADDED
    await fakes.clock.advance(119)
    assert await service.tend_drafts(dev_deps, state) == 0  # 119 s of silence: still collecting
    await fakes.clock.advance(1)
    assert await service.tend_drafts(dev_deps, state) == 1
    await until(draft_is(dev_deps, opened.draft_id, DraftStatus.PLANNING))
    row = await draft(dev_deps, opened.draft_id)
    assert row.project_id == "OquvMarkaziCRM"
    assert row.plan_turn_id is not None
    [prompt] = await delivery_texts(dev_deps, row.plan_turn_id)
    first = prompt.index("o'quv markazi uchun CRM")
    second = prompt.index("[voice note, transcribed] o'quvchilar va to'lovlar hisobi")
    third = prompt.index("BonviHR kabi oddiy bo'lsin")
    assert first < second < third


async def test_a_start_burst_ending_in_tamom_closes_at_once(dev_deps: DevDeps, state: NewProjectState) -> None:
    answer = await say(dev_deps, state, "Yangi loyiha: TezTaxi nomli taksi boti.\nTamom", start=True)
    assert answer.outcome is DraftOutcome.CLOSED
    assert answer.draft_id is not None
    await until(draft_is(dev_deps, answer.draft_id, DraftStatus.PLANNING))
    assert (await draft(dev_deps, answer.draft_id)).project_id == "TezTaxi"


async def test_tamom_with_nothing_described_waits_and_the_silence_drops_it(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    opened = await say(dev_deps, state, "yangi loyiha", start=True)
    assert opened.draft_id is not None
    assert (await say(dev_deps, state, "tamom")).outcome is DraftOutcome.EMPTY
    assert S.NEWPROJECT_EMPTY() in await texts(dev_deps)
    assert (await draft(dev_deps, opened.draft_id)).status == DraftStatus.COLLECTING.value
    await fakes.clock.advance(120)
    await service.tend_drafts(dev_deps, state)
    assert (await draft(dev_deps, opened.draft_id)).status == DraftStatus.CANCELLED.value
    assert S.NEWPROJECT_EMPTY_CANCELLED() in await texts(dev_deps)
    assert entries(root) == []


async def test_a_second_new_project_while_one_is_collected_joins_it(dev_deps: DevDeps, state: NewProjectState) -> None:
    first = await say(dev_deps, state, OPENING, start=True)
    again = await say(dev_deps, state, "yangi loyiha uchun yana: admin panel", start=True)
    assert (again.outcome, again.draft_id) == (DraftOutcome.ADDED, first.draft_id)
    assert len(await all_drafts(dev_deps)) == 1


# --- "yo'q" -----------------------------------------------------------------------------------------------------------
async def test_no_while_collecting_cancels_and_makes_nothing(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    opened = await say(dev_deps, state, OPENING, start=True)
    assert opened.draft_id is not None
    assert (await say(dev_deps, state, "yo'q")).outcome is DraftOutcome.CANCELLED
    row = await draft(dev_deps, opened.draft_id)
    assert row.status == DraftStatus.CANCELLED.value
    assert row.closed_at is not None
    assert S.NEWPROJECT_CANCELLED() in await texts(dev_deps)
    assert entries(root) == []
    assert fakes.github.calls == []
    assert (await say(dev_deps, state, "kitoblar ham bo'lsin")).outcome is DraftOutcome.NOT_TAKEN


async def test_no_at_the_plan_question_cancels_and_leaves_the_folder_and_repo(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id, _ = await reach_approval(dev_deps, fakes, state, root)
    assert (await say(dev_deps, state, "yo'q")).outcome is DraftOutcome.CANCELLED
    row = await draft(dev_deps, draft_id)
    assert (row.status, row.build_turn_id) == (DraftStatus.CANCELLED.value, None)
    assert (root / NAME / "docs" / "SPEC.md").exists()
    assert len(fakes.github.created) == 1
    assert S.NEWPROJECT_PROJECT_CANCELLED(project=NAME) in await texts(dev_deps)
    async with dev_deps.db.read() as s:
        kinds = [row.kind for row in await turns_store.list_project_turns(s, NAME, limit=10)]
    assert TurnKind.BUILD.value not in kinds


# --- routing step 4 ---------------------------------------------------------------------------------------------------
async def test_work_aimed_at_another_project_is_not_the_drafts(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    async with dev_deps.db.uow() as s:
        await make_project(s, "BonviHR", at=fakes.clock.now(), path=make_project_folder(root, "BonviHR"))
    await say(dev_deps, state, OPENING, start=True)
    assert (await say(dev_deps, state, "BonviHR'da login sahifasini tuzat")).outcome is DraftOutcome.NOT_TAKEN
    assert (await say(dev_deps, state, "BonviHR kabi oddiy bo'lsin")).outcome is DraftOutcome.ADDED


async def test_a_new_project_while_a_plan_waits_is_not_taken_as_what_to_change(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id, _ = await reach_approval(dev_deps, fakes, state, root)
    answer = await say(dev_deps, state, "Yangi loyiha: «TezTaxi» taksi chaqirish boti")
    assert answer.outcome is DraftOutcome.NOT_TAKEN  # routing's `new_project` opens it with `start_draft`
    assert (await draft(dev_deps, draft_id)).status == DraftStatus.AWAITING_APPROVAL.value


async def test_with_no_draft_open_nothing_is_taken(dev_deps: DevDeps, state: NewProjectState) -> None:
    assert (await say(dev_deps, state, "ha")).outcome is DraftOutcome.NOT_TAKEN


async def test_messages_while_planning_steer_the_plan_turn_never_a_coding_turn(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id = await open_and_close(dev_deps, state)
    plan_turn_id = (await draft(dev_deps, draft_id)).plan_turn_id
    assert plan_turn_id is not None
    fakes.agents.script_turn(Pause())
    await turns.schedule_turns(dev_deps, state.turns)
    await until(lambda: _running(dev_deps, plan_turn_id), what="plan turn running")
    steered = await say(dev_deps, state, "to'lov tizimi ham kerak")
    assert (steered.outcome, steered.turn_id) == (DraftOutcome.STEERED, plan_turn_id)
    async with dev_deps.db.read() as s:
        kinds = {row.kind for row in await turns_store.list_project_turns(s, NAME, limit=10)}
        modes = [row.mode for row in await turns_store.list_deliveries(s, plan_turn_id)]
    assert kinds == {TurnKind.PLAN.value}
    assert modes == ["new_turn", "next"]


async def _running(deps: DevDeps, turn_id: int) -> bool:
    return (await turn(deps, turn_id)).status == TurnStatus.RUNNING.value


async def test_two_waiting_plans_and_an_unnamed_answer_asks_which(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    first, _ = await reach_approval(dev_deps, fakes, state, root)
    second = await say(dev_deps, state, "yangi loyiha: «TezTaxi» taksi boti\ntamom", start=True)
    assert second.draft_id is not None
    await until(draft_is(dev_deps, second.draft_id, DraftStatus.PLANNING))
    second_plan = (await draft(dev_deps, second.draft_id)).plan_turn_id
    assert second_plan is not None
    fakes.agents.script_turn(plan_result())
    await run_turn(dev_deps, fakes, state.turns, second_plan)
    await until(draft_is(dev_deps, second.draft_id, DraftStatus.AWAITING_APPROVAL))

    assert (await say(dev_deps, state, "ha")).outcome is DraftOutcome.ASKED
    assert domain.which_text([NAME, "TezTaxi"]) in await texts(dev_deps)
    named = await say(dev_deps, state, "TezTaxi ha")
    assert (named.outcome, named.draft_id) == (DraftOutcome.BUILDING, second.draft_id)
    assert (await draft(dev_deps, first)).status == DraftStatus.AWAITING_APPROVAL.value
    assert await service.open_approvals(dev_deps) == (PendingItem("question", first, NAME),)


async def test_another_bots_origin_is_refused(dev_deps: DevDeps, state: NewProjectState) -> None:
    item = await make_owner_burst(dev_deps, OPENING, update_id=9)
    with pytest.raises(ValueError, match="personal"):
        await service.start_draft(dev_deps, state, make_owner_origin(BotName.PERSONAL), item)
    with pytest.raises(ValueError, match="personal"):
        await service.take_message(dev_deps, state, make_owner_origin(BotName.PERSONAL), item)


# --- failure and recovery -------------------------------------------------------------------------------------------
async def test_a_folder_that_cannot_be_made_fails_the_draft_and_says_so(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    root.parent.mkdir(parents=True)
    write(root, "not a folder")  # projects.root is a file
    opened = await say(dev_deps, state, OPENING, start=True)
    assert opened.draft_id is not None
    await say(dev_deps, state, "tamom")
    await until(draft_is(dev_deps, opened.draft_id, DraftStatus.FAILED), what="failed")
    row = await draft(dev_deps, opened.draft_id)
    assert row.error_code == "newproject_folder_failed"
    assert any(text.startswith("Yangi loyihani ochib bo'lmadi") for text in await texts(dev_deps))
    assert read(root) == "not a folder"


async def test_a_restart_mid_creation_reuses_this_drafts_folder(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    now = fakes.clock.now()
    part = domain.DraftPart(OPENING, now)
    async with dev_deps.db.uow() as s:
        row = await store.add_draft(
            s, status=DraftStatus.CREATING.value, parts=domain.parts_json([part]), opened_at=now, last_part_at=now
        )
        await store.set_draft(s, row.id, proposed_name=NAME)
    (root / NAME / ".git").mkdir(parents=True)  # the crash came after `git init`
    await service.tend_drafts(dev_deps, state)
    await until(draft_is(dev_deps, row.id, DraftStatus.PLANNING))
    assert (await draft(dev_deps, row.id)).project_id == NAME
    assert not (root / f"{NAME}2").exists()


async def test_a_turn_end_missed_by_the_hook_is_caught_by_the_timer(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    state.turns.turn_ended = None  # as if the bot restarted while the turn ended
    draft_id = await open_and_close(dev_deps, state)
    plan_turn_id = (await draft(dev_deps, draft_id)).plan_turn_id
    assert plan_turn_id is not None
    fakes.agents.script_turn(plan_result())
    await run_turn(dev_deps, fakes, state.turns, plan_turn_id)
    assert (await draft(dev_deps, draft_id)).status == DraftStatus.PLANNING.value
    assert await service.tend_drafts(dev_deps, state) == 1
    assert (await draft(dev_deps, draft_id)).status == DraftStatus.AWAITING_APPROVAL.value


async def test_a_plan_turn_that_did_not_finish_leaves_the_draft_planning(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState, root: Path
) -> None:
    draft_id = await open_and_close(dev_deps, state)
    plan_turn_id = (await draft(dev_deps, draft_id)).plan_turn_id
    assert plan_turn_id is not None
    fakes.agents.script_turn(Pause())
    await turns.schedule_turns(dev_deps, state.turns)
    await until(lambda: _running(dev_deps, plan_turn_id))
    await turns.stop_turn(dev_deps, state.turns, NAME)
    await until(lambda: _interrupted(dev_deps, plan_turn_id))
    assert await service.tend_drafts(dev_deps, state) == 0
    assert (await draft(dev_deps, draft_id)).status == DraftStatus.PLANNING.value


async def _interrupted(deps: DevDeps, turn_id: int) -> bool:
    return (await turn(deps, turn_id)).status == TurnStatus.INTERRUPTED.value


# --- retention --------------------------------------------------------------------------------------------------------
async def test_finished_drafts_go_after_400_days_and_open_ones_stay(
    dev_deps: DevDeps, fakes: Fakes, state: NewProjectState
) -> None:
    cancelled = await say(dev_deps, state, OPENING, start=True)
    await say(dev_deps, state, "yo'q")
    collecting = await say(dev_deps, state, "yangi loyiha: boshqa", start=True)
    await fakes.clock.advance(400 * 86400)
    assert await service.apply_retention(dev_deps) == 0  # closed exactly 400 days ago: kept
    await fakes.clock.advance(1)
    assert await service.apply_retention(dev_deps) == 1
    assert [row.id for row in await all_drafts(dev_deps)] == [collecting.draft_id]
    assert cancelled.draft_id != collecting.draft_id
