"""`MacOsHost` on recorded and synthetic macOS output (`tests/fixtures/macos/`, `tests/integration/macos_harness.py`):
memory pressure (SPEC §5.6, NFR-CAP-01), free disk (SPEC §9.2, NFR-CAP-05), the owner's sessions per spike S6
(SPEC §3.2.9) and ClaudeManager's token-free state (UC-06). `live` tests read this Mac, read-only.
"""

import json
import logging
import os
import shutil
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tests.integration.macos_harness import FakeMac, fake_mac, fixture_text, recorded_mac
from yordamchi.adapters.macos import host as host_module
from yordamchi.adapters.macos import sessions
from yordamchi.adapters.macos.claude_manager import parse_state
from yordamchi.adapters.macos.host import MacOsHost, free_bytes, parse_pressure_level
from yordamchi.core import proc
from yordamchi.core.errors import CallTimedOut, NotInstalled, ProviderUnavailable
from yordamchi.core.redact import LOG_FIELDS_ATTR
from yordamchi.ports.mac_host import InteractiveSession

LOGGER = "yordamchi.adapters.macos.host"
PS_ARGV_TAIL = ["-axww", "-o", "pid=,ppid=,lstart=,command="]
GIB = 1024**3

# The five terminal sessions open on the owner's Mac when the snapshot was taken (fixture ids).
RECORDED_OWNER_SESSIONS = [
    InteractiveSession(31496, "1b7c2f0e-4d3a-4c5e-9a10-000000031496", "interactive"),
    InteractiveSession(32804, "2c8d3a1f-5e4b-4d6f-8b21-000000032804", "interactive"),
    InteractiveSession(34055, "3d9e4b2a-6f5c-4e7a-9c32-000000034055", "interactive"),
    InteractiveSession(90938, "5fb06d4c-8b7e-4a9c-9e54-000000090938", "interactive"),
    InteractiveSession(94734, "60c17e5d-9c8f-4bad-8f65-000000094734", "interactive"),
]
EDGE_BOT_PID = 7000


@pytest.fixture
def mac(tmp_path: Path) -> FakeMac:
    return recorded_mac(tmp_path)


def _events(caplog: pytest.LogCaptureFixture) -> list[tuple[str, dict[str, object]]]:
    return [(record.getMessage(), dict(getattr(record, LOG_FIELDS_ATTR, {}))) for record in caplog.records]


@pytest.fixture
def unreadable() -> Iterator[list[Path]]:
    """Paths a test makes unreadable; their modes are restored so tmp_path can be cleaned up."""
    paths: list[Path] = []
    yield paths
    for path in paths:
        path.chmod(0o700)


# --- memory pressure --------------------------------------------------------------------------------------------------
async def test_memory_pressure_is_the_recorded_kernel_level(mac: FakeMac) -> None:
    assert await mac.host().memory_pressure_level(timeout=5.0) == 2
    assert mac.argv("sysctl") == [str(mac.bin / "sysctl"), "-n", "kern.memorystatus_vm_pressure_level"]


async def test_probes_run_with_their_own_environment_and_nothing_of_the_bots(
    mac: FakeMac, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok-should-never-reach-a-probe")
    await mac.host().memory_pressure_level(timeout=5.0)
    env = mac.env("sysctl")
    assert env["PATH"] == ":".join(proc.SYSTEM_PATH)
    assert (env["LC_ALL"], env["TZ"]) == ("C", "UTC")
    # `PWD`, `SHLVL` and `_` are set by /bin/sh itself.
    assert set(env) - {"PWD", "SHLVL", "_"} == {"PATH", "LC_ALL", "TZ"}


@pytest.mark.parametrize(
    ("output", "level"),
    [("1\n", 1), ("2\n", 2), ("4\n", 4), ("  4  \n", 4), ("kern.memorystatus_vm_pressure_level: 2\n", 2)],
)
def test_pressure_levels_parse(output: str, level: int) -> None:
    assert parse_pressure_level(output) == level


@pytest.mark.parametrize(
    "output", ["", "\n", "0\n", "3\n", "8\n", "two\n", "\N{FULLWIDTH DIGIT TWO}\n", "1\n2\n", "-1\n", "2.0\n"]
)
async def test_anything_but_a_known_level_is_an_error_not_a_guess(mac: FakeMac, output: str) -> None:
    mac.binary("sysctl", stdout=output)
    with pytest.raises(ProviderUnavailable) as caught:
        await mac.host().memory_pressure_level(timeout=5.0)
    assert caught.value.params == {"provider": "sysctl"}
    assert caught.value.retryable is False


async def test_an_unknown_sysctl_name_is_an_error(mac: FakeMac) -> None:
    mac.binary("sysctl", stderr="sysctl: unknown oid 'kern.memorystatus_vm_pressure_level'\n", exit_code=1)
    with pytest.raises(ProviderUnavailable) as caught:
        await mac.host().memory_pressure_level(timeout=5.0)
    assert caught.value.params == {"provider": "sysctl"}


async def test_a_slow_probe_is_killed_and_times_out(mac: FakeMac) -> None:
    mac.binary("sysctl", stdout="1\n", sleep_s=30)
    with pytest.raises(CallTimedOut) as caught:
        await mac.host().memory_pressure_level(timeout=0.3)
    assert caught.value.params == {"operation": "memory_pressure"}


async def test_a_missing_probe_binary_is_not_installed(tmp_path: Path) -> None:
    host = MacOsHost(home=tmp_path, sysctl=tmp_path / "nope" / "sysctl", ps=tmp_path / "nope" / "ps")
    with pytest.raises(NotInstalled) as caught:
        await host.memory_pressure_level(timeout=5.0)
    assert caught.value.params == {"binary": "sysctl"}
    with pytest.raises(NotInstalled) as caught:
        await host.interactive_sessions(timeout=5.0)
    assert caught.value.params == {"binary": "ps"}


# --- free disk --------------------------------------------------------------------------------------------------------
async def test_free_disk_is_what_statvfs_reports_for_the_volume(mac: FakeMac, tmp_path: Path) -> None:
    free = await mac.host().free_disk_bytes(tmp_path, timeout=5.0)
    assert abs(free - shutil.disk_usage(tmp_path).free) < GIB  # other processes write meanwhile


async def test_a_path_that_does_not_exist_yet_reads_its_nearest_ancestors_volume(mac: FakeMac, tmp_path: Path) -> None:
    missing = tmp_path / "data" / "not" / "created" / "yet"
    free = await mac.host().free_disk_bytes(missing, timeout=5.0)
    assert abs(free - shutil.disk_usage(tmp_path).free) < GIB


def test_free_bytes_walks_up_past_a_file_component(tmp_path: Path) -> None:
    file = tmp_path / "file.txt"
    file.write_text("x", encoding="utf-8")
    assert free_bytes(file / "below") > 0


async def test_an_unreadable_volume_is_an_error(
    mac: FakeMac, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def refuse(path: Path) -> int:
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr(host_module, "free_bytes", refuse)
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(ProviderUnavailable) as caught:
        await mac.host().free_disk_bytes(tmp_path, timeout=5.0)
    assert caught.value.params == {"provider": "disk"}
    assert _events(caplog) == [("free_disk_unreadable", {"errno": 13})]


async def test_a_hanging_volume_times_out(mac: FakeMac, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()

    def hang(path: Path) -> int:
        release.wait(5.0)
        return 0

    monkeypatch.setattr(host_module, "free_bytes", hang)
    try:
        with pytest.raises(CallTimedOut) as caught:
            await mac.host().free_disk_bytes(tmp_path, timeout=0.2)
    finally:
        release.set()
    assert caught.value.params == {"operation": "free_disk"}


# --- sessions: the recorded snapshot ----------------------------------------------------------------------------------
def test_the_recorded_process_table_parses_completely() -> None:
    processes, unparsed = sessions.parse_ps(fixture_text("ps.txt"))
    assert (len(processes), unparsed) == (25, 0)
    assert processes[90938].started == "Wed Oct 7 12:54:31 2026"  # the padded day is collapsed, as in procStart
    assert processes[48102].command == "/Applications/Claude Manager.app/Contents/MacOS/claude-manager"


async def test_the_owners_terminal_sessions_are_found_and_the_sdk_session_is_not(mac: FakeMac) -> None:
    found = await mac.host().interactive_sessions(timeout=5.0)
    assert found == RECORDED_OWNER_SESSIONS
    assert 72118 not in {session.pid for session in found}  # entrypoint sdk-py: Yordamchi's kind of session
    assert 54152 not in {session.pid for session in found}  # `claude --chrome-native-host` is no session
    assert mac.argv("ps") == [str(mac.bin / "ps"), *PS_ARGV_TAIL]
    assert mac.env("ps")["TZ"] == "UTC"  # so `lstart` has the registry's `procStart` form


async def test_descendants_of_the_bot_are_excluded_whatever_their_record_says(mac: FakeMac) -> None:
    # As if the spike's python (71831) were the bot: its SDK child 72118 is ours by ancestry too.
    assert await mac.host(own_pid=71831).interactive_sessions(timeout=5.0) == RECORDED_OWNER_SESSIONS
    # As if the owner's session 34055 were the bot: it and everything below it drop out.
    found = await mac.host(own_pid=34055).interactive_sessions(timeout=5.0)
    assert [session.pid for session in found] == [31496, 32804, 90938, 94734]


# --- sessions: synthetic edge cases -----------------------------------------------------------------------------------
@pytest.fixture
def edge_mac(tmp_path: Path) -> FakeMac:
    mac = fake_mac(tmp_path)
    mac.binary("ps", stdout=fixture_text("ps_edge.txt"))
    mac.use_registry("sessions_edge")
    return mac


async def test_edge_cases_follow_the_s6_rules(edge_mac: FakeMac) -> None:
    found = await edge_mac.host(own_pid=EDGE_BOT_PID).interactive_sessions(timeout=5.0)
    assert found == [
        # the owner resumed the bot's session at the Mac: its record (cli, procStart matches) names it
        InteractiveSession(8100, "aaaaaaaa-0000-4000-8000-000000007001", "interactive"),
        # no record: the command line names the session (`-r`, upper case normalised; `--session-id=`)
        InteractiveSession(8200, "cccccccc-0000-4000-8000-000000008200", "ps"),
        InteractiveSession(8300, "dddddddd-0000-4000-8000-000000008300", "ps"),
        # older CLI without procStart, and a procStart in a changed format: the live process is a claude
        InteractiveSession(8500, "85858585-0000-4000-8000-000000008500", "interactive"),
        InteractiveSession(8600, "86868686-0000-4000-8000-000000008600", "interactive"),
        # VS Code: an SDK-shaped command line, but not the Python SDK's entrypoint, so the owner's
        InteractiveSession(9100, "91919191-0000-4000-8000-000000009100", "interactive"),
    ]
    # Excluded: 7001/7003 (below the bot), 8000 (sdk-py orphan), 8400 (stale record, pid now a shell), 8700 (no id),
    # 8800 (python running a file called claude), 8900 (claude-manager), 9000 (not a uuid), 9999 (file of a dead pid).


async def test_without_a_registry_the_process_scan_stands_alone(edge_mac: FakeMac) -> None:
    shutil.rmtree(edge_mac.registry)
    found = await edge_mac.host(own_pid=EDGE_BOT_PID).interactive_sessions(timeout=5.0)
    assert [(session.pid, session.kind) for session in found] == [
        # Without a record nothing shows the SDK orphan is ours, and the Mac session wins when in doubt (UC-16).
        (8000, "ps"),
        (8100, "ps"),
        (8200, "ps"),
        (8300, "ps"),
    ]
    assert found[1].session_id == "aaaaaaaa-0000-4000-8000-000000007001"


async def test_an_unreadable_registry_is_logged_and_the_scan_goes_on(
    edge_mac: FakeMac, unreadable: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    edge_mac.registry.chmod(0o000)
    unreadable.append(edge_mac.registry)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        found = await edge_mac.host(own_pid=EDGE_BOT_PID).interactive_sessions(timeout=5.0)
    assert [session.pid for session in found] == [8000, 8100, 8200, 8300]
    assert [event for event, _ in _events(caplog)] == ["session_registry_unreadable"]


async def test_one_unreadable_record_does_not_hide_the_others(mac: FakeMac, unreadable: list[Path]) -> None:
    record = mac.registry / "31496.json"
    record.chmod(0o000)
    unreadable.append(record)
    found = await mac.host().interactive_sessions(timeout=5.0)
    assert [session.pid for session in found] == [32804, 34055, 90938, 94734]


async def test_an_oversized_record_is_ignored(mac: FakeMac) -> None:
    data = json.loads((mac.registry / "31496.json").read_text(encoding="utf-8"))
    data["padding"] = "x" * (sessions.RECORD_MAX_BYTES + 1)
    (mac.registry / "31496.json").write_text(json.dumps(data), encoding="utf-8")
    found = await mac.host().interactive_sessions(timeout=5.0)
    assert 31496 not in {session.pid for session in found}


async def test_a_ps_whose_output_no_longer_parses_is_an_error_not_an_empty_mac(mac: FakeMac) -> None:
    mac.binary("ps", stdout="PID PPID STARTED COMMAND\nsomething else entirely\n")
    with pytest.raises(ProviderUnavailable) as caught:
        await mac.host().interactive_sessions(timeout=5.0)
    assert caught.value.params == {"provider": "ps"}


async def test_a_failing_ps_is_an_error(mac: FakeMac) -> None:
    mac.binary("ps", stderr="ps: illegal option\n", exit_code=1)
    with pytest.raises(ProviderUnavailable):
        await mac.host().interactive_sessions(timeout=5.0)


async def test_a_slow_ps_times_out(mac: FakeMac) -> None:
    mac.binary("ps", stdout=fixture_text("ps.txt"), sleep_s=30)
    with pytest.raises(CallTimedOut) as caught:
        await mac.host().interactive_sessions(timeout=0.3)
    assert caught.value.params == {"operation": "process_scan"}


@pytest.mark.parametrize(
    ("command", "session_id"),
    [
        ("claude --resume 3d9e4b2a-6f5c-4e7a-9c32-000000034055", "3d9e4b2a-6f5c-4e7a-9c32-000000034055"),
        ("claude --resume=3D9E4B2A-6F5C-4E7A-9C32-000000034055", "3d9e4b2a-6f5c-4e7a-9c32-000000034055"),
        ("claude -r 3d9e4b2a-6f5c-4e7a-9c32-000000034055 --model opus", "3d9e4b2a-6f5c-4e7a-9c32-000000034055"),
        ("claude --session-id=3d9e4b2a-6f5c-4e7a-9c32-000000034055", "3d9e4b2a-6f5c-4e7a-9c32-000000034055"),
        ("claude --resume", None),
        ("claude --resume my-session-title", None),
        ("claude --resume 3d9e4b2a-6f5c-4e7a-9c32-000000034055x", None),
        ("claude --continue", None),
        ("claude --mcp-config-r 3d9e4b2a-6f5c-4e7a-9c32-000000034055", None),
    ],
)
def test_the_session_named_on_a_command_line(command: str, session_id: str | None) -> None:
    assert sessions.session_id_from_command(command) == session_id


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("claude", True),
        ("claude --resume x", True),
        ("/Users/owner/.local/bin/claude --output-format stream-json", True),
        ("/Applications/Claude Manager.app/Contents/MacOS/claude-manager", False),
        ("/usr/bin/python3 /Users/owner/bin/claude", False),
        ("claudette", False),
        ("-zsh", False),
    ],
)
def test_what_counts_as_a_claude_process(command: str, expected: bool) -> None:
    assert sessions.is_claude(command) is expected


def test_only_pid_named_json_files_are_registry_records() -> None:
    names = ["31496.json", "31496." + "a" * 64 + ".key", "notes.txt", "x31496.json", "9999.json", "123.json.tmp"]
    assert sessions.live_record_pids(names, [31496, 123]) == [31496]


# --- ClaudeManager ----------------------------------------------------------------------------------------------------
async def test_claude_manager_state_is_read_token_free(mac: FakeMac) -> None:
    state = await mac.host().claude_manager_state(timeout=5.0)
    assert state is not None
    assert state.active_id == "00000001-aaaa-4bbb-8ccc-000000000001"
    assert state.auto_switch is True
    assert [account.email for account in state.accounts] == [
        "owner+a@example.com",
        "owner+b@example.com",
        "owner+c@example.com",
    ]
    assert [account.is_active for account in state.accounts] == [True, False, False]
    first = state.accounts[0]
    assert (first.status, first.five_hour_pct, first.seven_day_pct) == ("ok", 77.0, 71.0)
    assert first.five_hour_resets_at == datetime(2026, 10, 24, 8, 29, 59, 729851, tzinfo=UTC)
    assert first.seven_day_resets_at == datetime(2026, 10, 26, 15, 59, 59, 729868, tzinfo=UTC)
    # Nothing of the token fingerprint, the oauthAccount object, settings or events comes through.
    dumped = repr(state)
    for absent in ("fingerprint", "synthetic-org", "accountUuid", "thresholdFiveHour", "synthetic warning"):
        assert absent not in dumped


async def test_no_claude_manager_is_none_and_quiet(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    mac = fake_mac(tmp_path)
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        assert await mac.host().claude_manager_state(timeout=5.0) is None
    assert caplog.records == []


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"{not json", "malformed"),
        (b"[]", "malformed"),
        (b"\xff\xfe", "malformed"),
        (b'{"padding": "' + b"x" * (1024 * 1024) + b'"}', "malformed"),
    ],
)
async def test_an_unusable_state_file_is_none_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, raw: bytes, reason: str
) -> None:
    mac = fake_mac(tmp_path)
    mac.use_manager_state(raw)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await mac.host().claude_manager_state(timeout=5.0) is None
    [(event, fields)] = _events(caplog)
    assert (event, fields["reason"]) == ("claude_manager_state_unreadable", reason)


async def test_an_unreadable_state_file_is_none_and_logged(
    mac: FakeMac, unreadable: list[Path], caplog: pytest.LogCaptureFixture
) -> None:
    mac.manager_state.chmod(0o000)
    unreadable.append(mac.manager_state)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert await mac.host().claude_manager_state(timeout=5.0) is None
    [(event, fields)] = _events(caplog)
    assert (event, fields["reason"]) == ("claude_manager_state_unreadable", "os_error")


def test_field_drift_degrades_per_field_not_per_file() -> None:
    raw = json.dumps(
        {
            "accounts": [
                {"id": "a", "email": "owner+a@example.com", "status": "needs_login", "usage": None},
                {"id": "b", "status": "ok"},  # no email: skipped
                {
                    "id": "c",
                    "email": "owner+c@example.com",
                    "usage": {
                        "fiveHour": {"utilization": "77", "resetsAt": "2026-10-24T08:29:59"},  # wrong type, no offset
                        "sevenDay": {"utilization": 12, "resetsAt": "2026-10-26T15:59:59.729868123Z"},  # nanoseconds
                    },
                },
                "not an account",
            ],
        }
    ).encode()
    state = parse_state(raw)
    assert state.active_id is None
    assert state.auto_switch is True  # serde's default for a missing field
    assert [account.email for account in state.accounts] == ["owner+a@example.com", "owner+c@example.com"]
    first, second = state.accounts
    assert (first.status, first.five_hour_pct, first.five_hour_resets_at, first.is_active) == (
        "needs_login",
        None,
        None,
        False,
    )
    assert (second.five_hour_pct, second.five_hour_resets_at) == (None, None)
    assert second.seven_day_pct == 12.0
    assert second.seven_day_resets_at == datetime(2026, 10, 26, 15, 59, 59, 729868, tzinfo=UTC)
    assert second.status == "unknown"


def test_a_non_finite_utilization_is_none() -> None:
    raw = b'{"activeId": "a", "accounts": [{"id": "a", "email": "e@x", "usage": {"fiveHour": {"utilization": NaN}}}]}'
    [account] = parse_state(raw).accounts
    assert account.five_hour_pct is None
    assert account.is_active is True


# --- live: this Mac, read-only ----------------------------------------------------------------------------------------
@pytest.mark.live
async def test_live_pressure_level_and_free_disk_match_the_system_tools() -> None:
    host = MacOsHost(home=Path.home())
    assert await host.memory_pressure_level(timeout=5.0) in {1, 2, 4}
    free = await host.free_disk_bytes(Path.home(), timeout=5.0)
    df = await proc.run(["/bin/df", "-k", "-P", str(Path.home())], env=host_module.PROBE_ENV, timeout=5.0)
    available_kib = int(df.stdout.decode().splitlines()[-1].split()[3])
    assert abs(free - available_kib * 1024) < GIB  # same statfs numbers; other processes write meanwhile


@pytest.mark.live
async def test_live_sessions_never_include_this_process_or_its_children() -> None:
    found = await MacOsHost(home=Path.home()).interactive_sessions(timeout=10.0)
    table = await proc.run(["/bin/ps", "-axww", "-o", sessions.PS_COLUMNS], env=host_module.PROBE_ENV, timeout=5.0)
    processes, unparsed = sessions.parse_ps(table.stdout.decode())
    assert processes
    assert unparsed == 0  # this Mac's ps still prints the format the parser expects
    pids = [session.pid for session in found]
    assert len(pids) == len(set(pids))
    assert not set(pids) & sessions.descendants(processes, os.getpid())
    assert all(session.session_id for session in found)


@pytest.mark.live
async def test_live_claude_manager_state_never_carries_token_fields() -> None:
    state = await MacOsHost(home=Path.home()).claude_manager_state(timeout=5.0)
    path = Path.home() / "Library" / "Application Support" / "uz.claudemanager.desktop" / "state.json"
    if not path.exists():
        pytest.skip("ClaudeManager is not installed on this Mac")
    assert state is not None
    assert state.accounts
    assert sum(account.is_active for account in state.accounts) <= 1
    assert "fingerprint" not in repr(state)
    assert "accessToken" not in repr(state)
