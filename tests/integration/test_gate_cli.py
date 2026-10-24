"""The shims and the pre-push hook as real subprocesses against a control socket in a temp dir (SPEC §5.7 Layer 2;
UC-13; NFR-SEC-02/11): allow, refuse, no socket → refuse, the secret block naming the file and line, and the real
gate service behind the socket, mounted the way T34 will (`core.control.dispatch`).

The scripts run with an environment built here from nothing, as an executor session's would be (SPEC §5.3): shims
first on PATH, `YORDAMCHI_PY`/`YORDAMCHI_SOCK`/`YORDAMCHI_SESSION_TOKEN`, and `core.hooksPath` through `GIT_CONFIG_*`.
"""

import asyncio
import hashlib
import json
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from tests.factories import FAKE_KEYS, UNKNOWN_FORMAT_KEY, make_gate_scope, projects_root_of
from yordamchi.core import control, proc
from yordamchi.core.control import GateAnswer, GateCheck, GateHandlers, SecretAnswer, SecretBlocked
from yordamchi.core.paths import Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.gate import service
from yordamchi.dev.gate.service import ShimRequest

REPO = Path(__file__).resolve().parents[2]
SHIMS = REPO / "shims"
GITHOOKS = REPO / "githooks"
TOKEN = "tok-session-7"
SYSTEM_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class FakeBot:
    """The dev bot's end of the control socket, with scripted gate answers."""

    def __init__(self, socket: Path) -> None:
        self.socket = socket
        self.checks: list[GateCheck] = []
        self.reports: list[SecretBlocked] = []
        self.gate = GateAnswer(allow=True, reason="free")
        self.secret = SecretAnswer(ok=True, enforce=True)
        self.silent = False  # read the request, close without answering
        self.handlers = GateHandlers(gate_check=self._gate_check, secret_blocked=self._secret_blocked)

    async def _gate_check(self, check: GateCheck) -> GateAnswer:
        self.checks.append(check)
        return self.gate

    async def _secret_blocked(self, report: SecretBlocked) -> SecretAnswer:
        self.reports.append(report)
        return self.secret

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        if not self.silent:
            writer.write(await control.dispatch(line, handlers=self.handlers))
            await writer.drain()
        writer.close()
        await writer.wait_closed()


@pytest.fixture
def run_dir() -> Iterator[Path]:
    """`DATA/run` stand-in. A unix socket path must stay under 104 bytes on macOS; tmp_path can be longer."""
    folder = Path(tempfile.mkdtemp(prefix="yd-", dir="/tmp"))
    yield folder
    shutil.rmtree(folder, ignore_errors=True)


@pytest.fixture
async def bot(run_dir: Path) -> AsyncIterator[FakeBot]:
    fake = FakeBot(run_dir / "dev.sock")
    async with await asyncio.start_unix_server(fake.serve, path=str(fake.socket)):
        yield fake


@pytest.fixture
def real_bin(tmp_path: Path) -> Path:
    """Stand-ins for the real binaries: each records its argv next to itself."""
    folder = tmp_path / "bin"
    folder.mkdir()
    for tool in ("ssh", "scp", "sftp", "rsync"):
        script = folder / tool
        script.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{folder}/{tool}.ran"\n', encoding="utf-8")
        script.chmod(0o755)
    return folder


def session_env(home: Path, *, socket: Path | None, real_bin: Path | None = None) -> dict[str, str]:
    env = {
        "PATH": ":".join(str(item) for item in (SHIMS, real_bin) if item) + ":" + SYSTEM_PATH,
        "HOME": str(home),
        "YORDAMCHI_PY": sys.executable,
        "YORDAMCHI_SESSION_TOKEN": TOKEN,
    }
    if socket is not None:
        env["YORDAMCHI_SOCK"] = str(socket)
    return env


def resolved(path: Path) -> str:
    return str(path.resolve())


def ran(real_bin: Path) -> list[Path]:
    """The stand-in binaries that ran."""
    return sorted(real_bin.glob("*.ran"))


async def run_shim(tool: str, *args: str, env: dict[str, str], cwd: Path) -> proc.ProcResult:
    return await proc.run([str(SHIMS / tool), *args], env=env, cwd=cwd, timeout=60)


# --- the shims ------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("ssh", ("prod", "uptime")),
        ("scp", ("prod:/var/log/app.log", ".")),
        ("sftp", ("-b", "batch.txt", "prod")),
        ("rsync", ("-a", "prod:/srv/backup/", "backup/")),
    ],
)
async def test_an_allowed_call_runs_the_real_binary_with_its_arguments(
    bot: FakeBot, real_bin: Path, tmp_path: Path, tool: str, args: tuple[str, ...]
) -> None:
    result = await run_shim(tool, *args, env=session_env(tmp_path, socket=bot.socket, real_bin=real_bin), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert (real_bin / f"{tool}.ran").read_text(encoding="utf-8").splitlines() == list(args)
    assert bot.checks == [GateCheck(tool, args, resolved(tmp_path), TOKEN)]


async def test_a_refused_call_never_runs_and_says_why(bot: FakeBot, real_bin: Path, tmp_path: Path) -> None:
    bot.gate = GateAnswer(allow=False, reason="remote_command")

    result = await run_shim(
        "ssh", "prod", "reboot", env=session_env(tmp_path, socket=bot.socket, real_bin=real_bin), cwd=tmp_path
    )

    assert result.returncode == 126
    assert b"ssh refused by the gate (remote_command)" in result.stderr
    assert not (real_bin / "ssh.ran").exists()


async def test_no_socket_means_refuse(run_dir: Path, real_bin: Path, tmp_path: Path) -> None:
    env = session_env(tmp_path, socket=run_dir / "dev.sock", real_bin=real_bin)  # nothing listens there

    result = await run_shim("ssh", "prod", "uptime", env=env, cwd=tmp_path)

    assert result.returncode == 126
    assert b"(no_answer)" in result.stderr
    assert not (real_bin / "ssh.ran").exists()


async def test_no_answer_or_no_session_means_refuse(bot: FakeBot, real_bin: Path, tmp_path: Path) -> None:
    bot.silent = True
    silent = await run_shim(
        "ssh", "prod", "uptime", env=session_env(tmp_path, socket=bot.socket, real_bin=real_bin), cwd=tmp_path
    )
    no_python = session_env(tmp_path, socket=bot.socket, real_bin=real_bin)
    del no_python["YORDAMCHI_PY"]
    unconfigured = await run_shim("ssh", "prod", "uptime", env=no_python, cwd=tmp_path)
    no_socket_var = await run_shim(
        "scp", "a", "prod:b", env=session_env(tmp_path, socket=None, real_bin=real_bin), cwd=tmp_path
    )

    assert (silent.returncode, unconfigured.returncode, no_socket_var.returncode) == (126, 126, 126)
    assert b"gate_unreachable" in unconfigured.stderr
    assert b"gate_unreachable" in no_socket_var.stderr
    assert ran(real_bin) == []


async def test_a_shim_alone_on_path_refuses_instead_of_calling_itself(bot: FakeBot, tmp_path: Path) -> None:
    env = session_env(tmp_path, socket=bot.socket)
    env["PATH"] = str(SHIMS)

    result = await run_shim("ssh", "prod", "uptime", env=env, cwd=tmp_path)

    assert result.returncode == 126
    assert b"(not_installed)" in result.stderr


async def test_the_real_gate_behind_the_socket(
    dev_deps: DevDeps, paths: Paths, run_dir: Path, real_bin: Path, tmp_path: Path
) -> None:
    """T34's mount: the token names the session's scope, `service.check_shim` decides."""
    scopes = {TOKEN: make_gate_scope(projects_root_of(paths))}

    async def gate_check(check: GateCheck) -> GateAnswer:
        answer = await service.check_shim(
            dev_deps, scopes.get(check.session_token), ShimRequest(check.tool, check.argv, check.cwd)
        )
        return GateAnswer(answer.allow, answer.reason)

    async def secret_blocked(report: SecretBlocked) -> SecretAnswer:
        raise AssertionError(report)

    handlers = GateHandlers(gate_check=gate_check, secret_blocked=secret_blocked)

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(await control.dispatch(await reader.readline(), handlers=handlers))
        await writer.drain()
        writer.close()

    socket = run_dir / "dev.sock"
    async with await asyncio.start_unix_server(serve, path=str(socket)):
        env = session_env(tmp_path, socket=socket, real_bin=real_bin)
        read_only = await run_shim("ssh", "prod", "uptime", env=env, cwd=tmp_path)
        write = await run_shim("ssh", "prod", "reboot", env=env, cwd=tmp_path)
        stranger = await run_shim("ssh", "prod", "uptime", env={**env, "YORDAMCHI_SESSION_TOKEN": "x"}, cwd=tmp_path)

    assert read_only.returncode == 0
    assert (write.returncode, stranger.returncode) == (126, 126)
    assert b"(remote_command)" in write.stderr
    assert b"(unknown_session)" in stranger.stderr
    assert (real_bin / "ssh.ran").read_text(encoding="utf-8").splitlines() == ["prod", "uptime"]


# --- the pre-push hook ----------------------------------------------------------------------------------------------
class Repo:
    def __init__(self, work: Path, remote: Path, env: dict[str, str]) -> None:
        self.work = work
        self.remote = remote
        self.env = env

    async def git(self, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> proc.ProcResult:
        return await proc.run(["git", *args], env=env or self.env, cwd=cwd or self.work, timeout=60)

    async def commit(self, files: dict[str, str], message: str = "change") -> None:
        for name, text in files.items():
            path = self.work / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assert (await self.git("add", "-A")).returncode == 0
        assert (await self.git("commit", "-q", "-m", message)).returncode == 0

    async def remote_head(self, branch: str = "main") -> str:
        result = await self.git("rev-parse", branch, cwd=self.remote)
        return result.stdout.decode().strip()


def git_env(home: Path) -> dict[str, str]:
    return {
        "PATH": SYSTEM_PATH,
        "HOME": str(home),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Agent",
        "GIT_AUTHOR_EMAIL": "agent@example.com",
        "GIT_COMMITTER_NAME": "Agent",
        "GIT_COMMITTER_EMAIL": "agent@example.com",
    }


@pytest.fixture
async def repo(tmp_path: Path) -> Repo:
    home = tmp_path / "home"
    home.mkdir()
    env = git_env(home)
    remote, work = tmp_path / "remote.git", tmp_path / "work"
    for argv in (["init", "-q", "--bare", "-b", "main", str(remote)], ["init", "-q", "-b", "main", str(work)]):
        assert (await proc.run(["git", *argv], env=env, cwd=tmp_path, timeout=60)).returncode == 0
    repository = Repo(work, remote, env)
    await repository.commit({"README.md": "demo\n"}, "init")
    assert (await repository.git("remote", "add", "origin", str(remote))).returncode == 0
    assert (await repository.git("push", "-q", "-u", "origin", "main")).returncode == 0  # no hook yet
    return repository


def write_hashes(run_dir: Path, *values: str) -> None:
    document = {"sha256": sorted(hashlib.sha256(value.encode()).hexdigest() for value in values)}
    (run_dir / "secret-hashes.json").write_text(json.dumps(document), encoding="utf-8")


def push_env(repo: Repo, *, socket: Path | None) -> dict[str, str]:
    env = {
        **repo.env,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.hooksPath",
        "GIT_CONFIG_VALUE_0": str(GITHOOKS),
    }
    env |= {"YORDAMCHI_PY": sys.executable, "YORDAMCHI_SESSION_TOKEN": TOKEN}
    if socket is not None:
        env["YORDAMCHI_SOCK"] = str(socket)
    return env


async def push(repo: Repo, *args: str, socket: Path | None) -> proc.ProcResult:
    return await repo.git("push", "-q", *(args or ("origin", "main")), env=push_env(repo, socket=socket))


async def test_a_clean_push_goes_through_without_asking_anyone(repo: Repo, bot: FakeBot, run_dir: Path) -> None:
    write_hashes(run_dir, UNKNOWN_FORMAT_KEY)
    await repo.commit({"src/app.py": "print('salom')\n", ".env.example": "DATABASE_URL=\n"})

    result = await push(repo, socket=bot.socket)

    assert result.returncode == 0, result.stderr
    assert await repo.remote_head() == (await repo.git("rev-parse", "HEAD")).stdout.decode().strip()
    assert bot.reports == []


async def test_a_key_in_a_pushed_commit_blocks_and_names_the_file_and_line(
    repo: Repo, bot: FakeBot, run_dir: Path
) -> None:
    write_hashes(run_dir)
    token = FAKE_KEYS["github_token"]
    before = await repo.remote_head()
    await repo.commit({"src/config.py": f'import os\n\nGITHUB = "{token}"\n'})

    result = await push(repo, socket=bot.socket)

    assert result.returncode != 0
    assert b"YORDAMCHI_SECRET_SCAN_BLOCKED file=src/config.py line=3 rule=github_token" in result.stderr
    assert token.encode() not in result.stderr
    assert bot.reports == [SecretBlocked(str(repo.work.resolve()), "src/config.py", 3, "github_token", TOKEN)]
    assert await repo.remote_head() == before


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"config/.env": "SECRET=x\n"}, b"file=config/.env line=1 rule=env_file"),
        ({"deploy/id_rsa": "-----BEGIN-----\n"}, b"file=deploy/id_rsa line=1 rule=ssh_key_file"),
        ({"notes.md": f"x\n{UNKNOWN_FORMAT_KEY}\n"}, b"file=notes.md line=2 rule=vault_value"),
    ],
)
async def test_secret_files_and_vault_values_block(
    repo: Repo, bot: FakeBot, run_dir: Path, files: dict[str, str], expected: bytes
) -> None:
    write_hashes(run_dir, UNKNOWN_FORMAT_KEY)
    await repo.commit(files)

    result = await push(repo, socket=bot.socket)

    assert result.returncode != 0
    assert expected in result.stderr
    assert UNKNOWN_FORMAT_KEY.encode() not in result.stderr


async def test_a_secret_removed_again_before_the_push_still_blocks(repo: Repo, bot: FakeBot, run_dir: Path) -> None:
    write_hashes(run_dir)
    await repo.commit({"src/config.py": f"KEY = '{FAKE_KEYS['openai_api_key']}'\n"}, "add")
    await repo.commit({"src/config.py": "KEY = os.environ['KEY']\n"}, "remove")

    result = await push(repo, socket=bot.socket)

    assert result.returncode != 0
    assert b"file=src/config.py line=1 rule=openai_api_key" in result.stderr  # it is still in the history


async def test_the_owner_can_switch_the_scan_off_at_the_bot(repo: Repo, bot: FakeBot, run_dir: Path) -> None:
    write_hashes(run_dir)
    bot.secret = SecretAnswer(ok=True, enforce=False)
    await repo.commit({".env": "X=1\n"})

    result = await push(repo, socket=bot.socket)

    assert result.returncode == 0, result.stderr
    assert len(bot.reports) == 1  # still reported and audited


async def test_without_the_bot_a_hit_still_blocks_and_a_clean_push_goes(repo: Repo, run_dir: Path) -> None:
    write_hashes(run_dir)
    socket = run_dir / "dev.sock"  # nothing listens
    await repo.commit({"src/app.py": "print(1)\n"})
    assert (await push(repo, socket=socket)).returncode == 0
    await repo.commit({".env": "X=1\n"})

    result = await push(repo, socket=socket)

    assert result.returncode != 0
    assert b"rule=env_file" in result.stderr


@pytest.mark.parametrize("broken", ["missing", "malformed", "no_interpreter", "no_socket_variable"])
async def test_a_scan_that_cannot_run_blocks(repo: Repo, bot: FakeBot, run_dir: Path, broken: str) -> None:
    if broken == "malformed":
        (run_dir / "secret-hashes.json").write_text("{", encoding="utf-8")
    elif broken != "missing":
        write_hashes(run_dir)
    await repo.commit({"src/app.py": "print(1)\n"})
    env = push_env(repo, socket=None if broken == "no_socket_variable" else bot.socket)
    if broken == "no_interpreter":
        del env["YORDAMCHI_PY"]

    result = await repo.git("push", "-q", "origin", "main", env=env)

    assert result.returncode != 0
    assert b"YORDAMCHI_SECRET_SCAN_BLOCKED file=- line=0 rule=scan_unavailable" in result.stderr


async def test_a_branch_deletion_is_not_scanned(repo: Repo, bot: FakeBot, run_dir: Path) -> None:
    write_hashes(run_dir)
    assert (await repo.git("push", "-q", "origin", "main:old")).returncode == 0  # created without the hook

    result = await push(repo, "origin", "--delete", "old", socket=bot.socket)

    assert result.returncode == 0, result.stderr


async def test_a_clean_push_runs_the_projects_own_pre_push_hook(repo: Repo, bot: FakeBot, run_dir: Path) -> None:
    write_hashes(run_dir)
    hook = repo.work / ".git" / "hooks" / "pre-push"
    marker = repo.work.parent / "project-hook.ran"
    hook.write_text(
        f'#!/bin/sh\necho "$1" > "{marker}"\ncat >> "{marker}"\nexit "${{HOOK_EXIT:-0}}"\n', encoding="utf-8"
    )
    hook.chmod(0o755)
    await repo.commit({"src/app.py": "print(1)\n"})

    refused = await repo.git(
        "push", "-q", "origin", "main", env={**push_env(repo, socket=bot.socket), "HOOK_EXIT": "1"}
    )
    passed = await push(repo, socket=bot.socket)

    assert refused.returncode != 0
    assert passed.returncode == 0, passed.stderr
    lines = marker.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "origin"
    assert lines[1].startswith("refs/heads/main ")  # git's ref lines reach the project's hook unchanged
