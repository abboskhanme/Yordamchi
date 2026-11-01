"""`scripts/windows/setup.py` (owner request 2026-10-31: other people run only the personal bot on Windows), proven on
this Mac with `platform="win32"`, a fake DPAPI and temporary `%LOCALAPPDATA%` / `%APPDATA%`.

- A fresh setup: the data tree, a DPAPI-sealed master key and token, `personal.env` with only the token's
  reference, and `alembic upgrade head` on `personal.db` with the exact argv `scripts/install.py` uses.
- A re-run keeps the master key (the vault stays readable), the token and the file; `--token-stdin` replaces the
  token; a plaintext token in the file is moved into the store.
- CI (no terminal): the token from one stdin line, the ids from flags; a missing owner id stops the setup.
- The real alembic, once, on a temp DB.
"""

import io
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from scripts.windows import setup

from tests.factories import OWNER_ID, PERSONAL_BOT_TOKEN
from tests.fakes.dpapi import FakeDpapi
from yordamchi.adapters.keystore.aead import decode_key
from yordamchi.core import config, keychain
from yordamchi.core.errors import KeychainItemMissing
from yordamchi.core.keychain import DpapiStore
from yordamchi.core.paths import BotName, Paths

ROOT = Path(__file__).resolve().parents[3]
NEW_TOKEN = "123456789:" + "B" * 35
GROUP_ID = "-1001234567890"
REFERENCE = keychain.PERSONAL_BOT_TOKEN.reference


@dataclass
class FakeRunner:
    returncode: int = 0
    stderr: bytes = b""
    calls: list[tuple[list[str], Path]] = field(default_factory=list)

    def __call__(self, argv: Sequence[str], *, cwd: Path, timeout: float) -> setup.Completed:
        assert timeout > 0
        self.calls.append((list(argv), cwd))
        return setup.Completed(self.returncode, stderr=self.stderr)


@dataclass
class FakePrompter:
    interactive: bool = False
    secrets: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    asked: list[str] = field(default_factory=list)

    def secret(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.secrets.pop(0) if self.secrets else ""

    def text(self, prompt: str) -> str:
        self.asked.append(prompt)
        return self.texts.pop(0) if self.texts else ""

    def line(self) -> str:
        return self.lines.pop(0) if self.lines else ""


@dataclass
class Machine:
    home: Path
    environ: dict[str, str]
    paths: Paths
    store: DpapiStore
    runner: FakeRunner = field(default_factory=FakeRunner)

    def setup(self, options: setup.Options, prompter: FakePrompter) -> tuple[int, str]:
        out = io.StringIO()
        code = setup.Setup(
            options,
            environ=self.environ,
            out=out,
            runner=self.runner,
            prompter=prompter,
            home=self.home,
            repo=ROOT,
            python=Path("C:/repo/.venv/Scripts/python.exe"),
            store=self.store,
            platform="win32",
        ).run()
        return code, out.getvalue()

    def env_text(self) -> str:
        return self.paths.env_file(BotName.PERSONAL).read_text(encoding="utf-8")


@pytest.fixture
def machine(tmp_path: Path) -> Machine:
    home = tmp_path / "home"
    environ = {"LOCALAPPDATA": str(tmp_path / "Local"), "APPDATA": str(tmp_path / "Roaming")}
    paths = config.load_paths(environ, home=home, platform="win32")
    return Machine(home=home, environ=environ, paths=paths, store=DpapiStore(paths.secret_store, lib=FakeDpapi().lib()))


def _ci(owner: str | None = str(OWNER_ID), group: str | None = None, *, token_stdin: bool = False) -> setup.Options:
    return setup.Options(owner_id=owner, group_id=group, token_stdin=token_stdin)


# --- a fresh machine -----------------------------------------------------------------------------------------------
def test_fresh_setup_seals_the_token_and_master_key_and_writes_only_the_reference(machine: Machine) -> None:
    code, out = machine.setup(_ci(group=GROUP_ID), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    assert code == 0, out
    paths = machine.paths
    assert paths.data == machine.home.parent / "Local" / "Yordamchi"
    assert paths.secret_store.is_relative_to(machine.home.parent / "Roaming" / "Yordamchi")
    for folder in setup.data_dirs(paths):
        assert folder.is_dir(), folder
    text = machine.env_text()
    assert text.startswith(setup.ENV_HEADER)
    assert f"PERSONAL_BOT_TOKEN={REFERENCE}\n" in text
    assert f"OWNER_ID={OWNER_ID}\n" in text
    assert f"PERSONAL_GROUP_ID={GROUP_ID}\n" in text
    assert PERSONAL_BOT_TOKEN not in text
    assert machine.store.read(keychain.PERSONAL_BOT_TOKEN) == PERSONAL_BOT_TOKEN
    assert len(decode_key(machine.store.read(keychain.MASTER_KEY))) == 32
    assert PERSONAL_BOT_TOKEN not in out
    assert "Tayyor" in out
    assert "Gemini" in out
    assert "[done] config: the bot reads it back" in out


def test_migrations_run_alembic_upgrade_head_on_personal_db_from_the_checkout(machine: Machine) -> None:
    code, _ = machine.setup(_ci(), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    assert code == 0
    db = machine.paths.db_file(BotName.PERSONAL)
    assert machine.runner.calls == [
        (
            [
                str(Path("C:/repo/.venv/Scripts/python.exe")),
                "-I",
                "-m",
                "alembic",
                "-c",
                str(ROOT / "alembic.ini"),
                "-x",
                f"db={db}",
                "upgrade",
                "head",
            ],
            ROOT,
        )
    ]


def test_the_bot_reads_the_written_config_back(machine: Machine) -> None:
    machine.setup(_ci(group=GROUP_ID), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    loaded = config.load_personal_config(
        machine.environ, home=machine.home, keychain_store=machine.store, platform="win32"
    )
    assert loaded.bot_token == PERSONAL_BOT_TOKEN
    assert loaded.owner_id == OWNER_ID
    assert loaded.personal_group_id == int(GROUP_ID)


# --- re-runs -------------------------------------------------------------------------------------------------------
def test_a_rerun_keeps_the_master_key_token_and_env_file(machine: Machine) -> None:
    machine.setup(_ci(), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))
    key = machine.store.read(keychain.MASTER_KEY)
    text = machine.env_text()

    code, out = machine.setup(_ci(owner=None), FakePrompter())

    assert code == 0, out
    assert machine.store.read(keychain.MASTER_KEY) == key
    assert machine.store.read(keychain.PERSONAL_BOT_TOKEN) == PERSONAL_BOT_TOKEN
    assert machine.env_text() == text
    assert "[unchanged] master key" in out
    assert "[unchanged] personal.env" in out
    assert len(machine.runner.calls) == 2  # migrations always run: a later pull may bring new ones


def test_token_stdin_replaces_the_stored_token(machine: Machine) -> None:
    machine.setup(_ci(), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    code, out = machine.setup(_ci(token_stdin=True), FakePrompter(lines=[NEW_TOKEN]))

    assert code == 0, out
    assert machine.store.read(keychain.PERSONAL_BOT_TOKEN) == NEW_TOKEN
    assert NEW_TOKEN not in machine.env_text()


def test_a_flag_replaces_the_owner_id_in_the_file(machine: Machine) -> None:
    machine.setup(_ci(), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    code, _ = machine.setup(_ci(owner="777", group=GROUP_ID), FakePrompter())

    assert code == 0
    assert "OWNER_ID=777\n" in machine.env_text()
    assert f"PERSONAL_GROUP_ID={GROUP_ID}\n" in machine.env_text()


def test_a_plaintext_token_in_the_file_moves_into_the_store(machine: Machine) -> None:
    env = machine.paths.env_file(BotName.PERSONAL)
    env.parent.mkdir(parents=True)
    env.write_text(f"PERSONAL_BOT_TOKEN={PERSONAL_BOT_TOKEN}\nOWNER_ID={OWNER_ID}\n", encoding="utf-8")

    code, out = machine.setup(_ci(owner=None), FakePrompter())

    assert code == 0, out
    assert machine.env_text() == f"PERSONAL_BOT_TOKEN={REFERENCE}\nOWNER_ID={OWNER_ID}\n"
    assert machine.store.read(keychain.PERSONAL_BOT_TOKEN) == PERSONAL_BOT_TOKEN


def test_a_malformed_master_key_is_never_overwritten(machine: Machine) -> None:
    machine.store.write(keychain.MASTER_KEY, "not-a-key")

    code, out = machine.setup(_ci(), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    assert code == 1
    assert "[failed] master key" in out
    assert machine.store.read(keychain.MASTER_KEY) == "not-a-key"
    assert not machine.paths.env_file(BotName.PERSONAL).exists()


# --- the terminal and CI -------------------------------------------------------------------------------------------
def test_on_a_terminal_the_token_is_asked_hidden_until_it_is_valid(machine: Machine) -> None:
    prompter = FakePrompter(interactive=True, secrets=["nonsense", PERSONAL_BOT_TOKEN], texts=[str(OWNER_ID), ""])

    code, out = machine.setup(setup.Options(), prompter)

    assert code == 0, out
    assert prompter.asked == [
        setup.PROMPTS["PERSONAL_BOT_TOKEN"],
        setup.PROMPTS["PERSONAL_BOT_TOKEN"],
        setup.PROMPTS["OWNER_ID"],
        setup.PROMPTS["PERSONAL_GROUP_ID"],
    ]
    assert "not a BotFather token" in out
    assert "PERSONAL_GROUP_ID=\n" in machine.env_text()


def test_without_a_terminal_a_missing_owner_id_stops_the_setup(machine: Machine) -> None:
    code, out = machine.setup(_ci(owner=None), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    assert code == 1
    assert "--owner-id" in out
    assert machine.runner.calls == []


def test_without_a_token_the_setup_stops_and_writes_no_file(machine: Machine) -> None:
    code, out = machine.setup(_ci(), FakePrompter())

    assert code == 1
    assert "[failed] PERSONAL_BOT_TOKEN" in out
    assert not machine.paths.env_file(BotName.PERSONAL).exists()
    with pytest.raises(KeychainItemMissing):
        machine.store.read(keychain.PERSONAL_BOT_TOKEN)


def test_an_invalid_token_from_stdin_is_refused(machine: Machine) -> None:
    code, out = machine.setup(_ci(), FakePrompter(lines=["123:short"]))

    assert code == 1
    assert "not a BotFather token" in out


@pytest.mark.parametrize(("flag", "value"), [("owner", "-5"), ("group", "12345")])
def test_invalid_ids_from_flags_are_refused(machine: Machine, flag: str, value: str) -> None:
    options = _ci(owner=value) if flag == "owner" else _ci(group=value)

    code, _ = machine.setup(options, FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    assert code == 1


def test_a_failed_migration_fails_the_setup(machine: Machine) -> None:
    machine.runner.returncode = 1
    machine.runner.stderr = b"Traceback\nalembic.util.exc.CommandError: boom\n"

    code, out = machine.setup(_ci(), FakePrompter(lines=[PERSONAL_BOT_TOKEN]))

    assert code == 1
    assert "[failed] migrations: alembic upgrade head on personal.db failed: alembic.util.exc.CommandError" in out


def test_the_token_grammar_matches_the_bots() -> None:
    assert setup.BOT_TOKEN.pattern == config._BOT_TOKEN.pattern


def test_main_refuses_outside_windows(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert setup.main([]) == 2
    assert "scripts/install.py" in capsys.readouterr().err


# --- the real alembic ----------------------------------------------------------------------------------------------
def test_the_real_alembic_brings_personal_db_to_head(machine: Machine) -> None:
    out = io.StringIO()
    code = setup.Setup(
        _ci(),
        environ=machine.environ,
        out=out,
        prompter=FakePrompter(lines=[PERSONAL_BOT_TOKEN]),
        home=machine.home,
        repo=ROOT,
        python=Path(sys.executable),
        store=machine.store,
        platform="win32",
    ).run()

    assert code == 0, out.getvalue()
    db = sqlite3.connect(machine.paths.db_file(BotName.PERSONAL))
    try:
        assert db.execute("SELECT count(*) FROM alembic_version").fetchone() == (1,)
    finally:
        db.close()
