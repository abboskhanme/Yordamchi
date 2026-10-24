"""selfupdate.domain (SPEC §3.2.12, UC-19, Q5, R-32): names, protected-core globs, safeguard settings, the change
plan, `request.json`, reading `status.json` and the owner's outcome message."""

from datetime import UTC, datetime, timedelta

import pytest

from yordamchi.core import strings as S
from yordamchi.dev.selfupdate import domain
from yordamchi.dev.selfupdate.domain import Outcome, UpdateKind, UpdateStatus

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
GLOBS = domain.protected_globs(
    "# comment\nsupervisor/**\nsrc/yordamchi/adapters/*/guard.py\n.protected-core\n# pending: src/yordamchi/x/**\n"
)


def test_names() -> None:
    assert domain.update_id(7) == "u000007"
    assert domain.slug("O'zingga ovoz sozlamasini qo'sh") == "ozingga-ovoz-sozlamasini-qosh"
    assert domain.slug("!!!") == "ozgarish"
    assert len(domain.slug("juda " * 40)) <= domain.SLUG_MAX
    assert domain.branch_name(7, "Ovoz") == "selfupdate/7-ovoz"


def test_protected_globs_include_pending_lines() -> None:
    assert GLOBS == ("supervisor/**", "src/yordamchi/adapters/*/guard.py", ".protected-core", "src/yordamchi/x/**")


@pytest.mark.parametrize(
    ("path", "core"),
    [
        ("supervisor/src/yordamchi_supervisor/updater.py", True),
        ("src/yordamchi/adapters/claude/guard.py", True),
        ("src/yordamchi/adapters/claude/sub/guard.py", False),  # `*` is one folder
        (".protected-core", True),
        ("src/yordamchi/x/new.py", True),
        ("src/yordamchi/dev/servers/domain.py", False),
        ("supervisorx/a.py", False),
    ],
)
def test_core_files_follow_the_glob_rules(path: str, *, core: bool) -> None:
    assert domain.core_files([path], GLOBS) == ((path,) if core else ())


BEFORE = """
A = Setting("gate.secret_scan_enabled", bool, True, check=None, weakens=disables)
B = Setting("voice.name", str, "Kore", check=None)
C = Setting("turns.max_concurrent", int, 15, check=Range(1, 30), weakens=increases)
"""


def test_a_changed_safeguard_declaration_is_found_and_other_settings_are_not() -> None:
    voice = BEFORE.replace('"Kore"', '"Puck"')
    assert domain.weakened_settings(BEFORE, voice) == ()
    caps = BEFORE.replace("Range(1, 30)", "Range(1, 99)")
    assert domain.weakened_settings(BEFORE, caps) == ("turns.max_concurrent",)
    gone = BEFORE.replace("A = Setting", "A = dict")
    assert domain.weakened_settings(BEFORE, gone) == ("gate.secret_scan_enabled",)
    assert domain.weakened_settings(BEFORE, "def broken(:") == ("defaults.py",)  # unreadable: fail closed
    assert domain.weakened_settings(BEFORE, BEFORE) == ()


def test_plan_change() -> None:
    plan = domain.plan_change(["README.md", "src/a.py"], GLOBS, defaults_before=None, defaults_after=None)
    assert (plan.kind, plan.asks_owner, plan.split_needed) == (UpdateKind.APP, False, False)
    plan = domain.plan_change(["supervisor/a.py"], GLOBS, defaults_before=None, defaults_after=None)
    assert (plan.kind, plan.core) == (UpdateKind.SUPERVISOR, ("supervisor/a.py",))
    plan = domain.plan_change(["supervisor/a.py", "src/a.py"], GLOBS, defaults_before=None, defaults_after=None)
    assert plan.split_needed
    caps = BEFORE.replace("Range(1, 30)", "Range(1, 99)")
    plan = domain.plan_change(
        ["src/yordamchi/shared/settings/defaults.py"], GLOBS, defaults_before=BEFORE, defaults_after=caps
    )
    assert plan.weakened == ("turns.max_concurrent",)
    assert domain.core_summary(plan) == ("sozlama turns.max_concurrent",)


def test_request_json_carries_what_the_supervisor_reads() -> None:
    plan = domain.plan_change(["src/a.py"], GLOBS, defaults_before=None, defaults_after=None)
    body = domain.request_json(
        update="u000001",
        kind=UpdateKind.APP,
        commit="b" * 40,
        branch="selfupdate/1-x",
        summary="s" * 900,
        plan=plan,
        decision_id=None,
        at=NOW,
        health_timeout_s=120,
        drain_max_min=30,
    )
    assert body["id"] == "u000001"
    assert body["changed_files"] == ["src/a.py"]
    assert isinstance(body["summary"], str)
    assert len(body["summary"]) == domain.SUMMARY_MAX


def test_read_outcome_is_strict() -> None:
    assert domain.read_outcome(None) is None
    assert domain.read_outcome({"status": "exploded"}) is None
    outcome = domain.read_outcome(
        {"status": "merged", "final": True, "version": "v2", "tests": {"app": 3, "x": "y"}, "switched_at": "bad"}
    )
    assert outcome == Outcome(status=UpdateStatus.MERGED, final=True, version="v2", tests={"app": 3})


def test_outcome_texts() -> None:
    merged = Outcome(UpdateStatus.MERGED, True, version="v2", tests={"app": 10}, previous_tests={"app": 9})
    text = domain.outcome_text(merged, kind=UpdateKind.APP, branch="b", summary="", files=["a.py", "b.py"])
    assert text == S.SELFUPDATE_DONE(project="Yordamchi", version="v2", changes="a.py, b.py", tests="10 ta (oldin 9)")
    alerted = Outcome(UpdateStatus.ROLLED_BACK, True, code="crash_loop", alerted=True)
    assert domain.outcome_text(alerted, kind=UpdateKind.APP, branch="b", summary="", files=[]) is None
    quiet = Outcome(UpdateStatus.ROLLED_BACK, True, version="v2", previous="v1", code="crash_loop")
    told = domain.outcome_text(quiet, kind=UpdateKind.APP, branch="b", summary="", files=[])
    assert told is not None
    assert "qayta-qayta yiqildi" in told
    assert domain.reason_text("nope") == S.SELFUPDATE_REASON_OTHER(code="nope")
    assert domain.changes_text([f"f{n}" for n in range(7)]) == "f0, f1, f2, f3, f4 (+2)"


def test_supervisor_switch_watch() -> None:
    switched = NOW - timedelta(seconds=61)
    outcome = Outcome(UpdateStatus.SWITCHING, False, version="v2", switched_at=switched)
    assert domain.supervisor_switch_failed(outcome, beat_release="v1", beat_at=NOW, now=NOW, wait_s=60)
    assert not domain.supervisor_switch_failed(outcome, beat_release="v2", beat_at=NOW, now=NOW, wait_s=60)
    assert not domain.supervisor_switch_failed(outcome, beat_release=None, beat_at=None, now=NOW, wait_s=120)
    stale = domain.supervisor_switch_failed(
        outcome, beat_release="v2", beat_at=switched - timedelta(seconds=1), now=NOW, wait_s=60
    )
    assert stale
