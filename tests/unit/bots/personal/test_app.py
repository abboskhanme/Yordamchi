"""`bots/personal/app.py` startup (SPEC §2.5 steps 3–10, §5.3 personal "start check"; NFR-SEC-04, NFR-SEC-08; T77).

- The refusals that exit 78 with `run/personal.lasterror` and at most one notice per 6 h, to the owner's DM.
- A clean start opens personal.db and dev.db read-only (the bridge), never the dev token.
- **The tool check** (spike S5): a session whose `init` tool or MCP list is not the allowlist refuses the start,
  is remembered per release and `claude` version (launchd's restarts do not ask the model again for 6 h), and is
  asked again after a `claude` update; Claude out of reach does not stop the bot. Once on the `FakeAssistant`, once
  on the real `ClaudeAssistant` over the recorded qa stream, whose launch options are the personal column of §5.3.
- The real entry point (`python -I -m yordamchi.bots.personal.app`, the plist's module) refuses with 78 in a child.

`personal.env` names the token's Keychain item; in-process, `/usr/bin/security` is a `FakeSecurity` (autouse).
"""

import json
import os
import sys
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from alembic import command
from alembic.config import Config
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

from tests.factories import OWNER_ID, PERSONAL_BOT_TOKEN, Fakes, make_personal_deps
from tests.fakes.claude_client import FakeClient, records
from tests.fakes.clock import FakeClock
from tests.fakes.security import FakeSecurity
from yordamchi.adapters.claude.assistant import ClaudeAssistant
from yordamchi.adapters.claude.options import ALWAYS_DISALLOWED
from yordamchi.adapters.system_clock import SystemClock
from yordamchi.bots.personal import app, runtime
from yordamchi.bots.personal.app import Boot
from yordamchi.bots.personal.runtime import PersonalApp
from yordamchi.core import keychain, proc
from yordamchi.core import strings as S
from yordamchi.core.config import PersonalConfig
from yordamchi.core.errors import PersonalToolsMismatch, ProviderUnavailable, RateLimited
from yordamchi.core.paths import BotName, Paths
from yordamchi.core.text import BotText
from yordamchi.persistence.uow import Database
from yordamchi.ports.agent import RuntimeStatus

ROOT = Path(__file__).resolve().parents[4]
REFUSED_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"  # one of core.proc.REFUSED_ENV_VARS
RELEASE = "2026.10.24-abc1234"


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> FakeSecurity:
    security = FakeSecurity()
    security.put(keychain.PERSONAL_BOT_TOKEN, PERSONAL_BOT_TOKEN)
    monkeypatch.setattr(keychain, "security_runner", security)
    return security


class Notices:
    def __init__(self) -> None:
        self.sent: list[tuple[int, BotText]] = []

    async def __call__(self, config: PersonalConfig, text: BotText) -> None:
        self.sent.append((config.owner_id, text))


class FailingNotices:
    async def __call__(self, config: PersonalConfig, text: BotText) -> None:
        del config, text
        raise ProviderUnavailable(provider="Telegram", retryable=True)


def _environ(paths: Paths, *, token: str = keychain.PERSONAL_BOT_TOKEN.reference, **extra: str) -> dict[str, str]:
    env_file = paths.env_file(BotName.PERSONAL)
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(f"PERSONAL_BOT_TOKEN={token}\nOWNER_ID={OWNER_ID}\nPERSONAL_GROUP_ID=\n")
    return {
        "YORDAMCHI_DATA_DIR": str(paths.data),
        "YORDAMCHI_CONFIG_DIR": str(paths.config),
        "YORDAMCHI_LOG_DIR": str(paths.logs),
        "YORDAMCHI_ENV_FILE": str(env_file),
        **extra,
    }


def _binaries(paths: Paths, *names: str) -> None:
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
        environ, clock=clock, notify=notices, home=paths.home, version=RELEASE, login_path=_no_login_shell
    )


# --- steps 3–8 -------------------------------------------------------------------------------------------------------
async def test_a_refused_credential_exits_78_and_tells_the_owner_in_his_dm(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    environ = _environ(paths, **{REFUSED_TOKEN: "sk-ant-oat01-not-a-real-token"})
    assert await _prepare(paths, environ, clock, notices) == proc.EXIT_REFUSED == 78
    lasterror = paths.last_error(BotName.PERSONAL).read_text()
    assert f"startup_refused credentials: env:{REFUSED_TOKEN}" in lasterror
    assert "sk-ant-oat01" not in lasterror
    assert notices.sent == [(OWNER_ID, S.STARTUP_REFUSED(what=f"env:{REFUSED_TOKEN}"))]
    await clock.advance(5 * 3600)
    assert await _prepare(paths, environ, clock, notices) == 78
    assert len(notices.sent) == 1  # one notice per 6 h, not one per launchd restart


async def test_a_missing_claude_is_fatal(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    _binaries(paths, "git")
    assert await _prepare(paths, _environ(paths, PATH=str(paths.local_bin)), clock, notices) == 78
    assert "startup_refused binaries: claude" in paths.last_error(BotName.PERSONAL).read_text()


async def test_a_clean_start_opens_dev_db_read_only_and_never_the_dev_token(
    paths: Paths, fake_keychain: FakeSecurity
) -> None:
    clock, notices = FakeClock(), Notices()
    _binaries(paths, "claude", "git")
    _migrate(paths.db_file(BotName.PERSONAL))
    _migrate(paths.db_file(BotName.DEV))
    environ = _environ(paths, CLAUDECODE="1")
    booted = await _prepare(paths, environ, clock, notices)
    assert isinstance(booted, Boot)
    try:
        assert booted.config.bot_token == PERSONAL_BOT_TOKEN
        assert fake_keychain.calls == [tuple(keychain.find_argv(keychain.PERSONAL_BOT_TOKEN, reveal=True))]
        assert "CLAUDECODE" not in environ
        assert booted.peer_db is not None
        assert booted.peer_db.readonly  # the bridge's handle: mode=ro + query_only (UC-36)
        assert booted.peer_db.engine.url.query.get("mode") == "ro"
        assert not booted.probation
        assert notices.sent == []
    finally:
        await booted.db.dispose()
        if booted.peer_db is not None:
            await booted.peer_db.dispose()


async def test_without_dev_db_the_bridge_is_off(paths: Paths) -> None:
    clock, notices = FakeClock(), Notices()
    _binaries(paths, "claude", "git")
    _migrate(paths.db_file(BotName.PERSONAL))
    booted = await _prepare(paths, _environ(paths), clock, notices)
    assert isinstance(booted, Boot)
    try:
        assert booted.peer_db is None
    finally:
        await booted.db.dispose()


# --- step 10: the tool check (NFR-SEC-08) ----------------------------------------------------------------------------
def _config(paths: Paths) -> PersonalConfig:
    return PersonalConfig(bot_token=PERSONAL_BOT_TOKEN, owner_id=OWNER_ID, personal_group_id=None, paths=paths)


def _app(personal_db: Database, dev_db_readonly: Database, paths: Paths, fakes: Fakes) -> PersonalApp:
    deps = make_personal_deps(db=personal_db, paths=paths, fakes=fakes, peer_db=dev_db_readonly)
    return runtime.build_app(deps, bot=None, version=RELEASE)


async def test_a_tool_list_outside_the_allowlist_refuses_the_start_and_is_remembered(
    personal_db: Database, dev_db_readonly: Database, paths: Paths, fakes: Fakes
) -> None:
    personal = _app(personal_db, dev_db_readonly, paths, fakes)
    notices = Notices()
    fakes.assistant.reply_with("qa", PersonalToolsMismatch())

    refused = await app.check_tools(personal, _config(paths), clock=fakes.clock, notify=notices)
    assert refused == 78
    assert "startup_refused tools: personal session tools" in paths.last_error(BotName.PERSONAL).read_text()
    assert notices.sent == [(OWNER_ID, S.PERSONAL_STARTUP_TOOLS_REFUSED())]
    [probe] = fakes.assistant.requests
    assert probe.kind == "qa"
    assert [toolset.server for toolset in probe.toolsets] == ["yordamchi_bridge", "yordamchi_media"]
    assert fakes.assistant.resets == [("qa", probe.conversation)]  # the throwaway conversation is closed
    memory = json.loads(app.tools_marker(paths).read_text())
    assert (memory["release"], memory["cli"]) == (RELEASE, "2.1.309")

    # launchd restarts it at once: refused from the marker, no model asked, no second notice
    await fakes.clock.advance(60)
    assert await app.check_tools(personal, _config(paths), clock=fakes.clock, notify=notices) == 78
    assert len(fakes.assistant.requests) == 1
    assert len(notices.sent) == 1

    # a `claude` update asks again; this time the session is right and the bot starts
    fakes.assistant.status = RuntimeStatus(installed=True, version="2.1.400", logged_in=True, auth_method="claude.ai")
    assert await app.check_tools(personal, _config(paths), clock=fakes.clock, notify=notices) is None
    assert len(fakes.assistant.requests) == 2


async def test_the_memory_holds_even_when_the_notice_could_not_be_sent(
    personal_db: Database, dev_db_readonly: Database, paths: Paths, fakes: Fakes
) -> None:
    """Telegram down, Claude up: the refusal is still remembered, so the restarts ask no model; the notice is tried
    again on the next start and goes out once."""
    personal = _app(personal_db, dev_db_readonly, paths, fakes)
    fakes.assistant.reply_with("qa", PersonalToolsMismatch())
    failing = FailingNotices()
    assert await app.check_tools(personal, _config(paths), clock=fakes.clock, notify=failing) == 78
    notices = Notices()
    assert await app.check_tools(personal, _config(paths), clock=fakes.clock, notify=notices) == 78
    assert len(fakes.assistant.requests) == 1
    assert notices.sent == [(OWNER_ID, S.PERSONAL_STARTUP_TOOLS_REFUSED())]
    await fakes.clock.advance(6 * 3600)  # after 6 h the check runs again
    assert await app.check_tools(personal, _config(paths), clock=fakes.clock, notify=notices) is None
    assert len(fakes.assistant.requests) == 2


async def test_claude_out_of_reach_does_not_stop_the_bot(
    personal_db: Database, dev_db_readonly: Database, paths: Paths, fakes: Fakes
) -> None:
    personal = _app(personal_db, dev_db_readonly, paths, fakes)
    fakes.assistant.reply_with("qa", RateLimited(provider="claude", retry_after=None))
    assert await app.check_tools(personal, _config(paths), clock=fakes.clock, notify=Notices()) is None
    assert not paths.last_error(BotName.PERSONAL).exists()


# --- the same check on the real adapter (the recorded qa stream) -----------------------------------------------------
def _segment(**init: object) -> list[dict[str, Any]]:
    """The first recorded ask of `assistant_qa.jsonl`, its `init` message changed by `init`."""
    found: list[dict[str, Any]] = []
    for record in records("assistant_qa"):
        if record["kind"] == "sent" and found:
            break
        if record["kind"] == "sent" or found:
            found.append(json.loads(json.dumps(record)))
    for record in found:
        if record["kind"] == "raw" and record["data"].get("subtype") == "init":
            record["data"].update(init)
    return found


class Factory:
    def __init__(self, recording: list[dict[str, Any]]) -> None:
        self.recording = recording
        self.options: list[ClaudeAgentOptions] = []

    def __call__(self, options: ClaudeAgentOptions) -> ClaudeSDKClient:
        self.options.append(options)
        return cast("ClaudeSDKClient", FakeClient(options, self.recording, warn=False))


ALLOWLIST = sorted(
    [
        "WebFetch",
        "WebSearch",
        "mcp__yordamchi_bridge__project_status",
        "mcp__yordamchi_bridge__server_status",
        "mcp__yordamchi_bridge__projects_overview",
        "mcp__yordamchi_media__analyze_video",
        "mcp__yordamchi_media__screenshot",
        "mcp__yordamchi_media__fetch_file",
        "mcp__yordamchi_media__download_video",
    ]
)
SERVERS = [
    {"name": "yordamchi_bridge", "status": "connected", "source": "sdk"},
    {"name": "yordamchi_media", "status": "connected", "source": "sdk"},
]


def _real_app(
    personal_db: Database, dev_db_readonly: Database, paths: Paths, fakes: Fakes, factory: Factory
) -> tuple[PersonalApp, ClaudeAssistant]:
    assistant = ClaudeAssistant(
        cli_path=Path("/Users/owner/.local/bin/claude"),
        search_path="/Users/owner/.local/bin:/usr/bin:/bin",
        sessions_dir=paths.data / "sessions",
        base_env={"HOME": str(paths.home), "PATH": "/usr/bin:/bin"},
        client_factory=factory,
        clock=fakes.clock,
    )
    deps = make_personal_deps(db=personal_db, paths=paths, fakes=fakes, peer_db=dev_db_readonly)
    return runtime.build_app(replace(deps, assistant=assistant), bot=None, version=RELEASE), assistant


async def test_the_real_session_refuses_an_extra_tool_and_runs_the_personal_column(
    personal_db: Database, dev_db_readonly: Database, paths: Paths, fakes: Fakes
) -> None:
    factory = Factory(_segment(tools=[*ALLOWLIST, "Bash"], mcp_servers=SERVERS))
    personal, assistant = _real_app(personal_db, dev_db_readonly, paths, fakes, factory)
    try:
        with pytest.raises(PersonalToolsMismatch):
            await personal.verify_tools()
        [options] = factory.options
        assert options.extra_args == {"restricted": None}  # S5
        assert options.permission_mode == "dontAsk"
        assert options.allowed_tools == ALLOWLIST  # the exact allowlist
        assert options.tools == ["WebSearch", "WebFetch"]
        assert options.strict_mcp_config is True
        assert options.setting_sources == []
        assert {"SendMessage", "ListAgents"} <= set(options.disallowed_tools)
        assert set(ALWAYS_DISALLOWED) <= set(options.disallowed_tools)
        env = options.env
        assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
        for name in proc.REFUSED_ENV_VARS:
            assert name not in env
        assert set(cast("dict[str, Any]", options.mcp_servers)) == {"yordamchi_bridge", "yordamchi_media"}
    finally:
        await assistant.close(timeout=5.0)


async def test_the_real_session_with_the_exact_allowlist_passes(
    personal_db: Database, dev_db_readonly: Database, paths: Paths, fakes: Fakes
) -> None:
    factory = Factory(_segment(tools=ALLOWLIST, mcp_servers=SERVERS))
    personal, assistant = _real_app(personal_db, dev_db_readonly, paths, fakes, factory)
    try:
        assert await personal.verify_tools() is True
    finally:
        await assistant.close(timeout=5.0)


# --- the entry point the plist runs ----------------------------------------------------------------------------------
async def test_the_entry_point_refuses_with_78_in_a_real_process(paths: Paths) -> None:
    """`python -I -m yordamchi.bots.personal.app`, the module `uz.yordamchi.personal.plist.tmpl` runs, with a
    plaintext token in `personal.env`: refused first, before any Keychain read, by name only, no connection."""
    marker = app.refusal_marker(paths)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"at": SystemClock().now().isoformat(), "reason": "credentials"}))
    environ = _environ(paths, token=PERSONAL_BOT_TOKEN)
    environ |= {"HOME": str(paths.home), "PATH": os.defpath, "LANG": "en_US.UTF-8"}
    result = await proc.run([sys.executable, "-I", "-m", "yordamchi.bots.personal.app"], env=environ, timeout=60)
    assert result.returncode == 78, result.stderr.decode(errors="replace")[-400:]
    lasterror = paths.last_error(BotName.PERSONAL).read_text()
    assert "startup_refused config: PERSONAL_BOT_TOKEN: plaintext_in_personal.env" in lasterror
    for text in (lasterror, paths.log_file("personal").read_text(), result.stdout.decode(), result.stderr.decode()):
        assert PERSONAL_BOT_TOKEN not in text
