"""Test data builders (CONVENTIONS §11): `await make_x(s, …)` for rows and `make_*_deps(…)` for dependencies.

Unit tests use a real SQLite file in `tmp_path` built with `create_all`, plus the fakes of `tests/fakes/`.

Block file (TASKS R2): each P1 feature adds its builders only inside its own `# --- <feature> ---` block.
"""

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from tests.fakes.agent import FakeAgentRuntime
from tests.fakes.assistant import FakeAssistant
from tests.fakes.clock import DEFAULT_START, FakeClock
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
from yordamchi.persistence.uow import Database, Session
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.access.domain import Arrival, ChatKind, EventType, OwnerOrigin
from yordamchi.shared.burst import domain as burst_domain
from yordamchi.shared.burst import store as burst_store
from yordamchi.shared.burst.domain import BurstPart, BurstStatus, CloseReason, WindowSettings
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.inbox.domain import Attachment, Content, IncomingUpdate, StoredUpdate, UpdateKind, UpdateStatus
from yordamchi.shared.settings import defaults, store
from yordamchi.shared.settings import domain as settings_domain
from yordamchi.shared.settings.domain import Snapshot
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


STRANGER_ID: Final = 200000002
FOREIGN_GROUP_ID: Final = -1005550001111  # a supergroup that is neither bot's chat
TELEGRAM_DATE: Final = 1761292800  # Telegram dates are Unix seconds

type TelegramJson = dict[str, object]


def own_chat_id(bot: BotName) -> int:
    """The chat the owner talks to `bot` in: the dev group, or the owner's DM."""
    return DEV_GROUP_ID if bot is BotName.DEV else OWNER_ID


def make_arrival(
    update_id: int = 1,
    *,
    bot: BotName = BotName.DEV,
    event: EventType = EventType.MESSAGE,
    user_id: int | None = OWNER_ID,
    chat_id: int | None = None,
    chat_kind: ChatKind | None = None,
    bot_present: bool = True,
) -> Arrival:
    """An update as `OwnerOnly` reads it; by default the owner's message in the bot's own chat. Without `chat_kind`, a
    positive chat id is a private chat and a negative one a supergroup."""
    chat = own_chat_id(bot) if chat_id is None else chat_id
    kind = chat_kind or (ChatKind.PRIVATE if chat > 0 else ChatKind.SUPERGROUP)
    return Arrival(
        bot=bot,
        update_id=update_id,
        event=event,
        user_id=user_id,
        chat_id=chat,
        chat_kind=kind,
        bot_present=bot_present,
    )


# Raw `getUpdates` items (Bot API JSON) for the `Dispatcher.feed_update` tests; `Update.model_validate` reads them.
def tg_user(user_id: int) -> TelegramJson:
    return {"id": user_id, "is_bot": False, "first_name": "Owner" if user_id == OWNER_ID else "Stranger"}


def tg_chat(chat_id: int, chat_type: str | None = None) -> TelegramJson:
    """A Bot API `Chat`: private for a positive id, a supergroup for a negative one, unless `chat_type` says."""
    kind = chat_type or ("private" if chat_id > 0 else "supergroup")
    if kind == "private":
        return {"id": chat_id, "type": kind, "first_name": "Owner" if chat_id == OWNER_ID else "Stranger"}
    return {"id": chat_id, "type": kind, "title": "Guruh"}


def tg_message(
    update_id: int,
    *,
    user_id: int | None = OWNER_ID,
    chat_id: int = DEV_GROUP_ID,
    chat_type: str | None = None,
    text: str = "BonviHR'da login sahifasini tuzat",
    field: str = "message",
) -> TelegramJson:
    """A text message in `field` (`message`, `edited_message`, `channel_post`, `edited_channel_post`).
    `user_id=None` leaves `from` out, as a channel post does."""
    message: TelegramJson = {
        "message_id": 5000 + update_id,
        "date": TELEGRAM_DATE,
        "chat": tg_chat(chat_id, chat_type),
        "text": text,
    }
    if user_id is not None:
        message["from"] = tg_user(user_id)
    if field.startswith("edited_"):
        message["edit_date"] = TELEGRAM_DATE + 60
    return {"update_id": update_id, field: message}


_ADMIN_FLAGS: Final = (
    "can_manage_chat",
    "can_manage_video_chats",
    "can_restrict_members",
    "can_promote_members",
    "can_change_info",
    "can_invite_users",
    "can_post_stories",
    "can_edit_stories",
    "can_delete_stories",
    "can_send_welcome_messages",
)


def tg_member_change(
    update_id: int, *, user_id: int, chat_id: int, old: str = "left", new: str = "member"
) -> TelegramJson:
    """A `my_chat_member`: `user_id` changed the bot's status in `chat_id` from `old` to `new`."""
    bot_user = {"id": 123456789, "is_bot": True, "first_name": "Yordamchi"}
    new_member: TelegramJson = {"user": bot_user, "status": new}
    if new == "kicked":
        new_member["until_date"] = 0
    if new == "administrator":  # the rights the Bot API requires on ChatMemberAdministrator
        new_member |= {"can_be_edited": False, "is_anonymous": False, "can_delete_messages": True}
        new_member |= dict.fromkeys(_ADMIN_FLAGS, False)
    change = {
        "chat": tg_chat(chat_id),
        "from": tg_user(user_id),
        "date": TELEGRAM_DATE,
        "old_chat_member": {"user": bot_user, "status": old},
        "new_chat_member": new_member,
    }
    return {"update_id": update_id, "my_chat_member": change}


def tg_callback(update_id: int, *, user_id: int, chat_id: int | None = None) -> TelegramJson:
    """A `callback_query` (the bots never send buttons, so one is never theirs), on a message in `chat_id` if given."""
    query: TelegramJson = {"id": f"cb-{update_id}", "from": tg_user(user_id), "chat_instance": "ci-1", "data": "1"}
    if chat_id is not None:
        query["message"] = {"message_id": 7000 + update_id, "date": TELEGRAM_DATE, "chat": tg_chat(chat_id)}
    return {"update_id": update_id, "callback_query": query}


def tg_inline_query(update_id: int, *, user_id: int) -> TelegramJson:
    query = {"id": f"iq-{update_id}", "from": tg_user(user_id), "query": "salom", "offset": ""}
    return {"update_id": update_id, "inline_query": query}


# --- outbox ---
# --- burst ---
_MIME: Final[dict[UpdateKind, str]] = {
    UpdateKind.PHOTO: "image/jpeg",
    UpdateKind.VIDEO: "video/mp4",
    UpdateKind.DOCUMENT: "application/pdf",
    UpdateKind.AUDIO: "audio/mpeg",
    UpdateKind.VOICE: "audio/ogg",
}


def make_default_snapshot(bot: BotName = BotName.DEV) -> Snapshot:
    """Every setting of `bot` at its default, resolved from the registry (values are never copied into tests)."""
    bootstrap = defaults.bootstrap_values(
        owner_id=OWNER_ID, dev_group_id=DEV_GROUP_ID, personal_group_id=PERSONAL_GROUP_ID
    )
    resolution = settings_domain.resolve(defaults.REGISTRY, bot, own={}, dev={}, bootstrap=bootstrap)
    return Snapshot(bot=bot, version=0, values=resolution.values, sources=resolution.sources)


def make_window_settings(**overrides: float) -> WindowSettings:
    """The SPEC §4.3 BURST defaults as one pass reads them, with fields replaced by name."""
    return replace(burst_domain.window_settings(make_default_snapshot()), **overrides)


def _attachment(update_id: int, kind: UpdateKind, duration_s: int | None) -> Attachment | None:
    if kind not in _MIME:
        return None
    return Attachment(file_id=f"file-{update_id}", mime=_MIME[kind], size_bytes=2048, duration_s=duration_s)


def make_media_update(
    update_id: int,
    *,
    kind: UpdateKind = UpdateKind.PHOTO,
    caption: str | None = None,
    media_group_id: str | None = None,
    duration_s: int | None = None,
    reply_to_message_id: int | None = None,
    bot: BotName = BotName.DEV,
) -> IncomingUpdate:
    """An owner photo, file or voice note (no text) as a handler would build it."""
    update = make_update(
        update_id,
        text=None,
        kind=kind,
        caption=caption,
        attachment=_attachment(update_id, kind, duration_s),
        bot=bot,
    )
    return replace(update, media_group_id=media_group_id, reply_to_message_id=reply_to_message_id)


def make_part(
    update_id: int = 1,
    *,
    at_s: float = 0.0,
    kind: UpdateKind | None = None,
    text: str | None = None,
    caption: str | None = None,
    media_group_id: str | None = None,
    duration_s: int | None = None,
    message_id: int | None = None,
    reply_to_message_id: int | None = None,
    synthetic: bool = False,
    start: datetime = DEFAULT_START,
) -> BurstPart:
    """A part received `at_s` seconds after `start`, built the way the burst builds it (`part_from_update`): a voice
    note starts out waiting for its transcript. Without `kind`, a text if `text` is given, else a photo."""
    part_kind = kind or (UpdateKind.TEXT if text is not None else UpdateKind.PHOTO)
    stored = StoredUpdate(
        update_id=update_id,
        chat_id=DEV_GROUP_ID,
        kind=part_kind,
        status=UpdateStatus.RECEIVED,
        user_id=OWNER_ID,
        message_id=message_id if message_id is not None else 5000 + update_id,
        media_group_id=media_group_id,
        reply_to_message_id=reply_to_message_id,
        synthetic=synthetic,
        received_at=start + timedelta(seconds=at_s),
        content=Content(text=text, caption=caption, attachment=_attachment(update_id, part_kind, duration_s)),
    )
    return burst_domain.part_from_update(stored)


async def make_burst_row(
    s: Session,
    *,
    bot: BotName = BotName.DEV,
    at: datetime = DEFAULT_START,
    status: BurstStatus = BurstStatus.CLOSED,
) -> int:
    """A closed one-part burst row, for tables that point at `bursts` (`inbound_updates.burst_id`). Returns its id."""
    part = make_part(text="salom", start=at)
    row = await burst_store.add_burst(
        s,
        bot=bot.value,
        chat_id=DEV_GROUP_ID if bot is BotName.DEV else OWNER_ID,
        status=status.value,
        opened_at=at,
        last_part_at=at,
        closes_at=at,
        closed_at=at,
        close_reason=CloseReason.QUIET.value,
        input_json=burst_domain.parts_json((part,), str),
    )
    return row.id


# --- projects ---
# Imports stay inside the block (TASKS R2): parallel tasks never touch the import list at the top.
from pathlib import PurePosixPath  # noqa: E402

import yaml  # noqa: E402

from yordamchi.dev.projects import store as projects_store  # noqa: E402
from yordamchi.dev.projects.domain import (  # noqa: E402
    UNPARSEABLE,
    DeployBranches,
    DescriptionSource,
    ProjectRecord,
    WorkflowFile,
)
from yordamchi.dev.projects.models import Project as ProjectRow  # noqa: E402
from yordamchi.persistence.uow import Session  # noqa: E402

WORKFLOW_FIXTURES: Final = Path(__file__).parent / "fixtures" / "workflows"
OWNER_PROJECTS_ROOT: Final = PurePosixPath("/Users/owner/Documents/Projects")


def projects_root_of(paths: Paths) -> Path:
    """Where `projects.root` ("~/Documents/Projects") points for these test paths."""
    return paths.home / "Documents" / "Projects"


def load_workflow(name: str) -> WorkflowFile:
    """A `tests/fixtures/workflows/` file as the service hands it to the domain (`yaml.safe_load`, or unparseable)."""
    data = (WORKFLOW_FIXTURES / name).read_bytes()
    try:
        document: object = yaml.safe_load(data)
    except yaml.YAMLError:
        document = UNPARSEABLE
    return WorkflowFile(name=name, document=document)


def make_project_folder(
    root: Path,
    name: str,
    *,
    readme: str | None = None,
    claude_md: str | None = None,
    spec: str | None = None,
    workflows: Mapping[str, str] | None = None,
    git_dir: bool = False,
) -> Path:
    """A project folder on disk. `git_dir=True` adds an empty `.git/` (the scan only looks for it; tests of git
    itself use real repositories). `workflows` maps a file name to its YAML text."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    if readme is not None:
        (folder / "README.md").write_text(readme, encoding="utf-8")
    if claude_md is not None:
        (folder / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    if spec is not None:
        (folder / "docs").mkdir(exist_ok=True)
        (folder / "docs" / "SPEC.md").write_text(spec, encoding="utf-8")
    if workflows:
        directory = folder / ".github" / "workflows"
        directory.mkdir(parents=True, exist_ok=True)
        for file_name, text in workflows.items():
            (directory / file_name).write_text(text, encoding="utf-8")
    if git_dir:
        (folder / ".git").mkdir(exist_ok=True)
    return folder


def make_project_record(
    name: str = "BonviHR",
    *,
    at: datetime,
    root: PurePosixPath = OWNER_PROJECTS_ROOT,
    description: str = "",
    description_source: DescriptionSource = DescriptionSource.NONE,
    is_worktree: bool = False,
    is_archive_name: bool = False,
    deploy: DeployBranches | None = None,
    missing_since: datetime | None = None,
) -> ProjectRecord:
    return ProjectRecord(
        id=name,
        path=str(root / name),
        description=description,
        description_source=description_source,
        is_worktree=is_worktree,
        is_archive_name=is_archive_name,
        executor="claude",
        git_remote_url=None,
        default_branch="main",
        deploy=deploy or DeployBranches(),
        deploy_checked_sha=None,
        discovered_at=at,
        last_seen_at=at,
        missing_since=missing_since,
    )


async def make_project(s: Session, name: str = "BonviHR", *, at: datetime, path: Path | None = None) -> ProjectRow:
    """A registered `projects` row, written as the scan would (inside the caller's unit of work)."""
    record = make_project_record(name, at=at)
    if path is not None:
        record = replace(record, path=str(path))
    await projects_store.set_project(s, record)
    row = await projects_store.get_project(s, name)
    assert row is not None
    return row


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
