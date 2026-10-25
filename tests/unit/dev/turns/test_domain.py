"""turns.domain: the scheduler, runaway, usage-limit, result and revert rules (SPEC §5.1–5.6, §6.2, §3.2.6)."""

from datetime import timedelta

import pytest

from tests.factories import make_part
from tests.fakes.clock import DEFAULT_START
from tests.owner_text import shown
from yordamchi.core import strings as S
from yordamchi.dev.gate.domain import Policy
from yordamchi.dev.turns import domain
from yordamchi.dev.turns.domain import (
    GIB,
    Admission,
    HandoffNote,
    HostState,
    ProgressKind,
    QueuedTurn,
    QueueReason,
    RunawayLimits,
    RunawayReason,
    SchedulerLimits,
    StartFiles,
    SuiteStatus,
    TurnKind,
    WorkFacts,
)
from yordamchi.ports.agent import ModelUsage
from yordamchi.ports.git import FileChange
from yordamchi.shared.burst.domain import CloseReason, TranscriptState, turn_input
from yordamchi.shared.inbox.domain import UpdateKind

NOW = DEFAULT_START
LIMITS = SchedulerLimits(max_concurrent=2, max_pressure_level=2, block_disk_gb=3)
CALM = HostState(pressure_level=1, free_disk_bytes=50 * GIB)


def queued(
    turn_id: int, project: str = "BonviHR", *, kind: TurnKind = TurnKind.INSTRUCT, session: str | None = None
) -> QueuedTurn:
    return QueuedTurn(turn_id=turn_id, project_id=project, kind=kind, session_id=session)


def starts(admissions: tuple[Admission, ...]) -> list[int]:
    return [item.turn_id for item in admissions if item.start]


# --- the scheduler (SPEC §5.6) --------------------------------------------------------------------------------------
def test_queued_turns_start_until_the_cap_and_the_rest_queue_with_their_position() -> None:
    turns = [queued(1, "A"), queued(2, "B"), queued(3, "C"), queued(4, "D")]
    admissions = domain.plan_starts(turns, working=0, busy=(), host=CALM, limits=LIMITS)
    assert starts(admissions) == [1, 2]
    waiting = [item for item in admissions if not item.start]
    assert [(item.turn_id, item.reason, item.position) for item in waiting] == [
        (3, QueueReason.CAP, 1),
        (4, QueueReason.CAP, 2),
    ]


def test_turns_already_working_count_toward_the_cap() -> None:
    admissions = domain.plan_starts([queued(1, "A")], working=2, busy=(), host=CALM, limits=LIMITS)
    assert admissions == (Admission(1, "A", start=False, reason=QueueReason.CAP, position=1),)


@pytest.mark.parametrize(("level", "starts_now"), [(1, True), (2, True), (3, False), (4, False)])
def test_memory_pressure_above_the_setting_queues(level: int, *, starts_now: bool) -> None:
    host = HostState(pressure_level=level, free_disk_bytes=50 * GIB)
    admissions = domain.plan_starts([queued(1)], working=0, busy=(), host=host, limits=LIMITS)
    assert admissions[0].start is starts_now
    if not starts_now:
        assert admissions[0].reason is QueueReason.PRESSURE


def test_an_unreadable_pressure_level_queues_every_turn() -> None:
    host = HostState(pressure_level=None, free_disk_bytes=50 * GIB)
    investigation = queued(2, "B", kind=TurnKind.INVESTIGATE)
    admissions = domain.plan_starts([queued(1), investigation], working=0, busy=(), host=host, limits=LIMITS)
    assert [item.reason for item in admissions] == [QueueReason.PRESSURE, QueueReason.PRESSURE]


def test_low_disk_queues_coding_turns_but_not_investigations() -> None:
    host = HostState(pressure_level=1, free_disk_bytes=2 * GIB)
    turns = [queued(1, "A"), queued(2, "B", kind=TurnKind.INVESTIGATE)]
    admissions = domain.plan_starts(turns, working=0, busy=(), host=host, limits=LIMITS)
    assert [(item.turn_id, item.start, item.reason) for item in admissions] == [
        (1, False, QueueReason.DISK),
        (2, True, None),
    ]


def test_investigations_sit_outside_the_cap() -> None:
    turns = [queued(1, "A", kind=TurnKind.INVESTIGATE), queued(2, "B", kind=TurnKind.INVESTIGATE)]
    admissions = domain.plan_starts(turns, working=2, busy=(), host=CALM, limits=LIMITS)
    assert starts(admissions) == [1, 2]


def test_a_project_runs_one_turn_per_session_kind_at_a_time() -> None:
    turns = [queued(1, "A"), queued(2, "A"), queued(3, "A", kind=TurnKind.INVESTIGATE)]
    admissions = domain.plan_starts(turns, working=0, busy={("A", "coding")}, host=CALM, limits=LIMITS)
    assert [(item.turn_id, item.reason) for item in admissions] == [
        (1, QueueReason.BUSY),
        (2, QueueReason.BUSY),
        (3, None),
    ]


def test_two_queued_turns_of_one_free_project_start_one_at_a_time() -> None:
    admissions = domain.plan_starts([queued(1, "A"), queued(2, "A")], working=0, busy=(), host=CALM, limits=LIMITS)
    outcome = [(item.turn_id, item.start, item.reason) for item in admissions]
    assert outcome == [(1, True, None), (2, False, QueueReason.BUSY)]


def test_a_session_open_at_the_mac_queues_its_project_only() -> None:
    host = HostState(pressure_level=1, free_disk_bytes=50 * GIB, attached_sessions=frozenset({"sess-A"}))
    turns = [queued(1, "A", session="sess-A"), queued(2, "B", session="sess-B")]
    admissions = domain.plan_starts(turns, working=0, busy=(), host=host, limits=LIMITS)
    assert [(item.turn_id, item.reason) for item in admissions] == [(1, QueueReason.MAC_ATTACHED), (2, None)]


def test_a_self_update_drain_holds_every_turn() -> None:
    host = HostState(pressure_level=1, free_disk_bytes=50 * GIB, draining=True)
    admissions = domain.plan_starts([queued(1)], working=0, busy=(), host=host, limits=LIMITS)
    assert admissions[0].reason is QueueReason.UPDATE_DRAIN


def test_the_queue_notice_names_project_place_and_reason() -> None:
    text = domain.queue_notice("BonviHR", 2, QueueReason.CAP, pressure_level=1)
    assert text == S.QUEUED(project="BonviHR", position=2, reason=S.QUEUE_REASONS["cap"]())
    assert text.startswith("BonviHR")


def test_critical_pressure_suggests_parking_a_project() -> None:
    text = domain.queue_notice("BonviHR", 1, QueueReason.PRESSURE, pressure_level=4)
    assert S.QUEUE_REASON_PRESSURE_CRITICAL() in text


def test_every_queue_reason_has_its_words() -> None:
    assert set(S.QUEUE_REASONS) == {reason.value for reason in QueueReason}


# --- kinds ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("kind", "session", "policy"),
    [
        (TurnKind.INSTRUCT, "coding", Policy.FULL),
        (TurnKind.BUILD, "coding", Policy.FULL),
        (TurnKind.PLAN, "coding", Policy.PLAN_ONLY),
        (TurnKind.SELFUPDATE, "coding", Policy.SELF_UPDATE),
        (TurnKind.INVESTIGATE, "investigation", Policy.READ_ONLY),
        (TurnKind.SMOKE, "smoke", Policy.FULL),
    ],
)
def test_each_turn_kind_has_its_session_and_gate_policy(kind: TurnKind, session: str, policy: Policy) -> None:
    assert domain.session_kind_of(kind) == session
    assert domain.policy_of(kind) is policy


def test_an_input_steers_a_busy_executor_and_starts_an_idle_one() -> None:
    assert domain.send_mode(cli_busy=True) == "next"
    assert domain.send_mode(cli_busy=False) == "new_turn"


def test_effort_values_map_to_the_port_literal() -> None:
    assert domain.effort_of("xhigh") == "xhigh"
    assert domain.effort_of(None) is None
    assert domain.effort_of("turbo") is None


def test_the_second_layer_forbids_yordamchi_data_and_keychain_reads() -> None:
    rules = domain.forbidden_tool_rules(data_dir="/Users/o/Library/Application Support/Yordamchi", config_dir="/c/y")
    assert "Read(//Users/o/Library/Application Support/Yordamchi/**)" in rules
    assert "Edit(//c/y/**)" in rules
    assert "Bash(security find-generic-password*)" in rules
    assert "Bash(launchctl * uz.yordamchi*)" in rules


# --- the routed instruction -----------------------------------------------------------------------------------------
def test_a_burst_becomes_one_request_with_its_parts_in_order() -> None:
    parts = (
        make_part(1, kind=UpdateKind.PHOTO, caption="xato shu", media_group_id="album-1"),
        make_part(2, at_s=3, text="login sahifasini tuzat"),
    )
    item = turn_input(7, -100, parts, opened_at=NOW, closed_at=NOW, reason=CloseReason.QUIET)
    request = domain.request_from_input(item, "BonviHR", files={1: "/data/media/1.jpg"}, images=("/data/media/1.jpg",))
    assert request.text == "[image] xato shu (/data/media/1.jpg)\n\nlogin sahifasini tuzat"
    assert request.burst_id == 7
    assert request.message_ids == (5001, 5002)
    assert request.media_group_ids == ("album-1",)
    assert request.last_part_at == NOW + timedelta(seconds=3)
    assert request.images == ("/data/media/1.jpg",)


def test_a_voice_note_is_labelled_and_an_unheard_one_says_so() -> None:
    assert domain.part_text(UpdateKind.VOICE, "salom", TranscriptState.DONE) == "[voice note, transcribed] salom"
    assert domain.part_text(UpdateKind.VOICE, "x", TranscriptState.FAILED).startswith("[voice note, not understood]")
    assert domain.part_text(UpdateKind.LINK, "https://e.uz", TranscriptState.NONE) == "https://e.uz"
    assert domain.part_text(UpdateKind.PHOTO, None, TranscriptState.NONE) == "[image]"


def test_delivery_media_round_trips_and_feeds_the_follow_up_rule() -> None:
    request = domain.TurnRequest(
        project_id="BonviHR", text="x", message_ids=(5, 6), media_group_ids=("g",), last_part_at=NOW
    )
    media = domain.media_from_json(domain.delivery_media(request))
    assert media.message_ids == (5, 6)
    assert media.last_part_at == NOW
    later = domain.DeliveryMedia(message_ids=(9,), last_part_at=NOW + timedelta(seconds=40))
    running = domain.running_turn("BonviHR", [media, later], bot_message_ids=[100], started_at=NOW)
    assert running.message_ids == frozenset({5, 6, 9, 100})
    assert running.media_group_ids == frozenset({"g"})
    assert running.last_part_at == NOW + timedelta(seconds=40)


def test_malformed_media_reads_as_empty() -> None:
    assert domain.media_from_json("x") == domain.DeliveryMedia()
    assert domain.media_from_json({"message_ids": [True, 3], "last_part_at": "nope"}).message_ids == (3,)


def test_a_title_is_the_first_line_within_120_characters() -> None:
    assert domain.title_of("\n  Login sahifasi\nikkinchi qator") == "Login sahifasi"
    assert len(domain.title_of("so'z " * 60)) <= domain.TITLE_MAX


# --- runaway (NFR-COST-03) ------------------------------------------------------------------------------------------
RUNAWAY = RunawayLimits(max_steps=200, max_wall=timedelta(minutes=60), max_same_failure=5)


def test_a_turn_runs_away_past_its_steps_its_wall_clock_or_a_repeated_failure() -> None:
    assert domain.runaway_of(steps=200, started_at=NOW, now=NOW, same_failures=0, limits=RUNAWAY) is None
    assert domain.runaway_of(steps=201, started_at=NOW, now=NOW, same_failures=0, limits=RUNAWAY) is (
        RunawayReason.STEPS
    )
    later = NOW + timedelta(minutes=61)
    assert domain.runaway_of(steps=1, started_at=NOW, now=later, same_failures=0, limits=RUNAWAY) is RunawayReason.WALL
    assert domain.runaway_of(steps=1, started_at=NOW, now=NOW, same_failures=5, limits=RUNAWAY) is (
        RunawayReason.SAME_FAILURE
    )


def test_investigations_and_smoke_have_the_fixed_bounds_of_spec_5_3() -> None:
    investigation = domain.runaway_limits(TurnKind.INVESTIGATE, max_steps=200, max_wall_min=60, max_same_failure=5)
    assert investigation.max_steps == 60
    assert investigation.max_wall == timedelta(minutes=3)
    smoke = domain.runaway_limits(TurnKind.SMOKE, max_steps=200, max_wall_min=60, max_same_failure=5)
    assert smoke.max_wall == timedelta(minutes=5)


def test_the_same_failure_is_the_same_tool_and_target() -> None:
    assert domain.failure_key("Bash", "make test") == domain.failure_key("Bash", "make test")
    assert domain.failure_key("Bash", "make test") != domain.failure_key("Bash", "make lint")


def test_the_runaway_notice_says_why_and_asks() -> None:
    text = domain.runaway_notice("BonviHR", RunawayReason.WALL, RUNAWAY)
    assert text == S.TURN_RUNAWAY(project="BonviHR", why=S.RUNAWAY_WALL(minutes=60))
    assert text.endswith("Davom ettiraymi?")


# --- usage limit (SPEC §5.6) ----------------------------------------------------------------------------------------
def test_a_limited_turn_retries_at_reset_after_a_rotation_or_every_ten_minutes() -> None:
    def due(*, after: timedelta, resets_in: timedelta | None = None, rotated: timedelta | None = None) -> bool:
        return domain.limit_retry_due(
            now=NOW + after,
            waiting_since=NOW,
            resets_at=NOW + resets_in if resets_in is not None else None,
            account_changed_at=NOW + rotated if rotated is not None else None,
        )

    assert not due(after=timedelta(minutes=9))
    assert due(after=timedelta(minutes=10))
    assert due(after=timedelta(minutes=2), resets_in=timedelta(minutes=2))
    assert not due(after=timedelta(seconds=30), rotated=timedelta(seconds=1))
    assert due(after=timedelta(seconds=41), rotated=timedelta(seconds=1))


def test_a_rotation_before_the_limit_does_not_count() -> None:
    assert not domain.limit_retry_due(
        now=NOW + timedelta(minutes=1), waiting_since=NOW, resets_at=None, account_changed_at=NOW - timedelta(hours=1)
    )


# --- the final message (SPEC §5.5) ----------------------------------------------------------------------------------
FINAL = """Ishni tugatdim.

Natija: Login sahifasidagi xato tuzatildi.
Testlar: o’tdi (42 passed)
<handoff>
Goal: fix login
Decisions: none
State: committed and pushed
Next: nothing
</handoff>"""


def test_the_result_contract_is_parsed() -> None:
    parsed = domain.parse_result(FINAL)
    assert parsed.natija == "Login sahifasidagi xato tuzatildi."
    assert parsed.tests is SuiteStatus.PASSED
    assert parsed.tests_summary == "42 passed"
    assert parsed.handoff is not None
    assert parsed.handoff.startswith("Goal: fix login")


@pytest.mark.parametrize(
    ("line", "status"),
    [
        ("Testlar: yiqildi (3 failed)", SuiteStatus.FAILED),
        ("Testlar: ishga tushirilmadi", SuiteStatus.NOT_RUN),
        ("**Testlar:** o'tdi", SuiteStatus.PASSED),
        ("Testlar: something else", SuiteStatus.UNKNOWN),
    ],
)
def test_each_tests_word_is_recognised(line: str, status: SuiteStatus) -> None:
    assert domain.parse_result(f"Natija: ok.\n{line}").tests is status


def test_a_missing_natija_is_the_first_sentence_and_missing_tests_are_unknown() -> None:
    parsed = domain.parse_result("Fixed the login bug. Then refactored the module.")
    assert parsed.natija == "Fixed the login bug."
    assert parsed.tests is SuiteStatus.UNKNOWN
    assert parsed.handoff is None


def test_the_handoff_block_is_capped_at_1500_characters() -> None:
    parsed = domain.parse_result("Natija: x\n<handoff>" + "word " * 600 + "</handoff>")
    assert parsed.handoff is not None
    assert len(parsed.handoff) <= domain.HANDOFF_BLOCK_MAX


# --- git facts, result and progress (SPEC §6.2) --------------------------------------------------------------------
PUSHED = WorkFacts(
    end_head="a1b2c3d4e5f6",
    commits=("a1b2c3d4e5f6",),
    changed=("src/app/login.py", "tests/test_login.py"),
    upstream="origin/main",
    ahead=0,
)


def test_pushed_work_is_finished_and_unpushed_or_dirty_work_is_not() -> None:
    assert PUSHED.pushed
    assert not PUSHED.unfinished
    assert WorkFacts(end_head="x", commits=("x",), upstream="origin/main", ahead=1).unfinished
    assert WorkFacts(end_head="x", commits=("x",), upstream=None).unfinished
    assert WorkFacts(end_head="x", uncommitted=2).unfinished
    assert WorkFacts(end_head="x", created=("new.py",)).unfinished
    assert not WorkFacts(end_head="x").unfinished
    assert not WorkFacts(end_head=None, known=False, uncommitted=3).unfinished


def test_created_files_are_the_untracked_ones_that_were_not_there_before() -> None:
    assert domain.created_files(["a.py", "b.py", "old.log"], ["old.log"]) == ("a.py", "b.py")


def test_the_result_follows_the_golden_format() -> None:
    parsed = domain.parse_result(FINAL)
    text = domain.result_text("BonviHR", parsed, PUSHED, seconds=7 * 60, push_missed=False, scrub=str)
    assert shown(text).splitlines() == [
        "BonviHR · Natija: Login sahifasidagi xato tuzatildi.",
        "Fayllar: 2 ta (login.py, test_login.py)",
        "Testlar: o'tdi (42 passed)",
        "Commit: a1b2c3d · push: bajarildi (origin/main)",
        "Vaqt: 7 daq",
    ]


def test_a_result_without_changes_or_tests_says_so() -> None:
    parsed = domain.parse_result("Natija: Hech narsa o'zgarmadi.")
    text = domain.result_text("HRMentor", parsed, WorkFacts(end_head="x"), seconds=30, push_missed=False, scrub=str)
    assert shown(text).splitlines() == [
        "HRMentor · Natija: Hech narsa o'zgarmadi.",
        "Fayllar: o'zgarmadi",
        "Testlar: noma'lum",
        "Commit: yo'q",
        "Vaqt: 0 daq",
    ]


def test_many_files_are_cut_after_five_names() -> None:
    files = tuple(f"f{n}.py" for n in range(8))
    text = domain.result_text(
        "X",
        domain.parse_result("Natija: ok."),
        WorkFacts(end_head="x", changed=files),
        seconds=0,
        push_missed=False,
        scrub=str,
    )
    assert "Fayllar: 8 ta (f0.py, f1.py, f2.py, f3.py, f4.py, …)" in shown(text)


def test_a_second_missed_push_shows_commit_push_qilinmadi() -> None:
    unpushed = WorkFacts(end_head="abcdef1234", commits=("abcdef1234",), upstream="origin/main", ahead=1)
    assert shown(domain.commit_line(unpushed, push_missed=True)) == "Commit: commit/push qilinmadi"
    assert shown(domain.commit_line(unpushed, push_missed=False)) == "Commit: abcdef1 · push: qilinmadi"
    assert domain.commit_line(WorkFacts(end_head=None, known=False), push_missed=False) == S.TURN_RESULT_GIT_UNKNOWN()


def test_the_natija_passes_the_scrubber() -> None:
    parsed = domain.parse_result("Natija: kalit sk-secret")
    text = domain.result_text("X", parsed, WorkFacts(end_head="x"), seconds=0, push_missed=False, scrub=lambda _: "***")
    assert shown(text).splitlines()[0] == "X · Natija: ***"


def test_the_progress_line_shows_time_steps_and_the_three_latest_steps() -> None:
    text = domain.progress_text("BonviHR", seconds=4 * 60, steps=12, latest=["a", "b", "c", "d"])
    assert shown(text).splitlines() == ["BonviHR · ishlayapti 4 daq · 12 qadam", "• b", "• c", "• d"]


def test_a_progress_step_names_its_tool_and_fits_200_characters() -> None:
    assert domain.progress_line(ProgressKind.TOOL, "Bash", "make  test") == "Bash: make test"
    assert domain.progress_line(ProgressKind.TOOL, "Read", "") == "Read"
    assert len(domain.progress_line(ProgressKind.TEXT, None, "x" * 500)) <= domain.SUMMARY_MAX


def test_usage_units_and_main_model() -> None:
    usage = {
        "claude-opus": ModelUsage(input_tokens=10, output_tokens=90),
        "claude-haiku": ModelUsage(input_tokens=5, output_tokens=1, cache_read_tokens=3),
    }
    assert domain.usage_units(usage) == {
        "input_tokens": 15,
        "output_tokens": 91,
        "cache_read_tokens": 3,
        "cache_write_tokens": 0,
    }
    assert domain.main_model(usage, "opus") == "claude-opus"
    assert domain.main_model({}, "opus") == "opus"


# --- the handoff note (SPEC §3.2.4) ---------------------------------------------------------------------------------
def test_the_handoff_note_adds_files_and_commits_from_git() -> None:
    note = HandoffNote("BonviHR", 3, NOW, block="Goal: x\nState: y", files=("a.py",), commits=("abcdef123",))
    assert note.source == "executor"
    assert note.text() == "Goal: x\nState: y\n\nFiles touched: a.py\n\nCommits: abcdef1"
    assert domain.handoff_from_json("BonviHR", 3, domain.handoff_json(note)) == note


def test_a_missing_block_asks_digest_to_synthesize_from_the_progress() -> None:
    note = HandoffNote("BonviHR", 3, NOW, block=None, progress=("Bash: make test",))
    assert note.needs_synthesis
    assert note.source == "synthesized"
    assert domain.handoff_from_json("BonviHR", 3, {"block": 1}) is None


def test_start_files_round_trip_and_say_whether_the_tree_was_dirty() -> None:
    files = StartFiles(dirty=("a.py",), untracked=("n.txt",))
    assert domain.start_files_from_json(domain.start_files_json(files)) == files
    assert files.is_dirty
    assert not StartFiles().is_dirty
    assert domain.start_files_from_json(None) == StartFiles()


# --- wrong target (UC-10 F) -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../b", ""])
def test_a_path_that_could_leave_the_repo_is_refused(path: str) -> None:
    assert domain.safe_relative(path) is None


def test_the_revert_restores_deletes_and_reverts_and_keeps_the_owners_own_edits() -> None:
    uncommitted = [
        FileChange("M", "src/a.py"),
        FileChange("A", "src/new.py"),
        FileChange("R100", "src/moved.py", old_path="src/old.py"),
        FileChange("M", "notes.md"),
        FileChange("M", "../escape.py"),
    ]
    plan = domain.revert_plan(
        uncommitted, created=["tmp/out.json", "/abs"], commits=["c2", "c1"], owner_dirty={"notes.md"}
    )
    assert plan.restore == ("src/a.py", "src/moved.py", "src/old.py")
    assert plan.delete == ("tmp/out.json", "src/new.py")
    assert plan.revert == ("c2", "c1")
    assert plan.kept == ("notes.md",)
    assert not plan.empty


def test_a_turn_that_changed_nothing_has_nothing_to_revert() -> None:
    assert domain.revert_plan([], created=[], commits=[]).empty


def test_the_offer_lists_at_most_ten_files() -> None:
    assert domain.revert_files_text([f"{n}.py" for n in range(12)]).endswith(", …")


# --- retention (SPEC §4.2) ------------------------------------------------------------------------------------------
def test_retention_keeps_turns_400_days_texts_30_and_steps_7() -> None:
    assert domain.turn_cutoff(NOW) == NOW - timedelta(days=400)
    assert domain.delivery_text_cutoff(NOW) == NOW - timedelta(days=30)
    assert domain.progress_cutoff(NOW) == NOW - timedelta(days=7)


def test_only_photos_and_images_sent_as_files_reach_the_agent_as_images() -> None:
    from yordamchi.shared.inbox.domain import UpdateKind

    assert domain.image_suffix(UpdateKind.PHOTO, None) == ".jpg"
    assert domain.image_suffix(UpdateKind.DOCUMENT, "image/PNG") == ".png"
    assert domain.image_suffix(UpdateKind.DOCUMENT, "application/pdf") is None
    assert domain.image_suffix(UpdateKind.VIDEO, "video/mp4") is None
    assert domain.image_suffix(UpdateKind.VOICE, "audio/ogg") is None


@pytest.mark.parametrize(
    ("latest", "phase"),
    [
        ((), S.PHASE_WORKING()),
        (("Read: src/app/login.py",), S.PHASE_READING()),
        (("Grep: def login",), S.PHASE_READING()),
        (("Edit: src/app/login.py",), S.PHASE_WRITING()),
        (("Bash: uv run pytest -q",), S.PHASE_TESTING()),
        (("Bash: make check",), S.PHASE_TESTING()),
        (("Bash: git commit -m fix",), S.PHASE_COMMITTING()),
        (("Bash: ls -la",), S.PHASE_RUNNING()),
        (("Edit: a.py", "Testlar o'tdi, endi commit qilaman."), S.PHASE_WORKING()),
    ],
)
def test_the_phase_names_what_the_turn_does_never_the_command(latest: tuple[str, ...], phase: str) -> None:
    assert domain.phase_of(latest) == phase


@pytest.mark.parametrize(("seconds", "text"), [(0, "0:00"), (45.9, "0:45"), (252, "4:12"), (3852, "1:04:12")])
def test_the_running_time_reads_as_a_clock(seconds: float, text: str) -> None:
    assert domain.elapsed_text(seconds) == text


def test_the_plain_live_line_has_no_steps() -> None:
    plain = domain.progress_text("BonviHR", seconds=252, steps=12, latest=("Bash: make test",), detail=0)
    assert plain == S.TURN_PROGRESS_PLAIN(project="BonviHR", phase=S.PHASE_TESTING(), elapsed="4:12")
    assert "make test" not in plain
