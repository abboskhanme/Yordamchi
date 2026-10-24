"""The `MacHost` contract: the fake, `MacOsHost` on the recorded snapshot (offline), and `MacOsHost` on this Mac
(`pytest -m live`, read-only) behave the same."""

from pathlib import Path

import pytest

from tests.fakes.mac_host import FakeMacHost
from tests.integration.macos_harness import recorded_mac
from yordamchi.adapters.macos.host import MacOsHost
from yordamchi.core.errors import CallTimedOut
from yordamchi.ports.mac_host import ClaudeManagerState, MacHost


class MacHostContract:
    @pytest.fixture
    def host(self) -> MacHost:
        raise NotImplementedError

    async def test_memory_pressure_is_a_kernel_level(self, host: MacHost) -> None:
        assert await host.memory_pressure_level(timeout=5.0) in {1, 2, 4}

    async def test_free_disk_is_non_negative(self, host: MacHost, tmp_path: Path) -> None:
        assert await host.free_disk_bytes(tmp_path, timeout=5.0) >= 0

    async def test_sessions_and_claude_manager_state_are_readable(self, host: MacHost) -> None:
        assert isinstance(await host.interactive_sessions(timeout=5.0), list)
        await host.claude_manager_state(timeout=5.0)

    async def test_sessions_are_unique_and_named(self, host: MacHost) -> None:
        found = await host.interactive_sessions(timeout=5.0)
        assert all(session.session_id for session in found)
        assert len({session.pid for session in found}) == len(found)

    async def test_at_most_one_claude_account_is_active(self, host: MacHost) -> None:
        state = await host.claude_manager_state(timeout=5.0)
        if state is not None:
            active = [account for account in state.accounts if account.is_active]
            assert len(active) <= 1
            assert not active or state.active_id is not None


# --- the fake ---------------------------------------------------------------------------------------------------------
class TestFakeMacHost(MacHostContract):
    @pytest.fixture
    def host(self) -> MacHost:
        return FakeMacHost()


# --- the adapter on the recorded snapshot (offline) -------------------------------------------------------------------
class TestMacOsHostOnRecordedOutput(MacHostContract):
    @pytest.fixture
    def host(self, tmp_path: Path) -> MacHost:
        return recorded_mac(tmp_path / "mac").host()

    async def test_the_recorded_snapshot_reads_as_the_owners_mac(self, host: MacHost) -> None:
        assert await host.memory_pressure_level(timeout=5.0) == 2
        assert len(await host.interactive_sessions(timeout=5.0)) == 5
        state = await host.claude_manager_state(timeout=5.0)
        assert isinstance(state, ClaudeManagerState)
        assert len(state.accounts) == 3

    async def test_a_probe_slower_than_its_timeout_is_call_timed_out(self, tmp_path: Path) -> None:
        mac = recorded_mac(tmp_path / "mac")
        mac.binary("sysctl", stdout="1\n", sleep_s=30)
        with pytest.raises(CallTimedOut):
            await mac.host().memory_pressure_level(timeout=0.3)


# --- the adapter on this Mac (live, read-only) ------------------------------------------------------------------------
@pytest.mark.live
class TestMacOsHostLive(MacHostContract):
    @pytest.fixture
    def host(self) -> MacHost:
        return MacOsHost(home=Path.home())
