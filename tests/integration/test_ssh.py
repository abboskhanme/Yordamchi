"""`OpenSshRemoteShell` against a fake `ssh` on PATH (`tests/integration/ssh_harness.py`), replaying
`tests/fixtures/ssh/`: the non-interactive argv, the cut-down child environment, exit-code semantics, the error
mapping for every OpenSSH failure (UC-15; CONVENTIONS §7), retries inside the timeout, and logs without content.
Nothing here reaches a real host.
"""

import logging
import time
from pathlib import Path

import pytest

from tests.integration.ssh_harness import HOST, FakeSsh, fake_ssh, fixture
from yordamchi.adapters.ssh import translate
from yordamchi.adapters.ssh.shell import OpenSshRemoteShell, check_alias, connect_timeout_s
from yordamchi.core import retry
from yordamchi.core.errors import AuthFailed, CallTimedOut, ExternalError, NotInstalled, ProviderUnavailable
from yordamchi.core.redact import LOG_FIELDS_ATTR

LOGGER = "yordamchi.adapters.ssh.shell"
EXPECTED_OPTIONS = [
    "-T",
    "-o",
    "BatchMode=yes",
    "-o",
    "ClearAllForwardings=yes",
    "-o",
    "ForwardAgent=no",
    "-o",
    "ForwardX11=no",
    "-o",
    "PermitLocalCommand=no",
    "-o",
    "RemoteCommand=none",
    "-o",
    "ControlMaster=no",
]


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)


@pytest.fixture
def ssh(tmp_path: Path) -> FakeSsh:
    return fake_ssh(tmp_path)


def _run_calls(fake: FakeSsh) -> list[list[str]]:
    return [call for call in fake.calls() if "-G" not in call]


def _fields(caplog: pytest.LogCaptureFixture) -> list[tuple[str, dict[str, object]]]:
    return [(record.getMessage(), dict(getattr(record, LOG_FIELDS_ATTR, {}))) for record in caplog.records]


# --- the call ---------------------------------------------------------------------------------------------------------
async def test_runs_the_command_non_interactively_on_the_alias(ssh: FakeSsh) -> None:
    ssh.reply(HOST, stdout=fixture("df_h.stdout"))
    result = await ssh.shell().run(HOST, "df -h", timeout=30.0)
    assert result.host == HOST
    assert result.exit_code == 0
    assert result.stdout == fixture("df_h.stdout").decode()
    assert result.stderr == ""
    assert result.duration_ms >= 0
    [argv] = ssh.calls()
    assert argv == [str(ssh.bin / "ssh"), *EXPECTED_OPTIONS, "-o", "ConnectTimeout=10", "--", HOST, "df -h"]


@pytest.mark.parametrize(("remaining_s", "expected"), [(30.0, 10), (12.0, 6), (5.0, 2), (1.0, 1), (0.2, 1)])
def test_connect_timeout_is_half_the_remaining_call_within_1_to_10_s(remaining_s: float, expected: int) -> None:
    assert connect_timeout_s(remaining_s) == expected


async def test_a_short_call_gets_a_short_connect_timeout(ssh: FakeSsh) -> None:
    await ssh.shell().run(HOST, "uptime", timeout=5.0)
    [argv] = ssh.calls()
    assert "ConnectTimeout=2" in argv


async def test_a_non_zero_remote_exit_is_the_commands_answer_not_an_error(ssh: FakeSsh) -> None:
    ssh.reply(HOST, stdout=fixture("systemctl_inactive.stdout"), stderr=b"", exit_code=3)
    result = await ssh.shell().run(HOST, "systemctl is-active nginx", timeout=30.0)
    assert (result.exit_code, result.stdout) == (3, "inactive\n")
    assert len(ssh.calls()) == 1  # an answer is never retried


async def test_remote_output_that_is_not_utf8_is_decoded_with_replacement(ssh: FakeSsh) -> None:
    ssh.reply(HOST, stdout=b"caf\xe9\n", stderr=b"\xff warn\n")
    result = await ssh.shell().run(HOST, "tail -n 1 /var/log/syslog", timeout=30.0)
    assert result.stdout == "caf�\n"
    assert result.stderr == "� warn\n"


async def test_the_child_environment_holds_only_what_ssh_needs(ssh: FakeSsh, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SSH_AUTH_SOCK", "/private/tmp/com.apple.launchd.x/Listeners")
    monkeypatch.setenv("LC_CTYPE", "UTF-8")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "tok-should-never-reach-ssh")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("YORDAMCHI_ENV_FILE", "/data/dev.env")
    await ssh.shell().run(HOST, "uptime", timeout=30.0)
    env = ssh.env()
    assert env["PATH"] == ssh.path
    assert env["SSH_AUTH_SOCK"] == "/private/tmp/com.apple.launchd.x/Listeners"
    assert env["LC_CTYPE"] == "UTF-8"
    allowed = {"HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "SSH_AUTH_SOCK", "LANG", "PATH"}
    # `PWD`, `SHLVL` and `_` are set by /bin/sh itself, not inherited.
    assert {name for name in env if not name.startswith("LC_")} - allowed - {"PWD", "SHLVL", "_"} == set()


# --- what never reaches ssh -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "host", ["-oProxyCommand=touch_x", "deploy@bonvi-srv", "bonvi srv", "ssh://bonvi-srv", "", "bonvi-srv\n", "a:22"]
)
async def test_a_host_that_is_not_a_config_alias_is_refused_before_ssh_runs(ssh: FakeSsh, host: str) -> None:
    with pytest.raises(ValueError, match="alias"):
        await ssh.shell().run(host, "uptime", timeout=30.0)
    assert ssh.calls() == []


def test_aliases_from_ssh_config_pass() -> None:
    for host in ("bonvi-srv", "bonvi_srv.vm", "hetzner-1", "Prod2"):
        check_alias(host)


@pytest.mark.parametrize("command", ["", "   ", "\n"])
async def test_an_empty_command_is_refused_because_it_would_open_a_login_shell(ssh: FakeSsh, command: str) -> None:
    with pytest.raises(ValueError, match="empty"):
        await ssh.shell().run(HOST, command, timeout=30.0)
    assert ssh.calls() == []


async def test_a_command_that_looks_like_an_option_stays_the_remote_command(ssh: FakeSsh) -> None:
    await ssh.shell().run(HOST, "-oProxyCommand=evil", timeout=30.0)
    [argv] = ssh.calls()
    assert argv[-3:] == ["--", HOST, "-oProxyCommand=evil"]  # after `--`, OpenSSH parses no more options


def test_the_gate_shims_never_stand_in_for_the_bots_own_client(tmp_path: Path) -> None:
    shims = tmp_path / "Yordamchi" / "releases" / "current" / "shims"
    with pytest.raises(ValueError, match="shims"):
        OpenSshRemoteShell(ssh="/usr/bin/ssh", path=f"{shims}:/usr/bin:/bin")


async def test_a_missing_client_is_not_installed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(NotInstalled) as caught:
        await OpenSshRemoteShell(ssh="ssh", path=str(empty)).run(HOST, "uptime", timeout=5.0)
    assert caught.value.params == {"binary": "ssh"}
    assert caught.value.retryable is False


# --- error mapping (exit 255 + OpenSSH's message) ---------------------------------------------------------------------
ERROR_CASES = [
    # (stderr fixture, ssh -G fixture or None, error class, retryable, reason)
    ("resolve_failed.stderr", None, ProviderUnavailable, False, "host_unknown"),
    ("resolve_failed.stderr", "ssh_g_configured.stdout", ProviderUnavailable, True, "dns_failed"),
    ("resolve_failed_linux.stderr", "ssh_g_configured.stdout", ProviderUnavailable, True, "dns_failed"),
    ("permission_denied.stderr", None, AuthFailed, False, "auth_refused"),
    ("too_many_auth_failures.stderr", None, AuthFailed, False, "auth_refused"),
    ("host_key_unknown.stderr", None, AuthFailed, False, "host_key_rejected"),
    ("host_key_changed.stderr", None, AuthFailed, False, "host_key_rejected"),
    ("connection_refused.stderr", None, ProviderUnavailable, True, "connection_refused"),
    ("proxyjump_refused.stderr", None, ProviderUnavailable, True, "connection_refused"),
    ("connect_timed_out.stderr", None, CallTimedOut, True, "connect_timed_out"),
    ("banner_timed_out.stderr", None, CallTimedOut, True, "connect_timed_out"),
    ("no_route_to_host.stderr", None, ProviderUnavailable, True, "unreachable"),
    ("network_unreachable.stderr", None, ProviderUnavailable, True, "unreachable"),
    ("kex_connection_closed.stderr", None, ProviderUnavailable, True, "connection_lost"),
    ("negotiation_failed.stderr", None, ProviderUnavailable, False, "negotiation_failed"),
    ("bad_config_permissions.stderr", None, ProviderUnavailable, False, "config_invalid"),
    ("unrecognised.stderr", None, ProviderUnavailable, False, "unknown"),
]


@pytest.mark.parametrize(("stderr", "config", "error_class", "retryable", "reason"), ERROR_CASES)
async def test_each_openssh_failure_maps_to_its_error(  # one row of ERROR_CASES
    ssh: FakeSsh,
    caplog: pytest.LogCaptureFixture,
    stderr: str,
    config: str | None,
    error_class: type[ExternalError],
    retryable: bool,
    reason: str,
) -> None:
    ssh.fail(HOST, stderr)
    if config is not None:
        ssh.configure(HOST, config)
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(error_class) as caught:
        await ssh.shell().run(HOST, "uptime", timeout=30.0)
    error = caught.value
    assert type(error) is error_class
    assert error.retryable is retryable
    expected_params = {"operation": "ssh"} if error_class is CallTimedOut else {"provider": HOST}
    assert error.params == expected_params
    assert len(_run_calls(ssh)) == (retry.DEFAULT_ATTEMPTS if retryable else 1)
    [(event, fields)] = _fields(caplog)
    assert event == "ssh_command_failed"
    assert fields["reason"] == reason
    assert fields["host"] == HOST


async def test_a_resolution_failure_reads_the_config_without_connecting(ssh: FakeSsh) -> None:
    ssh.fail(HOST, "resolve_failed.stderr")
    ssh.configure(HOST, "ssh_g_configured.stdout")
    with pytest.raises(ProviderUnavailable):
        await ssh.shell().run(HOST, "uptime", timeout=30.0)
    config_calls = [call for call in ssh.calls() if "-G" in call]
    assert config_calls
    assert config_calls[0] == [str(ssh.bin / "ssh"), "-G", "--", HOST]


async def test_an_alias_the_config_does_not_define_is_host_unknown(ssh: FakeSsh) -> None:
    ssh.fail("no-such-alias", "resolve_failed.stderr")
    ssh.configure("no-such-alias", "ssh_g_unconfigured.stdout")  # recorded: `hostname no-such-alias`
    with pytest.raises(ProviderUnavailable) as caught:
        await ssh.shell().run("no-such-alias", "uptime", timeout=30.0)
    assert caught.value.retryable is False
    assert caught.value.params == {"provider": "no-such-alias"}
    assert len(_run_calls(ssh)) == 1


def test_the_ssh_g_hostname_line_is_read_from_the_recorded_config() -> None:
    assert translate.configured_hostname(fixture("ssh_g_configured.stdout").decode()) == "127.0.0.1"
    assert translate.configured_hostname(fixture("ssh_g_unconfigured.stdout").decode()) == "no-such-alias"
    assert translate.is_configured("bonvi-srv", fixture("ssh_g_configured.stdout").decode())
    assert not translate.is_configured("no-such-alias", fixture("ssh_g_unconfigured.stdout").decode())
    assert not translate.is_configured("No-Such-Alias", "hostname no-such-alias\n")  # ssh lower-cases it
    assert translate.configured_hostname("port 22\n") is None


# --- retries and timeouts ---------------------------------------------------------------------------------------------
async def test_a_transient_failure_is_retried_and_the_answer_returned(ssh: FakeSsh) -> None:
    ssh.fail(HOST, "connection_refused.stderr", attempt=1)
    ssh.reply(HOST, stdout=fixture("uptime.stdout"), attempt=2)
    result = await ssh.shell().run(HOST, "uptime", timeout=30.0)
    assert result.exit_code == 0
    assert result.stdout == fixture("uptime.stdout").decode()
    assert len(ssh.calls()) == 2


async def test_a_call_longer_than_its_timeout_is_killed_and_times_out(
    ssh: FakeSsh, caplog: pytest.LogCaptureFixture
) -> None:
    ssh.reply(HOST, sleep_s=30)
    started = time.monotonic()
    with caplog.at_level(logging.INFO, logger=LOGGER), pytest.raises(CallTimedOut) as caught:
        await ssh.shell().run(HOST, "uptime", timeout=0.5)
    assert time.monotonic() - started < 3.0  # the fake's process group was killed, not waited for
    assert caught.value.params == {"operation": "ssh"}
    [(event, fields)] = _fields(caplog)
    assert (event, fields["reason"]) == ("ssh_command_failed", "timed_out")


# --- logs -------------------------------------------------------------------------------------------------------------
async def test_logs_hold_the_alias_exit_code_and_duration_but_never_content(
    ssh: FakeSsh, caplog: pytest.LogCaptureFixture
) -> None:
    marker = "CONTENT-MARKER-7f3a"
    ssh.reply(HOST, stdout=f"{marker} in stdout\n".encode(), stderr=f"{marker} in stderr\n".encode())
    ssh.fail("other-host", "permission_denied.stderr")
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        await ssh.shell().run(HOST, f"tail -n 5 /var/log/{marker}.log", timeout=30.0)
        with pytest.raises(AuthFailed):
            await ssh.shell().run("other-host", f"cat /var/log/{marker}", timeout=30.0)
    records = _fields(caplog)
    assert [event for event, _ in records] == ["ssh_command_finished", "ssh_command_failed"]
    assert set(records[0][1]) == {"host", "exit_code", "duration_ms"}
    assert set(records[1][1]) == {"host", "code", "reason", "retryable", "attempts", "duration_ms"}
    dumped = repr(records)
    assert marker not in dumped
    assert "10.20.30.40" not in dumped  # the stderr of the failure stays out too
