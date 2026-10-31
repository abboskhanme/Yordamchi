"""Windows seams of the adapters (owner request 2026-10-31: the personal bot from a terminal), proven on this Mac.

- `KeychainKeystore(store=DpapiStore)`: the vault's master key comes from its DPAPI file, scoped to the bot.
- `ClaudeAssistant(platform="win32")`: a stuck CLI is ended with its tree by `taskkill /T /F /PID`, never through
  `ps` or a process group; when taskkill cannot run, the CLI alone is ended.
"""

import os
import signal
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from tests.fakes.dpapi import FakeDpapi
from yordamchi.adapters.claude import assistant as assistant_module
from yordamchi.adapters.claude import proctree
from yordamchi.adapters.claude.assistant import ClaudeAssistant
from yordamchi.adapters.keystore.aead import encode_key
from yordamchi.adapters.keystore.keychain import KeychainKeystore
from yordamchi.core import keychain, proc
from yordamchi.core.errors import VaultKeyMissing
from yordamchi.core.keychain import DpapiStore
from yordamchi.core.paths import BotName


# --- the keystore --------------------------------------------------------------------------------------------------
def test_the_keystore_reads_its_master_key_from_the_dpapi_store(tmp_path: Path) -> None:
    store = DpapiStore(tmp_path / "secrets", lib=FakeDpapi().lib())
    store.write(keychain.MASTER_KEY, encode_key(bytes(range(32))))
    keystore = KeychainKeystore(bot=BotName.PERSONAL, legacy_key_file=tmp_path / "master.key", store=store)
    sealed = keystore.seal(b"api-key", aad=b"row")
    assert keystore.unseal(sealed, aad=b"row", name="row") == b"api-key"
    assert len(keystore.fingerprint()) > 0


def test_without_a_dpapi_master_key_the_vault_is_degraded(tmp_path: Path) -> None:
    store = DpapiStore(tmp_path / "secrets", lib=FakeDpapi().lib())
    keystore = KeychainKeystore(bot=BotName.PERSONAL, legacy_key_file=tmp_path / "master.key", store=store)
    with pytest.raises(VaultKeyMissing):
        keystore.fingerprint()


# --- the assistant's kill ------------------------------------------------------------------------------------------
class _Client:
    """The fields `_kill` reads."""

    def __init__(self, pid: int | None) -> None:
        self.cli_pid = pid
        self.session_id = "s-windows"
        self.pump = None


def _assistant(tmp_path: Path) -> ClaudeAssistant:
    return ClaudeAssistant(
        cli_path=tmp_path / "claude.exe",
        search_path=str(tmp_path),
        sessions_dir=tmp_path / "sessions",
        base_env={},
        kill_grace_s=0.01,
        platform="win32",
    )


@pytest.fixture
def no_posix_kill(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """`ps`, `getpgrp` and `killpg` do not exist on Windows: any use fails the test. `os.kill` is recorded."""

    async def no_ps(**kwargs: object) -> list[proctree.ProcRow]:
        raise AssertionError(kwargs)

    def no_group(*args: object) -> None:
        raise AssertionError(args)

    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(proctree, "list_processes", no_ps)
    monkeypatch.setattr(os, "killpg", no_group)
    monkeypatch.setattr(os, "getpgrp", no_group)
    monkeypatch.setattr(os, "kill", lambda pid, signum: killed.append((pid, signum)))
    return killed


async def test_a_windows_kill_ends_the_cli_tree_with_taskkill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_posix_kill: list[tuple[int, int]]
) -> None:
    calls: list[tuple[str, ...]] = []

    async def run(argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> proc.ProcResult:
        del env, timeout
        calls.append(tuple(argv))
        return proc.ProcResult(argv=tuple(argv), returncode=0, stdout=b"", stderr=b"", duration_ms=1)

    monkeypatch.setattr(proc, "run", run)
    await _assistant(tmp_path)._kill(cast("assistant_module._Client", _Client(4242)))
    assert calls == [("taskkill", "/T", "/F", "/PID", "4242")]
    assert no_posix_kill == []


@pytest.mark.parametrize("failure", ["exit", "missing"])
async def test_without_taskkill_the_cli_alone_is_ended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_posix_kill: list[tuple[int, int]], failure: str
) -> None:
    async def run(argv: Sequence[str], *, env: Mapping[str, str], timeout: float) -> proc.ProcResult:
        del env, timeout
        if failure == "missing":
            raise FileNotFoundError(argv[0])
        return proc.ProcResult(argv=tuple(argv), returncode=128, stdout=b"", stderr=b"", duration_ms=1)

    monkeypatch.setattr(proc, "run", run)
    await _assistant(tmp_path)._kill(cast("assistant_module._Client", _Client(4242)))
    assert no_posix_kill == [(4242, signal.SIGTERM)]
