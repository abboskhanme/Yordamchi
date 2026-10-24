"""What the owner reads, family by family (T35; CONVENTIONS §10, §11; NFR-LANG-01): there is no other UI.

Every case is rendered by the function the service calls (`turns.domain.result_text`, `health.domain.holat_text`,
`gate.service.check_call` → the question the fake Messenger got, `bots.dev.app.prepare` → its notice, …) on fixed
inputs, and compared with `tests/golden/<family>.txt`. A changed word fails here with the diff. After a reviewed
change, regenerate with `pytest tests/unit/test_golden_messages.py --update-golden` and review `git diff tests/golden`.

The settings list (UC-09) and "xarajat?" (UC-07) goldens are older and live with their features
(`tests/unit/shared/settings/test_listing.py`, `tests/unit/shared/cost/test_report.py`); they honour the same flag.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import pytest

from tests.factories import (
    DEV_BOT_TOKEN,
    DEV_GROUP_ID,
    JUMP_HOST,
    OWNER_ID,
    SAMPLE_CONTAINERS,
    SAMPLE_DF,
    SAMPLE_FREE,
    SAMPLE_UPTIME,
    SERVER_HOST,
    Fakes,
    agent_question,
    df_with_root_at,
    docker_ps_table,
    make_bash_call,
    make_default_snapshot,
    make_gate_scope,
    make_part,
    make_paths,
    make_project,
    make_project_folder,
    make_server_check,
    make_turn_input,
    make_turn_summary,
    make_voice_part,
    open_test_db,
    projects_root_of,
)
from tests.fakes.clock import FakeClock
from tests.fakes.security import FakeSecurity
from tests.goldens import Case, check_cases
from yordamchi.bots.dev import app, intents
from yordamchi.core import keychain, proc, redact
from yordamchi.core import strings as S
from yordamchi.core.config import DevConfig
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName, Paths
from yordamchi.core.text import BotText, join
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.digest import domain as digest
from yordamchi.dev.digest.domain import CommitLine, Digest, Freshness, GitState, ProjectState, ResultLine
from yordamchi.dev.gate import domain as gate_domain
from yordamchi.dev.gate import service as gate
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.domain import GateClass
from yordamchi.dev.questions import domain as questions
from yordamchi.dev.routing import domain as routing
from yordamchi.dev.routing.domain import Candidate, Intent, Method, Outcome, RouteDecision
from yordamchi.dev.servers import domain as servers
from yordamchi.dev.servers.domain import NO_STATE, HostStatus, Limits, Probe, ProbeKind, ProbeOutcome, ProbeStatus
from yordamchi.dev.turns import domain as turns
from yordamchi.dev.turns.domain import ProgressKind, QueueReason, TurnStatus, WorkFacts
from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState
from yordamchi.shared.health import domain as health
from yordamchi.shared.health.domain import GIB, UNREAD, ClaudeCheck, ErrorNote, Gap, HealthView, QueuedWork, WorkStatus
from yordamchi.shared.inbox.domain import UpdateKind
from yordamchi.shared.outbox.domain import TEXT_LIMIT, tg_len
from yordamchi.shared.settings import defaults

ROOT = Path(__file__).resolve().parents[2]
PROJECT: Final = "BonviHR"
OTHER: Final = "HRMentor"
NOW: Final = datetime(2026, 10, 24, 9, 5, tzinfo=UTC)  # 14:05 in Tashkent
TZ: Final = make_default_snapshot()[defaults.UI_TIMEZONE]
FINAL: Final = (
    "Ishni tugatdim.\n\nNatija: Login sahifasidagi xato tuzatildi.\nTestlar: o'tdi (42 passed)\n"
    "<handoff>\nGoal: fix login\nDecisions: none\nState: pushed\nNext: nothing\n</handoff>"
)
scrub = redact.scrubbed


def ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


def golden(config: pytest.Config, name: str, cases: Sequence[Case], *, project: str | None = None) -> None:
    """Every case fits one Telegram message as it is (no split, no cut); a project family's every case starts with
    the project's name (CONVENTIONS §10). Then the file comparison."""
    too_long = [title for title, text in cases if tg_len(text) > TEXT_LIMIT]
    assert too_long == [], f"longer than one Telegram message: {too_long}"
    if project is not None:
        unnamed = [title for title, text in cases if not text.startswith(project)]
        assert unnamed == [], f"does not start with {project}: {unnamed}"
    check_cases(config, name, cases)


# --- progress line (UC-11, SPEC §6.2) -------------------------------------------------------------------------------
def test_the_progress_line(pytestconfig: pytest.Config) -> None:
    steps = [
        turns.progress_line(ProgressKind.TOOL, "Read", "src/app/login.py"),
        turns.progress_line(ProgressKind.TOOL, "Edit", "src/app/login.py"),
        turns.progress_line(ProgressKind.TOOL, "Bash", "make   test"),
        turns.progress_line(ProgressKind.TEXT, None, "Testlar o'tdi, endi commit qilaman."),
    ]
    minutes = turns.minutes_of(17 * 60 + 20)
    cases: list[Case] = [
        ("just started, no step yet", turns.progress_text(PROJECT, seconds=0, steps=0, latest=())),
        (
            "working: the three latest of four steps",
            turns.progress_text(PROJECT, seconds=4 * 60, steps=12, latest=steps),
        ),
        # The other states replace the line in place; turns.service renders each with these entries and minutes_of.
        ("waiting for the owner's answer", S.TURN_PROGRESS_WAITING(project=PROJECT, minutes=minutes, steps=14)),
        ("paused by the runaway guard", S.TURN_PROGRESS_PAUSED(project=PROJECT, minutes=minutes, steps=14)),
        ("stopped by the owner", S.TURN_PROGRESS_STOPPED(project=PROJECT, minutes=minutes, steps=14)),
        ("finished", S.TURN_PROGRESS_FINISHED(project=PROJECT, minutes=minutes, steps=14)),
    ]
    golden(pytestconfig, "progress.txt", cases, project=PROJECT)


# --- final result (UC-11, SPEC §6.2) --------------------------------------------------------------------------------
PUSHED: Final = WorkFacts(
    end_head="a1b2c3d4e5f6",
    commits=("a1b2c3d4e5f6",),
    changed=("src/app/login.py", "tests/test_login.py"),
    upstream="origin/main",
    ahead=0,
)


def _result(parsed: str, facts: WorkFacts, *, minutes: float, push_missed: bool = False) -> BotText:
    return turns.result_text(
        PROJECT, turns.parse_result(parsed), facts, seconds=minutes * 60, push_missed=push_missed, scrub=scrub
    )


def test_the_final_result(pytestconfig: pytest.Config) -> None:
    many = WorkFacts(
        end_head="0f1e2d3c4b5a",
        commits=("0f1e2d3c4b5a",),
        changed=tuple(f"src/reports/page_{n}.py" for n in range(7)),
        created=("docs/reports.md",),
        upstream="origin/main",
        ahead=1,
    )
    cases: list[Case] = [
        ("committed and pushed", _result(FINAL, PUSHED, minutes=7)),
        (
            "nothing changed, tests unknown",
            _result("Natija: Hech narsa o'zgarmadi.", WorkFacts(end_head="x"), minutes=0),
        ),
        (
            "tests failed, eight files, committed but not pushed",
            _result("Natija: Hisobot sahifalari qo'shildi.\nTestlar: yiqildi (3 failed, 40 passed)", many, minutes=26),
        ),
        ("still unpushed after the nudge", _result("Natija: Hisobot qo'shildi.", many, minutes=31, push_missed=True)),
        ("git could not be read", _result(FINAL, WorkFacts(end_head=None, known=False), minutes=3)),
        ("no Natija line in an empty answer", _result("", WorkFacts(end_head="x"), minutes=1)),
    ]
    golden(pytestconfig, "result.txt", cases, project=PROJECT)


# --- "holat?" (UC-06, SPEC §6.2) ------------------------------------------------------------------------------------
MANAGER: Final = ClaudeManagerState(
    active_id="b",
    auto_switch=True,
    accounts=(
        ClaudeAccount("other@example.com", "ok", 90.0, 70.0, None, None),
        ClaudeAccount("owner@example.com", "ok", 42.0, 13.0, None, None, is_active=True),
    ),
)
CHECK: Final = ClaudeCheck(at=NOW, installed=True, version="2.1.310", logged_in=True, auth_method="claude.ai")


def _view(**changes: object) -> HealthView:
    fields: dict[str, object] = {
        "bot": BotName.DEV,
        "now": NOW,
        "timezone": TZ,
        "started_at": NOW - timedelta(hours=3, minutes=12),
        "version": "2026.10.24-abcdef0",
        "sdk_version": "0.2.173",
        "claude": CHECK,
        "manager": MANAGER,
        "speech_providers": ("google", "openrouter"),
        "work": WorkStatus(running=(PROJECT, OTHER), queued=(QueuedWork("Wunderkind", 1),), decisions=1, questions=2),
        "pressure_level": 2,
        "free_disk_bytes": 85 * GIB + 5,
        "degraded": ("ffmpeg", "gh"),
        "errors": (ErrorNote("stt_failed", NOW - timedelta(minutes=30)), ErrorNote("git_failed", NOW)),
    }
    fields.update(changes)
    return HealthView(**fields)  # type: ignore[arg-type]  # each case passes values of the field's own type


def test_holat(pytestconfig: pytest.Config) -> None:
    logged_out = ClaudeCheck(at=NOW, installed=True, version="2.1.310", logged_in=False)
    api_key = ClaudeCheck(at=NOW, installed=True, version="2.1.310", logged_in=True, auth_method="api_key")
    cases: list[Case] = [
        ("dev bot, everything known", health.holat_text(_view())),
        (
            "dev bot, idle, two days up, nothing degraded",
            health.holat_text(
                _view(started_at=NOW - timedelta(days=2, hours=5), work=WorkStatus(), degraded=(), errors=())
            ),
        ),
        (
            "personal bot, no speech key, high pressure",
            health.holat_text(
                _view(bot=BotName.PERSONAL, work=None, speech_providers=(), pressure_level=4, degraded=(), errors=())
            ),
        ),
        (
            "parts that could not be read",
            health.holat_text(
                _view(
                    claude=None,
                    manager=UNREAD,
                    work=UNREAD,
                    speech_providers=UNREAD,
                    pressure_level=UNREAD,
                    free_disk_bytes=UNREAD,
                    sdk_version="",
                )
            ),
        ),
        ("Claude logged out", health.holat_text(_view(claude=logged_out, manager=None))),
        ("Claude on an API key instead of claude.ai", health.holat_text(_view(claude=api_key))),
        ("claude not installed", health.holat_text(_view(claude=ClaudeCheck(at=NOW)))),
    ]
    golden(pytestconfig, "holat.txt", cases)


# --- an agent's question (UC-12, SPEC §3.2.2) -----------------------------------------------------------------------
def _asked(*raw: dict[str, JsonValue]) -> BotText:
    """questions.service: the agent's `questions` input, scrubbed, then the owner's text."""
    return questions.question_text(PROJECT, questions.parse_questions(raw, scrub))


def test_an_agent_question(pytestconfig: pytest.Config) -> None:
    cases: list[Case] = [
        ("one question, described options", _asked(agent_question())),
        (
            "several may be picked",
            _asked(
                agent_question(
                    "Qaysi sahifalarga hisobot qo'shamiz?",
                    [("Xodimlar", ""), ("Bo'limlar", "jami soni bilan"), ("Ta'tillar", "")],
                    multi_select=True,
                )
            ),
        ),
        ("no options", _asked(agent_question("Deploy uchun qaysi domen?", []))),
        (
            "two questions with headers",
            _asked(
                agent_question(),
                agent_question("Migratsiyani hozir yozaymi?", [("Ha", ""), ("Keyin", "avval testlar")], header=""),
            ),
        ),
    ]
    golden(pytestconfig, "question.txt", cases, project=PROJECT)


# --- an important decision, one per gate class (UC-13, SPEC §3.2.3 step 4, §5.7) ------------------------------------
# A real command per class through `gate.service.check_call`: the gate classifies it, records the decision and sends
# its one question; the golden is the text the Messenger got.
GATE_COMMANDS: Final[tuple[tuple[str, str], ...]] = (
    (GateClass.DEPLOY, "vercel deploy --prod"),
    (GateClass.SERVER_WRITE, "ssh prod 'docker compose up -d'"),
    (GateClass.DEPLOY_PUSH, "git push origin main"),
    (GateClass.FORCE_PUSH, "git push --force origin main"),
    (GateClass.DATA_LOSS_DB, "dropdb bonvihr"),
    (GateClass.DOCKER_DESTROY, "docker compose down -v"),
    (GateClass.FS_DESTROY, "rm -rf .git"),
    (GateClass.REPO_DELETE, "gh repo delete abboskhanme/BonviHR --yes"),
    (GateClass.REPO_PUBLIC, "gh repo edit --visibility public"),
    (GateClass.SPEND, "aws ec2 run-instances --instance-type t3.large"),
    (GateClass.THIRD_PARTY_SEND, "sendmail boss@example.com < report.txt"),
    ("unclear", "$RUNNER build"),  # an ask with no class: the program is only known at run time
)


async def test_an_important_decision_question_per_gate_class(
    pytestconfig: pytest.Config, dev_deps: DevDeps, fakes: Fakes, paths: Paths
) -> None:
    root = projects_root_of(paths)
    workflow = (ROOT / "tests" / "fixtures" / "workflows" / "deploy_on_push_main.yml").read_text(encoding="utf-8")
    folder = make_project_folder(root, PROJECT, git_dir=True, workflows={"deploy.yml": workflow})
    async with dev_deps.db.uow() as s:
        await make_project(s, PROJECT, at=fakes.clock.now(), path=folder)
    fakes.git.remotes[(folder, "origin")] = "git@github.com:abboskhanme/BonviHR.git"
    scope = make_gate_scope(root)
    cases: list[Case] = []
    for number, (gate_class, command) in enumerate(GATE_COMMANDS, start=1):
        verdict = await gate.check_call(dev_deps, scope, make_bash_call(command, tool_use_id=f"toolu_{number:02}"))
        assert not verdict.allow, command
        async with dev_deps.db.read() as s:
            row = await gate_store.get_decision(s, number)
        assert row is not None, command
        assert (row.gate_class or "unclear") == gate_class, command
        cases.append((f"{gate_class}: {command}", str(fakes.messenger.sent()[-1].payload["text"])))
    # `architecture` is asked by prompt instruction only (gate.domain never detects it): no gate path stores such a
    # row today. Rendered from a stored row, so the template a reviewer approves is the one the owner would get.
    async with dev_deps.db.uow() as s:
        row = await gate_store.add_decision(
            s,
            kind="gate",
            project_id=PROJECT,
            turn_id=31,
            gate_class=GateClass.ARCHITECTURE.value,
            tool_name="AskUserQuestion",
            input_sha256=None,
            input_redacted="",
            target=None,
            payload={},
            created_at=fakes.clock.now(),
            expires_at=gate_domain.decision_expires_at(fakes.clock.now(), 120),
        )
    assert await gate.post_question(dev_deps, row.id)
    cases.append(("architecture: no detector creates this row", str(fakes.messenger.sent()[-1].payload["text"])))
    assert len(fakes.messenger.sent()) == len(GATE_COMMANDS) + 1  # one question per decision, no other text
    assert {gate_class for gate_class, _ in GATE_COMMANDS} | {GateClass.ARCHITECTURE} == {*GateClass, "unclear"}
    golden(pytestconfig, "decision.txt", cases, project=PROJECT)


# --- routing's one clarification (UC-10 F, UC-02 AC) ----------------------------------------------------------------
def _ask(item_text: str | None, *candidates: str, kind: UpdateKind | None = None) -> BotText:
    if kind is UpdateKind.VOICE:
        item = make_turn_input(make_voice_part(1, at_s=0, transcript="buni ham tuzat"))
    else:
        item = make_turn_input(make_part(1, kind=kind, text=item_text))
    decision = RouteDecision(
        Outcome.ASK, Method.LLM, Intent.INSTRUCT, None, 0.5, tuple(Candidate(name, 0.4) for name in candidates)
    )
    reply = routing.first_reply(decision, item, scrub)
    assert reply is not None
    return reply


def test_the_routing_clarification(pytestconfig: pytest.Config) -> None:
    unanswered = routing.OpenClarification(
        id=1,
        burst_id=1,
        kind=routing.ClarificationKind.WHICH_PROJECT,
        candidates=(PROJECT, OTHER),
        message_id=101,
        asked_at=ago(minutes=40),
    )
    cases: list[Case] = [
        ("two candidates", _ask("login sahifasini tuzat", PROJECT, OTHER)),
        ("three candidates, a photo", _ask(None, PROJECT, OTHER, "Wunderkind", kind=UpdateKind.PHOTO)),
        ("one candidate, a voice note", _ask(None, PROJECT, kind=UpdateKind.VOICE)),
        ("no candidate, a long text", _ask("bu yerda " * 30 + "tugmani ko'k qil")),
        ("the answer's number is out of range", routing.out_of_range_text([PROJECT, OTHER])),
        ("no answer came", routing.clarification_expired_text(unanswered)),
    ]
    golden(pytestconfig, "routing.txt", cases)


# --- queued (SPEC §5.6, NFR-CAP-01/05) ------------------------------------------------------------------------------
def test_the_queued_notice(pytestconfig: pytest.Config) -> None:
    cases: list[Case] = [
        (f"{reason.value}", turns.queue_notice(PROJECT, position, reason, pressure_level=2))
        for position, reason in enumerate(QueueReason, start=1)
    ]
    critical = turns.queue_notice(PROJECT, 1, QueueReason.PRESSURE, pressure_level=turns.CRITICAL_PRESSURE)
    cases.append(("pressure, critical", critical))
    golden(pytestconfig, "queued.txt", cases, project=PROJECT)


# --- a report with its freshness line (UC-15, SPEC §3.2.4) ----------------------------------------------------------
def _digest(**changes: object) -> Digest:
    fields: dict[str, object] = {
        "project_id": PROJECT,
        "checked_at": ago(minutes=3),
        "updated_at": ago(minutes=3),
        "git": GitState(branch="main", head="a1b2c3d", upstream="origin/main", head_at=ago(hours=1)),
        "commits": (CommitLine("a1b2c3d", "fix login", ago(hours=1)), CommitLine("9f8e7d6", "add tests", ago(days=2))),
    }
    fields.update(changes)
    return Digest(**fields)  # type: ignore[arg-type]  # each case passes values of the field's own type


def _report(body: BotText, fresh: Freshness) -> BotText:
    """digest.service: the template body, then the line saying how old its data is."""
    return join([body, digest.freshness_line(fresh, now=NOW)])


def _project(state: ProjectState, fresh: Freshness) -> BotText:
    return _report(digest.project_report(state, now=NOW, timezone=TZ, scrub=scrub), fresh)


def test_a_report_with_its_freshness_line(pytestconfig: pytest.Config) -> None:
    busy = ProjectState(
        PROJECT,
        _digest(
            git=GitState(branch="main", head="a1", upstream="origin/main", ahead=2, dirty=1, untracked=2),
            files=("src/app/login.py", "tests/test_login.py"),
            files_total=2,
        ),
        (
            make_turn_summary(5, status=TurnStatus.RUNNING, started_at=ago(minutes=12)),
            make_turn_summary(7, status=TurnStatus.WAITING_QUESTION, title="Hisobot", started_at=ago(minutes=4)),
            make_turn_summary(6, status=TurnStatus.QUEUED, title="Keyingisi"),
            make_turn_summary(
                4, status=TurnStatus.DONE, ended_at=ago(minutes=40), natija="Xato tuzatildi.", tests="passed"
            ),
        ),
        decisions=1,
    )
    failed = make_turn_summary(4, status=TurnStatus.FAILED, ended_at=ago(days=1), title="Deploy")
    old = ProjectState(PROJECT, _digest(commits=(CommitLine("a1", "old fix", ago(days=2)),)), (failed,))
    hr = ProjectState(
        OTHER,
        _digest(
            project_id=OTHER,
            commits=(CommitLine("b1", "add tests", ago(minutes=30)), CommitLine("b0", "init", ago(minutes=50))),
            results=(ResultLine(3, TurnStatus.DONE, "Tests", "Testlar qo'shildi.", "passed", ago(minutes=29)),),
        ),
        decisions=1,
    )
    running = ProjectState(
        PROJECT, _digest(), (make_turn_summary(5, status=TurnStatus.RUNNING, started_at=ago(minutes=7)),)
    )
    quiet = ProjectState("Wunderkind", _digest(project_id="Wunderkind", commits=()))
    checks = [
        make_server_check("web", at=ago(minutes=12)),
        make_server_check("db", at=ago(minutes=12), ok=False, summary="disk 95%"),
    ]
    cases: list[Case] = [
        ("a project at work, fresh digest", _project(busy, Freshness(ago(minutes=3)))),
        (
            "first report: the digest was collected now",
            _project(ProjectState(PROJECT, None), Freshness(None, refreshed=True)),
        ),
        ("a failed last turn, digest read just now", _project(old, Freshness(ago(seconds=20)))),
        (
            "a stale digest refreshed first",
            _project(running, Freshness(ago(seconds=1), refreshed=True, previous_at=ago(minutes=45))),
        ),
        ("a stale digest whose refresh failed", _project(running, Freshness(ago(hours=2), failed=True))),
        ("git never read", _project(ProjectState(PROJECT, None), Freshness(None))),
        (
            "every project at once, with the servers",
            _report(
                digest.all_report([running, hr, quiet], checks, now=NOW, timezone=TZ, scrub=scrub),
                Freshness(ago(minutes=5)),
            ),
        ),
        (
            "a quiet day",
            _report(digest.all_report([quiet], [], now=NOW, timezone=TZ, scrub=scrub), Freshness(ago(days=3))),
        ),
        (
            "servers from fresh checks",
            _report(digest.servers_line(checks, now=NOW), Freshness(min(check.at for check in checks))),
        ),
    ]
    golden(pytestconfig, "report.txt", cases)
    assert all(text.splitlines()[-1].startswith("Ma'lumot:") for _, text in cases)  # UC-15: every answer says so


# --- server status (UC-15, SPEC §3.2.4) -----------------------------------------------------------------------------
def _ok(kind: ProbeKind, stdout: str) -> ProbeOutcome:
    command = {
        ProbeKind.UPTIME: servers.UPTIME_COMMAND,
        ProbeKind.DISK: servers.DISK_COMMAND,
        ProbeKind.MEMORY: servers.MEMORY_COMMAND,
        ProbeKind.CONTAINERS: servers.CONTAINERS_COMMAND,
        ProbeKind.UNITS: servers.UNITS_COMMAND,
    }[kind]
    return ProbeOutcome(Probe(kind, command), ProbeStatus.OK, exit_code=0, stdout=stdout)


def _checked(host: str, outcomes: Sequence[ProbeOutcome], *, minutes_ago: float) -> HostStatus:
    """servers.service: one check evaluated, then read back as the cached status a reader gets."""
    at = ago(minutes=minutes_ago)
    evaluation = servers.evaluate(
        host, outcomes, NO_STATE, now=at, limits=Limits(disk_warn_pct=90, memory_warn_pct=90), scrub=scrub
    )
    return servers.status_of(
        host, checked_at=at, report=evaluation.report, state=evaluation.state, error_code=evaluation.report.error_code
    )


def _healthy(**overrides: ProbeOutcome) -> list[ProbeOutcome]:
    outcomes = {
        "uptime": _ok(ProbeKind.UPTIME, SAMPLE_UPTIME),
        "disk": _ok(ProbeKind.DISK, SAMPLE_DF),
        "memory": _ok(ProbeKind.MEMORY, SAMPLE_FREE),
        "containers": _ok(ProbeKind.CONTAINERS, docker_ps_table(SAMPLE_CONTAINERS)),
        "units": _ok(ProbeKind.UNITS, ""),
    }
    outcomes.update(overrides)
    return list(outcomes.values())


def test_the_server_status(pytestconfig: pytest.Config) -> None:
    healthy = _checked(SERVER_HOST, _healthy(), minutes_ago=12)
    full = _checked(SERVER_HOST, _healthy(disk=_ok(ProbeKind.DISK, df_with_root_at(92))), minutes_ago=12)
    rejected = ProbeOutcome(
        Probe(ProbeKind.UPTIME, servers.UPTIME_COMMAND), ProbeStatus.ERROR, error_code="auth_failed"
    )
    unreachable = _checked("db-1", [rejected], minutes_ago=2)
    cases: list[Case] = [
        ("one healthy host", servers.status_answer([healthy], NOW)),
        ("a full disk", servers.status_answer([full], NOW)),
        (
            "every kind of host",
            servers.status_answer(
                [healthy, unreachable, HostStatus(JUMP_HOST, jump_only=True), HostStatus("new-host")], NOW
            ),
        ),
        ("checked just now", servers.status_answer([_checked(SERVER_HOST, _healthy(), minutes_ago=0)], NOW)),
        ("the alert for a new issue", servers.alert_text(SERVER_HOST, full.issues)),
    ]
    golden(pytestconfig, "servers.txt", cases)


# --- after a restart, a sleep or an outage (SPEC §2.5 step 8, §2.8; NFR-REL) ---------------------------------------
def test_the_restart_notice(pytestconfig: pytest.Config) -> None:
    cases: list[Case] = [
        (
            "two turns cut off",
            health.restart_text([PROJECT, OTHER, PROJECT], last_alive_at=ago(minutes=40), timezone=TZ),
        ),
        ("nothing cut off", health.restart_text([], last_alive_at=ago(minutes=2), timezone=TZ)),
        ("no heartbeat file to read", health.restart_text([PROJECT], last_alive_at=None, timezone=TZ)),
    ]
    golden(pytestconfig, "restart.txt", cases)


def test_the_wake_and_network_gaps(pytestconfig: pytest.Config) -> None:
    night = Gap(from_=ago(hours=7, minutes=20), to=ago(minutes=5))
    outage = health.network_gap(ago(minutes=48), NOW)
    assert outage is not None
    cases: list[Case] = [
        ("the Mac slept", health.wake_text(night, updates=3, timezone=TZ)),
        ("the Mac slept, nothing came", health.wake_text(night, updates=0, timezone=TZ)),
        ("the network was down", health.network_gap_text(outage, timezone=TZ)),
    ]
    golden(pytestconfig, "gaps.txt", cases)


# --- a later phase's request (SPEC §7) ------------------------------------------------------------------------------
def test_not_yet(pytestconfig: pytest.Config) -> None:
    phrases = ("integratsiyalar", "yangi loyiha: kutubxona uchun bot", "o'zingga yangi buyruq qo'sh")
    cases: list[Case] = []
    for phrase in phrases:
        command = intents.command_of(phrase)
        assert command is not None, phrase
        assert not command.built, phrase
        cases.append((f"«{phrase}»", S.NOT_YET(phase=command.phase)))  # bots/dev/pipeline.py answers with this
    golden(pytestconfig, "not_yet.txt", cases)


# --- startup refusal (SPEC §2.5 steps 3–8, NFR-SEC-04) --------------------------------------------------------------
class Notices:
    def __init__(self) -> None:
        self.sent: list[tuple[int, BotText]] = []

    async def __call__(self, config: DevConfig, text: BotText) -> None:
        self.sent.append((config.dev_group_id, text))


def _environ(paths: Paths, **extra: str) -> dict[str, str]:
    env_file = paths.env_file(BotName.DEV)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    token = keychain.DEV_BOT_TOKEN.reference  # dev.env names the Keychain item, never the token itself
    env_file.write_text(f"DEV_BOT_TOKEN={token}\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID={DEV_GROUP_ID}\n")
    return {
        "YORDAMCHI_DATA_DIR": str(paths.data),
        "YORDAMCHI_CONFIG_DIR": str(paths.config),
        "YORDAMCHI_LOG_DIR": str(paths.logs),
        "YORDAMCHI_ENV_FILE": str(env_file),
        **extra,
    }


def _binaries(paths: Paths, *names: str) -> None:
    paths.local_bin.mkdir(parents=True, exist_ok=True)
    for name in names:
        binary = paths.local_bin / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)


async def _no_login_shell(environ: object) -> str | None:
    del environ
    return None


async def _refusal(paths: Paths, environ: dict[str, str]) -> BotText:
    """`app.prepare` as launchd's start runs it: exit 78 and exactly one notice to the dev group."""
    notices = Notices()
    outcome = await app.prepare(
        environ,
        clock=FakeClock(),
        notify=notices,
        home=paths.home,
        version="2026.10.24-abc1234",
        login_path=_no_login_shell,
    )
    assert outcome == proc.EXIT_REFUSED
    [(chat_id, text)] = notices.sent
    assert chat_id == DEV_GROUP_ID
    return text


async def test_the_startup_refusal(
    pytestconfig: pytest.Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    security = FakeSecurity()  # as tests/unit/bots/dev/test_app.py: never the real Keychain
    security.put(keychain.DEV_BOT_TOKEN, DEV_BOT_TOKEN)
    monkeypatch.setattr(keychain, "security_runner", security)

    env_paths = make_paths(tmp_path / "env")
    by_env = await _refusal(env_paths, _environ(env_paths, CLAUDE_CODE_OAUTH_TOKEN="sk-ant-oat01-not-a-real-token"))

    helper_paths = make_paths(tmp_path / "helper")
    helper_paths.claude_user_settings.parent.mkdir(parents=True)
    helper_paths.claude_user_settings.write_text('{"apiKeyHelper": "/usr/local/bin/key"}')
    by_helper = await _refusal(helper_paths, _environ(helper_paths))

    missing_paths = make_paths(tmp_path / "missing")
    _binaries(missing_paths, "git")
    missing = await _refusal(missing_paths, _environ(missing_paths, PATH=str(missing_paths.local_bin)))

    schema_paths = make_paths(tmp_path / "schema")
    _binaries(schema_paths, "claude", "git")
    db = await open_test_db(schema_paths.db_file(BotName.DEV))  # every table, but no alembic revision
    await db.dispose()
    schema = await _refusal(schema_paths, _environ(schema_paths))

    cases: list[Case] = [
        ("a Claude token in the environment", by_env),
        ("an apiKeyHelper in ~/.claude/settings.json", by_helper),
        ("claude not on the PATH", missing),
        ("dev.db behind this version", schema),
    ]
    assert "sk-ant-oat01" not in by_env  # names only, never the value
    golden(pytestconfig, "startup.txt", cases)
