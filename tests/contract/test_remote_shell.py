import pytest

from tests.fakes.remote_shell import FakeRemoteShell
from yordamchi.core.errors import CallTimedOut
from yordamchi.ports.remote_shell import RemoteResult, RemoteShell


class RemoteShellContract:
    @pytest.fixture
    def shell(self) -> RemoteShell:
        raise NotImplementedError

    async def test_run_reports_host_and_exit_code(self, shell: RemoteShell) -> None:
        result = await shell.run("bonvi-srv", "uptime", timeout=30.0)
        assert result.host == "bonvi-srv"
        assert isinstance(result.exit_code, int)


class TestFakeRemoteShell(RemoteShellContract):
    @pytest.fixture
    def shell(self) -> RemoteShell:
        return FakeRemoteShell()

    async def test_scripted_results_and_errors(self) -> None:
        shell = FakeRemoteShell()
        shell.respond("bonvi-srv", "df -h", RemoteResult("bonvi-srv", 0, "/dev/disk1 50%", "", 12))
        shell.respond("bonvi-srv", "uptime", CallTimedOut(operation="ssh"))
        assert (await shell.run("bonvi-srv", "df -h", timeout=30.0)).stdout == "/dev/disk1 50%"
        with pytest.raises(CallTimedOut):
            await shell.run("bonvi-srv", "uptime", timeout=30.0)
