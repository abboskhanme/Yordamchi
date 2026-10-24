from pathlib import Path

import pytest

from tests.fakes.mac_host import FakeMacHost
from yordamchi.ports.mac_host import MacHost


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


class TestFakeMacHost(MacHostContract):
    @pytest.fixture
    def host(self) -> MacHost:
        return FakeMacHost()
