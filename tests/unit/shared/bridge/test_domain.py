"""`shared/bridge/domain.py`: which messages are plain status questions, what the owner reads, what the tool returns."""

from datetime import UTC, datetime, timedelta

import pytest

from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.shared.bridge import domain
from yordamchi.shared.bridge.domain import CommitView, ProjectRef, ProjectStatus, ResultView, ServerView

NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)  # 14:00 in Tashkent
ZONE = "Asia/Tashkent"
PROJECTS = (
    ProjectRef("BonviHR", "HR tizimi"),
    ProjectRef("HRMentor", "mentorlik ilovasi"),
    ProjectRef("Wunderkind-School"),
    ProjectRef("Old", missing=True),
    ProjectRef("Go"),
)


@pytest.mark.parametrize(
    ("text", "project"),
    [
        ("BonviHR qanday?", "BonviHR"),
        ("bonvihr qanday", "BonviHR"),
        ("BonviHR'da nima gap?", "BonviHR"),
        ("HRMentor’da nima bo‘ldi?", "HRMentor"),
        ("wunderkindschool holati", "Wunderkind-School"),
        ("Как дела \u0441 BonviHR?", "BonviHR"),
        ("how is HRMentor going", "HRMentor"),
        ("BonviHR", "BonviHR"),
    ],
)
def test_a_plain_status_question_names_its_project(text: str, project: str) -> None:
    assert domain.status_question(text, PROJECTS) == project


@pytest.mark.parametrize(
    "text",
    [
        "BonviHR'ni qanday deploy qilaman?",  # a real question: the assistant answers it
        "BonviHR va HRMentor qanday?",  # two projects
        "Kvant kompyuter nima?",  # no project
        "go qanday",  # two-letter names never match
        "",
        "BonviHR qanday? Menga uzoq tushuntirib bering, nima o'zgardi, hammasini",  # too long
    ],
)
def test_anything_else_goes_to_the_assistant(text: str) -> None:
    assert domain.status_question(text, PROJECTS) is None


def test_find_project_takes_the_name_with_an_ending() -> None:
    assert domain.find_project("bonvihr'da", PROJECTS) == PROJECTS[0]
    assert domain.find_project("Wunderkind School", PROJECTS) == PROJECTS[2]
    assert domain.find_project("Nonexistent", PROJECTS) is None


def _status(**overrides: object) -> ProjectStatus:
    values: dict[str, object] = {
        "project_id": "BonviHR",
        "description": "HR tizimi",
        "checked_at": NOW - timedelta(minutes=3),
        "branch": "main",
        "head": "a1b2c3d",
        "upstream": "origin/main",
        "ahead": 2,
        "dirty": 1,
        "commits": (CommitView("Login tuzatildi", NOW - timedelta(hours=1)),),
        "results": (ResultView("done", "Login", "Login sahifasi tuzatildi", "passed", NOW - timedelta(hours=1)),),
        "pending_decisions": 1,
    }
    values.update(overrides)
    return ProjectStatus(**values)  # type: ignore[arg-type]


def test_the_status_text_ends_with_the_freshness_line() -> None:
    text = domain.status_text(_status(), now=NOW, timezone=ZONE)
    lines = text.splitlines()
    assert lines[0] == S.BRIDGE_PROJECT(project="BonviHR", description="HR tizimi")
    assert "Login sahifasi tuzatildi" in text
    assert S.REPORT_GIT_AHEAD(count=2) in text
    assert S.REPORT_GIT_DIRTY(count=1) in text
    assert S.REPORT_TODAY(count=1, subjects="«Login tuzatildi»") in text
    assert S.REPORT_PENDING(questions=0, decisions=1) in text
    assert lines[-1] == S.FRESHNESS(age=S.AGE_MINUTES(count=3))


def test_a_project_without_a_digest_says_so() -> None:
    text = domain.status_text(ProjectStatus("BonviHR"), now=NOW, timezone=ZONE)
    assert text.splitlines() == [S.BRIDGE_NO_DIGEST(project="BonviHR"), S.FRESHNESS_NONE()]


def test_unreadable_git_skips_the_commits_line() -> None:
    text = domain.status_text(_status(git_error_code="git_failed"), now=NOW, timezone=ZONE)
    assert S.REPORT_GIT_UNKNOWN() in text
    assert "Login tuzatildi»" not in text


@pytest.mark.parametrize(
    ("age", "line"),
    [
        (timedelta(seconds=20), S.FRESHNESS_JUST_NOW()),
        (timedelta(minutes=45), S.FRESHNESS(age=S.AGE_MINUTES(count=45))),
        (timedelta(hours=5), S.FRESHNESS(age=S.AGE_HOURS(count=5))),
        (timedelta(days=3), S.FRESHNESS(age=S.AGE_DAYS(count=3))),
    ],
)
def test_freshness_wording(age: timedelta, line: str) -> None:
    assert domain.freshness_line(NOW - age, now=NOW) == line


def test_the_tool_json_is_data_with_its_age() -> None:
    data = domain.status_json(_status(handoff_note="Goal: x\nNext: y"), now=NOW)
    assert data["project"] == "BonviHR"
    assert data["data_minutes_ago"] == 3
    assert data["git"] == {
        "readable": True,
        "branch": "main",
        "upstream": "origin/main",
        "ahead": 2,
        "behind": 0,
        "uncommitted_files": 1,
    }
    assert data["waiting_for_owner"] == {"questions": 0, "decisions": 1}
    assert data["handoff_note"] == "Goal: x Next: y"


def test_an_unknown_project_lists_the_known_ones() -> None:
    data = domain.unknown_project_json("Foo", PROJECTS)
    assert data["error"] == "unknown_project"
    assert data["known_projects"] == ["BonviHR", "HRMentor", "Wunderkind-School", "Go"]


def test_server_summaries_read_the_dev_issue_shape() -> None:
    raw: JsonValue = {"issues": [{"kind": "disk", "subject": "/", "value": 93, "since": "2026-10-24T08:00:00+00:00"}]}
    assert domain.summary_of(raw) == "disk / 93"
    servers = domain.servers_json(
        [ServerView("bonvi-srv", NOW - timedelta(minutes=10), ok=False, summary="x")], now=NOW
    )
    assert servers == {
        "servers": [{"host": "bonvi-srv", "ok": False, "summary": "x", "error": None, "minutes_ago": 10}]
    }


def test_the_digest_json_columns_parse_and_skip_junk() -> None:
    commits = domain.commits_from_json([{"sha": "a", "subject": "s", "at": "2026-10-24T08:00:00+00:00"}, "junk", {}])
    assert commits == (CommitView("s", datetime(2026, 10, 24, 8, 0, tzinfo=UTC)),)
    results = domain.results_from_json([{"status": "failed", "title": "t", "at": None}, 3])
    assert results == (ResultView("failed", "t"),)
    assert domain.commits_from_json(None) == ()


def test_the_bridge_reads_only_the_spec_tables() -> None:
    spec_2_3 = {
        "projects",
        "digests",
        "server_checks",
        "integrations",
        "vault_secrets",
        "setting_versions",
        "usage_records",
    }
    assert spec_2_3 == domain.BRIDGE_TABLES
