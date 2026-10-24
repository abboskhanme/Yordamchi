"""`bots/dev/app.py` startup (SPEC §2.5 steps 3–8; NFR-SEC-04): the refusals that exit 78 with `run/dev.lasterror`
and at most one notice per 6 h, the environment scrub, the child PATH and the schema check. Then the real entry point
in a child process: it refuses with exit 78 and never touches the network while its notice marker is fresh."""

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path

from alembic import command
from alembic.config import Config

from tests.factories import DEV_BOT_TOKEN, DEV_GROUP_ID, OWNER_ID, open_test_db
from tests.fakes.clock import FakeClock
from yordamchi.adapters.system_clock import SystemClock
from yordamchi.bots.dev import app
from yordamchi.bots.dev.app import Boot
from yordamchi.core import proc
from yordamchi.core import strings as S
from yordamchi.core.config import DevConfig
from yordamchi.core.paths import BotName, Paths
from yordamchi.core.text import BotText

ROOT = Path(__file__).resolve().parents[4]
REFUSED_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"  # one of core.proc.REFUSED_ENV_VARS


class Notices:
    def __init__(self) -> None:
        self.sent: list[tuple[int, BotText]] = []

    async def __call__(self, config: DevConfig, text: BotText) -> None:
        self.sent.append((config.dev_group_id, text))


def _environ(paths: Paths, **extra: str) -> dict[str, str]:
    env_file = paths.env_file(BotName.DEV)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(f"DEV_BOT_TOKEN={DEV_BOT_TOKEN}\nOWNER_ID={OWNER_ID}\nDEV_GROUP_ID={DEV_GROUP_ID}\n")
    return {
        "YORDAMCHI_DATA_DIR": str(paths.data),
        "YORDAMCHI_CONFIG_DIR": str(paths.config),
        "YORDAMCHI_LOG_DIR": str(paths.logs),
        "YORDAMCHI_ENV_FILE": str(env_file),
        **extra,
    }


def _binaries(paths: Paths, *names: str) -> None:
    """Executables on the child PATH (`~/.local/bin`), standing in for claude and git."""
    paths.local_bin.mkdir(parents=True, exist_ok=True)
    for name in names:
        binary = paths.local_bin / name
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)


async def _no_login_shell(environ: Mapping[str, str]) -> str | None:
    del environ
    return None


def _migrate(db_path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["db_path"] = db_path
    config.attributes["skip_logging_config"] = True
    command.upgrade(config, "head")


async def _prepare(paths: Paths, environ: dict[str, str], clock: FakeClock, notices: Notices) -> Boot | int:
    return await app.prepare(
        environ, clock=clock, notify=notices, home=paths.home, version="2026.10.24-abc1234", login_path=_no_login_shell
    )


async def test_a_refused_credential_exits_78_writes_lasterror_and_tells_the_owner_once_per_six_hours(
    paths: Paths,
) -> None:
    clock, notices = FakeClock(), Notices()
    environ = _environ(paths, **{REFUSED_TOKEN: "sk-ant-oat01-not-a-real-token"})

    assert await _prepare(paths, environ, clock, notices) == proc.EXIT_REFUSED == 78
    lasterror = paths.last_error(BotName.DEV).read_text()
    assert f"startup_refused credentials: env:{REFUSED_TOKEN}" in lasterror
    assert "sk-ant-oat01" not in lasterror  # names only, never the value
    assert paths.last_error(BotName.DEV).stat().st_mode & 0o777 == 0o600
    assert notices.sent == [(DEV_GROUP_ID, S.STARTUP_REFUSED(what=f"env:{REFUSED_TOKEN}"))]

    await clock.advance(5 * 3600)  # launchd restarts it every 10 s: one notice per 6 h, not one per start
    assert await _prepare(paths, environ, clock, notices) == 78
    assert len(notices.sent) == 1
    await clock.advance(3600)
    assert await _prepare(paths, environ, clock, notices) == 78
    assert len(notices.sent) == 2
    assert environ[REFUSED_TOKEN]  # refused before the scrub: nothing was hidden from the check


async def test_an_api_key_helper_in_the_owners_claude_settings_is_refused(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    paths.claude_user_settings.parent.mkdir(parents=True)
    paths.claude_user_settings.write_text(json.dumps({"apiKeyHelper": "/usr/local/bin/key"}))
    assert await _prepare(paths, _environ(paths), clock, notices) == 78
    assert "settings.json:apiKeyHelper" in paths.last_error(BotName.DEV).read_text()
    assert len(notices.sent) == 1


async def test_a_missing_env_file_exits_78_without_a_notice(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    environ = _environ(paths)
    paths.env_file(BotName.DEV).unlink()
    assert await _prepare(paths, environ, clock, notices) == 78
    assert "startup_refused config: YORDAMCHI_ENV_FILE: file_missing" in paths.last_error(BotName.DEV).read_text()
    assert notices.sent == []  # no token, no one to tell


async def test_a_missing_claude_or_git_is_fatal(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    _binaries(paths, "git")
    environ = _environ(paths, PATH=str(paths.local_bin))
    # The child PATH is the shims, the test home's ~/.local/bin, DATA/bin and the system dirs: claude is in none.
    assert await _prepare(paths, environ, clock, notices) == 78
    assert "startup_refused binaries: claude" in paths.last_error(BotName.DEV).read_text()
    assert notices.sent == [(DEV_GROUP_ID, S.ERR_NOT_INSTALLED(binary="claude"))]


async def test_a_db_behind_the_head_is_refused_unless_an_update_runs(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    _binaries(paths, "claude", "git")
    db = await open_test_db(paths.db_file(BotName.DEV))  # every table, but no alembic revision
    await db.dispose()
    assert await _prepare(paths, _environ(paths), clock, notices) == 78
    assert "startup_refused schema: dev.db" in paths.last_error(BotName.DEV).read_text()
    assert notices.sent == [(DEV_GROUP_ID, S.STARTUP_SCHEMA_MISMATCH())]

    paths.drain_flag.parent.mkdir(parents=True, exist_ok=True)
    paths.drain_flag.write_text("{}")  # the supervisor drains for an update: the schema may lag for a while
    booted = await _prepare(paths, _environ(paths), clock, notices)
    assert isinstance(booted, Boot)
    await booted.db.dispose()


async def test_a_clean_start_scrubs_the_environment_and_puts_the_shims_first(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    _binaries(paths, "claude", "git")
    _migrate(paths.db_file(BotName.DEV))
    environ = _environ(paths, CLAUDECODE="1", CLAUDE_CODE_ENTRYPOINT="cli")
    booted = await _prepare(paths, environ, clock, notices)
    assert isinstance(booted, Boot)
    try:
        assert "CLAUDECODE" not in environ
        assert "CLAUDE_CODE_ENTRYPOINT" not in environ
        assert booted.search_path.split(":")[:3] == [str(paths.shims), str(paths.local_bin), str(paths.bin)]
        assert str(paths.shims) not in booted.ssh_path.split(":")  # the real ssh never finds the shims
        assert booted.version == "2026.10.24-abc1234"
        assert not booted.probation
        assert booted.peer_db is None  # no personal.db yet (P3)
        assert notices.sent == []
    finally:
        await booted.db.dispose()


async def test_the_probation_flag_naming_this_version_starts_probation(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    _binaries(paths, "claude", "git")
    _migrate(paths.db_file(BotName.DEV))
    paths.probation_flag.parent.mkdir(parents=True, exist_ok=True)
    paths.probation_flag.write_text(json.dumps({"version": "2026.10.24-abc1234", "nonce": "n"}))
    booted = await _prepare(paths, _environ(paths), clock, notices)
    assert isinstance(booted, Boot)
    try:
        assert booted.probation
    finally:
        await booted.db.dispose()


def test_the_release_version_comes_from_the_installers_marker(tmp_path: Path) -> None:
    assert app.release_version(tmp_path) == app.DEV_VERSION
    (tmp_path / app.RELEASE_MARKER).write_text(json.dumps({"version": "2026.10.24-abc1234"}))
    assert app.release_version(tmp_path) == "2026.10.24-abc1234"


async def test_the_entry_point_refuses_with_78_in_a_real_process(paths: Paths) -> None:
    """`python -I -m yordamchi.bots.dev.app` as launchd runs it. The notice marker is fresh, so the refusal is
    reported to `run/dev.lasterror` only: the child never opens a connection."""
    marker = app.refusal_marker(paths)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"at": SystemClock().now().isoformat(), "reason": "credentials"}))
    environ = _environ(paths, **{REFUSED_TOKEN: "sk-ant-oat01-not-a-real-token"})
    environ |= {"HOME": str(paths.home), "PATH": os.defpath, "LANG": "en_US.UTF-8"}
    result = await proc.run([sys.executable, "-I", "-m", "yordamchi.bots.dev.app"], env=environ, timeout=60)
    assert result.returncode == 78, result.stderr.decode(errors="replace")[-400:]
    assert f"env:{REFUSED_TOKEN}" in paths.last_error(BotName.DEV).read_text()
    logged = paths.log_file("dev").read_text()
    assert '"event": "startup_refused"' in logged
    assert "sk-ant-oat01" not in logged
