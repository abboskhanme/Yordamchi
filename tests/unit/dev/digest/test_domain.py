"""digest.domain: which path a report takes, staleness and polling, the templates, the freshness line, the Assistant's
inputs and the handoff note (SPEC §3.2.4; UC-15). Pure: every input is an argument, on fixed instants."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

import pytest

from tests.factories import (
    FAKE_KEYS,
    make_part,
    make_project_record,
    make_report_question,
    make_server_check,
    make_turn_input,
    make_turn_summary,
)
from tests.owner_text import shown
from yordamchi.core import redact
from yordamchi.dev.digest import domain
from yordamchi.dev.digest.domain import (
    CommitLine,
    Digest,
    Freshness,
    GitRead,
    GitState,
    HandoffState,
    ProjectState,
    ReportMode,
    ReportScope,
    ResultLine,
)
from yordamchi.dev.projects.domain import ProjectPrefs, project_view
from yordamchi.dev.routing.domain import Intent, Method, Outcome, RouteDecision
from yordamchi.dev.turns.domain import HandoffNote, TurnKind, TurnStatus
from yordamchi.ports.git import Commit, GitStatus

TZ = "Asia/Tashkent"  # UTC+5
NOW = datetime(2026, 10, 23, 9, 0, tzinfo=UTC)  # 14:00 in Tashkent
TODAY_START = datetime(2026, 10, 22, 19, 0, tzinfo=UTC)  # 00:00 in Tashkent
NAMES = ("BonviHR", "HRMentor", "bonvi")
scrub = redact.scrubbed


def ago(**delta: float) -> datetime:
    return NOW - timedelta(**delta)


def make_digest(**changes: object) -> Digest:
    base = Digest(
        project_id="BonviHR",
        checked_at=ago(minutes=3),
        updated_at=ago(minutes=3),
        git=GitState(branch="main", head="a1b2c3d", upstream="origin/main", head_at=ago(hours=1)),
        commits=(CommitLine("a1b2c3d", "fix login", ago(hours=1)), CommitLine("9f8e7d6", "add tests", ago(days=2))),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


# --- which path a question takes ------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "project", "intent", "scope", "mode"),
    [
        ("BonviHR qanday?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.TEMPLATE),
        ("holat", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.TEMPLATE),
        ("BonviHR'da bugun nima o'zgardi?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.TEMPLATE),
        ("hrmentorga nima bo'ldi?", "HRMentor", Intent.REPORT, ReportScope.PROJECT, ReportMode.TEMPLATE),
        ("как дела в BonviHR?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.TEMPLATE),
        ("/hisobot BonviHR", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.TEMPLATE),
        ("BonviHR login qachon tugaydi?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.ASSISTANT),
        ("login sahifasi qanday?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.ASSISTANT),
        ("nega test yiqilyapti?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.INVESTIGATE),
        ("serverda nima bo'lyapti?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.INVESTIGATE),
        ("BonviHR loglarida nima bor?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.INVESTIGATE),
        ("nima uchun deploy to'xtadi?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.INVESTIGATE),
        ("почему падает сборка?", "BonviHR", Intent.REPORT, ReportScope.PROJECT, ReportMode.INVESTIGATE),
        ("server qanday?", "BonviHR", Intent.SERVER_STATUS, ReportScope.PROJECT, ReportMode.INVESTIGATE),
        ("bugun qaysi loyihalarda nima o'zgardi?", None, Intent.REPORT_ALL, ReportScope.ALL, ReportMode.TEMPLATE),
        ("hammasi qanday?", None, Intent.REPORT_ALL, ReportScope.ALL, ReportMode.TEMPLATE),
        ("qaysi loyihada eng ko'p ish bo'ldi?", None, Intent.REPORT_ALL, ReportScope.ALL, ReportMode.ASSISTANT),
        ("nega hamma joyda test yiqilyapti?", None, Intent.REPORT_ALL, ReportScope.ALL, ReportMode.ASSISTANT),
        ("serverlar qanday?", None, Intent.SERVER_STATUS, ReportScope.SERVERS, ReportMode.TEMPLATE),
    ],
)
def test_plan_report_picks_the_path_of_each_question(
    text: str, project: str | None, intent: Intent, scope: ReportScope, mode: ReportMode
) -> None:
    plan = domain.plan_report(make_report_question(text, project, intent=intent), NAMES)
    assert (plan.scope, plan.mode) == (scope, mode)


def test_routing_needs_fresh_turns_even_a_plain_question_into_an_investigation() -> None:
    plan = domain.plan_report(make_report_question("BonviHR qanday?", needs_fresh=True), NAMES)
    assert (plan.mode, plan.reason) == (ReportMode.INVESTIGATE, "needs_fresh")


def test_a_project_name_with_a_suffix_is_not_a_question_word() -> None:
    assert domain.question_words("BonviHR'dagi holat", NAMES) == ("holat",)


def test_login_is_not_logs() -> None:
    assert not domain.is_deep("login sahifasi", ("login", "sahifasi"))
    assert domain.is_deep("logda nima bor", ("logda", "nima", "bor"))


def test_question_of_reads_a_routed_report_burst() -> None:
    item = make_turn_input(make_part(1, text="BonviHR qanday?"), make_part(2, text="tez ayt"), burst_id=7)
    decision = RouteDecision(Outcome.ROUTE, Method.EXPLICIT, Intent.REPORT, "BonviHR", 1.0, needs_fresh=True)
    question = domain.question_of(item, decision, route_id=3)
    assert (question.text, question.project_id, question.needs_fresh, question.burst_id, question.route_id) == (
        "BonviHR qanday? tez ayt",
        "BonviHR",
        True,
        7,
        3,
    )


def test_question_of_refuses_work_intents() -> None:
    item = make_turn_input(make_part(1, text="BonviHR'da login tuzat"))
    decision = RouteDecision(Outcome.ROUTE, Method.EXPLICIT, Intent.INSTRUCT, "BonviHR", 1.0)
    with pytest.raises(ValueError, match="not a report"):
        domain.question_of(item, decision)


# --- freshness ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("fresh", "line"),
    [
        (Freshness(ago(minutes=3)), "Ma'lumot: 3 daqiqa oldin"),
        (Freshness(ago(seconds=20)), "Ma'lumot: hozirgina"),
        (Freshness(ago(hours=2)), "Ma'lumot: 2 soat oldin"),
        (Freshness(ago(days=3)), "Ma'lumot: 3 kun oldin"),
        (
            Freshness(NOW, refreshed=True, previous_at=ago(minutes=45)),
            "Ma'lumot: eskirgan edi (45 daqiqa oldin) — hozir yangiladim",
        ),
        (Freshness(NOW, refreshed=True), "Ma'lumot: hozir yig'dim"),
        (Freshness(ago(minutes=50), failed=True), "Ma'lumot: 50 daqiqa oldin — yangilab bo'lmadi"),
        (Freshness(None, failed=True), "Ma'lumot: hali yig'ilmagan — git'ni o'qib bo'lmadi"),
    ],
)
def test_the_freshness_line_says_how_old_the_data_is(fresh: Freshness, line: str) -> None:
    assert domain.freshness_line(fresh, now=NOW) == line


def test_a_digest_is_stale_after_digest_stale_after_min() -> None:
    assert domain.is_stale(None, now=NOW, stale_after_min=30)
    assert not domain.is_stale(make_digest(checked_at=ago(minutes=30)), now=NOW, stale_after_min=30)
    assert domain.is_stale(make_digest(checked_at=ago(minutes=31)), now=NOW, stale_after_min=30)


# --- polling --------------------------------------------------------------------------------------------------------
def test_a_project_is_active_within_seven_days_or_while_a_turn_runs() -> None:
    idle = make_digest(git=GitState(head="a", head_at=ago(days=8)), results=())
    project = project_view(make_project_record("BonviHR", at=ago(days=30)), ProjectPrefs())
    assert not domain.is_active(project, idle, has_turn=False, now=NOW)
    assert domain.is_active(project, idle, has_turn=True, now=NOW)
    assert domain.is_active(replace(project, last_activity_at=ago(days=6)), idle, has_turn=False, now=NOW)
    recent = replace(idle, git=GitState(head="a", head_at=ago(days=1)))
    assert domain.is_active(project, recent, has_turn=False, now=NOW)
    done = ResultLine(1, TurnStatus.DONE, "x", "ok", "passed", ago(hours=2))
    assert domain.is_active(project, replace(idle, results=(done,)), has_turn=False, now=NOW)
    assert not domain.is_active(project, None, has_turn=False, now=NOW)


def test_active_projects_poll_every_active_poll_s_and_idle_ones_every_idle_poll_min() -> None:
    def due(minutes: float, *, active: bool) -> bool:
        digest = make_digest(checked_at=ago(minutes=minutes))
        return domain.poll_due(digest, active=active, now=NOW, active_poll_s=120, idle_poll_min=60)

    assert due(2, active=True)
    assert not due(1.9, active=True)
    assert not due(59, active=False)
    assert due(60, active=False)
    assert domain.poll_due(None, active=False, now=NOW, active_poll_s=120, idle_poll_min=60)


def test_the_poll_sees_a_change_in_head_branch_counts_or_files() -> None:
    stored = GitState(branch="main", head="a1", upstream="origin/main", ahead=0, behind=0, dirty=1, untracked=0)
    same = GitStatus("main", "a1", "origin/main", 0, 0, 1, 0)
    assert not domain.git_changed(stored, same)
    assert domain.git_changed(stored, replace(same, head="b2"))
    assert domain.git_changed(stored, replace(same, branch="feature"))
    assert domain.git_changed(stored, replace(same, ahead=1))
    assert domain.git_changed(stored, replace(same, changed=2))
    assert domain.git_changed(stored, replace(same, untracked=1))
    assert domain.git_changed(GitState(error_code="git_failed"), same)


# --- git facts ------------------------------------------------------------------------------------------------------
def test_today_starts_at_the_owners_midnight() -> None:
    late = CommitLine("a", "late", TODAY_START - timedelta(minutes=30))
    early = CommitLine("b", "early", TODAY_START + timedelta(minutes=10))
    assert domain.day_start(NOW, TZ) == TODAY_START
    assert domain.commits_today([early, late], now=NOW, timezone=TZ) == (early,)


def test_todays_files_are_counted_from_the_parent_of_todays_oldest_commit() -> None:
    state = GitState(head="c3", dirty=0)
    today = (CommitLine("c3", "x", ago(hours=1)), CommitLine("b2", "y", ago(hours=2)))
    old = (CommitLine("a1", "z", ago(days=2)),)
    assert domain.files_base((*today, *old), state, now=NOW, timezone=TZ) == "b2^"
    assert domain.files_base(old, replace(state, dirty=2), now=NOW, timezone=TZ) == "HEAD"
    assert domain.files_base(old, state, now=NOW, timezone=TZ) is None
    assert domain.files_base(today, GitState(head=None), now=NOW, timezone=TZ) is None
    assert domain.files_base(today, GitState(head="c3", error_code="git_failed"), now=NOW, timezone=TZ) is None


def test_git_state_and_commit_lines_come_from_the_git_port() -> None:
    commit = Commit("a1b2c3d4", "  fix   login  ", ago(hours=1))
    state = domain.git_state_of(GitStatus("main", "a1b2c3d4", "origin/main", 2, 1, 3, 4), commit)
    assert (state.branch, state.head, state.head_subject, state.ahead, state.behind, state.dirty, state.untracked) == (
        "main",
        "a1b2c3d4",
        "  fix   login  ",
        2,
        1,
        3,
        4,
    )
    assert domain.commit_lines([commit], scrub) == (CommitLine("a1b2c3d4", "fix login", commit.committed_at),)


# --- turns ----------------------------------------------------------------------------------------------------------
def test_results_are_the_latest_finished_turns_newest_first_without_smoke_turns() -> None:
    turns = [
        make_turn_summary(1, status=TurnStatus.DONE, ended_at=ago(hours=3), natija="eski"),
        make_turn_summary(2, status=TurnStatus.FAILED, ended_at=ago(hours=1)),
        make_turn_summary(3, status=TurnStatus.DONE, ended_at=ago(minutes=5), kind=TurnKind.SMOKE),
        make_turn_summary(4, status=TurnStatus.RUNNING, started_at=ago(minutes=4)),
        make_turn_summary(5, status=TurnStatus.CANCELLED, ended_at=ago(minutes=2)),
    ]
    assert [(r.turn_id, r.status) for r in domain.results_of(turns, scrub)] == [
        (2, TurnStatus.FAILED),
        (1, TurnStatus.DONE),
    ]


def test_active_turns_list_working_ones_first_and_count_the_queue_and_questions() -> None:
    turns = [
        make_turn_summary(1, status=TurnStatus.WAITING_QUESTION, started_at=ago(minutes=30)),
        make_turn_summary(2, status=TurnStatus.RUNNING, started_at=ago(minutes=12)),
        make_turn_summary(3, status=TurnStatus.QUEUED),
        make_turn_summary(4, status=TurnStatus.QUEUED),
    ]
    assert [turn.turn_id for turn in domain.active_of(turns)] == [2, 1]
    assert domain.queued_of(turns) == 2
    assert domain.questions_of(turns) == 1


# --- the handoff note -----------------------------------------------------------------------------------------------
def _note(block: str | None, turn_id: int = 9) -> HandoffNote:
    return HandoffNote(
        "BonviHR", turn_id, ago(minutes=1), block, files=("src/login.py",), commits=("a1b2c3d4e5",), progress=("Read",)
    )


def test_a_note_with_its_executor_block_is_taken_as_it_is() -> None:
    step = domain.handoff_step(None, _note("Goal: fix login\nState: done"))
    assert step.synthesize is None
    assert step.handoff is not None
    assert (step.handoff.source, step.handoff.turn_id) == ("executor", 9)
    assert step.handoff.text.startswith("Goal: fix login\nState: done\n\nFiles touched: src/login.py")


def test_a_note_without_its_block_keeps_the_previous_one_until_it_is_synthesized() -> None:
    previous = HandoffState("Goal: old", ago(days=1), "executor", 8)
    step = domain.handoff_step(previous, _note(None))
    assert step.handoff == previous
    assert step.synthesize == _note(None)


def test_a_note_already_taken_is_not_taken_again() -> None:
    previous = HandoffState("Goal: synthesized", ago(days=1), "synthesized", 9)
    assert domain.handoff_step(previous, _note(None)) == domain.HandoffStep(previous)
    assert domain.handoff_step(previous, None) == domain.HandoffStep(previous)


def test_a_synthesized_note_gets_the_files_and_commits_from_git() -> None:
    text = domain.synthesized_note("Goal: g\nDecisions: none\nState: s\nNext: n\n", _note(None), fallback="unused")
    assert text == "Goal: g\nDecisions: none\nState: s\nNext: n\n\nFiles touched: src/login.py\n\nCommits: a1b2c3d"


def test_an_empty_or_failed_synthesis_falls_back_to_the_last_steps() -> None:
    fallback = "Goal: x\nState: unknown; the task's last steps were:\n" + domain.steps_text(("Read", "Bash: make"))
    for reply in (None, "  "):
        text = domain.synthesized_note(reply, _note(None), fallback=fallback)
        assert text.startswith("Goal: x\nState: unknown; the task's last steps were:\n- Read\n- Bash: make")
        assert text.endswith("Files touched: src/login.py\n\nCommits: a1b2c3d")
    assert domain.steps_text(()) == "- (none recorded)"


def test_the_synthesis_ask_carries_the_previous_note_the_steps_and_the_git_facts() -> None:
    previous = HandoffState("Goal: old", ago(days=1), "executor", 8)
    raw = domain.synthesis_prompt("BonviHR", "Login", _note(None), previous, GitState(branch="main", dirty=2), scrub)
    payload = json.loads(raw)
    assert payload["task"] == "handoff_note"
    assert (payload["previous_note"], payload["progress"], payload["files"], payload["commits"]) == (
        "Goal: old",
        ["Read"],
        ["src/login.py"],
        ["a1b2c3d"],
    )
    assert payload["git"]["uncommitted_files"] == 2


# --- building the digest --------------------------------------------------------------------------------------------
def test_an_unchanged_refresh_keeps_updated_at_and_a_changed_one_moves_it() -> None:
    git = GitRead(GitState(branch="main", head="a1"), (CommitLine("a1", "fix", ago(hours=1)),))
    first = domain.build_digest(
        "BonviHR",
        at=ago(minutes=10),
        git=git,
        summaries=(),
        decisions=0,
        handoff=None,
        executor="claude",
        previous=None,
        scrub=scrub,
    )
    again = domain.build_digest(
        "BonviHR",
        at=NOW,
        git=git,
        summaries=(),
        decisions=0,
        handoff=None,
        executor="claude",
        previous=first,
        scrub=scrub,
    )
    assert (again.checked_at, again.updated_at) == (NOW, ago(minutes=10))
    moved = domain.build_digest(
        "BonviHR",
        at=NOW,
        git=git,
        summaries=(),
        decisions=1,
        handoff=None,
        executor="claude",
        previous=first,
        scrub=scrub,
    )
    assert moved.updated_at == NOW


def test_unreadable_git_keeps_the_commits_and_files_read_before() -> None:
    previous = make_digest(files=("a.py",), files_total=1)
    broken = GitRead(GitState(error_code="git_failed"))
    digest = domain.build_digest(
        "BonviHR",
        at=NOW,
        git=broken,
        summaries=(),
        decisions=0,
        handoff=None,
        executor="claude",
        previous=previous,
        scrub=scrub,
    )
    assert digest.git.error_code == "git_failed"
    assert (digest.commits, digest.files, digest.files_total) == (previous.commits, ("a.py",), 1)


def test_the_digest_keeps_its_results_when_the_turns_read_holds_no_finished_turn() -> None:
    kept = (ResultLine(4, TurnStatus.DONE, "Login", "tuzatildi", "passed", ago(hours=1)),)
    digest = domain.build_digest(
        "BonviHR",
        at=NOW,
        git=GitRead(GitState(head="a")),
        executor="claude",
        decisions=0,
        handoff=None,
        summaries=(make_turn_summary(5, status=TurnStatus.QUEUED),),
        previous=make_digest(results=kept),
        scrub=scrub,
    )
    assert digest.results == kept
    assert (digest.queued, digest.last_done, digest.last_turn_id) == (1, kept[0], 4)


def test_json_columns_round_trip_and_bad_items_are_dropped() -> None:
    commits = (CommitLine("a1", "fix", ago(hours=1)),)
    results = (ResultLine(4, TurnStatus.DONE, "Login", "tuzatildi", "passed", ago(hours=1)),)
    assert domain.commits_from_json(domain.commits_json(commits)) == commits
    assert domain.results_from_json(domain.results_json(results)) == results
    assert domain.files_from_json(domain.files_json(["a.py"])) == ("a.py",)
    assert domain.commits_from_json([{"sha": "x"}, 3, {"sha": "a", "subject": "s", "at": "2026-10-23T09:00"}]) == ()
    assert domain.results_from_json([{"turn_id": 1, "status": "queued", "title": "t"}, "x"]) == ()
    assert domain.commits_from_json(None) == ()
    assert domain.results_from_json({}) == ()
    assert domain.files_from_json("x") == ()
    assert domain.handoff_source("executor") == "executor"
    assert domain.handoff_source("other") is None


# --- what the owner reads -------------------------------------------------------------------------------------------
def test_the_project_report_leads_with_the_running_turn_and_ends_before_the_freshness_line() -> None:
    state = ProjectState(
        "BonviHR",
        make_digest(
            git=GitState(branch="main", head="a1", upstream="origin/main", ahead=2, dirty=1, untracked=2),
            files=("src/app/login.py", "tests/test_login.py"),
            files_total=2,
        ),
        (
            make_turn_summary(5, status=TurnStatus.RUNNING, started_at=ago(minutes=12)),
            make_turn_summary(6, status=TurnStatus.QUEUED, title="Keyingisi"),
            make_turn_summary(
                4, status=TurnStatus.DONE, ended_at=ago(minutes=40), natija="Xato tuzatildi.", tests="passed"
            ),
        ),
        decisions=1,
    )
    text = domain.project_report(state, now=NOW, timezone=TZ, scrub=scrub)
    assert shown(text).splitlines() == [
        "BonviHR · hozir: «Login sahifasini tuzat», ishlayapti, 12 daq",
        "Navbatda: 1 ta topshiriq",
        "Oxirgi natija (13:20): Xato tuzatildi. · Testlar: o'tdi",
        "Git: main · 2 commit push qilinmagan · 3 fayl commit qilinmagan",
        "Bugun: 1 commit — «fix login»",
        "O'zgargan fayllar: 2 ta (login.py, test_login.py)",
        "Javobingizni kutyapti: 0 ta savol, 1 ta qaror",
    ]


def test_an_idle_project_with_no_digest_yet_says_so() -> None:
    text = domain.project_report(ProjectState("BonviHR", None), now=NOW, timezone=TZ, scrub=scrub)
    assert shown(text).splitlines() == ["BonviHR · hozir ish yo'q", "Hali natija yo'q", "Git: o'qib bo'lmadi"]


def test_a_failed_last_turn_and_an_old_last_commit() -> None:
    digest = make_digest(commits=(CommitLine("a1", "old fix", ago(days=2)),))
    failed = make_turn_summary(4, status=TurnStatus.FAILED, ended_at=ago(days=1), title="Deploy")
    text = domain.project_report(ProjectState("BonviHR", digest, (failed,)), now=NOW, timezone=TZ, scrub=scrub)
    assert "Oxirgi topshiriq (22.10 14:00) xato bilan tugadi: «Deploy»" in shown(text).splitlines()
    assert "Bugun commit yo'q. Oxirgisi (21.10 14:00): «old fix»" in shown(text).splitlines()


@pytest.mark.parametrize(
    ("git", "line"),
    [
        (GitState(branch="main", head="a", upstream="origin/main"), "Git: main · origin/main bilan bir xil · toza"),
        (GitState(branch="main", head="a", upstream="origin/main", behind=3), "Git: main · 3 commit orqada · toza"),
        (
            GitState(branch="dev", head="a", upstream="o/dev", ahead=1, behind=2),
            "Git: dev · 1 commit oldinda, 2 commit orqada · toza",
        ),
        (GitState(branch=None, head="a"), "Git: detached HEAD · upstream yo'q · toza"),
        (GitState(branch="main", head=None), "Git: main · hali commit yo'q"),
        (GitState(error_code="not_installed"), "Git: o'qib bo'lmadi"),
    ],
)
def test_the_git_line(git: GitState, line: str) -> None:
    assert shown(domain.git_line(git)) == line


def test_ten_commits_today_read_as_ten_or_more() -> None:
    commits = tuple(CommitLine(f"c{i}", f"step {i}", ago(minutes=10 + i)) for i in range(domain.RECENT_COMMITS_MAX))
    line = domain.commits_line(make_digest(commits=commits), now=NOW, timezone=TZ, scrub=scrub)
    assert shown(line) == "Bugun: 10+ commit — «step 0»; «step 1»; «step 2»"


def test_the_cross_project_report_lists_what_changed_today_runs_now_and_waits() -> None:
    bonvi = ProjectState(
        "BonviHR",
        make_digest(),
        (make_turn_summary(5, status=TurnStatus.RUNNING, started_at=ago(minutes=7)),),
    )
    hr = ProjectState(
        "HRMentor",
        make_digest(
            project_id="HRMentor",
            commits=(CommitLine("b1", "add tests", ago(minutes=30)), CommitLine("b0", "init", ago(minutes=50))),
            results=(ResultLine(3, TurnStatus.DONE, "Tests", "Testlar qo'shildi.", "passed", ago(minutes=29)),),
        ),
        decisions=1,
    )
    quiet = ProjectState("Wunderkind", make_digest(project_id="Wunderkind", commits=()))
    checks = [
        make_server_check("web", at=ago(minutes=12)),
        make_server_check("db", at=ago(minutes=12), ok=False, summary="disk 95%"),
    ]
    text = domain.all_report([bonvi, hr, quiet], checks, now=NOW, timezone=TZ, scrub=scrub)
    assert shown(text).splitlines() == [
        "Bugun o'zgargan loyihalar: 2 ta",
        "• BonviHR: 1 commit (oxirgisi: «fix login»)",
        "• HRMentor: 2 commit (oxirgisi: «add tests»); natija: Testlar qo'shildi.",
        "Hozir ishlayapti: BonviHR («Login sahifasini tuzat», 7 daq)",
        "Javobingizni kutyapti: HRMentor",
        "Qolgan 1 ta loyihada bugun o'zgarish yo'q",
        "Serverlar: db ✗ disk 95% (12 daqiqa oldin), web ✓ (12 daqiqa oldin)",
    ]


def test_a_quiet_day_says_nothing_changed() -> None:
    quiet = ProjectState("BonviHR", make_digest(commits=()))
    assert shown(domain.all_report([quiet], [], now=NOW, timezone=TZ, scrub=scrub)) == (
        "Bugun hech qaysi loyihada o'zgarish bo'lmadi"
    )


def test_server_checks_answer_only_while_fresh() -> None:
    fresh = [make_server_check("web", at=ago(minutes=10))]
    assert domain.servers_fresh(fresh, now=NOW, stale_after_min=30)
    assert not domain.servers_fresh([make_server_check("web", at=ago(minutes=31))], now=NOW, stale_after_min=30)
    assert not domain.servers_fresh([], now=NOW, stale_after_min=30)


def test_the_assistant_answer_follows_the_live_turn_line() -> None:
    state = ProjectState("BonviHR", make_digest(), (make_turn_summary(5, started_at=ago(minutes=3)),))
    text = domain.assistant_report(state, "  Login hali tugamagan.  ", now=NOW, scrub=scrub)
    assert text is not None
    assert shown(text) == "BonviHR · hozir: «Login sahifasini tuzat», ishlayapti, 3 daq\nLogin hali tugamagan."
    assert domain.assistant_report(state, "   ", now=NOW, scrub=scrub) is None
    assert domain.all_assistant_report(" Hammasi joyida. ", scrub) == "Hammasi joyida."
    assert domain.all_assistant_report("", scrub) is None


def test_a_chat_marked_answer_goes_alone() -> None:
    """Owner, 2026-10-24: "salomlarim" got a project report. The Assistant marks small talk with `[chat]`."""
    assert domain.chat_reply("[chat]\nSalom! Nima qilamiz?", scrub) == "Salom! Nima qilamiz?"
    assert domain.chat_reply("  [chat] Yaxshi, rahmat!  ", scrub) == "Yaxshi, rahmat!"
    assert domain.chat_reply("[chat]", scrub) is None  # nothing after the mark: the template answers
    assert domain.chat_reply("Bugun 2 commit. [chat]", scrub) is None  # only a first-line mark counts


def test_the_answer_ask_is_data_only_and_scrubbed() -> None:
    key = FAKE_KEYS["github_token"]
    result = ResultLine(4, TurnStatus.DONE, "Login", f"token {key} ishlatildi", "passed", ago(minutes=40))
    state = ProjectState(
        "BonviHR",
        make_digest(results=(result,)),
        (make_turn_summary(5, started_at=ago(minutes=3)),),
        description="HR tizimi",
    )
    raw = domain.answer_prompt(
        f"BonviHR qanday? {key}", [state], [make_server_check(at=ago(minutes=5))], now=NOW, timezone=TZ, scrub=scrub
    )
    assert key not in raw
    payload = json.loads(raw)
    assert (payload["task"], payload["now"]) == ("answer", "2026-10-23T14:00+05:00")
    project = payload["projects"][0]
    assert project["now_running"] == [{"title": "Login sahifasini tuzat", "state": "running", "minutes": 3}]
    assert project["data_minutes_ago"] == 3
    assert project["commits"][0] == {"subject": "fix login", "when": "2026-10-23T13:00+05:00", "today": True}
    assert payload["servers"] == [{"host": "web", "ok": True, "summary": "", "minutes_ago": 5}]


def test_known_names_are_folder_names_and_aliases() -> None:
    project = project_view(make_project_record("BonviHR", at=NOW), ProjectPrefs(aliases={"bonvi": "BonviHR"}))
    assert domain.known_names([project]) == ("BonviHR", "bonvi")
    assert isinstance(project.path, PurePosixPath)
