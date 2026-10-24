"""gate.service on a real SQLite file with fakes (SPEC §3.2.3, §5.7; UC-13; NFR-SEC-06, NFR-SEC-10).

UC-13's acceptance rows: one question per ask, zero runs before "ha", "ha" runs the exact action once, a different
command or host asks again, expiry asks again, and a stray "ha" with two decisions pending approves nothing. Plus the
fact loop, the deny prompts, the shim and pre-push backends, audit and retention.
"""

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest

from tests.factories import (
    GATE_SESSION_KEY,
    Fakes,
    add_setting_row,
    make_bash_call,
    make_decision,
    make_gate_scope,
    make_owner_origin,
    make_project,
    make_project_folder,
    projects_root_of,
)
from yordamchi.core import strings as S
from yordamchi.core.errors import DecisionNotFound, GitFailed, ProviderUnavailable
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import domain, service, store
from yordamchi.dev.gate.domain import GateClass, Policy, Reason
from yordamchi.dev.gate.models import Decision
from yordamchi.dev.gate.service import AnswerKind, DecisionReply, GateScope, SecretReport, ShimRequest
from yordamchi.ports.agent import GuardVerdict, ToolCall
from yordamchi.ports.git import GitStatus
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.audit.domain import AuditKind
from yordamchi.shared.audit.models import AuditEvent

DEPLOY = "ssh prod 'docker compose up -d'"
GROUP = -1001234567890  # tests.factories.DEV_GROUP_ID


@pytest.fixture
def root(paths: Paths) -> Path:
    return projects_root_of(paths)


@pytest.fixture
def scope(root: Path) -> GateScope:
    return make_gate_scope(root)


async def check(deps: DevDeps, scope: GateScope, command: str, *, cwd: str | None = None) -> GuardVerdict:
    return await service.check_call(deps, scope, make_bash_call(command, cwd=cwd))


async def answer(
    deps: DevDeps, text: str, *, update_id: int = 900, reply_to: int | None = None
) -> service.DecisionAnswer:
    origin = make_owner_origin(update_id=update_id)
    return await service.answer_decision(deps, origin, DecisionReply(text, reply_to_message_id=reply_to))


async def decisions(deps: DevDeps) -> list[Decision]:
    async with deps.db.read() as s:
        return sorted(await store.list_recent_decisions(s, limit=100), key=lambda row: row.id)


async def events(deps: DevDeps, kind: AuditKind | None = None) -> list[AuditEvent]:
    async with deps.db.read() as s:
        return await audit_store.list_audit_events(s, kind=kind.value if kind else None)


def as_object(value: JsonValue) -> JsonObject:
    assert isinstance(value, dict)
    return value


def texts(fakes: Fakes) -> list[str]:
    return [str(request.payload["text"]) for request in fakes.messenger.sent()]


# --- one question, zero runs before "ha" (UC-13) --------------------------------------------------------------------
async def test_an_ask_is_denied_at_once_with_one_decision_and_one_question(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope
) -> None:
    verdict = await check(dev_deps, scope, DEPLOY)

    assert not verdict.allow
    assert verdict.reason_code == Reason.REMOTE_COMMAND.value
    assert verdict.message.startswith("BLOCKED: owner decision #1 has been asked.")
    (row,) = await decisions(dev_deps)
    assert (row.status, row.kind, row.gate_class, row.project_id, row.turn_id) == (
        "pending",
        "gate",
        "server_write",
        "BonviHR",
        31,
    )
    assert row.input_redacted == DEPLOY
    assert row.expires_at == fakes.clock.now() + timedelta(minutes=120)
    (question,) = fakes.messenger.sent()
    assert question.chat_id == GROUP
    text = str(question.payload["text"])
    assert text.startswith("BonviHR · Muhim qaror #1: serverda o'zgarish qilmoqchiman")
    assert DEPLOY in text
    assert str(S.GATE_CONSEQUENCES["remote_command"]()) in text
    assert text.endswith("Bajaraymi? (ha / yo'q / boshqa ko'rsatma)")
    assert row.payload_json == {
        "reason": "remote_command",
        "session_key": GATE_SESSION_KEY,
        "findings": ["remote_command"],
        "message_id": 101,
    }


async def test_retrying_before_the_answer_never_runs_and_never_asks_again(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope
) -> None:
    verdicts = [await check(dev_deps, scope, DEPLOY) for _ in range(3)]
    verdicts.append(await service.check_call(dev_deps, scope, make_bash_call(f"  {DEPLOY}\n", tool_use_id="t2")))

    assert not any(verdict.allow for verdict in verdicts)
    assert len(await decisions(dev_deps)) == 1
    assert len(fakes.messenger.sent()) == 1
    assert {verdict.message for verdict in verdicts} == {verdicts[0].message}


async def test_ha_runs_the_exact_action_once(dev_deps: DevDeps, fakes: Fakes, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)

    result = await answer(dev_deps, "ha")

    assert (result.kind, result.decision_id, result.project_id, result.turn_id) == (
        AnswerKind.APPROVED,
        1,
        "BonviHR",
        31,
    )
    assert result.delivery == f"[Yordamchi] Owner approved #1. Run exactly this, once:\n\n{DEPLOY}\n"
    assert texts(fakes)[-1] == "BonviHR · #1 tasdiqlandi — aynan shu buyruq bir marta bajariladi."
    assert fakes.messenger.sent()[-1].payload["reply_to"] == 101  # threads under the question
    (row,) = await decisions(dev_deps)
    assert (row.status, row.decided_update_id) == ("approved", 900)
    assert row.grant_expires_at == fakes.clock.now() + timedelta(minutes=120)

    assert (await check(dev_deps, scope, DEPLOY)).allow
    assert not (await check(dev_deps, scope, DEPLOY)).allow  # single use: the second run asks again
    first, second = await decisions(dev_deps)
    assert (first.status, first.consumed_at) == ("consumed", fakes.clock.now())
    assert second.status == "pending"
    assert texts(fakes)[-1].startswith("BonviHR · Muhim qaror #2:")


@pytest.mark.parametrize(
    "other",
    [
        "ssh prod2 'docker compose up -d'",  # another host
        "ssh prod 'docker compose up'",  # another command
        "ssh prod 'docker compose up -d && reboot'",
    ],
)
async def test_a_different_command_or_host_asks_again_after_ha(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope, other: str
) -> None:
    await check(dev_deps, scope, DEPLOY)
    await answer(dev_deps, "ha")

    verdict = await check(dev_deps, scope, other)

    assert not verdict.allow
    assert [row.status for row in await decisions(dev_deps)] == ["approved", "pending"]
    assert (await check(dev_deps, scope, DEPLOY)).allow  # the approved one is still there, once


async def test_a_grant_is_bound_to_its_project(dev_deps: DevDeps, root: Path, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    await answer(dev_deps, "ha")

    other = make_gate_scope(root, "HRMentor")
    assert not (await check(dev_deps, other, DEPLOY)).allow
    assert (await check(dev_deps, scope, DEPLOY)).allow


async def test_a_stray_ha_with_two_decisions_pending_approves_nothing(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope
) -> None:
    await check(dev_deps, scope, DEPLOY)
    await check(dev_deps, scope, "ssh prod 'systemctl restart nginx'")

    result = await answer(dev_deps, "ha")

    assert result.kind is AnswerKind.AMBIGUOUS
    assert texts(fakes)[-1] == str(S.WHICH_DECISION(decisions="#1 (BonviHR), #2 (BonviHR)"))
    assert [row.status for row in await decisions(dev_deps)] == ["pending", "pending"]
    assert not (await check(dev_deps, scope, DEPLOY)).allow
    assert len(await decisions(dev_deps)) == 2  # and no new question either


async def test_a_reply_to_one_question_binds_it_with_two_pending(dev_deps: DevDeps, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    await check(dev_deps, scope, "ssh prod 'systemctl restart nginx'")
    second = (await decisions(dev_deps))[1]

    result = await answer(dev_deps, "ha", reply_to=int(str(as_object(second.payload_json)["message_id"])))

    assert (result.kind, result.decision_id) == (AnswerKind.APPROVED, 2)
    assert [row.status for row in await decisions(dev_deps)] == ["pending", "approved"]


async def test_naming_the_decision_binds_it(dev_deps: DevDeps, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    await check(dev_deps, scope, "ssh prod 'systemctl restart nginx'")

    assert (await answer(dev_deps, "#1 ha")).decision_id == 1
    with pytest.raises(DecisionNotFound):
        await answer(dev_deps, "#99 ha")


async def test_a_reply_to_another_message_is_not_an_approval(dev_deps: DevDeps, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)

    assert (await answer(dev_deps, "ha", reply_to=5555)).kind is AnswerKind.NOT_AN_APPROVAL
    assert (await answer(dev_deps, "testlarni yurgiz")).kind is AnswerKind.NOT_AN_APPROVAL
    assert (await decisions(dev_deps))[0].status == "pending"


async def test_an_open_agent_question_makes_a_bare_ha_ambiguous(dev_deps: DevDeps, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    reply = DecisionReply("ha", open_questions=(domain.PendingItem("question", 5, "BonviHR"),))

    result = await service.answer_decision(dev_deps, make_owner_origin(update_id=901), reply)

    assert result.kind is AnswerKind.AMBIGUOUS


async def test_only_this_bots_owner_origin_answers(dev_deps: DevDeps, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    with pytest.raises(ValueError, match="owner origin"):
        await service.answer_decision(dev_deps, make_owner_origin(BotName.PERSONAL), DecisionReply("ha"))


# --- "yo'q", another instruction ------------------------------------------------------------------------------------
async def test_no_declines_and_a_retry_asks_again(dev_deps: DevDeps, fakes: Fakes, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)

    result = await answer(dev_deps, "yo'q")

    assert result.kind is AnswerKind.DECLINED
    assert result.delivery == "[Yordamchi] Owner declined #1; do not do it or anything with the same effect.\n"
    assert texts(fakes)[-1] == "BonviHR · #1 bekor qilindi, bajarilmaydi."
    assert not (await check(dev_deps, scope, DEPLOY)).allow
    assert [row.status for row in await decisions(dev_deps)] == ["denied", "pending"]


async def test_another_instruction_declines_and_carries_the_text_but_not_into_the_audit(
    dev_deps: DevDeps, scope: GateScope
) -> None:
    await check(dev_deps, scope, DEPLOY)

    result = await answer(dev_deps, "ha lekin avval staging'ga")

    assert result.kind is AnswerKind.INSTRUCTED
    assert result.delivery.endswith("The owner said instead: ha lekin avval staging'ga\n")
    assert (await decisions(dev_deps))[0].status == "denied"
    assert all("staging" not in json.dumps(event.data_json) for event in await events(dev_deps))


async def test_no_withdraws_an_unused_grant(dev_deps: DevDeps, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    await answer(dev_deps, "ha")

    result = await answer(dev_deps, "#1 yo'q")

    assert result.kind is AnswerKind.DECLINED
    assert (await decisions(dev_deps))[0].status == "cancelled"
    assert not (await check(dev_deps, scope, DEPLOY)).allow


async def test_answers_to_a_closed_decision_change_nothing(dev_deps: DevDeps, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    await answer(dev_deps, "ha")
    assert (await answer(dev_deps, "#1 ha")).kind is AnswerKind.ALREADY_APPROVED
    await check(dev_deps, scope, DEPLOY)  # consumed
    assert (await answer(dev_deps, "#1 ha")).kind is AnswerKind.CLOSED
    assert (await answer(dev_deps, "#1 yo'q")).kind is AnswerKind.CLOSED


# --- expiry (UC-13 F: after 2 h the approval expires and is asked again) --------------------------------------------
async def test_ha_to_an_expired_question_asks_again(dev_deps: DevDeps, fakes: Fakes, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    await fakes.clock.advance(121 * 60)

    result = await answer(dev_deps, "ha")

    assert (result.kind, result.decision_id) == (AnswerKind.REASKED, 2)
    old, fresh = await decisions(dev_deps)
    assert (old.status, old.superseded_by) == ("expired", 2)
    assert (fresh.status, fresh.input_sha256) == ("pending", old.input_sha256)
    assert as_object(fresh.payload_json)["reasked_from"] == 1
    assert texts(fakes)[-2:] == [
        "BonviHR · #1 muddati o'tib ketgan — yangidan so'rayman.",
        texts(fakes)[-1],
    ]
    assert texts(fakes)[-1].startswith("BonviHR · Muhim qaror #2:")
    assert not (await check(dev_deps, scope, DEPLOY)).allow  # still waiting, on #2


async def test_a_retry_after_the_question_expired_asks_a_fresh_one(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope
) -> None:
    await check(dev_deps, scope, DEPLOY)
    await fakes.clock.advance(121 * 60)

    assert not (await check(dev_deps, scope, DEPLOY)).allow

    assert [row.status for row in await decisions(dev_deps)] == ["expired", "pending"]
    assert len(fakes.messenger.sent()) == 2


async def test_an_unused_grant_expires(dev_deps: DevDeps, fakes: Fakes, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    await answer(dev_deps, "ha")
    await fakes.clock.advance(121 * 60)

    assert not (await check(dev_deps, scope, DEPLOY)).allow
    assert [row.status for row in await decisions(dev_deps)] == ["expired", "pending"]


# --- forbid, allow, policies ----------------------------------------------------------------------------------------
async def test_a_forbidden_call_is_denied_without_a_question_and_audited(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope
) -> None:
    verdict = await check(dev_deps, scope, "cat ~/.claude/.credentials.json")

    assert not verdict.allow
    assert verdict.reason_code == "forbidden_path"
    assert verdict.message.startswith("BLOCKED by Yordamchi's safety rules (forbidden_path:")
    assert await decisions(dev_deps) == []
    assert fakes.messenger.sent() == []
    (event,) = await events(dev_deps, AuditKind.GATE_VERDICT)
    assert as_object(event.data_json)["outcome"] == "denied"


@pytest.mark.parametrize(
    ("policy", "command", "opening"),
    [
        (Policy.READ_ONLY, "touch notes.txt", "BLOCKED: this is a read-only investigation session (touch)."),
        (Policy.PLAN_ONLY, "pytest -q", "BLOCKED: this is a planning session (pytest)."),
        (Policy.SELF_UPDATE, "gh pr merge 7", "BLOCKED: a Yordamchi self-update never pushes to or merges into main"),
    ],
)
async def test_each_restricted_policy_has_its_own_deny_prompt(
    dev_deps: DevDeps, root: Path, policy: Policy, command: str, opening: str
) -> None:
    verdict = await check(dev_deps, make_gate_scope(root, policy=policy), command)

    assert not verdict.allow
    assert verdict.message.startswith(opening)
    assert await decisions(dev_deps) == []


async def test_routine_work_runs_and_writes_nothing(dev_deps: DevDeps, fakes: Fakes, scope: GateScope) -> None:
    for command in ("pytest -q", "git add -A && git commit -m 'x'", "uv sync"):
        assert (await check(dev_deps, scope, command)).allow
    assert await decisions(dev_deps) == []
    assert await events(dev_deps) == []
    assert fakes.messenger.sent() == []


async def test_make_guard_reads_the_scope_at_every_call(dev_deps: DevDeps, root: Path) -> None:
    scopes = iter([make_gate_scope(root, dirty_before_turn=False), make_gate_scope(root, dirty_before_turn=True)])
    guard = service.make_guard(dev_deps, lambda: next(scopes))
    call = make_bash_call("git reset --hard")

    assert (await guard(call)).allow
    assert not (await guard(call)).allow


async def test_a_question_that_could_not_be_sent_is_sent_later_once(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope
) -> None:
    fakes.messenger.fail_next("send_text", ProviderUnavailable(provider="telegram"))
    assert not (await check(dev_deps, scope, DEPLOY)).allow
    assert "message_id" not in as_object((await decisions(dev_deps))[0].payload_json)

    assert await service.post_unasked_decisions(dev_deps) == 1
    assert await service.post_unasked_decisions(dev_deps) == 0
    assert len(fakes.messenger.sent()) == 1
    assert await service.pending_items(dev_deps) == (domain.PendingItem("decision", 1, "BonviHR"),)


# --- facts (SPEC §3.2.3 step 2) -------------------------------------------------------------------------------------
async def test_untracked_files_are_fetched_once_and_decide_a_delete(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope
) -> None:
    assert (await check(dev_deps, scope, "rm notes.txt")).allow
    fakes.git.untracked[scope.project_root] = ["notes.txt"]
    verdict = await check(dev_deps, scope, "rm notes.txt")

    assert verdict.reason_code == Reason.DELETE_UNTRACKED.value
    assert [call[0] for call in fakes.git.calls] == ["untracked_files", "untracked_files"]


async def test_a_fact_that_cannot_be_read_fails_closed(dev_deps: DevDeps, fakes: Fakes, scope: GateScope) -> None:
    fakes.git.failures["untracked_files"] = GitFailed(operation="git_untracked", reason="failed")

    verdict = await check(dev_deps, scope, "rm notes.txt")

    assert (verdict.allow, verdict.reason_code) == (False, Reason.DELETE_UNTRACKED.value)


async def test_the_executors_cwd_decides_relative_paths(dev_deps: DevDeps) -> None:
    # Not under tmp_path: that is inside `/private/var/folders`, a temp root where deleting never asks.
    owner_root = Path("/Users/owner/Documents/Projects")
    scope = make_gate_scope(owner_root)
    assert (await check(dev_deps, scope, "rm -rf build")).allow

    verdict = await check(dev_deps, scope, "rm -rf Other", cwd=str(owner_root))  # after an earlier `cd ..`

    assert verdict.reason_code == Reason.DELETE_OUTSIDE_PROJECT.value


async def test_the_turn_start_state_decides_discarding_commands(dev_deps: DevDeps, root: Path) -> None:
    assert (await check(dev_deps, make_gate_scope(root, dirty_before_turn=False), "git checkout -- .")).allow
    dirty = await check(dev_deps, make_gate_scope(root, dirty_before_turn=True), "git checkout -- .")
    unknown = await check(dev_deps, make_gate_scope(root, dirty_before_turn=None), "git stash drop")
    assert dirty.reason_code == unknown.reason_code == Reason.DISCARD_CHANGES.value


async def _register(deps: DevDeps, fakes: Fakes, root: Path, *, workflows: dict[str, str] | None = None) -> Path:
    folder = make_project_folder(root, "BonviHR", workflows=workflows, git_dir=True)
    async with deps.db.uow() as s:
        await make_project(s, at=fakes.clock.now(), path=folder)
    fakes.git.branches[folder] = "feature/login"
    return folder


async def test_a_push_reads_its_remote_and_asks_when_it_points_at_a_server(
    dev_deps: DevDeps, fakes: Fakes, root: Path, scope: GateScope
) -> None:
    folder = await _register(dev_deps, fakes, root)
    fakes.git.remotes[(folder, "origin")] = "git@github.com:abboskhanme/BonviHR.git"
    fakes.git.remotes[(folder, "prod")] = "deploy@prod.example.com:/srv/bonvihr.git"
    fakes.git.statuses[folder] = GitStatus("feature/login", "abc", "origin/feature/login", 1, 0, 0, 0)

    assert (await check(dev_deps, scope, "git push")).allow
    verdict = await check(dev_deps, scope, "git push prod feature/login")

    assert verdict.reason_code == Reason.PUSH_TO_SERVER.value
    asked = [call for call in fakes.git.calls if call[0] == "remote_url"]
    assert asked == [("remote_url", folder, ("origin",)), ("remote_url", folder, ("prod",))]


async def test_a_push_to_a_deploying_branch_asks_from_the_projects_workflows(
    dev_deps: DevDeps, fakes: Fakes, root: Path, scope: GateScope
) -> None:
    workflow = (Path(__file__).parents[3] / "fixtures" / "workflows" / "deploy_on_push_main.yml").read_text()
    folder = await _register(dev_deps, fakes, root, workflows={"deploy.yml": workflow})
    fakes.git.remotes[(folder, "origin")] = "git@github.com:abboskhanme/BonviHR.git"

    assert (await check(dev_deps, scope, "git push origin feature/login")).allow
    verdict = await check(dev_deps, scope, "git push origin main")

    assert verdict.reason_code == Reason.DEPLOY_BRANCH.value
    row = (await decisions(dev_deps))[-1]
    assert row.gate_class == GateClass.DEPLOY_PUSH.value


async def test_recipes_are_read_from_the_project(dev_deps: DevDeps, scope: GateScope) -> None:
    root = scope.project_root
    root.mkdir(parents=True)
    (root / "Makefile").write_text("check: lint\n\tpytest -q\nlint:\n\truff check .\nship:\n\tscp dist.tgz prod:/srv\n")
    (root / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run", "prepare": "husky"}}))
    (root / "run.sh").write_text("#!/bin/sh\nssh prod reboot\n")

    assert (await check(dev_deps, scope, "make check")).allow
    assert (await check(dev_deps, scope, "make ship")).reason_code == Reason.REMOTE_COPY.value
    assert (await check(dev_deps, scope, "npm test")).allow
    assert (await check(dev_deps, scope, "./run.sh")).reason_code == Reason.REMOTE_COMMAND.value


async def test_a_script_that_cannot_be_read_safely_stays_unknown_and_asks(dev_deps: DevDeps, scope: GateScope) -> None:
    root = scope.project_root
    root.mkdir(parents=True)
    os.mkfifo(root / "run.sh")  # opening a FIFO would block the reader for good

    verdict = await check(dev_deps, scope, "./run.sh")

    assert verdict.reason_code == Reason.RECIPE_UNKNOWN.value


def test_the_fact_reader_never_opens_yordamchis_own_files(paths: Paths) -> None:
    secret = paths.data / "dev.env"
    secret.parent.mkdir(parents=True)
    secret.write_text("DEV_BOT_TOKEN=x\n", encoding="utf-8")
    private = (paths.data, paths.config, paths.home / ".claude")

    with pytest.raises(PermissionError):
        service._read_prefix(secret, 100, private)
    assert service._read_prefix(paths.home / "missing.sh", 100, private) is None


async def test_the_projects_db_config_decides_a_migration(dev_deps: DevDeps, scope: GateScope) -> None:
    root = scope.project_root
    root.mkdir(parents=True)
    assert (await check(dev_deps, scope, "alembic upgrade head")).allow
    (root / ".env").write_text("DATABASE_URL=postgres://app:pw@db.prod.internal/app\n")

    verdict = await check(dev_deps, scope, "alembic upgrade head")

    assert verdict.reason_code == Reason.MIGRATION_REMOTE_DB.value
    assert "pw" not in json.dumps([row.target for row in await decisions(dev_deps)])


# --- the shim backend (`gate_check`) --------------------------------------------------------------------------------
def shim_request(*argv: str, tool: str = "ssh", cwd: str = "") -> ShimRequest:
    return ShimRequest(tool=tool, argv=argv, cwd=cwd)


async def test_a_shim_runs_read_only_commands_and_refuses_writes(dev_deps: DevDeps, scope: GateScope) -> None:
    allowed = await service.check_shim(dev_deps, scope, shim_request("prod", "uptime"))
    refused = await service.check_shim(dev_deps, scope, shim_request("prod", "docker compose up -d"))

    assert (allowed.allow, refused.allow, refused.reason) == (True, False, Reason.REMOTE_COMMAND.value)
    assert await decisions(dev_deps) == []  # a shim never asks
    (event,) = await events(dev_deps, AuditKind.GATE_VERDICT)
    assert as_object(event.data_json)["outcome"] == "shim_refused"


async def test_a_shim_runs_what_a_grant_consumed_moments_ago_covers(
    dev_deps: DevDeps, fakes: Fakes, root: Path, scope: GateScope
) -> None:
    covered = shim_request("prod", "docker compose up -d")
    await check(dev_deps, scope, DEPLOY)
    await answer(dev_deps, "ha")
    assert not (await service.check_shim(dev_deps, scope, covered)).allow  # approved, not yet run
    assert (await check(dev_deps, scope, DEPLOY)).allow  # Layer 1 consumes the grant

    assert (await service.check_shim(dev_deps, scope, covered)).allow
    assert not (await service.check_shim(dev_deps, scope, shim_request("prod", "reboot"))).allow
    other_session = make_gate_scope(root, session_key="agent-session-8")
    assert not (await service.check_shim(dev_deps, other_session, covered)).allow
    await fakes.clock.advance(121)
    assert not (await service.check_shim(dev_deps, scope, covered)).allow  # past `gate.shim_grant_window_s`


async def test_an_unknown_session_token_is_refused(dev_deps: DevDeps) -> None:
    answered = await service.check_shim(dev_deps, None, shim_request("prod", "uptime"))
    assert (answered.allow, answered.reason) == (False, "unknown_session")


# --- the pre-push backend (`secret_blocked`) ------------------------------------------------------------------------
REPORT = SecretReport(cwd="/x", file="config/.env", line=3, rule="env_file")


async def test_a_blocked_push_tells_the_owner_the_file_and_line(
    dev_deps: DevDeps, fakes: Fakes, scope: GateScope
) -> None:
    answered = await service.report_secret_block(dev_deps, scope, REPORT)

    assert (answered.ok, answered.enforce) == (True, True)
    assert texts(fakes) == [str(S.PUSH_BLOCKED_SECRET(project="BonviHR", file="config/.env", line=3, rule="env_file"))]
    (event,) = await events(dev_deps, AuditKind.GATE_VERDICT)
    assert event.data_json == {
        "session_key": GATE_SESSION_KEY,
        "file": "config/.env",
        "line": 3,
        "rule": "env_file",
        "enforced": True,
    }


async def test_the_owner_can_switch_the_secret_scan_off(dev_deps: DevDeps, fakes: Fakes, scope: GateScope) -> None:
    await add_setting_row(dev_deps.db, "gate.secret_scan_enabled", False, at=fakes.clock.now())

    answered = await service.report_secret_block(dev_deps, scope, REPORT)

    assert answered.enforce is False
    assert fakes.messenger.sent() == []
    assert len(await events(dev_deps, AuditKind.GATE_VERDICT)) == 1  # still audited


async def test_an_unknown_session_is_always_blocked(dev_deps: DevDeps, fakes: Fakes) -> None:
    await add_setting_row(dev_deps.db, "gate.secret_scan_enabled", False, at=fakes.clock.now())
    assert (await service.report_secret_block(dev_deps, None, REPORT)).enforce


# --- audit (NFR-SEC-10) and retention (SPEC §4.2) -------------------------------------------------------------------
async def test_every_decision_step_is_audited(dev_deps: DevDeps, scope: GateScope) -> None:
    await check(dev_deps, scope, DEPLOY)
    await check(dev_deps, scope, DEPLOY)
    await answer(dev_deps, "ha")
    await check(dev_deps, scope, DEPLOY)

    kinds = [event.kind for event in await events(dev_deps)]
    assert kinds == ["decision_asked", "gate_verdict", "decision_resolved", "grant_consumed"]
    assert all(event.ref_id in ("1", "toolu_01") for event in await events(dev_deps))


async def test_retention_deletes_only_old_finished_decisions(dev_deps: DevDeps, fakes: Fakes) -> None:
    old = fakes.clock.now() - timedelta(days=91)
    async with dev_deps.db.uow() as s:
        await make_decision(s, at=old, status="consumed")
        await make_decision(s, at=old, status="pending")
        await make_decision(s, at=fakes.clock.now() - timedelta(days=10), status="denied")

    assert await service.apply_retention(dev_deps) == 1
    assert [row.status for row in await decisions(dev_deps)] == ["pending", "denied"]


# --- the catalog the questions use ----------------------------------------------------------------------------------
def test_every_reason_and_class_has_its_owner_text() -> None:
    assert set(S.GATE_CONSEQUENCES) == {reason.value for reason in Reason} - {Reason.FREE.value}
    assert set(S.DECISION_QUESTIONS) == {item.value for item in GateClass} | {"unclear"}
    for question in S.DECISION_QUESTIONS.values():
        text = question(project="BonviHR", decision_id=17, command="vercel --prod", where="prod", consequence="x")
        assert text.startswith("BonviHR · Muhim qaror #17: ")


def test_a_tool_call_carries_the_executors_cwd() -> None:
    assert ToolCall("Bash", {}, "t", None, "s").cwd is None
