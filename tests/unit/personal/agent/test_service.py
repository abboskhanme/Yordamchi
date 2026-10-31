"""The personal agent's guard at work (SPEC §3.2.18 items 1–2): agent mode from the settings, the guard's verdicts
and messages, the one registered question, the owner's grant used once, a refusal, and the audit trail."""

from pathlib import Path

import pytest

from tests.factories import FAKE_KEYS, Fakes, add_setting_row, make_owner_origin, make_personal_deps
from yordamchi.core.paths import BotName, Paths
from yordamchi.persistence.uow import Database
from yordamchi.personal.agent import domain
from yordamchi.personal.agent import service as agent
from yordamchi.personal.agent.domain import PendingAsk
from yordamchi.personal.agent.service import AgentState
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.agent import ToolCall
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.audit.domain import AuditKind
from yordamchi.shared.settings import defaults

DEPLOY = "vercel deploy --prod"
CHAT_ID = 777


@pytest.fixture
def deps(personal_db: Database, paths: Paths, fakes: Fakes) -> PersonalDeps:
    return make_personal_deps(db=personal_db, paths=paths, fakes=fakes)


def bash(command: str) -> ToolCall:
    return ToolCall("Bash", {"command": command}, "toolu_1", None, "session-1")


async def _kinds(deps: PersonalDeps) -> list[str]:
    async with deps.db.read() as s:
        return [event.kind for event in await audit_store.list_audit_events(s)]


async def test_agent_mode_is_on_by_default_in_the_workspace_under_the_owners_home(deps: PersonalDeps) -> None:
    mode = await agent.agent_mode(deps, AgentState())
    assert mode is not None
    assert mode.workspace == deps.paths.home / "Documents" / "Projects" / "_shaxsiy"


async def test_agent_mode_off_gives_the_restricted_session(deps: PersonalDeps, fakes: Fakes) -> None:
    await add_setting_row(deps.db, defaults.AGENT_ENABLED.key, False, at=fakes.clock.now())
    assert await agent.agent_mode(deps, AgentState()) is None


@pytest.mark.parametrize(
    ("configured", "relative"),
    [("~", ""), ("~/Ish/bot", "Ish/bot"), ("Ish", "Ish"), (" ~/x ", "x")],
)
def test_the_workspace_is_under_the_owners_home(deps: PersonalDeps, configured: str, relative: str) -> None:
    assert agent.workspace_of(deps, configured) == deps.paths.home / relative


def test_an_absolute_workspace_stays_as_it_is(deps: PersonalDeps) -> None:
    assert agent.workspace_of(deps, "/Volumes/Ish") == Path("/Volumes/Ish")


async def test_ordinary_work_is_allowed_without_an_audit_row(deps: PersonalDeps) -> None:
    state = AgentState()
    workspace = agent.workspace_of(deps, "~/w")
    verdict = await agent.check_call(deps, state, workspace, bash("ls -la"))
    assert verdict.allow
    assert state.pending is None
    assert await _kinds(deps) == []


async def test_a_forbidden_call_is_denied_with_the_reason_and_never_asked(deps: PersonalDeps) -> None:
    state = AgentState()
    workspace = agent.workspace_of(deps, "~/w")
    call = ToolCall("Read", {"file_path": str(deps.paths.data / "personal.db")}, "toolu_2", None, "session-1")
    verdict = await agent.check_call(deps, state, workspace, call)
    assert not verdict.allow
    assert verdict.reason_code == "forbidden_path"
    assert verdict.message.startswith("BLOCKED by Yordamchi's safety rules (forbidden_path:")
    assert "cannot be approved" in verdict.message
    assert state.pending is None  # never asked
    assert await _kinds(deps) == [AuditKind.GATE_VERDICT.value]


async def test_a_dangerous_call_is_denied_at_once_and_registers_one_question(deps: PersonalDeps) -> None:
    state = AgentState()
    workspace = agent.workspace_of(deps, "~/w")
    verdict = await agent.check_call(deps, state, workspace, bash(DEPLOY))
    assert not verdict.allow
    assert verdict.message.startswith("WAITING FOR THE OWNER")
    assert state.pending == PendingAsk(number=1, key=domain.key_of(bash(DEPLOY)), what=DEPLOY, reason="deploy_cli")
    second = await agent.check_call(deps, state, workspace, bash("ssh prod 'rm -rf /srv'"))
    assert not second.allow
    assert state.pending is not None
    assert state.pending.number == 1  # ONE question: the second ask waits behind it, not asked
    assert state.take_pending() is not None
    assert state.take_pending() is None


async def test_the_question_never_carries_a_key(deps: PersonalDeps) -> None:
    state = AgentState()
    key = FAKE_KEYS["openai_api_key"]
    command = f"curl -X POST -H 'Authorization: Bearer {key}' https://api.example.com/send -d x=1"
    await agent.check_call(deps, state, agent.workspace_of(deps, "~/w"), bash(command))
    assert state.pending is not None
    assert key not in state.pending.what
    async with deps.db.read() as s:
        [event] = await audit_store.list_audit_events(s)
    assert key not in str(event.data_json)


async def test_ha_grants_that_exact_call_once_and_returns_the_follow_up(deps: PersonalDeps) -> None:
    state = AgentState()
    workspace = agent.workspace_of(deps, "~/w")
    await agent.check_call(deps, state, workspace, bash(DEPLOY))
    ask = state.take_pending()
    assert ask is not None
    origin = make_owner_origin(BotName.PERSONAL, update_id=9)
    follow_up = await agent.answer_ask(deps, state, origin, ask, approved=True, chat_id=CHAT_ID, burst_id=42)
    assert follow_up.chat_id == CHAT_ID
    assert follow_up.burst_id == 42
    assert follow_up.own_text.startswith(f"[Yordamchi] The owner approved: {DEPLOY}")
    assert (await agent.check_call(deps, state, workspace, bash(DEPLOY))).allow  # the retry runs
    again = await agent.check_call(deps, state, workspace, bash(DEPLOY))
    assert not again.allow  # single use
    kinds = await _kinds(deps)
    assert AuditKind.DECISION_RESOLVED.value in kinds
    assert AuditKind.GRANT_CONSUMED.value in kinds


async def test_a_grant_expires_after_the_approval_window(deps: PersonalDeps, fakes: Fakes) -> None:
    state = AgentState()
    workspace = agent.workspace_of(deps, "~/w")
    await agent.check_call(deps, state, workspace, bash(DEPLOY))
    ask = state.take_pending()
    assert ask is not None
    origin = make_owner_origin(BotName.PERSONAL)
    await agent.answer_ask(deps, state, origin, ask, approved=True, chat_id=CHAT_ID, burst_id=1)
    ttl = defaults.GATE_APPROVAL_TTL_MIN.default
    assert isinstance(ttl, int)
    await fakes.clock.advance(ttl * 60)
    assert not (await agent.check_call(deps, state, workspace, bash(DEPLOY))).allow


async def test_yoq_refuses_it_and_the_agent_is_told_so(deps: PersonalDeps) -> None:
    state = AgentState()
    workspace = agent.workspace_of(deps, "~/w")
    await agent.check_call(deps, state, workspace, bash(DEPLOY))
    ask = state.take_pending()
    assert ask is not None
    origin = make_owner_origin(BotName.PERSONAL)
    follow_up = await agent.answer_ask(deps, state, origin, ask, approved=False, chat_id=CHAT_ID, burst_id=2)
    assert follow_up.own_text.startswith(f"[Yordamchi] The owner refused: {DEPLOY}")
    retry = await agent.check_call(deps, state, workspace, bash(DEPLOY))
    assert not retry.allow
    assert retry.message.startswith("REFUSED")
    assert state.pending is None  # not asked again


async def test_only_a_verified_owner_origin_answers(deps: PersonalDeps) -> None:
    state = AgentState()
    ask = PendingAsk(number=1, key="k", what=DEPLOY, reason="deploy_cli")
    with pytest.raises(TypeError):
        await agent.answer_ask(deps, state, object(), ask, approved=True, chat_id=CHAT_ID, burst_id=1)  # type: ignore[arg-type]  # a forged origin
    with pytest.raises(ValueError, match="cannot change or answer"):
        await agent.answer_ask(
            deps, state, make_owner_origin(BotName.DEV), ask, approved=True, chat_id=CHAT_ID, burst_id=1
        )
    assert state.grants == ()


async def test_the_guard_uses_the_gate_defaults_the_personal_bot_cannot_change(deps: PersonalDeps) -> None:
    """`gate.*` is the dev bot's scope: the personal guard runs on the declared defaults (ASSUMPTIONS 2026-10-31)."""
    state = AgentState()
    guard = agent.make_guard(deps, state, agent.workspace_of(deps, "~/w"))
    assert not (await guard(bash(DEPLOY))).allow
    assert (await guard(bash("echo salom"))).allow
