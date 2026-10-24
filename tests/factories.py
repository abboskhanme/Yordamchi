"""Test data builders (CONVENTIONS §11): `await make_x(s, …)` for rows and `make_*_deps(…)` for dependencies.

Unit tests use a real SQLite file in `tmp_path` built with `create_all`, plus the fakes of `tests/fakes/`.

Block file (TASKS R2): each P1 feature adds its builders only inside its own `# --- <feature> ---` block.
"""

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

from tests.fakes.agent import FakeAgentRuntime
from tests.fakes.assistant import FakeAssistant
from tests.fakes.clock import FakeClock
from tests.fakes.git import FakeGit, FakeGitHub
from tests.fakes.keystore import InMemoryKeystore
from tests.fakes.mac_host import FakeMacHost
from tests.fakes.mcp import FakeMcpClient
from tests.fakes.media import FakeMediaAnalyzer
from tests.fakes.messenger import FakeMessenger
from tests.fakes.remote_shell import FakeRemoteShell
from tests.fakes.speech import FakeSynthesizer, FakeTranscriber
from tests.fakes.web import FakeWebTools
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.persistence.base import import_all_models
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.uow import Database
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.inbox.domain import Attachment, IncomingUpdate, UpdateKind
from yordamchi.shared.settings import defaults, store
from yordamchi.shared.settings.models import SettingVersion

# --- core ---
# Fake bootstrap values. The token has the real shape, so redaction tests exercise the real pattern.
DEV_BOT_TOKEN: Final = "123456789:AAFakeDevTokenForTestsOnly_01234567"
PERSONAL_BOT_TOKEN: Final = "987654321:AAFakePersonalTokenForTests_0123456"
OWNER_ID: Final = 424242
DEV_GROUP_ID: Final = -1001234567890
PERSONAL_GROUP_ID: Final = -1009876543210


def make_paths(root: Path) -> Paths:
    """Every Yordamchi root under `root` (nothing under the real home)."""
    home = root / "home"
    return Paths.defaults(home, data=root / "data", config=root / "config", logs=root / "logs")


async def open_test_db(path: Path) -> Database:
    """A real SQLite file with every table (`create_all`; migrations are tested in tests/migrations)."""
    db = open_database(path)
    async with db.engine.begin() as connection:
        await connection.run_sync(import_all_models().create_all)
    return db


@dataclass
class Fakes:
    """One fake per port, shared by the deps built from it so a test can script and inspect them."""

    clock: FakeClock = field(default_factory=FakeClock)
    messenger: FakeMessenger = field(default_factory=FakeMessenger)
    keystore: InMemoryKeystore = field(default_factory=InMemoryKeystore)
    assistant: FakeAssistant = field(default_factory=FakeAssistant)
    transcriber: FakeTranscriber = field(default_factory=FakeTranscriber)
    synthesizer: FakeSynthesizer = field(default_factory=FakeSynthesizer)
    mac_host: FakeMacHost = field(default_factory=FakeMacHost)
    mcp: FakeMcpClient = field(default_factory=FakeMcpClient)
    agents: FakeAgentRuntime = field(default_factory=FakeAgentRuntime)
    git: FakeGit = field(default_factory=FakeGit)
    github: FakeGitHub = field(default_factory=FakeGitHub)
    remote_shell: FakeRemoteShell = field(default_factory=FakeRemoteShell)
    media: FakeMediaAnalyzer = field(default_factory=FakeMediaAnalyzer)
    web: FakeWebTools = field(default_factory=FakeWebTools)


def make_dev_deps(*, db: Database, paths: Paths, fakes: Fakes, peer_db: Database | None = None) -> DevDeps:
    return DevDeps(
        bot=BotName.DEV,
        db=db,
        peer_db=peer_db,
        clock=fakes.clock,
        paths=paths,
        bootstrap=defaults.bootstrap_values(owner_id=OWNER_ID, dev_group_id=DEV_GROUP_ID),
        messenger=fakes.messenger,
        keystore=fakes.keystore,
        assistant=fakes.assistant,
        transcribers={fakes.transcriber.provider: fakes.transcriber},
        synthesizers={fakes.synthesizer.provider: fakes.synthesizer},
        mac_host=fakes.mac_host,
        mcp=fakes.mcp,
        agents=fakes.agents,
        git=fakes.git,
        github=fakes.github,
        remote_shell=fakes.remote_shell,
    )


def make_personal_deps(*, db: Database, paths: Paths, fakes: Fakes, peer_db: Database | None = None) -> PersonalDeps:
    return PersonalDeps(
        bot=BotName.PERSONAL,
        db=db,
        peer_db=peer_db,
        clock=fakes.clock,
        paths=paths,
        bootstrap=defaults.bootstrap_values(owner_id=OWNER_ID, personal_group_id=PERSONAL_GROUP_ID),
        messenger=fakes.messenger,
        keystore=fakes.keystore,
        assistant=fakes.assistant,
        transcribers={fakes.transcriber.provider: fakes.transcriber},
        synthesizers={fakes.synthesizer.provider: fakes.synthesizer},
        mac_host=fakes.mac_host,
        mcp=fakes.mcp,
        media=fakes.media,
        web=fakes.web,
    )


# --- inbox ---
# One fake key per format UC-04 names (all fake; split so a secret scanner does not flag the repo). The text a test
# sends is `f"... kalit: {value} ..."`, so the generic one sits next to a keyword, as the SPEC §4.5 rule needs.
FAKE_KEYS: Final[dict[str, str]] = {
    "anthropic_api_key": "sk-ant-api03-" + "Zx9Yw8Vu7Ts6Rq5Po4Nm3Lk2Ji1Hg0Fe",
    "openai_api_key": "sk-proj-" + "Ab3dEf6hIj9kLm2nOp5qRs8t",
    "google_api_key": "AIza" + "SyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "openrouter_api_key": "sk-or-v1-" + "0123456789abcdef" * 4,
    "github_token": "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "telegram_bot_token": "555666777:" + "AAHq8Lz3Rk9Tm2Wp7Xc4Vb6Nd1Fg5Jh0Ks9",
    "generic_secret": "Q7v!x9Lr2Tz@8Wn4Kp6Hs3Yb",
}
CLAUDE_LOGIN_TOKEN: Final = "sk-ant-oat01-" + "Rk2Vq9Lw4Tz7Xn1Pc8Hd3Jf6Mb5Gs0Ya"
# No pattern knows this shape and no keyword sits next to it: only "logs hold no content" keeps it out of logs.
UNKNOWN_FORMAT_KEY: Final = "zq81.QWmd7#Lp3vTx9!Rk2s.vault-me-not"


def make_update(
    update_id: int = 1,
    *,
    text: str | None = "BonviHR'da login sahifasini tuzat",
    bot: BotName = BotName.DEV,
    kind: UpdateKind | None = None,
    message_id: int | None = None,
    caption: str | None = None,
    attachment: Attachment | None = None,
    synthetic: bool = False,
) -> IncomingUpdate:
    """An owner message in the bot's own chat, as a handler would build it."""
    return IncomingUpdate(
        bot=bot,
        update_id=update_id,
        chat_id=DEV_GROUP_ID if bot is BotName.DEV else OWNER_ID,
        kind=kind or (UpdateKind.TEXT if text is not None else UpdateKind.PHOTO),
        user_id=OWNER_ID,
        message_id=message_id if message_id is not None else 5000 + update_id,
        text=text,
        caption=caption,
        attachment=attachment,
        synthetic=synthetic,
    )


def origin_of(update: IncomingUpdate) -> OwnerOrigin:
    """The origin `shared/access` would mint for this update."""
    return make_owner_origin(update.bot, update_id=update.update_id, chat_id=update.chat_id)


# --- audit ---
# --- vault ---
def write_master_key(path: Path, key: bytes | None = None, *, mode: int = 0o600) -> bytes:
    """A master key file as the installer writes it (32 random bytes, base64, newline). Returns the raw key."""
    raw = key if key is not None else os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(base64.b64encode(raw) + b"\n")
    path.chmod(mode)
    return raw


def db_file_bytes(path: Path) -> bytes:
    """Everything SQLite wrote for one DB: the main file plus its WAL and shared-memory files."""
    files = (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"))
    return b"".join(file.read_bytes() for file in files if file.exists())


# --- cost ---
def make_usage(
    *,
    source: str = "stt",
    provider: Provider = Provider.GOOGLE,
    model: str = "gemini-3.5-transcribe",
    is_paid_api: bool = True,
    cost_usd_micros: int = 1_250,
    duration_ms: int = 900,
    units: Mapping[str, int] | None = None,
    project_id: str | None = None,
    turn_id: int | None = None,
) -> Usage:
    """A valid STT call on a paid Google key by default."""
    return Usage(
        source=source,
        provider=provider,
        model=model,
        is_paid_api=is_paid_api,
        cost_usd_micros=cost_usd_micros,
        duration_ms=duration_ms,
        units={"audio_ms": 61_000} if units is None else units,
        project_id=project_id,
        turn_id=turn_id,
    )


# --- access ---
def make_owner_origin(bot: BotName = BotName.DEV, *, update_id: int = 1, chat_id: int | None = None) -> OwnerOrigin:
    """Stands in for `shared/access` minting an origin from a verified owner update."""
    default_chat = DEV_GROUP_ID if bot is BotName.DEV else OWNER_ID
    return OwnerOrigin(bot=bot, update_id=update_id, user_id=OWNER_ID, chat_id=chat_id or default_chat)


# --- outbox ---
# --- burst ---
# --- projects ---
# --- gate ---
# --- voice ---
# --- routing ---
# --- settings ---
async def add_setting_row(
    db: Database, key: str, value: JsonValue, *, at: datetime, version: int | None = None
) -> SettingVersion:
    """Write a `setting_versions` row directly (including values the registry would refuse)."""
    async with db.uow() as s:
        number = version if version is not None else await store.get_latest_version_number(s, key) + 1
        return await store.add_setting_version(
            s,
            key=key,
            value=value,
            version=number,
            reason="change",
            actor="owner",
            origin_update_id=None,
            created_at=at,
        )


# --- turns ---
# --- questions ---
# --- digest ---
# --- servers ---
# --- health ---
# --- wiring (bots) ---
