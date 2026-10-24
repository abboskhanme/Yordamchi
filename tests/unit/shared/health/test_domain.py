"""health.domain: the heartbeat file, sleep and wake, restarts, the pinned "Onlayn", the CLI smoke test, the disk
guard, the media purge and the `holat?` text (SPEC §2.5 step 8, §2.8, §5.6, §6.2, §9.2; UC-06, NFR-REL-01/04/08,
NFR-CAP-05)."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from yordamchi.core.paths import BotName
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.mac_host import ClaudeAccount, ClaudeManagerState
from yordamchi.shared.health import domain
from yordamchi.shared.health.domain import (
    GIB,
    UNREAD,
    ClaudeCheck,
    CliChange,
    DiskLevel,
    DiskWarning,
    ErrorNote,
    Gap,
    HealthView,
    Heartbeat,
    Mode,
    OnlineMessage,
    OnlineStep,
    PollErrorKind,
    QueuedWork,
    WorkStatus,
)
from yordamchi.shared.outbox.domain import Code, Delivery, Status

NOW = datetime(2026, 10, 24, 9, 5, tzinfo=UTC)  # 14:05 in Tashkent
TZ = "Asia/Tashkent"


# --- the heartbeat file -------------------------------------------------------------------------------------------
def test_the_heartbeat_holds_the_spec_and_attempt_fields_with_aware_iso_instants() -> None:
    beat = Heartbeat(
        pid=4242,
        version="2026.10.24-abcdef0",
        started_at=NOW - timedelta(hours=1),
        last_poll_ok_at=NOW - timedelta(minutes=2),
        mode=Mode.PROBATION,
        loop_lag_ms=3,
        last_poll_attempt_at=NOW,
        last_poll_error_kind=PollErrorKind.NETWORK,
        consecutive_poll_failures=4,
    )
    assert domain.heartbeat_json(beat) == {
        "pid": 4242,
        "version": "2026.10.24-abcdef0",
        "started_at": "2026-10-24T08:05:00+00:00",
        "last_poll_ok_at": "2026-10-24T09:03:00+00:00",
        "mode": "probation",
        "loop_lag_ms": 3,
        "last_poll_attempt_at": "2026-10-24T09:05:00+00:00",
        "last_poll_error_kind": "network",
        "consecutive_poll_failures": 4,
    }


def test_before_the_first_poll_the_poll_fields_are_empty_and_counts_never_negative() -> None:
    beat = Heartbeat(pid=1, version="v", started_at=NOW, last_poll_ok_at=None, mode=Mode.NORMAL, loop_lag_ms=-2)
    body = domain.heartbeat_json(beat)
    assert body["last_poll_ok_at"] is None
    assert body["loop_lag_ms"] == 0
    assert body["last_poll_attempt_at"] is None
    assert body["last_poll_error_kind"] == "none"
    assert body["consecutive_poll_failures"] == 0
    assert domain.heartbeat_json(replace(beat, consecutive_poll_failures=-1))["consecutive_poll_failures"] == 0


def test_a_naive_instant_is_refused() -> None:
    beat = Heartbeat(
        pid=1, version="v", started_at=NOW.replace(tzinfo=None), last_poll_ok_at=None, mode=Mode.NORMAL, loop_lag_ms=0
    )
    with pytest.raises(ValueError, match="naive"):
        domain.heartbeat_json(beat)


def test_the_last_poll_time_is_read_back_from_a_previous_file() -> None:
    assert domain.last_alive({"last_poll_ok_at": "2026-10-24T09:05:00+00:00"}) == NOW
    assert domain.last_alive({"last_poll_ok_at": None}) is None
    assert domain.last_alive({"last_poll_ok_at": "2026-10-24T09:05:00"}) is None  # naive: not trusted
    assert domain.last_alive({"last_poll_ok_at": "yesterday"}) is None
    assert domain.last_alive(["not", "a", "dict"]) is None
    assert domain.last_alive(None) is None


@pytest.mark.parametrize("mode", [Mode.PAUSED, Mode.PROBATION])
def test_the_timer_always_writes_while_polling_is_paused_on_purpose(mode: Mode) -> None:
    long_ago = NOW - timedelta(hours=2)
    assert domain.timer_writes(mode, last_poll_ok_at=long_ago, started_at=long_ago, now=NOW)


def test_in_normal_mode_only_a_successful_poll_refreshes_the_file() -> None:
    started = NOW - timedelta(hours=1)
    assert not domain.timer_writes(Mode.NORMAL, last_poll_ok_at=NOW, started_at=started, now=NOW)
    assert not domain.timer_writes(Mode.NORMAL, last_poll_ok_at=NOW - timedelta(minutes=5), started_at=started, now=NOW)


def test_before_the_first_poll_the_timer_shows_the_new_process_for_a_minute() -> None:
    grace = domain.FIRST_POLL_GRACE
    assert domain.timer_writes(Mode.NORMAL, last_poll_ok_at=None, started_at=NOW - grace, now=NOW)
    assert not domain.timer_writes(
        Mode.NORMAL, last_poll_ok_at=None, started_at=NOW - grace - timedelta(seconds=1), now=NOW
    )


def test_while_polls_fail_on_the_network_the_timer_writes_within_a_minute_of_the_last_attempt() -> None:
    started, ok = NOW - timedelta(hours=1), NOW - timedelta(minutes=10)
    window = domain.LIVE_ATTEMPT_WINDOW
    for attempt, writes in ((NOW - window, True), (NOW - window - timedelta(seconds=1), False)):
        assert (
            domain.timer_writes(
                Mode.NORMAL,
                last_poll_ok_at=ok,
                started_at=started,
                now=NOW,
                last_poll_attempt_at=attempt,
                last_poll_error_kind=PollErrorKind.NETWORK,
            )
            is writes
        )


@pytest.mark.parametrize("kind", [PollErrorKind.NONE, PollErrorKind.OTHER])
def test_the_timer_never_covers_a_working_poll_or_another_failure(kind: PollErrorKind) -> None:
    started = NOW - timedelta(hours=1)
    assert not domain.timer_writes(
        Mode.NORMAL,
        last_poll_ok_at=NOW - timedelta(minutes=10),
        started_at=started,
        now=NOW,
        last_poll_attempt_at=NOW,
        last_poll_error_kind=kind,
    )


def test_a_live_loop_attempts_well_within_the_window() -> None:
    # bots.common.PollConfig: the long poll and its grace, or the longest backoff and a probe.
    assert max(25 + 10, 30 + 10) < domain.LIVE_ATTEMPT_WINDOW.total_seconds() < 120


# --- network outages ----------------------------------------------------------------------------------------------
def test_an_outage_of_five_minutes_is_told_from_its_first_failure() -> None:
    since = NOW - timedelta(minutes=5)
    assert domain.network_gap(since, NOW) == Gap(from_=since, to=NOW)
    assert domain.network_gap(since + timedelta(seconds=1), NOW) is None
    assert domain.network_gap(None, NOW) is None


def test_the_outage_notice_names_it_in_tashkent_time() -> None:
    gap = Gap(from_=datetime(2026, 10, 24, 9, 2, tzinfo=UTC), to=datetime(2026, 10, 24, 9, 41, tzinfo=UTC))
    assert domain.network_gap_text(gap, timezone=TZ) == "Internet uzilgan edi 14:02–14:41"
    assert domain.network_gap_ref(gap) == f"network-{int(gap.to.timestamp())}"


# --- sleep and wake -----------------------------------------------------------------------------------------------
def test_a_gap_of_five_minutes_between_ticks_means_the_mac_slept() -> None:
    before = NOW - timedelta(minutes=5)
    assert domain.gap_since(before, NOW) == Gap(from_=before, to=NOW)


def test_shorter_gaps_and_the_first_tick_are_not_sleep() -> None:
    assert domain.gap_since(NOW - timedelta(minutes=4, seconds=59), NOW) is None
    assert domain.gap_since(None, NOW) is None


def test_two_sleeps_before_the_next_poll_are_one_gap() -> None:
    first = Gap(from_=NOW - timedelta(hours=3), to=NOW - timedelta(hours=2))
    second = Gap(from_=NOW - timedelta(hours=1), to=NOW)
    assert domain.merge_gaps(first, second) == Gap(from_=first.from_, to=second.to)
    assert domain.merge_gaps(None, second) == second
    assert domain.merge_gaps(first, None) == first
    assert domain.merge_gaps(None, None) is None


def test_the_wake_notice_names_the_gap_in_tashkent_time_and_the_batch_size() -> None:
    gap = Gap(from_=datetime(2026, 10, 23, 20, 10, tzinfo=UTC), to=datetime(2026, 10, 24, 2, 45, tzinfo=UTC))
    assert domain.wake_text(gap, updates=3, timezone=TZ) == "Mac uxlagan edi 01:10–07:45, 3 ta xabar qayta ishlanmoqda"
    assert domain.wake_ref(gap) == f"wake-{int(gap.to.timestamp())}"


# --- restarts -----------------------------------------------------------------------------------------------------
def test_the_restart_notice_lists_each_interrupted_project_once_and_asks_to_continue() -> None:
    text = domain.restart_text(["BonviHR", "HRMentor", "BonviHR"], last_alive_at=NOW, timezone=TZ)
    assert text == (
        "Qayta ishga tushdim (oldingi ish kutilmaganda uzildi, oxirgi marta 14:05 da ishlagan edim). "
        "Uzilib qolgan ishlar: BonviHR, HRMentor — davom ettiraymi?"
    )


def test_a_restart_with_nothing_cut_off_says_so() -> None:
    text = domain.restart_text([], last_alive_at=None, timezone=TZ)
    assert text == "Qayta ishga tushdim (oldingi ish kutilmaganda uzildi). Uzilib qolgan ish yo'q."


# --- the pinned "Onlayn" message ----------------------------------------------------------------------------------
def delivery(
    key: str = "heartbeat:online:0",
    *,
    status: Status = Status.SENT,
    message_id: int | None = 101,
    attempts: int = 1,
    code: str | None = None,
) -> Delivery:
    return Delivery(
        dedupe_key=key, status=status, attempts=attempts, last_error_code=code, message_id=message_id, sent_at=None
    )


def test_the_first_message_is_sent_and_pinned_as_heartbeat_online_0() -> None:
    plan = domain.plan_online(None, sent=None, last_edit=None)
    assert plan.step is OnlineStep.SEND
    assert plan.message == OnlineMessage()
    assert (plan.message.ref, plan.message.send_key) == ("online", "heartbeat:online:0")


def test_while_the_first_post_is_queued_there_is_nothing_to_edit() -> None:
    current = OnlineMessage()
    plan = domain.plan_online(current, sent=delivery(status=Status.PENDING, message_id=None), last_edit=None)
    assert plan == domain.OnlinePlan(OnlineStep.WAIT, current)


def test_once_posted_its_message_id_is_kept_and_it_is_edited_in_place() -> None:
    plan = domain.plan_online(OnlineMessage(), sent=delivery(message_id=101), last_edit=None)
    assert plan.step is OnlineStep.EDIT
    assert plan.message == OnlineMessage(generation=1, message_id=101, edits=1)
    assert plan.message.last_edit_key == "heartbeat:online:edit1"


def test_each_edit_gets_its_own_key() -> None:
    current = OnlineMessage(message_id=101, edits=4)
    plan = domain.plan_online(current, sent=None, last_edit=delivery("heartbeat:online:edit4"))
    assert plan.message == OnlineMessage(message_id=101, edits=5)


def test_a_refused_edit_means_the_message_is_gone_so_a_new_one_is_sent_and_pinned() -> None:
    current = OnlineMessage(message_id=101, edits=4)
    refused = delivery("heartbeat:online:edit4", status=Status.FAILED, attempts=1, code="provider_unavailable")
    plan = domain.plan_online(current, sent=None, last_edit=refused)
    assert plan.step is OnlineStep.SEND
    assert plan.message == OnlineMessage(generation=2)
    assert (plan.message.ref, plan.message.send_key) == ("online-2", "heartbeat:online-2:0")


def test_an_edit_that_failed_only_after_retries_is_a_network_drop_not_a_gone_message() -> None:
    current = OnlineMessage(message_id=101, edits=4)
    dropped = delivery("heartbeat:online:edit4", status=Status.FAILED, attempts=5, code="provider_unavailable")
    assert domain.plan_online(current, sent=None, last_edit=dropped).step is OnlineStep.EDIT
    unknown = delivery("heartbeat:online:edit4", status=Status.FAILED, attempts=1, code=Code.DELIVERY_UNKNOWN.value)
    assert not domain.edit_refused(unknown)
    missing = delivery("heartbeat:online:edit4", status=Status.FAILED, attempts=3, code=Code.TARGET_MISSING.value)
    assert domain.edit_refused(missing)
    assert not domain.edit_refused(delivery(status=Status.SUPERSEDED))


def test_a_first_post_that_failed_or_vanished_is_sent_again_as_the_next_generation() -> None:
    current = OnlineMessage(generation=2)
    assert domain.plan_online(current, sent=delivery(status=Status.FAILED, message_id=None), last_edit=None) == (
        domain.OnlinePlan(OnlineStep.SEND, OnlineMessage(generation=3))
    )
    assert domain.plan_online(current, sent=None, last_edit=None).step is OnlineStep.SEND


def test_the_stored_state_round_trips_and_garbage_reads_as_none() -> None:
    message = OnlineMessage(generation=3, message_id=55, edits=7)
    assert OnlineMessage.from_json(message.to_json()) == message
    assert OnlineMessage.from_json({"generation": 1, "message_id": None, "edits": 0}) == OnlineMessage()
    assert OnlineMessage.from_json({"generation": True, "message_id": None, "edits": 0}) is None
    assert OnlineMessage.from_json({"generation": 1, "message_id": "55", "edits": 0}) is None
    assert OnlineMessage.from_json("online") is None


def test_the_online_text_is_hh_mm_in_tashkent() -> None:
    assert domain.online_text(NOW, timezone=TZ) == "Onlayn: 14:05"


# --- the CLI smoke test -------------------------------------------------------------------------------------------
def test_the_first_cli_version_is_the_reference_and_a_different_one_is_a_change() -> None:
    assert domain.cli_change(None, "2.1.310") is CliChange.BASELINE
    assert domain.cli_change("2.1.310", "2.1.310") is CliChange.SAME
    assert domain.cli_change("2.1.309", "2.1.310") is CliChange.CHANGED


def test_a_smoke_failure_names_the_version_and_what_failed() -> None:
    text = domain.smoke_failed_text("2.1.310", "commit")
    assert text.startswith("Claude CLI 2.1.310 ga yangilandi, lekin sinov ishi o'tmadi: fayl commit qilinmadi.")
    assert "ishlashda davom etyapman" in text
    assert "(error_max_turns)" in domain.smoke_failed_text("2.1.310", "error_max_turns")
    assert "(unknown)" in domain.smoke_failed_text("2.1.310", "")


def test_one_smoke_failure_notice_per_version() -> None:
    assert domain.smoke_ref("2.1.310") == "smoke-2.1.310"
    assert len(domain.smoke_ref("9" * 500)) == len("smoke-") + domain.VERSION_MAX


def test_the_probe_result_becomes_the_cached_check() -> None:
    status = RuntimeStatus(installed=True, version="2.1.310", logged_in=True, auth_method="claude.ai")
    assert domain.claude_check(status, at=NOW) == ClaudeCheck(
        at=NOW, installed=True, version="2.1.310", logged_in=True, auth_method="claude.ai"
    )


# --- disk guard and media purge -----------------------------------------------------------------------------------
def test_free_disk_levels_follow_the_two_settings() -> None:
    assert domain.disk_level(10 * GIB, warn_gb=10, block_gb=3) is DiskLevel.OK
    assert domain.disk_level(10 * GIB - 1, warn_gb=10, block_gb=3) is DiskLevel.LOW
    assert domain.disk_level(3 * GIB - 1, warn_gb=10, block_gb=3) is DiskLevel.BLOCKED


def test_the_disk_warning_repeats_every_six_hours_but_at_once_when_it_got_worse() -> None:
    last = DiskWarning(at=NOW, level=DiskLevel.LOW)
    assert domain.disk_warning_due(DiskLevel.LOW, last=None, now=NOW)
    assert not domain.disk_warning_due(DiskLevel.LOW, last=last, now=NOW + timedelta(hours=5, minutes=59))
    assert domain.disk_warning_due(DiskLevel.LOW, last=last, now=NOW + domain.DISK_WARN_EVERY)
    assert domain.disk_warning_due(DiskLevel.BLOCKED, last=last, now=NOW + timedelta(minutes=10))
    blocked = DiskWarning(at=NOW, level=DiskLevel.BLOCKED)
    assert not domain.disk_warning_due(DiskLevel.LOW, last=blocked, now=NOW + timedelta(hours=1))
    assert not domain.disk_warning_due(DiskLevel.OK, last=None, now=NOW)


def test_the_disk_warning_state_round_trips() -> None:
    warning = DiskWarning(at=NOW, level=DiskLevel.BLOCKED)
    assert DiskWarning.from_json(warning.to_json()) == warning
    assert DiskWarning.from_json({"at": "2026-10-24T09:05:00", "level": "low"}) is None
    assert DiskWarning.from_json({"at": NOW.isoformat(), "level": "full"}) is None
    assert DiskWarning.from_json(None) is None


def test_disk_texts_say_how_much_is_left_in_whole_gb() -> None:
    low = domain.disk_text(DiskLevel.LOW, int(7.9 * GIB), warn_gb=10, block_gb=3)
    assert low == "Diskda 7 GB joy qoldi (10 GB dan kam). Keraksiz fayllarni o'chirib, joy bo'shating."
    blocked = domain.disk_text(DiskLevel.BLOCKED, 2 * GIB, warn_gb=10, block_gb=3)
    assert blocked.startswith("Diskda atigi 2 GB qoldi (3 GB dan kam) — yangi kodlash ishlari navbatda turadi.")


def test_only_dated_media_folders_older_than_the_retention_go_oldest_first() -> None:
    names = ["20261016", "20261017", "20261010", "20261024", "notes", "2026101", "20261399", ".DS_Store"]
    assert domain.expired_media_dirs(names, today=date(2026, 10, 24), keep_days=7) == ("20261010", "20261016")


def test_events_are_kept_ninety_days() -> None:
    assert domain.event_cutoff(NOW) == NOW - timedelta(days=90)


# --- holat? -------------------------------------------------------------------------------------------------------
def account(email: str, *, active: bool, five: float | None = 42.4, seven: float | None = 13.0) -> ClaudeAccount:
    return ClaudeAccount(
        email=email,
        status="ok",
        five_hour_pct=five,
        seven_day_pct=seven,
        five_hour_resets_at=None,
        seven_day_resets_at=None,
        is_active=active,
    )


MANAGER = ClaudeManagerState(
    active_id="b",
    auto_switch=True,
    accounts=(account("a@example.com", active=False), account("owner@example.com", active=True)),
)
CHECK = ClaudeCheck(at=NOW, installed=True, version="2.1.310", logged_in=True, auth_method="claude.ai")


def view(**changes: object) -> HealthView:
    base = HealthView(
        bot=BotName.DEV,
        now=NOW,
        timezone=TZ,
        started_at=NOW - timedelta(hours=3, minutes=12),
        version="2026.10.24-abcdef0",
        sdk_version="0.2.173",
        claude=CHECK,
        manager=MANAGER,
        speech_providers=("google", "openrouter"),
        work=WorkStatus(
            running=("BonviHR", "HRMentor"), queued=(QueuedWork("Wunderkind", 1),), decisions=1, questions=0
        ),
        pressure_level=2,
        free_disk_bytes=85 * GIB + 5,
        degraded=("ffmpeg", "gh"),
        errors=(ErrorNote("stt_failed", NOW - timedelta(minutes=30)), ErrorNote("git_failed", NOW)),
    )
    return replace(base, **changes)  # type: ignore[arg-type]  # each test passes values of the right field types


def test_holat_shows_every_spec_part_in_order() -> None:
    assert domain.holat_text(view()).splitlines() == [
        "Holat · dev bot",
        "Ish vaqti: 3 soat 12 daq · Yordamchi 2026.10.24-abcdef0 · SDK 0.2.173",
        "Claude CLI 2.1.310: login — claude.ai",
        "ClaudeManager: owner@example.com · 5 soat: 42% · 7 kun: 13%",
        "Ovoz hozir: Google Gemini, zaxira — OpenRouter (Gemini).",
        "Hozir ishlayapti: BonviHR, HRMentor",
        "Navbatda: Wunderkind (1-o'rin)",
        "Javob kutayotgan qarorlar: 1 · savollar: 0",
        "Mac: xotira bosimi — o'rtacha · diskda 85 GB bo'sh",
        "Cheklangan imkoniyatlar: ffmpeg, gh topilmadi",
        "Oxirgi xatolar: git_failed (14:05), stt_failed (13:35)",
    ]


def test_the_personal_bot_has_no_work_lines_and_nothing_degraded_is_not_listed() -> None:
    lines = domain.holat_text(view(bot=BotName.PERSONAL, work=None, degraded=(), errors=())).splitlines()
    assert lines[0] == "Holat · shaxsiy bot"
    assert not any(
        line.startswith(("Hozir ishlayapti", "Navbatda", "Javob kutayotgan", "Cheklangan")) for line in lines
    )
    assert lines[-1] == "Oxirgi xatolar: yo'q"


def test_parts_that_could_not_be_read_say_so() -> None:
    lines = domain.holat_text(
        view(manager=UNREAD, work=UNREAD, pressure_level=UNREAD, free_disk_bytes=UNREAD, speech_providers=UNREAD)
    ).splitlines()
    assert "ClaudeManager: holatini o'qib bo'lmadi" in lines
    assert "Ishlar: o'qib bo'lmadi" in lines
    assert "Ovoz: kalitlar holatini o'qib bo'lmadi" in lines
    assert "Mac: xotira bosimi — noma'lum · diskda noma'lum bo'sh" in lines


def test_an_idle_dev_bot_shows_nothing_running_and_no_queue() -> None:
    lines = domain.holat_text(view(work=WorkStatus())).splitlines()
    assert "Hozir ishlayapti: hech narsa" in lines
    assert not any(line.startswith("Navbatda") for line in lines)


@pytest.mark.parametrize(
    ("check", "line"),
    [
        (None, "Claude CLI: hali tekshirilmadi"),
        (ClaudeCheck(at=NOW, error_code="call_timed_out"), "Claude CLI: tekshirib bo'lmadi (call_timed_out)"),
        (ClaudeCheck(at=NOW, installed=False), "Claude CLI: topilmadi"),
        (
            ClaudeCheck(at=NOW, installed=True, version="2.1.310", logged_in=False),
            "Claude CLI 2.1.310: login qilinmagan — Mac'da `claude` ni ochib, kiring",
        ),
        (
            ClaudeCheck(at=NOW, installed=True, version="2.1.310", logged_in=True, auth_method="api_key"),
            (
                "Claude CLI 2.1.310: login usuli «api_key», claude.ai bo'lishi kerak — Mac'da `claude auth status` "
                "ni tekshiring"
            ),
        ),
    ],
)
def test_the_claude_line_says_what_is_wrong_with_the_login(check: ClaudeCheck | None, line: str) -> None:
    assert domain.holat_text(view(claude=check)).splitlines()[2] == line


def test_claude_manager_without_an_active_account_or_with_unknown_usage() -> None:
    idle = ClaudeManagerState(active_id=None, auto_switch=False, accounts=(account("a@example.com", active=False),))
    assert domain.holat_text(view(manager=idle)).splitlines()[3] == "ClaudeManager: faol akkaunt yo'q"
    unknown = ClaudeManagerState(
        active_id="a", auto_switch=False, accounts=(account("a@example.com", active=True, five=None, seven=None),)
    )
    assert domain.holat_text(view(manager=unknown)).splitlines()[3] == (
        "ClaudeManager: a@example.com · 5 soat: noma'lum · 7 kun: noma'lum"
    )
    assert domain.holat_text(view(manager=None)).splitlines()[3] == "ClaudeManager: holatini o'qib bo'lmadi"


def test_no_speech_key_is_said_plainly() -> None:
    lines = domain.holat_text(view(speech_providers=())).splitlines()
    assert "Ovoz: ishlaydigan kalit yo'q — ovozli xabarlarni matnga o'gira olmayman." in lines


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0 daq"), (59 * 60 + 59, "59 daq"), (3600, "1 soat 0 daq"), (26 * 3600 + 5 * 60, "1 kun 2 soat")],
)
def test_uptime_reads_in_minutes_hours_or_days(seconds: float, text: str) -> None:
    assert domain.duration_text(seconds) == text
