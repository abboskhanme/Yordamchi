"""Test data builders (CONVENTIONS §11): `await make_x(s, …)` for rows and `make_*_deps(…)` for dependencies.

Unit tests use a real SQLite file in `tmp_path` built with `create_all`, plus the fakes of `tests/fakes/`.

Block file (TASKS R2): each P1 feature adds its builders only inside its own `# --- <feature> ---` block.
"""

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
# --- audit ---
# --- vault ---
# --- cost ---
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
