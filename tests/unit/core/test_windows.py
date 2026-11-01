"""Windows seams of `core` (owner request 2026-10-31: the personal bot from a terminal), proven on this Mac.

- `core.dpapi`: the `DATA_BLOB` marshalling round-trips through a fake `crypt32` (`tests.fakes.dpapi`), every output
  buffer is released, a failure carries the Windows error code, and the real lib exists only on Windows.
- `core.keychain.DpapiStore`: one sealed file per item, atomic, missing → `KeychainItemMissing`, unreadable or
  foreign → `KeychainUnavailable`; the per-bot scope (UC-36) still applies above it; `default_store` picks by platform.
- `core.config.load_paths(platform="win32")`: `%LOCALAPPDATA%` / `%APPDATA%` defaults and the same overrides; the
  personal config resolves its token reference through DPAPI.
- `core.proc`: `;`-joined PATH without the POSIX system dirs, and a timeout kill that needs no process group.
"""

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from tests.factories import OWNER_ID, PERSONAL_BOT_TOKEN
from tests.fakes.dpapi import ERROR_INVALID_DATA, FakeDpapi
from yordamchi.core import config, dpapi, keychain, proc
from yordamchi.core.errors import KeychainItemForbidden, KeychainItemMissing, KeychainUnavailable
from yordamchi.core.keychain import DpapiStore, Keychain, SecurityStore
from yordamchi.core.paths import BotName, Paths

MASTER_VALUE = "q83vEjRWeJq83vEjRWeJq83vEjRWeJq83vEjRWeJq80="


# --- core.dpapi ----------------------------------------------------------------------------------------------------
def test_dpapi_round_trips_through_the_blob_marshalling_and_frees_every_output() -> None:
    fake = FakeDpapi()
    sealed = dpapi.protect(b"secret-value", entropy=b"uz.yordamchi/vault-master-key", lib=fake.lib())
    assert b"secret-value" not in sealed
    assert dpapi.unprotect(sealed, entropy=b"uz.yordamchi/vault-master-key", lib=fake.lib()) == b"secret-value"
    assert fake.calls == [("protect", dpapi.CRYPTPROTECT_UI_FORBIDDEN), ("unprotect", dpapi.CRYPTPROTECT_UI_FORBIDDEN)]
    assert sorted(fake.freed) == sorted(fake.allocated)
    assert len(fake.freed) == 2


def test_dpapi_refuses_a_blob_under_other_entropy_with_the_windows_error_code() -> None:
    fake = FakeDpapi()
    sealed = dpapi.protect(b"value", entropy=b"uz.yordamchi/personal-bot-token", lib=fake.lib())
    with pytest.raises(dpapi.DpapiError) as caught:
        dpapi.unprotect(sealed, entropy=b"uz.yordamchi/vault-master-key", lib=fake.lib())
    assert caught.value.errno == ERROR_INVALID_DATA
    assert isinstance(caught.value, OSError)
    assert len(fake.freed) == 1  # only the successful call had an output to free


def test_a_failing_protect_raises_without_freeing_anything() -> None:
    fake = FakeDpapi(fail=5)
    with pytest.raises(dpapi.DpapiError) as caught:
        dpapi.protect(b"value", entropy=b"item", lib=fake.lib())
    assert caught.value.errno == 5
    assert fake.freed == []


@pytest.mark.skipif(sys.platform == "win32", reason="the real DPAPI is there")
def test_the_real_dpapi_exists_only_on_windows() -> None:
    with pytest.raises(dpapi.DpapiError, match="only on Windows"):
        dpapi.system()
    with pytest.raises(dpapi.DpapiError):
        dpapi.protect(b"value", entropy=b"item")


# --- core.keychain.DpapiStore --------------------------------------------------------------------------------------
@pytest.fixture
def store(tmp_path: Path) -> DpapiStore:
    return DpapiStore(tmp_path / "secrets", lib=FakeDpapi().lib())


def test_a_dpapi_store_round_trips_one_sealed_file_per_item(store: DpapiStore, tmp_path: Path) -> None:
    store.write(keychain.MASTER_KEY, MASTER_VALUE)
    store.write(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    path = tmp_path / "secrets" / "uz.yordamchi" / "vault-master-key.dpapi"
    assert store.file(keychain.MASTER_KEY) == path
    assert MASTER_VALUE.encode() not in path.read_bytes()
    assert store.read(keychain.MASTER_KEY) == MASTER_VALUE
    assert store.read(keychain.PERSONAL_BOT_TOKEN) == PERSONAL_BOT_TOKEN
    assert sorted(entry.name for entry in path.parent.iterdir()) == [
        "personal-bot-token.dpapi",
        "vault-master-key.dpapi",
    ]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_a_dpapi_file_is_private_and_replaced_atomically(store: DpapiStore) -> None:
    store.write(keychain.MASTER_KEY, MASTER_VALUE)
    store.write(keychain.MASTER_KEY, "bmV3LWtleQ==")
    path = store.file(keychain.MASTER_KEY)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert [entry.name for entry in path.parent.iterdir()] == ["vault-master-key.dpapi"]  # no temp file left
    assert store.read(keychain.MASTER_KEY) == "bmV3LWtleQ=="


def test_a_missing_item_is_missing(store: DpapiStore) -> None:
    with pytest.raises(KeychainItemMissing):
        store.read(keychain.MASTER_KEY)


def test_a_foreign_or_corrupt_blob_is_unavailable(store: DpapiStore) -> None:
    store.write(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    path = store.file(keychain.MASTER_KEY)
    # Another item's blob moved under this item's name does not open (the entropy is the item name).
    path.write_bytes(store.file(keychain.PERSONAL_BOT_TOKEN).read_bytes())
    with pytest.raises(KeychainUnavailable):
        store.read(keychain.MASTER_KEY)
    path.write_bytes(b"garbage")
    with pytest.raises(KeychainUnavailable):
        store.read(keychain.MASTER_KEY)


def test_a_failing_seal_writes_nothing(tmp_path: Path) -> None:
    store = DpapiStore(tmp_path / "secrets", lib=FakeDpapi(fail=5).lib())
    with pytest.raises(KeychainUnavailable):
        store.write(keychain.MASTER_KEY, MASTER_VALUE)
    assert not store.file(keychain.MASTER_KEY).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the real DPAPI is there")
def test_without_dpapi_the_store_is_unavailable_not_a_crash(tmp_path: Path) -> None:
    store = DpapiStore(tmp_path / "secrets")
    with pytest.raises(KeychainUnavailable):
        store.write(keychain.MASTER_KEY, MASTER_VALUE)
    store.file(keychain.MASTER_KEY).parent.mkdir(parents=True)
    store.file(keychain.MASTER_KEY).write_bytes(b"blob")
    with pytest.raises(KeychainUnavailable):
        store.read(keychain.MASTER_KEY)


def test_a_value_stdin_could_misread_is_refused_by_every_store(store: DpapiStore) -> None:
    with pytest.raises(ValueError, match="refusing to store") as caught:
        store.write(keychain.MASTER_KEY, 'a "quoted" value')
    assert "quoted" not in str(caught.value)


def test_the_bot_scope_holds_above_the_dpapi_store(store: DpapiStore) -> None:
    store.write(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    chain = Keychain(BotName.PERSONAL, store=store)
    assert chain.read(keychain.PERSONAL_BOT_TOKEN) == PERSONAL_BOT_TOKEN
    with pytest.raises(KeychainItemForbidden):
        chain.read(keychain.DEV_BOT_TOKEN)
    with pytest.raises(KeychainItemForbidden):
        chain.write(keychain.DEV_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    assert not store.file(keychain.DEV_BOT_TOKEN).exists()


def test_the_default_store_is_dpapi_on_windows_and_the_keychain_elsewhere(tmp_path: Path) -> None:
    paths = Paths.windows_defaults(tmp_path)
    windows = keychain.default_store(paths, platform="win32")
    assert isinstance(windows, DpapiStore)
    assert windows.file(keychain.MASTER_KEY).parent == paths.config / "secrets" / "uz.yordamchi"
    assert isinstance(keychain.default_store(paths, platform="darwin"), SecurityStore)


# --- paths on Windows ----------------------------------------------------------------------------------------------
def test_windows_paths_default_to_local_and_roaming_app_data(tmp_path: Path) -> None:
    home = tmp_path / "user"
    environ = {"LOCALAPPDATA": str(tmp_path / "Local"), "APPDATA": str(tmp_path / "Roaming")}
    paths = config.load_paths(environ, home=home, platform="win32")
    assert paths.home == home
    assert paths.data == tmp_path / "Local" / "Yordamchi"
    assert paths.logs == tmp_path / "Local" / "Yordamchi" / "logs"
    assert paths.config == tmp_path / "Roaming" / "Yordamchi"
    assert paths.secret_store == tmp_path / "Roaming" / "Yordamchi" / "secrets"
    assert paths.local_bin == home / ".local" / "bin"  # the native claude.exe installer's folder
    assert paths.env_file(BotName.PERSONAL) == paths.data / "personal.env"


def test_windows_paths_without_the_variables_fall_back_to_app_data_under_home(tmp_path: Path) -> None:
    paths = config.load_paths({}, home=tmp_path, platform="win32")
    assert paths.data == tmp_path / "AppData" / "Local" / "Yordamchi"
    assert paths.config == tmp_path / "AppData" / "Roaming" / "Yordamchi"


def test_windows_paths_keep_the_yordamchi_overrides(tmp_path: Path) -> None:
    environ = {
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "YORDAMCHI_DATA_DIR": str(tmp_path / "data"),
        "YORDAMCHI_CONFIG_DIR": str(tmp_path / "config"),
        "YORDAMCHI_LOG_DIR": str(tmp_path / "logs"),
    }
    paths = config.load_paths(environ, home=tmp_path, platform="win32")
    assert (paths.data, paths.config, paths.logs) == (tmp_path / "data", tmp_path / "config", tmp_path / "logs")
    only_data = config.load_paths({"YORDAMCHI_DATA_DIR": str(tmp_path / "data")}, home=tmp_path, platform="win32")
    assert only_data.logs == tmp_path / "data" / "logs"


def test_the_macos_defaults_ignore_the_windows_variables(tmp_path: Path) -> None:
    paths = config.load_paths({"LOCALAPPDATA": str(tmp_path / "Local")}, home=tmp_path, platform="darwin")
    assert paths == Paths.defaults(tmp_path)


def test_the_personal_config_resolves_its_token_through_dpapi_on_windows(tmp_path: Path) -> None:
    environ = {"LOCALAPPDATA": str(tmp_path / "Local"), "APPDATA": str(tmp_path / "Roaming")}
    paths = config.load_paths(environ, home=tmp_path, platform="win32")
    store = DpapiStore(paths.secret_store, lib=FakeDpapi().lib())
    store.write(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    env_file = paths.env_file(BotName.PERSONAL)
    env_file.parent.mkdir(parents=True)
    env_file.write_text(f"PERSONAL_BOT_TOKEN={keychain.PERSONAL_BOT_TOKEN.reference}\nOWNER_ID={OWNER_ID}\n")
    loaded = config.load_personal_config(environ, home=tmp_path, keychain_store=store, platform="win32")
    assert loaded.bot_token == PERSONAL_BOT_TOKEN
    assert loaded.paths == paths


# --- core.proc on Windows ------------------------------------------------------------------------------------------
def test_the_windows_search_path_is_semicolon_joined_without_posix_dirs(tmp_path: Path) -> None:
    paths = Paths.windows_defaults(tmp_path)
    login = "C:\\Windows\\system32;C:\\Program Files\\Git\\cmd\\;c:\\windows\\System32;;C:\\Tools"
    joined = proc.build_search_path(paths, login, platform="win32")
    entries = joined.split(";")
    assert entries[:3] == [str(paths.shims), str(paths.local_bin), str(paths.bin)]
    assert entries[3:] == ["C:\\Windows\\system32", "C:\\Program Files\\Git\\cmd", "C:\\Tools"]
    assert not set(proc.SYSTEM_PATH) & set(entries)
    assert proc.path_separator("win32") == ";"
    assert proc.path_separator("darwin") == ":"


@pytest.mark.skipif(sys.platform == "win32", reason="checks the macOS default of the platform parameter")
def test_the_macos_search_path_is_unchanged_by_the_platform_parameter(tmp_path: Path) -> None:
    paths = Paths.defaults(tmp_path)
    login = "/opt/homebrew/bin:/usr/bin"
    assert proc.build_search_path(paths, login, platform="darwin") == proc.build_search_path(paths, login)


class _AsyncChild:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.killed = 0

    def kill(self) -> None:
        self.killed += 1
        self.returncode = 1

    async def wait(self) -> int:
        return self.returncode or 0


async def test_a_windows_timeout_kills_the_child_without_a_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_groups(pid: int, signum: int) -> None:
        raise AssertionError((pid, signum))

    monkeypatch.setattr(os, "killpg", no_groups, raising=False)  # absent on Windows itself
    child = _AsyncChild()
    await proc._kill_group(child, 0.01, platform="win32")  # type: ignore[arg-type]
    assert child.killed == 1
    await proc._kill_group(child, 0.01, platform="win32")  # type: ignore[arg-type]
    assert child.killed == 1  # already gone: nothing to do


def test_a_windows_sync_timeout_kills_the_child_without_a_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_groups(pid: int, signum: int) -> None:
        raise AssertionError((pid, signum))

    class Child:
        pid = 4242
        returncode: int | None = None
        waited = 0

        def kill(self) -> None:
            self.returncode = 1

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            self.waited += 1
            return self.returncode or 0

    monkeypatch.setattr(os, "killpg", no_groups, raising=False)  # absent on Windows itself
    child = Child()
    proc._kill_group_sync(child, 0.01, platform="win32")  # type: ignore[arg-type]
    assert (child.returncode, child.waited) == (1, 1)


async def test_a_windows_kill_tolerates_a_child_that_already_exited() -> None:
    class Gone(_AsyncChild):
        def kill(self) -> None:
            self.returncode = 0
            raise ProcessLookupError

    child = Gone()
    await asyncio.wait_for(proc._kill_group(child, 0.01, platform="win32"), 1)  # type: ignore[arg-type]
    assert child.returncode == 0
