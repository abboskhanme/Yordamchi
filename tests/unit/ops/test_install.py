"""`scripts/install.py` against a temporary HOME with a fake uv, a fake `git archive`, a fake ffmpeg download and a
fake launchctl (SPEC §9.3; T28). Nothing here touches the real HOME, the network or launchd.

Golden plists live in `tests/golden/launchd/`. After a reviewed template change, regenerate them with
`uv run python -c "from tests.unit.ops.test_install import write_goldens; write_goldens()"`.
"""

import base64
import hashlib
import io
import plistlib
import sqlite3
import stat
import tarfile
import zipfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from scripts import install
from scripts.install import Completed, Installer, Options, Pin, Status, Tools

from tests.factories import DEV_BOT_TOKEN, DEV_GROUP_ID, OWNER_ID, PERSONAL_BOT_TOKEN
from yordamchi.core import config as yd_config
from yordamchi.core.errors import ConfigInvalid
from yordamchi.core.paths import BotName, Paths

ROOT = Path(__file__).resolve().parents[3]
GOLDEN = ROOT / "tests" / "golden" / "launchd"
SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = "2026.10.24-0123456"
NEW_DEV_TOKEN = "111222333:AAnewDevTokenAfterRotation_01234567"
FFMPEG_NAMES = ("ffmpeg", "ffprobe")


# --- fakes -----------------------------------------------------------------------------------------------------------
def _tar(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def app_archive() -> bytes:
    templates = {
        f"ops/launchd/{path.name}": path.read_bytes() for path in sorted((ROOT / "ops" / "launchd").glob("*.tmpl"))
    }
    return _tar(
        {
            "pyproject.toml": b"[project]\nname = 'yordamchi'\n",
            "alembic.ini": b"[alembic]\n",
            "src/yordamchi/__init__.py": b"",
            **templates,
        }
    )


def ffmpeg_zip(name: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, f"#!/bin/sh\necho {name}\n")
    return buffer.getvalue()


def good_pins() -> dict[str, Pin | None]:
    return {
        name: Pin(f"https://ffmpeg.example/{name}.zip", hashlib.sha256(ffmpeg_zip(name)).hexdigest())
        for name in FFMPEG_NAMES
    }


@dataclass
class FakeMac:
    """The commands the installer runs, answered in-process; launchd state is a set of loaded labels."""

    uv_version: str = install.UV_VERSION
    python_installed: bool = True
    loaded: set[str] = field(default_factory=set)
    fail: set[str] = field(default_factory=set)  # "uv sync", "alembic", "bootstrap", …
    calls: list[tuple[str, ...]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)

    def runner(self, argv: Sequence[str], *, cwd: Path | None = None, timeout: float) -> Completed:
        del timeout
        call = tuple(argv)
        self.calls.append(call)
        tool = Path(argv[0]).name
        failing = [key for key in self.fail if key in " ".join(call)]
        if failing:
            return Completed(1, b"", f"fake failure: {failing[0]}".encode())
        handlers = {
            "uv": lambda: self._uv(call, cwd),
            "git": lambda: self._git(call),
            "launchctl": lambda: self._launchctl(call),
            "python": lambda: self._python(call),
            "ffmpeg": lambda: Completed(0, b" A....D libopus  libopus Opus\n"),
            "tmutil": lambda: Completed(0),
        }
        handler = handlers.get(tool)
        return handler() if handler else Completed(127, b"", b"unknown tool")

    def _python(self, call: tuple[str, ...]) -> Completed:
        if "alembic" in call:  # `alembic upgrade head` creates the DB file, as the real one does
            db = Path(next(arg for arg in call if arg.startswith("db=")).removeprefix("db="))
            sqlite3.connect(db).close()
            return Completed(0)
        return Completed(0, b"Yordamchi tekshiruvi\n")

    def _uv(self, call: tuple[str, ...], cwd: Path | None) -> Completed:
        if call[1] == "--version":
            return Completed(0, f"uv {self.uv_version} (fake)\n".encode())
        if call[1:3] == ("python", "find"):
            return Completed(0 if self.python_installed else 2)
        if call[1] == "sync":
            assert cwd is not None
            bin_dir = cwd / ".venv" / "bin"
            bin_dir.mkdir(parents=True)
            (bin_dir / "python").write_text("#!/bin/sh\n")
            (bin_dir / "python").chmod(0o755)
            site = cwd / ".venv" / "lib" / "python3.12" / "site-packages"
            site.mkdir(parents=True)
            (site / "yordamchi.pth").write_text("")
            return Completed(0)
        return Completed(0)

    def _git(self, call: tuple[str, ...]) -> Completed:
        verb = call[3]
        if verb == "rev-parse":
            return Completed(0, f"{SHA}\n".encode())
        if verb == "log":
            return Completed(0, b"2026.10.24\n")
        if verb == "archive":
            treeish = call[-1]
            if treeish.endswith(":supervisor"):
                return Completed(0, _tar({"pyproject.toml": b"", "src/yordamchi_supervisor/__init__.py": b""}))
            return Completed(0, app_archive())
        return Completed(1)

    def _launchctl(self, call: tuple[str, ...]) -> Completed:
        verb, target = call[1], call[2]
        label = Path(call[3]).stem if verb == "bootstrap" else target.rsplit("/", 1)[-1]
        if verb == "print":
            return Completed(0 if label in self.loaded else 113)
        if verb == "bootstrap":
            self.loaded.add(label)
        elif verb == "bootout":
            self.loaded.discard(label)
        return Completed(0)

    def download(self, url: str, dest: Path, *, timeout: float) -> None:
        del timeout
        self.downloads.append(url)
        dest.write_bytes(ffmpeg_zip(Path(url).stem))

    def launchctl_calls(self) -> list[tuple[str, ...]]:
        return [call[1:] for call in self.calls if Path(call[0]).name == "launchctl"]


PROMPT_KEYS = {
    "DEV_BOT_TOKEN": "Dev bot token",
    "PERSONAL_BOT_TOKEN": "Personal bot token",
    "OWNER_ID": "Telegram user id",
    "DEV_GROUP_ID": "Dev group id",
    "PERSONAL_GROUP_ID": "Personal group id",
}


@dataclass
class KeyedPrompter:
    """Answers by env key, recognised from the installer's prompt text."""

    answers: dict[str, list[str]] = field(default_factory=dict)
    interactive: bool = True
    asked: list[str] = field(default_factory=list)

    def _answer(self, prompt: str) -> str:
        key = next(key for key, text in PROMPT_KEYS.items() if text in prompt)
        self.asked.append(key)
        queue = self.answers.get(key, [])
        return queue.pop(0) if queue else ""

    def secret(self, prompt: str) -> str:
        assert "token" in prompt.lower(), f"only tokens are read hidden: {prompt}"
        return self._answer(prompt)

    def text(self, prompt: str) -> str:
        assert "token" not in prompt.lower(), f"a token must be read hidden: {prompt}"
        return self._answer(prompt)


@dataclass
class Rig:
    home: Path
    mac: FakeMac
    prompter: KeyedPrompter
    out: io.StringIO
    environ: dict[str, str]

    @property
    def paths(self) -> Paths:
        return Paths.defaults(self.home)

    def tools(self) -> Tools:
        local = self.home / "tools" / ".local" / "bin"
        return Tools(
            uv=local / "uv",
            git=local / "git",
            claude=local / "claude",
            gh=local / "gh",
            node=local / "node",
            launchctl=local / "launchctl",
            tmutil=local / "tmutil",
        )

    def installer(
        self,
        *,
        pins: dict[str, Pin | None] | None = None,
        commit: str = "HEAD",
        dry_run: bool = False,
        rotate_tokens: bool = False,
        env_only: bool = False,
        enable_personal: bool = False,
        skip_ffmpeg: bool = False,
    ) -> Installer:
        options = Options(
            home=self.home,
            repo=self.home / "repo",
            uid=501,
            commit=commit,
            dry_run=dry_run,
            rotate_tokens=rotate_tokens,
            env_only=env_only,
            enable_personal=enable_personal,
            skip_ffmpeg=skip_ffmpeg,
        )
        return Installer(
            options,
            tools=self.tools(),
            runner=self.mac.runner,
            download=self.mac.download,
            prompter=self.prompter,
            environ=self.environ,
            out=self.out,
            pins=good_pins() if pins is None else pins,
            machine="darwin/arm64",
            stamp=lambda: "20261024T040000",
        )

    def install(
        self,
        *,
        commit: str = "HEAD",
        dry_run: bool = False,
        rotate_tokens: bool = False,
        enable_personal: bool = False,
    ) -> int:
        installer = self.installer(
            commit=commit, dry_run=dry_run, rotate_tokens=rotate_tokens, enable_personal=enable_personal
        )
        return installer.install()

    def steps(self, installer: Installer) -> dict[str, Status]:
        return {step.name: step.status for step in installer.steps}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    home = tmp_path / "home"
    local = home / "tools" / ".local" / "bin"
    local.mkdir(parents=True)
    for name in ("uv", "git", "claude", "gh", "node", "launchctl", "tmutil"):
        (local / name).write_text("#!/bin/sh\n")
        (local / name).chmod(0o755)
    return Rig(home=home, mac=FakeMac(), prompter=KeyedPrompter(), out=io.StringIO(), environ={})


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def write_env(path: Path, text: str, *, file_mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(file_mode)


def writable_paths(root: Path) -> Iterator[Path]:
    for path in [root, *root.rglob("*")]:
        if not path.is_symlink() and path.stat().st_mode & 0o222:
            yield path


# --- dry run ---------------------------------------------------------------------------------------------------------
def test_dry_run_builds_the_tree_with_private_modes_and_never_calls_launchctl(rig: Rig) -> None:
    installer = rig.installer(dry_run=True)
    assert installer.install() == 0, rig.out.getvalue()
    paths = rig.paths
    for directory in install.data_dirs(paths):
        assert mode(directory) == 0o700, directory
    assert mode(paths.master_key) == 0o600
    assert mode(paths.env_file(BotName.DEV)) == 0o600
    called = {Path(call[0]).name for call in rig.mac.calls}
    assert "launchctl" not in called
    assert "tmutil" not in called
    assert rig.mac.downloads == []
    assert not any(call[1:3] == ("self", "update") or call[1:3] == ("python", "install") for call in rig.mac.calls)
    steps = rig.steps(installer)
    assert steps["launchd dev"] is Status.DRY_RUN
    assert steps["time machine exclusion"] is Status.DRY_RUN
    assert steps["check"] is Status.DRY_RUN
    assert rig.prompter.asked == []  # a dry run never asks for a token


def test_dry_run_sync_is_offline_and_never_downloads_python(rig: Rig) -> None:
    rig.mac.uv_version = "0.12.9"
    rig.mac.python_installed = False
    installer = rig.installer(dry_run=True)
    installer.install()
    syncs = [call for call in rig.mac.calls if call[1:2] == ("sync",)]
    assert len(syncs) == 2
    assert all("--offline" in call and "--no-python-downloads" in call and "--frozen" in call for call in syncs)
    assert all(call[call.index("--link-mode") + 1] == "clone" for call in syncs)
    assert rig.steps(installer)["uv 0.12.9 -> 0.13.5"] is Status.DRY_RUN
    assert rig.steps(installer)["python"] is Status.DRY_RUN


def test_dry_run_keeps_every_existing_env_value(rig: Rig) -> None:
    paths = rig.paths
    dev_text = f"# owner notes\nDEV_BOT_TOKEN={DEV_BOT_TOKEN}\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID=\nEXTRA=1\n"
    personal_text = f"PERSONAL_BOT_TOKEN={PERSONAL_BOT_TOKEN}\n"
    write_env(paths.env_file(BotName.DEV), dev_text, file_mode=0o644)
    write_env(paths.env_file(BotName.PERSONAL), personal_text)
    rig.install(dry_run=True, rotate_tokens=True)
    assert paths.env_file(BotName.DEV).read_text() == dev_text
    assert mode(paths.env_file(BotName.DEV)) == 0o600  # the mode is fixed, the content is not touched
    personal = yd_config.parse_env_file(paths.env_file(BotName.PERSONAL).read_text())
    assert personal["PERSONAL_BOT_TOKEN"] == PERSONAL_BOT_TOKEN
    assert personal["OWNER_ID"] == str(OWNER_ID)  # an empty value is filled from the other file, never replaced
    for secret in (DEV_BOT_TOKEN, PERSONAL_BOT_TOKEN):
        assert secret not in rig.out.getvalue()


def test_dry_run_release_is_read_only(rig: Rig) -> None:
    assert rig.install(dry_run=True) == 0
    release = rig.paths.releases / VERSION
    assert (release / ".venv" / "bin" / "python").exists()
    assert list(writable_paths(release)) == []
    assert list(writable_paths(rig.paths.supervisor / "releases" / VERSION)) == []
    assert rig.paths.current_release.resolve() == release.resolve()
    assert (rig.paths.supervisor / "current").resolve() == (rig.paths.supervisor / "releases" / VERSION).resolve()
    assert (rig.paths.supervisor / "current").readlink() == Path("releases") / VERSION
    assert rig.paths.current_release.readlink() == Path(VERSION)


def test_the_release_comes_from_git_archive_of_the_commit(rig: Rig) -> None:
    rig.install(dry_run=True, commit="v1")
    git = [call[3:] for call in rig.mac.calls if Path(call[0]).name == "git"]
    assert ("rev-parse", "--verify", "--quiet", "v1^{commit}") in git
    assert ("archive", "--format=tar", SHA) in git
    assert ("archive", "--format=tar", f"{SHA}:supervisor") in git


def test_dry_run_reports_what_it_would_download(rig: Rig) -> None:
    installer = rig.installer(dry_run=True)
    installer.install()
    assert rig.steps(installer)["ffmpeg: ffmpeg"] is Status.DRY_RUN
    assert "would download https://ffmpeg.example/ffmpeg.zip" in rig.out.getvalue()


def test_the_cli_refuses_a_dry_run_into_the_real_home() -> None:
    assert install.main(["--dry-run", "--home", str(Path.home())]) == 2


def test_the_cli_refuses_home_without_dry_run(tmp_path: Path) -> None:
    assert install.main(["--home", str(tmp_path)]) == 2


# --- ffmpeg pins -----------------------------------------------------------------------------------------------------
def test_unpinned_ffmpeg_stops_a_real_install_before_anything_changes(rig: Rig) -> None:
    installer = rig.installer(pins=dict(install.FFMPEG_PINS))
    assert installer.install() == 1
    assert "not pinned in docs/STACK.md §5" in rig.out.getvalue()
    assert not rig.paths.data.exists()
    assert not rig.paths.master_key.exists()


def test_the_shipped_pins_are_empty_until_stack_records_them() -> None:
    # STACK §5 has no SHA-256 for the 9.0.2 zips; the installer must not carry a guessed one.
    assert all(pin is None for pin in install.FFMPEG_PINS.values())


def test_a_dry_run_names_the_missing_pins_and_shows_the_rest(rig: Rig) -> None:
    installer = rig.installer(dry_run=True, pins=dict(install.FFMPEG_PINS))
    assert installer.install() == 1
    steps = rig.steps(installer)
    assert steps["preflight: ffmpeg pins"] is Status.FAILED
    assert steps["ffmpeg"] is Status.BLOCKED
    assert steps["app release"] is Status.DONE


def test_skip_ffmpeg_installs_without_it(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    installer = rig.installer(pins=dict(install.FFMPEG_PINS), skip_ffmpeg=True)
    assert installer.install() == 0, rig.out.getvalue()
    assert rig.steps(installer)["ffmpeg"] is Status.WARN


def test_a_checksum_mismatch_installs_nothing(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    pins = good_pins()
    pins["ffmpeg"] = Pin("https://ffmpeg.example/ffmpeg.zip", "0" * 64)
    assert rig.installer(pins=pins).install() == 1
    assert "SHA-256 mismatch" in rig.out.getvalue()
    assert not (rig.paths.bin / "ffmpeg").exists()
    assert list(rig.paths.bin.iterdir()) == []
    assert not rig.paths.current_release.exists()  # stopped before switching anything


def test_ffmpeg_is_verified_unpacked_and_checked_for_libopus(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    assert rig.install() == 0, rig.out.getvalue()
    for name in ("ffmpeg", "ffprobe"):
        assert mode(rig.paths.bin / name) == 0o700
        assert (rig.paths.bin / name).read_text().startswith("#!/bin/sh")
    assert (rig.paths.bin / "ffmpeg", "-hide_banner", "-encoders") in [
        (Path(call[0]), *call[1:]) for call in rig.mac.calls
    ]
    rig.mac.downloads.clear()
    rig.install()
    assert rig.mac.downloads == []  # pinned and present: not downloaded again


# --- real mode with fakes --------------------------------------------------------------------------------------------
def test_a_real_install_asks_only_for_missing_values_and_loads_dev_and_supervisor(rig: Rig) -> None:
    write_env(rig.paths.env_file(BotName.DEV), f"DEV_BOT_TOKEN={DEV_BOT_TOKEN}\nOWNER_ID=\n")
    rig.prompter.answers = {"OWNER_ID": [str(OWNER_ID)], "DEV_GROUP_ID": ["-12345", str(DEV_GROUP_ID)]}
    installer = rig.installer()
    assert installer.install() == 0, rig.out.getvalue()
    assert rig.prompter.asked == ["OWNER_ID", "DEV_GROUP_ID", "DEV_GROUP_ID"]  # the token was there: never asked
    values = yd_config.parse_env_file(rig.paths.env_file(BotName.DEV).read_text())
    assert values == {"DEV_BOT_TOKEN": DEV_BOT_TOKEN, "OWNER_ID": str(OWNER_ID), "DEV_GROUP_ID": str(DEV_GROUP_ID)}
    assert "not a -100… supergroup id" in rig.out.getvalue()
    assert ("bootstrap", "gui/501", str(rig.paths.home / "Library/LaunchAgents/uz.yordamchi.dev.plist")) in (
        rig.mac.launchctl_calls()
    )
    assert rig.mac.loaded == {"uz.yordamchi.dev", "uz.yordamchi.supervisor"}
    assert ("tmutil", "addexclusion", str(rig.paths.config)) in [(Path(c[0]).name, *c[1:]) for c in rig.mac.calls]
    assert DEV_BOT_TOKEN not in rig.out.getvalue()


def test_a_typed_token_is_hidden_validated_and_never_printed(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": ["not-a-token", DEV_BOT_TOKEN]}
    assert rig.install() == 0, rig.out.getvalue()
    assert rig.prompter.asked[:2] == ["DEV_BOT_TOKEN", "DEV_BOT_TOKEN"]
    assert yd_config.parse_env_file(rig.paths.env_file(BotName.DEV).read_text())["DEV_BOT_TOKEN"] == DEV_BOT_TOKEN
    assert DEV_BOT_TOKEN not in rig.out.getvalue()


def test_a_real_install_without_a_dev_token_stops_before_the_release(rig: Rig) -> None:
    rig.prompter.interactive = False
    installer = rig.installer()
    assert installer.install() == 1
    assert "Required and still empty: ['DEV_BOT_TOKEN']" in rig.out.getvalue()
    assert not rig.paths.releases.joinpath(VERSION).exists()


def test_an_existing_invalid_value_stops_the_install_and_is_kept(rig: Rig) -> None:
    text = f"DEV_BOT_TOKEN={DEV_BOT_TOKEN}\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID=-12345\n"
    write_env(rig.paths.env_file(BotName.DEV), text)
    assert rig.install() == 1
    assert rig.paths.env_file(BotName.DEV).read_text() == text
    assert "DEV_GROUP_ID (not a -100… supergroup id" in rig.out.getvalue()
    assert rig.mac.launchctl_calls() == []


def test_rotate_tokens_replaces_only_the_tokens_and_restarts_loaded_bots(rig: Rig) -> None:
    text = f"# keep\nDEV_BOT_TOKEN={DEV_BOT_TOKEN}\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID={DEV_GROUP_ID}\n"
    write_env(rig.paths.env_file(BotName.DEV), text)
    rig.mac.loaded = {"uz.yordamchi.dev"}
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN, NEW_DEV_TOKEN]}  # the old one is refused first
    installer = rig.installer(env_only=True, rotate_tokens=True)
    assert installer.install() == 0, rig.out.getvalue()
    assert rig.paths.env_file(BotName.DEV).read_text() == text.replace(DEV_BOT_TOKEN, NEW_DEV_TOKEN)
    assert "that is the old token" in rig.out.getvalue()
    assert ("kickstart", "-k", "gui/501/uz.yordamchi.dev") in rig.mac.launchctl_calls()
    assert rig.steps(installer)["restart personal"] is Status.UNCHANGED
    for secret in (DEV_BOT_TOKEN, NEW_DEV_TOKEN):
        assert secret not in rig.out.getvalue()


def test_master_key_is_created_once_and_never_overwritten(rig: Rig) -> None:
    rig.install(dry_run=True)
    key = rig.paths.master_key.read_bytes()
    assert key.endswith(b"\n")
    assert len(base64.b64decode(key.strip(), validate=True)) == 32
    assert mode(rig.paths.config) == 0o700
    rig.install(dry_run=True)
    assert rig.paths.master_key.read_bytes() == key


def test_a_damaged_master_key_stops_the_install_and_is_kept(rig: Rig) -> None:
    rig.paths.config.mkdir(parents=True)
    rig.paths.master_key.write_text("garbage\n")
    assert rig.install() == 1
    assert rig.paths.master_key.read_text() == "garbage\n"


def test_a_refused_credential_source_stops_the_install(rig: Rig) -> None:
    rig.environ["ANTHROPIC_BASE_URL"] = "https://proxy.invalid"
    assert rig.install() == 1
    assert "env:ANTHROPIC_BASE_URL" in rig.out.getvalue()
    assert not rig.paths.data.exists()


def test_a_rerun_reuses_the_release_and_restarts_instead_of_reloading(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    assert rig.install() == 0
    rig.mac.calls.clear()
    installer = rig.installer()
    assert installer.install() == 0, rig.out.getvalue()
    steps = rig.steps(installer)
    assert steps["app release"] is Status.UNCHANGED
    assert steps["master.key"] is Status.UNCHANGED
    launchctl = rig.mac.launchctl_calls()
    assert ("bootout", "gui/501/uz.yordamchi.dev") in launchctl  # stopped for the migration
    assert ("kickstart", "-k", "gui/501/uz.yordamchi.supervisor") in launchctl  # unchanged plist, loaded
    assert not any(call[1:2] == ("sync",) for call in rig.mac.calls)  # the built release is reused as it is


def test_existing_databases_are_backed_up_before_migrating(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    db = rig.paths.db_file(BotName.DEV)
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE kv_state (k TEXT)")
        conn.execute("INSERT INTO kv_state VALUES ('kept')")
    conn.close()
    installer = rig.installer()
    assert installer.install() == 0, rig.out.getvalue()
    copy = rig.paths.backups / "pre-update" / f"install-{VERSION}-20261024T040000" / "dev.db"
    assert mode(copy) == 0o600
    with sqlite3.connect(copy) as conn:
        assert conn.execute("SELECT k FROM kv_state").fetchall() == [("kept",)]
    conn.close()
    assert mode(db) == 0o600
    order = [step.name for step in installer.steps]
    assert order.index("db backup") < order.index("migrations") < order.index("switch current")
    assert installer.steps[order.index("db backup")].status is Status.DONE


def test_a_failed_migration_leaves_current_where_it_was(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    rig.mac.fail = {"alembic"}
    assert rig.install() == 1
    assert not rig.paths.current_release.exists()
    assert rig.mac.loaded == set()


def test_a_failed_migration_on_a_reinstall_brings_the_stopped_bot_back(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    assert rig.install() == 0
    before = rig.paths.current_release.readlink()
    rig.mac.fail = {"alembic"}
    installer = rig.installer()
    assert installer.install() == 1
    assert rig.paths.current_release.readlink() == before
    assert "uz.yordamchi.dev" in rig.mac.loaded  # booted out for the migration, bootstrapped again on the old release
    assert rig.steps(installer)["restart dev"] is Status.DONE


def test_a_second_release_prints_the_rollback_target(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    rig.install()
    old = rig.paths.releases / VERSION
    other = "fedcba9876543210fedcba9876543210fedcba98"

    def runner(argv: Sequence[str], *, cwd: Path | None = None, timeout: float) -> Completed:
        result = rig.mac.runner(argv, cwd=cwd, timeout=timeout)
        if Path(argv[0]).name == "git" and argv[3] == "rev-parse":
            return Completed(0, f"{other}\n".encode())
        return result

    installer = rig.installer()
    installer.run_cmd = runner
    assert installer.install() == 0, rig.out.getvalue()
    assert rig.paths.current_release.readlink() == Path("2026.10.24-fedcba9")
    assert old.exists()
    assert "point releases/current back at 2026.10.24-0123456" in rig.out.getvalue()


# --- plists ----------------------------------------------------------------------------------------------------------
def render_all(home: Path) -> dict[str, str]:
    values = install.plist_values(Paths.defaults(home))
    return {
        path.name.removesuffix(".tmpl"): install.render_plist(path.read_text(), values)
        for path in sorted((ROOT / "ops" / "launchd").glob("*.plist.tmpl"))
    }


def write_goldens() -> None:
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name, text in render_all(Path("/Users/owner")).items():
        (GOLDEN / name).write_text(text)


def test_rendered_plists_match_the_golden_files() -> None:
    rendered = render_all(Path("/Users/owner"))
    assert sorted(rendered) == [
        "uz.yordamchi.dev.plist",
        "uz.yordamchi.personal.plist",
        "uz.yordamchi.supervisor.plist",
    ]
    for name, text in rendered.items():
        assert text == (GOLDEN / name).read_text(), f"{name} differs from tests/golden/launchd (see module docstring)"


@pytest.mark.parametrize(("bot", "exit_timeout"), [("dev", 20), ("personal", 20)])
def test_bot_plists_follow_spec_9_1(bot: str, exit_timeout: int) -> None:
    data = "/Users/owner/Library/Application Support/Yordamchi"
    plist = plistlib.loads(render_all(Path("/Users/owner"))[f"uz.yordamchi.{bot}.plist"].encode())
    assert plist["Label"] == f"uz.yordamchi.{bot}"
    assert plist["ProgramArguments"] == [
        f"{data}/releases/current/.venv/bin/python",
        "-I",
        "-m",
        f"yordamchi.bots.{bot}.app",
    ]
    env = plist["EnvironmentVariables"]
    assert env["YORDAMCHI_DATA_DIR"] == data
    assert "YORDAMCHI_DATA" not in env
    assert env["YORDAMCHI_ENV_FILE"] == f"{data}/{bot}.env"
    assert env["PATH"].split(":")[0] == f"{data}/releases/current/shims"
    assert env["PATH"].split(":")[1] == "/Users/owner/.local/bin"
    assert (env["LANG"], env["PYTHONUNBUFFERED"]) == ("en_US.UTF-8", "1")
    assert plist["ProcessType"] == "Interactive"
    assert (plist["RunAtLoad"], plist["KeepAlive"], plist["AbandonProcessGroup"]) == (True, True, False)
    assert (plist["ThrottleInterval"], plist["ExitTimeOut"]) == (10, exit_timeout)
    assert plist["WorkingDirectory"] == f"{data}/run"
    assert plist["SoftResourceLimits"] == {"NumberOfFiles": 8192}
    log = f"/Users/owner/Library/Logs/Yordamchi/uz.yordamchi.{bot}.launchd.log"
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == log


def test_the_supervisor_plist_runs_isolated_without_shims() -> None:
    data = "/Users/owner/Library/Application Support/Yordamchi"
    plist = plistlib.loads(render_all(Path("/Users/owner"))["uz.yordamchi.supervisor.plist"].encode())
    assert plist["ProgramArguments"] == [
        f"{data}/supervisor/current/.venv/bin/python",
        "-I",
        "-m",
        "yordamchi_supervisor",
    ]
    env = plist["EnvironmentVariables"]
    assert env["YORDAMCHI_DATA_DIR"] == data
    assert "YORDAMCHI_DATA" not in env
    assert "shims" not in env["PATH"]
    assert env["PATH"] == "/Users/owner/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    assert (plist["ExitTimeOut"], plist["ThrottleInterval"], plist["AbandonProcessGroup"]) == (10, 10, False)
    assert plist["KeepAlive"] is True


def test_values_are_xml_escaped_and_placeholders_must_all_be_filled() -> None:
    rendered = install.render_plist("<plist><string>{{DATA}}</string></plist>", {"DATA": "/a&b"})
    assert "/a&amp;b" in rendered
    with pytest.raises(ValueError, match="needs"):
        install.render_plist("<plist>{{LOGS}}</plist>", {})


def test_the_personal_plist_is_rendered_but_not_installed_before_p3(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN]}
    assert rig.install() == 0
    staged = install.staging_dir(rig.paths) / "uz.yordamchi.personal.plist"
    assert staged.exists()
    assert mode(staged) == 0o600
    agents = rig.paths.home / "Library" / "LaunchAgents"
    assert sorted(p.name for p in agents.iterdir()) == ["uz.yordamchi.dev.plist", "uz.yordamchi.supervisor.plist"]
    bootstrapped = [Path(call[2]).name for call in rig.mac.launchctl_calls() if call[0] == "bootstrap"]
    assert bootstrapped == ["uz.yordamchi.dev.plist", "uz.yordamchi.supervisor.plist"]
    assert plistlib.loads((agents / "uz.yordamchi.dev.plist").read_bytes())["Label"] == "uz.yordamchi.dev"


def test_enable_personal_installs_and_loads_it(rig: Rig) -> None:
    rig.prompter.answers = {"DEV_BOT_TOKEN": [DEV_BOT_TOKEN], "PERSONAL_BOT_TOKEN": [PERSONAL_BOT_TOKEN]}
    assert rig.install(enable_personal=True) == 0, rig.out.getvalue()
    assert "uz.yordamchi.personal" in rig.mac.loaded


# --- one definition of the grammar -----------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "token",
    [DEV_BOT_TOKEN, PERSONAL_BOT_TOKEN, "123:short", "1234567890:" + "a" * 35, "12345678:" + "b" * 36, ""],
)
def test_the_installer_accepts_exactly_the_tokens_core_config_accepts(token: str) -> None:
    try:
        yd_config.dev_config_from(
            {"DEV_BOT_TOKEN": token, "OWNER_ID": "1", "DEV_GROUP_ID": "-1001"}, Paths.defaults(Path("/x"))
        )
        core_ok = True
    except ConfigInvalid:
        core_ok = False
    assert (install.env_problem("DEV_BOT_TOKEN", token) is None) is core_ok


def test_every_data_dir_is_a_core_paths_location() -> None:
    paths = Paths.defaults(Path("/Users/owner"))
    dirs = set(install.data_dirs(paths))
    expected = {paths.data, paths.db_dir, paths.releases, paths.run, paths.flags, paths.updates, paths.backups}
    expected |= {paths.media, paths.bin, paths.config, paths.logs, paths.session_dir("smoke")}
    assert expected <= dirs
    assert paths.master_key.parent == paths.config
