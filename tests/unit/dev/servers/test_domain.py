"""servers.domain: the probes and the gate's say over them, the parsers, issues and alert-once, the cache and the
owner's text (SPEC §3.2.4, §5.7; UC-15; REQUIREMENTS Q3). Pure: every input is an argument, time included."""

from collections.abc import Sequence
from datetime import timedelta

import pytest

from tests.factories import (
    JUMP_HOST,
    SAMPLE_CONTAINERS,
    SAMPLE_DF,
    SAMPLE_FREE,
    SAMPLE_UPTIME,
    SERVER_HOST,
    df_with_root_at,
    docker_ps_table,
    make_default_snapshot,
)
from tests.fakes.clock import DEFAULT_START
from tests.owner_text import shown
from yordamchi.core import redact
from yordamchi.dev.gate.domain import GateContext, GateRules, Policy
from yordamchi.dev.servers import domain
from yordamchi.dev.servers.domain import (
    NO_STATE,
    CheckState,
    ContainerState,
    HostStatus,
    Issue,
    IssueKind,
    Limits,
    Probe,
    ProbeKind,
    ProbeOutcome,
    ProbeStatus,
)
from yordamchi.shared.settings import defaults

NOW = DEFAULT_START
SYSLOG = "/var/log/syslog"
LIMITS = Limits(disk_warn_pct=90, memory_warn_pct=90)
SNAP = make_default_snapshot()
RULES = GateRules.from_settings(
    ask_classes=SNAP[defaults.GATE_ASK_CLASSES],
    readonly_remote_commands=SNAP[defaults.GATE_READONLY_REMOTE_COMMANDS],
    jump_only_hosts=SNAP[defaults.GATE_JUMP_ONLY_HOSTS],
    git_hosts=SNAP[defaults.GATE_GIT_HOSTS],
    http_write_allow_hosts=SNAP[defaults.GATE_HTTP_WRITE_ALLOW_HOSTS],
    temp_roots=SNAP[defaults.GATE_TEMP_ROOTS],
)
CTX = GateContext(
    policy=Policy.READ_ONLY,
    home="/Users/owner",
    cwd="/Users/owner",
    project_root="/Users/owner",
    data_dir="/Users/owner/Library/Application Support/Yordamchi",
    config_dir="/Users/owner/.config/yordamchi",
)


def ok(kind: ProbeKind, stdout: str, *, path: str = "") -> ProbeOutcome:
    command = {
        ProbeKind.UPTIME: domain.UPTIME_COMMAND,
        ProbeKind.DISK: domain.DISK_COMMAND,
        ProbeKind.MEMORY: domain.MEMORY_COMMAND,
        ProbeKind.CONTAINERS: domain.CONTAINERS_COMMAND,
        ProbeKind.UNITS: domain.UNITS_COMMAND,
        ProbeKind.LOG: domain.log_command(path),
    }[kind]
    return ProbeOutcome(Probe(kind, command, path), ProbeStatus.OK, exit_code=0, stdout=stdout)


def not_run(kind: ProbeKind, status: ProbeStatus, *, error_code: str | None = None) -> ProbeOutcome:
    return ProbeOutcome(Probe(kind, "x"), status, error_code=error_code)


def healthy(**overrides: ProbeOutcome) -> list[ProbeOutcome]:
    outcomes = {
        "uptime": ok(ProbeKind.UPTIME, SAMPLE_UPTIME),
        "disk": ok(ProbeKind.DISK, SAMPLE_DF),
        "memory": ok(ProbeKind.MEMORY, SAMPLE_FREE),
        "containers": ok(ProbeKind.CONTAINERS, docker_ps_table(SAMPLE_CONTAINERS)),
        "units": ok(ProbeKind.UNITS, ""),
    }
    outcomes.update(overrides)
    return list(outcomes.values())


def run(outcomes: Sequence[ProbeOutcome], previous: CheckState = NO_STATE, *, at_min: float = 0) -> domain.Evaluation:
    return domain.evaluate(
        SERVER_HOST, outcomes, previous, now=NOW + timedelta(minutes=at_min), limits=LIMITS, scrub=redact.scrubbed
    )


def keys(issues: Sequence[Issue]) -> list[str]:
    return [issue.key for issue in issues]


# --- probes and the gate ---------------------------------------------------------------------------------------------
def test_a_check_runs_uptime_disk_memory_containers_units_then_each_configured_log() -> None:
    paths = domain.log_paths_for(
        SERVER_HOST, ["bonvi-srv:/var/log/syslog", "other:/var/log/x.log", "BONVI-SRV:/opt/app/app.log"]
    )
    probes = domain.plan_probes(paths)
    assert [probe.command for probe in probes] == [
        "uptime",
        "df -h",
        "free -m",
        "docker ps",
        "systemctl --failed --no-legend",
        "tail -n 200 /var/log/syslog",
        "tail -n 200 /opt/app/app.log",
    ]
    assert probes[-1].key == "log:/opt/app/app.log"


def test_the_gate_decides_every_probe_from_its_own_read_only_list() -> None:
    probes = domain.plan_probes(["/var/log/syslog"])
    # The default list covers every probe, the system-wide failed-units command included.
    assert not any(domain.refused(SERVER_HOST, probe, rules=RULES, ctx=CTX, now=NOW) for probe in probes)
    without_units = GateRules.from_settings(
        ask_classes=(),
        readonly_remote_commands=[
            command for command in SNAP[defaults.GATE_READONLY_REMOTE_COMMANDS] if command != domain.UNITS_COMMAND
        ],
        jump_only_hosts=["server"],
        git_hosts=["github.com"],
        http_write_allow_hosts=(),
        temp_roots=("/tmp",),
    )
    refused = [
        probe.command for probe in probes if domain.refused(SERVER_HOST, probe, rules=without_units, ctx=CTX, now=NOW)
    ]
    assert refused == ["systemctl --failed --no-legend"]  # off the list: only that probe is refused


def test_a_log_path_the_gate_would_not_read_is_refused() -> None:
    escaping = Probe(ProbeKind.LOG, domain.log_command("/var/log/../../etc/shadow"), "/var/log/../../etc/shadow")
    assert domain.refused(SERVER_HOST, escaping, rules=RULES, ctx=CTX, now=NOW)


def test_the_jump_only_host_is_never_a_target_for_any_probe() -> None:
    assert domain.is_jump_only(JUMP_HOST, RULES)
    assert domain.is_jump_only("SERVER", RULES)
    assert not domain.is_jump_only(SERVER_HOST, RULES)
    for probe in domain.plan_probes(["/var/log/syslog"]):
        assert domain.refused(JUMP_HOST, probe, rules=RULES, ctx=CTX, now=NOW)


def test_clip_and_scrubbed_clip_keep_stored_text_short_and_keyless() -> None:
    assert domain.clip("abcdef", 4) == "abc…"
    assert domain.clip("abc", 4) == "abc"
    token = "sk-ant-api03-" + "Q" * 40 + "x7Yz9Kp2Lm4Nq8Rs"
    text = "x" * 190 + token  # the cut at 200 falls inside the key
    kept = domain.scrubbed_clip(text, 200, redact.scrubbed)
    assert len(kept) <= 200
    assert "QQQQ" not in kept


def test_huge_output_is_cut_before_parsing_keeping_a_tables_start_and_a_logs_end() -> None:
    table = docker_ps_table(SAMPLE_CONTAINERS * 2000)
    kept_table = domain.kept_output(Probe(ProbeKind.CONTAINERS, domain.CONTAINERS_COMMAND), table)
    assert len(kept_table) == domain.PARSE_MAX_CHARS
    assert kept_table.startswith("CONTAINER ID")
    log = "\n".join(f"line {index} " + "x" * 90 for index in range(5000))
    kept_log = domain.kept_output(Probe(ProbeKind.LOG, domain.log_command(SYSLOG), SYSLOG), log)
    assert len(kept_log) <= domain.PARSE_MAX_CHARS
    assert kept_log.startswith("line ")  # from a whole line on
    assert kept_log.endswith(log.splitlines()[-1])  # the newest line is kept
    assert domain.kept_output(Probe(ProbeKind.UPTIME, "uptime"), SAMPLE_UPTIME) == SAMPLE_UPTIME


def test_each_command_gets_its_own_bound_within_what_is_left_of_the_check() -> None:
    assert domain.command_timeout_s(150, 30) == 30
    assert domain.command_timeout_s(12, 30) == 12
    assert domain.command_timeout_s(1.5, 30) is None


# --- parsers ---------------------------------------------------------------------------------------------------------
def test_uptime_gives_the_load_and_how_long_it_has_been_up() -> None:
    assert domain.parse_uptime(SAMPLE_UPTIME) == ((0.52, 0.48, 0.40), "12 days, 3:04")
    translated = " 10:15:01 up 3 min,  2 users,  Durchschnittslast: 1,05, 0,98, 0,77"
    assert domain.parse_uptime(translated)[0] == (1.05, 0.98, 0.77)
    assert domain.parse_uptime("garbage") == (None, None)


def test_df_keeps_real_disks_fullest_first_and_skips_pseudo_filesystems() -> None:
    disks = domain.parse_disks(SAMPLE_DF)
    assert [(disk.mount, disk.used_pct) for disk in disks] == [("/", 85), ("/srv/data", 9)]
    assert (disks[0].size, disks[0].avail) == ("147G", "22G")
    runner = "Filesystem Size Used Avail Use% Mounted on\n/dev/sdc1 50G 47G 3G 94% /runner\n"
    assert [disk.mount for disk in domain.parse_disks(runner)] == ["/runner"]  # not under /run


def test_df_joins_a_wrapped_device_name_and_reads_macos_columns() -> None:
    wrapped = "Filesystem Size Used Avail Use% Mounted on\n/dev/mapper/ubuntu--vg-ubuntu--lv\n  98G 91G 3G 97% /\n"
    assert [(disk.mount, disk.used_pct) for disk in domain.parse_disks(wrapped)] == [("/", 97)]
    macos = (
        "Filesystem       Size   Used  Avail Capacity iused ifree %iused  Mounted on\n"
        "/dev/disk3s1s1  460Gi   10Gi  200Gi     5%    404k  2.1G    0%   /\n"
    )
    assert [(disk.mount, disk.used_pct) for disk in domain.parse_disks(macos)] == [("/", 5)]


def test_free_gives_memory_in_use_from_available_or_the_old_buffers_row() -> None:
    memory = domain.parse_memory(SAMPLE_FREE)
    assert memory is not None
    assert (memory.total_mb, memory.available_mb, memory.used_pct) == (11950, 6500, 46)
    old = (
        "             total       used       free     shared    buffers     cached\n"
        "Mem:          2000       1900        100          0        200        800\n"
        "-/+ buffers/cache:        900       1100\n"
    )
    old_memory = domain.parse_memory(old)
    assert old_memory is not None
    assert old_memory.used_pct == 45
    assert domain.parse_memory("") is None
    assert domain.parse_memory("header\nMem: nothing here\n") is None


@pytest.mark.parametrize(
    ("status", "state"),
    [
        ("Up 2 hours (healthy)", ContainerState.UP),
        ("Up 10 seconds (health: starting)", ContainerState.UP),
        ("Up 3 hours (unhealthy)", ContainerState.UNHEALTHY),
        ("Up 2 days (Paused)", ContainerState.PAUSED),
        ("Restarting (1) 5 seconds ago", ContainerState.RESTARTING),
        ("Exited (137) 2 hours ago", ContainerState.DOWN),
        ("Dead", ContainerState.DOWN),
        ("Created", ContainerState.CREATED),
        ("Removal In Progress", ContainerState.OTHER),
    ],
)
def test_container_state_from_the_status_column(status: str, state: ContainerState) -> None:
    assert domain.container_state(status) is state


def test_docker_ps_is_cut_by_its_header_offsets_even_with_empty_ports() -> None:
    rows = (
        *SAMPLE_CONTAINERS,
        ("3c4d5e6f7a8b", "bonvicall:1.0.21", '"python -m app"', "5 minutes ago", "Restarting (1) 20 seconds ago", "",
         "bonvicall-prod-worker-1"),
    )  # fmt: skip
    containers = domain.parse_containers(docker_ps_table(rows))
    assert [(item.name, item.state) for item in containers] == [
        ("hrmentor-api-1", ContainerState.UP),
        ("hrmentor-postgres-1", ContainerState.UP),
        ("bonvicall-prod-worker-1", ContainerState.RESTARTING),
    ]
    assert containers[2].status == "Restarting (1) 20 seconds ago"
    assert domain.parse_containers("") == ()


def test_docker_ps_without_a_usable_header_falls_back_to_the_last_word() -> None:
    text = "something else\nabc  img  cmd  1 hour ago  Up 1 hour  80/tcp  web-1\n"
    assert [(item.name, item.state) for item in domain.parse_containers(text)] == [("web-1", ContainerState.UP)]


def test_failed_units_drop_the_bullet_and_keep_each_unit_once() -> None:
    text = "● nginx.service loaded failed failed A high performance web server\nbackup.timer loaded failed failed x\n"
    assert domain.parse_failed_units(text + "● nginx.service loaded failed failed again\n") == (
        "nginx.service",
        "backup.timer",
    )
    assert domain.parse_failed_units("") == ()


def test_log_errors_count_every_hit_and_keep_the_latest_few_clipped() -> None:
    lines = [f"Oct 24 10:{minute:02d} app[1]: ERROR request {minute} failed" for minute in range(8)]
    text = "\n".join(["Oct 24 09:59 app[1]: started", *lines, "Oct 24 11:00 app[1]: " + "z" * 500 + " fatal"])
    errors = domain.parse_log_errors("/var/log/app.log", text, redact.scrubbed)
    assert errors.count == 9
    assert len(errors.lines) == domain.LOG_LINES_KEPT
    assert errors.lines[0].endswith("request 4 failed")
    assert len(errors.lines[-1]) == domain.LINE_MAX_CHARS


# --- issues and alerts -----------------------------------------------------------------------------------------------
def test_a_healthy_host_has_no_issue_and_its_readings_are_reported() -> None:
    evaluation = run(healthy())
    assert evaluation.ok
    assert evaluation.state.issues == ()
    assert evaluation.new_issues == ()
    report = evaluation.report
    assert report.load == (0.52, 0.48, 0.40)
    assert report.memory is not None
    assert report.memory.used_pct == 46
    assert report.containers is not None
    assert len(report.containers) == 2
    assert report.failed_units == ()


def test_disk_at_the_threshold_memory_a_failed_unit_and_bad_containers_are_issues() -> None:
    free_full = SAMPLE_FREE.replace("6500\n", "500\n")
    rows = (
        *SAMPLE_CONTAINERS,
        ("3c4d", "worker", '"run"', "1 hour ago", "Restarting (1) 3 seconds ago", "", "worker-1"),
        ("4d5e", "tunnel", '"run"', "4 days ago", "Up 4 days (unhealthy)", "", "tunnel-1"),
    )  # fmt: skip
    evaluation = run(
        healthy(
            disk=ok(ProbeKind.DISK, df_with_root_at(90)),
            memory=ok(ProbeKind.MEMORY, free_full),
            containers=ok(ProbeKind.CONTAINERS, docker_ps_table(rows)),
            units=ok(ProbeKind.UNITS, "● nginx.service loaded failed failed nginx\n"),
        )
    )
    assert keys(evaluation.state.issues) == [
        "disk_full:/",
        "memory_high:",
        "unit_failed:nginx.service",
        "container_restarting:worker-1",
        "container_unhealthy:tunnel-1",
    ]
    assert evaluation.state.issues[0].value == "90"
    assert evaluation.new_issues == evaluation.state.issues
    assert not evaluation.ok


def test_a_container_running_last_time_and_gone_now_is_flagged_down() -> None:
    first = run(healthy())
    gone = run(healthy(containers=ok(ProbeKind.CONTAINERS, docker_ps_table(SAMPLE_CONTAINERS[:1]))), first.state)
    assert keys(gone.new_issues) == ["container_down:hrmentor-postgres-1"]
    later = run(
        healthy(containers=ok(ProbeKind.CONTAINERS, docker_ps_table(SAMPLE_CONTAINERS[:1]))),
        gone.state,
        at_min=60 * 25,
    )
    assert later.state.issues == ()  # forgotten a day after it was last seen running


def test_docker_that_used_to_answer_and_now_fails_is_an_issue() -> None:
    first = run(healthy())
    failing = ProbeOutcome(Probe(ProbeKind.CONTAINERS, domain.CONTAINERS_COMMAND), ProbeStatus.FAILED, exit_code=1)
    down = run(healthy(containers=failing), first.state, at_min=30)
    assert keys(down.state.issues) == ["docker_down:"]
    never = run(healthy(containers=failing))  # no permission from the start: nothing to compare with
    assert never.state.issues == ()


def test_error_lines_in_a_configured_log_are_an_issue() -> None:
    log = ok(ProbeKind.LOG, "a\nb ERROR c\nd\n", path="/var/log/app.log")
    evaluation = run([*healthy(), log])
    assert keys(evaluation.state.issues) == ["log_errors:/var/log/app.log"]
    assert evaluation.state.issues[0].value == "1"


def test_an_issue_keeps_its_start_and_is_alerted_only_once() -> None:
    full = healthy(disk=ok(ProbeKind.DISK, df_with_root_at(95)))
    first = run(full)
    assert keys(first.new_issues) == ["disk_full:/"]
    second = run(full, first.state, at_min=30)
    assert second.new_issues == ()
    assert second.state.issues[0].since == NOW


def test_an_issue_that_flaps_alerts_once_and_again_only_after_a_quiet_day() -> None:
    full, fine = healthy(disk=ok(ProbeKind.DISK, df_with_root_at(91))), healthy()
    first = run(full)
    resolved = run(fine, first.state, at_min=30)
    assert resolved.state.issues == ()
    back = run(full, resolved.state, at_min=60)
    assert back.new_issues == ()
    quiet = run(fine, back.state, at_min=90)
    much_later = run(full, quiet.state, at_min=90 + 60 * 25)
    assert keys(much_later.new_issues) == ["disk_full:/"]


def test_what_a_probe_could_not_re_check_stays_as_it_was() -> None:
    first = run(healthy(disk=ok(ProbeKind.DISK, df_with_root_at(96))))
    timed_out = not_run(ProbeKind.DISK, ProbeStatus.ERROR, error_code="call_timed_out")
    second = run(healthy(disk=timed_out), first.state, at_min=30)
    assert keys(second.state.issues) == ["disk_full:/"]
    assert second.state.issues[0].since == NOW
    assert second.new_issues == ()


def test_an_issue_of_a_log_no_longer_configured_goes_with_it() -> None:
    log = ok(ProbeKind.LOG, "x ERROR y\n", path="/var/log/app.log")
    first = run([*healthy(), log])
    assert keys(first.state.issues) == ["log_errors:/var/log/app.log"]
    assert run(healthy(), first.state, at_min=30).state.issues == ()


def test_a_host_is_unreachable_only_after_two_checks_without_a_connection() -> None:
    down = [
        not_run(ProbeKind.UPTIME, ProbeStatus.ERROR, error_code="provider_unavailable"),
        *(not_run(kind, ProbeStatus.SKIPPED) for kind in (ProbeKind.DISK, ProbeKind.MEMORY, ProbeKind.CONTAINERS)),
    ]
    first = run(healthy(disk=ok(ProbeKind.DISK, df_with_root_at(93))))
    blip = run(down, first.state, at_min=30)
    assert not blip.report.reachable
    assert blip.report.error_code == "provider_unavailable"
    assert keys(blip.state.issues) == ["disk_full:/"]  # unknown is not resolved
    assert blip.new_issues == ()
    second = run(down, blip.state, at_min=60)
    assert keys(second.new_issues) == ["unreachable:"]
    back = run(healthy(), second.state, at_min=90)
    assert back.state.issues == ()
    assert back.state.failures == 0


def test_a_check_the_gate_refused_entirely_says_nothing_about_the_host() -> None:
    refused = [not_run(kind, ProbeStatus.REFUSED) for kind in ProbeKind if kind is not ProbeKind.LOG]
    evaluation = run(refused, CheckState(failures=1))
    assert evaluation.report.all_refused
    assert evaluation.state.failures == 1
    assert evaluation.state.issues == ()


# --- schedule, cache, words ------------------------------------------------------------------------------------------
def test_a_check_is_fresh_for_the_stale_after_minutes() -> None:
    assert domain.is_fresh(NOW - timedelta(minutes=30), NOW, max_age_min=30)
    assert not domain.is_fresh(NOW - timedelta(minutes=31), NOW, max_age_min=30)
    assert not domain.is_fresh(None, NOW, max_age_min=30)


def test_hosts_fall_due_after_the_interval_and_the_loop_wakes_in_time() -> None:
    checked = {"a": NOW - timedelta(minutes=30), "b": NOW - timedelta(minutes=10)}
    assert domain.due_hosts(["a", "b", "c"], checked, NOW, interval_min=30) == ("a", "c")
    assert domain.next_pass_in_s(["a", "b"], checked, NOW, interval_min=30) == 1.0  # "a" is due now
    assert domain.next_pass_in_s(["b"], checked, NOW, interval_min=30) == domain.CHECK_TICK_S
    soon = {"b": NOW - timedelta(minutes=29, seconds=30)}
    assert domain.next_pass_in_s(["b"], soon, NOW, interval_min=30) == 30
    assert domain.next_pass_in_s([], {}, NOW, interval_min=30) == domain.CHECK_TICK_S


def test_checking_minutes_round_up_and_retention_keeps_thirty_days() -> None:
    assert domain.checking_minutes(150) == 3
    assert domain.checking_minutes(20) == 1
    assert domain.retention_cutoff(NOW) == NOW - timedelta(days=30)


def test_the_owner_names_hosts_by_whole_word_in_any_case() -> None:
    hosts = ["bonvi-srv", "srv", "db.local"]
    assert domain.mentioned_hosts("Bonvi-srv da nima bo'lyapti?", hosts) == ("bonvi-srv",)
    assert domain.mentioned_hosts("srv va db.local qanday", hosts) == ("srv", "db.local")
    assert domain.mentioned_hosts("serverda nima bo'lyapti?", hosts) == ()


def status(evaluation: domain.Evaluation, *, at_min: float = 0) -> HostStatus:
    return domain.status_of(
        SERVER_HOST,
        checked_at=NOW + timedelta(minutes=at_min),
        report=evaluation.report,
        state=evaluation.state,
        error_code=evaluation.report.error_code,
    )


def test_status_lines_start_with_the_host_and_say_what_is_wrong() -> None:
    assert shown(domain.status_line(status(run(healthy())))) == (
        "bonvi-srv: hammasi joyida · yuklama 0.52 · disk 85% (/) · xotira 46% · 2 ta konteyner"
    )
    problems = status(run(healthy(disk=ok(ProbeKind.DISK, df_with_root_at(92)))))
    assert shown(domain.status_line(problems)).startswith("bonvi-srv: muammo — disk / 92% to'lgan · yuklama 0.52")
    down = run([not_run(ProbeKind.UPTIME, ProbeStatus.ERROR, error_code="auth_failed")])
    assert shown(domain.status_line(status(down))) == "bonvi-srv: ulanib bo'lmadi — kalit yoki login rad etildi"
    jump = shown(domain.status_line(HostStatus(JUMP_HOST, jump_only=True)))
    assert jump.startswith("server: faqat o'tish (jump) uchun")
    assert shown(domain.status_line(HostStatus("new-host"))) == "new-host: hali tekshirilmagan"
    refused = run([not_run(ProbeKind.UPTIME, ProbeStatus.REFUSED)])
    assert shown(domain.status_line(status(refused))).startswith("bonvi-srv: gate hech bir tekshiruv buyrug'iga")


def test_the_answer_ends_with_how_old_the_data_is() -> None:
    lines = domain.status_answer([status(run(healthy()))], NOW + timedelta(minutes=12)).splitlines()
    assert lines[-1] == "Ma'lumot: 12 daqiqa oldin"
    assert domain.status_answer([status(run(healthy()))], NOW).splitlines()[-1] == "Ma'lumot: hozirgina"
    assert shown(domain.status_answer([HostStatus("x")], NOW)) == "x: hali tekshirilmagan"


def test_an_alert_names_the_host_and_its_new_issues() -> None:
    issues = run(healthy(disk=ok(ProbeKind.DISK, df_with_root_at(97)))).new_issues
    assert shown(domain.alert_text(SERVER_HOST, issues)) == "bonvi-srv serverida muammo: disk / 97% to'lgan."
    every_kind = [Issue(kind, "s", NOW, "1") for kind in IssueKind]
    assert all(domain.issue_text(issue) for issue in every_kind)


def test_check_facts_give_an_investigation_the_last_check_as_data() -> None:
    facts = domain.check_facts(status(run(healthy(disk=ok(ProbeKind.DISK, df_with_root_at(95))))))
    assert facts.startswith("- bonvi-srv (2026-10-23T09:00:00+00:00): load 0.52 0.48 0.40;")
    assert "disk / 95%;" in facts
    assert facts.endswith("issues: disk_full:/")
    assert domain.check_facts(HostStatus(JUMP_HOST, jump_only=True)) == "- server: jump-only host, never checked"
    assert domain.check_facts(HostStatus("x")) == "- x: not checked yet"


# --- storage ---------------------------------------------------------------------------------------------------------
def test_the_summary_round_trips_and_carries_the_state_to_the_next_check() -> None:
    log = ok(ProbeKind.LOG, "x ERROR y\n", path="/var/log/app.log")
    evaluation = run([*healthy(disk=ok(ProbeKind.DISK, df_with_root_at(94))), log])
    stored = domain.summary_json(evaluation.report, evaluation.state, trigger="scheduled")
    report, state = domain.read_summary(SERVER_HOST, stored)
    assert report == evaluation.report
    assert state == evaluation.state
    assert stored["trigger"] == "scheduled"


@pytest.mark.parametrize("raw", [None, "x", [], {"v": 99}, {"v": 1, "issues": [{"kind": "nope", "since": "x"}]}])
def test_a_malformed_summary_reads_as_a_fresh_start(raw: object) -> None:
    report, state = domain.read_summary(SERVER_HOST, raw)  # type: ignore[arg-type]
    assert state.issues == ()
    assert state.alerted == {}
    if raw is None or not isinstance(raw, dict) or raw.get("v") != 1:
        assert report is None
