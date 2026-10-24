"""The `RemoteShell` contract: the fake and `OpenSshRemoteShell` (offline, on a fake `ssh` replaying
`tests/fixtures/ssh/`) behave the same. There is no live variant: tests never ssh anywhere real."""

from pathlib import Path

import pytest

from tests.fakes.remote_shell import FakeRemoteShell
from tests.integration.ssh_harness import HOST, fake_ssh, fixture
from yordamchi.core import retry
from yordamchi.core.errors import CallTimedOut, ExternalError, ProviderUnavailable
from yordamchi.ports.remote_shell import RemoteResult, RemoteShell

UNKNOWN_HOST = "no-such-alias"


class RemoteShellContract:
    @pytest.fixture
    def shell(self) -> RemoteShell:
        raise NotImplementedError

    async def test_run_reports_host_and_exit_code(self, shell: RemoteShell) -> None:
        result = await shell.run("bonvi-srv", "uptime", timeout=30.0)
        assert result.host == "bonvi-srv"
        assert isinstance(result.exit_code, int)
        assert isinstance(result.stdout, str)
        assert isinstance(result.stderr, str)
        assert result.duration_ms >= 0


class RemoteShellFailureContract:
    """Every implementation surfaces an unknown alias and a slow host as the same `core.errors` classes."""

    @pytest.fixture
    def failing(self) -> RemoteShell:
        """`UNKNOWN_HOST` is not in the config; `HOST` takes longer than any test's timeout."""
        raise NotImplementedError

    async def test_an_unknown_alias_is_a_final_refusal(self, failing: RemoteShell) -> None:
        with pytest.raises(ProviderUnavailable) as caught:
            await failing.run(UNKNOWN_HOST, "uptime", timeout=30.0)
        assert isinstance(caught.value, ExternalError)
        assert caught.value.retryable is False

    async def test_a_call_longer_than_its_timeout_is_call_timed_out(self, failing: RemoteShell) -> None:
        with pytest.raises(CallTimedOut):
            await failing.run(HOST, "uptime", timeout=0.3)


# --- the fake ---------------------------------------------------------------------------------------------------------
class TestFakeRemoteShell(RemoteShellContract, RemoteShellFailureContract):
    @pytest.fixture
    def shell(self) -> RemoteShell:
        return FakeRemoteShell()

    @pytest.fixture
    def failing(self) -> RemoteShell:
        fake = FakeRemoteShell()
        fake.respond(UNKNOWN_HOST, "uptime", ProviderUnavailable(provider=UNKNOWN_HOST, retryable=False))
        fake.respond(HOST, "uptime", CallTimedOut(operation="ssh"))
        return fake

    async def test_scripted_results_and_errors(self) -> None:
        shell = FakeRemoteShell()
        shell.respond("bonvi-srv", "df -h", RemoteResult("bonvi-srv", 0, "/dev/disk1 50%", "", 12))
        shell.respond("bonvi-srv", "uptime", CallTimedOut(operation="ssh"))
        assert (await shell.run("bonvi-srv", "df -h", timeout=30.0)).stdout == "/dev/disk1 50%"
        with pytest.raises(CallTimedOut):
            await shell.run("bonvi-srv", "uptime", timeout=30.0)


# --- the adapter on a fake ssh (offline) ------------------------------------------------------------------------------
class TestOpenSshRemoteShell(RemoteShellContract, RemoteShellFailureContract):
    @pytest.fixture(autouse=True)
    def _instant_backoff(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(retry, "backoff_delay", lambda attempt, *, base_s, max_s: 0.0)

    @pytest.fixture
    def shell(self, tmp_path: Path) -> RemoteShell:
        ssh = fake_ssh(tmp_path / "ssh")
        ssh.reply(HOST, stdout=fixture("uptime.stdout"))
        return ssh.shell()

    @pytest.fixture
    def failing(self, tmp_path: Path) -> RemoteShell:
        ssh = fake_ssh(tmp_path / "ssh")
        ssh.fail(UNKNOWN_HOST, "resolve_failed.stderr")
        ssh.reply(HOST, sleep_s=30)
        return ssh.shell()
