"""Test data builders (CONVENTIONS §11): `await make_x(s, …)` for rows and `make_*_deps(…)` for dependencies.

Unit tests use a real SQLite file in `tmp_path` built with `create_all`, plus the fakes of `tests/fakes/`.

Block file (TASKS R2): each P1 feature adds its builders only inside its own `# --- <feature> ---` block.
"""

import base64
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Final

from sqlalchemy import select

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
from yordamchi.dev.digest.domain import ReportQuestion, ServerCheck
from yordamchi.dev.digest.service import DigestState
from yordamchi.dev.gate import domain as gate_domain
from yordamchi.dev.gate import service as gate_service
from yordamchi.dev.gate import store as gate_store
from yordamchi.dev.gate.domain import Policy
from yordamchi.dev.gate.models import Decision
from yordamchi.dev.gate.service import GateScope
from yordamchi.dev.projects.domain import Project, ProjectPrefs, is_archive_name, project_view
from yordamchi.dev.questions import domain as questions_domain
from yordamchi.dev.questions import store as questions_store
from yordamchi.dev.questions.domain import QuestionStatus
from yordamchi.dev.routing.domain import Activity, ActivityKind, Intent
from yordamchi.dev.servers import domain as servers_domain
from yordamchi.dev.servers.models import ServerCheck as ServerCheckRow
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.domain import DeliveryMode, DeliverySource, TurnKind, TurnRequest, TurnStatus, TurnSummary
from yordamchi.dev.turns.models import Turn
from yordamchi.dev.turns.service import TurnsState
from yordamchi.persistence.base import import_all_models
from yordamchi.persistence.engine import open_database
from yordamchi.persistence.uow import Database, Session
from yordamchi.personal.artefacts import domain as artefacts_domain
from yordamchi.personal.artefacts.domain import VideoLimits
from yordamchi.personal.deps import PersonalDeps
from yordamchi.ports.agent import ToolCall
from yordamchi.ports.git import Commit, GitStatus
from yordamchi.ports.mcp import McpTool
from yordamchi.ports.remote_shell import RemoteResult
from yordamchi.ports.speech import SpeechAudio, Transcript
from yordamchi.shared.access.domain import Arrival, ChatKind, EventType, OwnerOrigin
from yordamchi.shared.burst import domain as burst_domain
from yordamchi.shared.burst import store as burst_store
from yordamchi.shared.burst.domain import BurstPart, BurstStatus, CloseReason, RunningTurn, TurnInput, WindowSettings
from yordamchi.shared.cost.domain import Provider, Usage
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.health import store as health_store
from yordamchi.shared.health.models import HealthEvent
from yordamchi.shared.health.service import HealthState
from yordamchi.shared.inbox.domain import (
    Attachment,
    Content,
    ForwardSource,
    IncomingUpdate,
    StoredUpdate,
    UpdateKind,
    UpdateStatus,
)
from yordamchi.shared.outbox import service as outbox_service
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.outbox.models import OutboxMessage
from yordamchi.shared.outbox.service import SenderState
from yordamchi.shared.settings import defaults, store
from yordamchi.shared.settings import domain as settings_domain
from yordamchi.shared.settings.domain import PendingChange, Snapshot
from yordamchi.shared.settings.models import SettingVersion
from yordamchi.shared.settings.service import AskOwner
from yordamchi.shared.vault import service as vault_service
from yordamchi.shared.vault.domain import NewSecret, SecretRef

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
    forward: ForwardSource | None = None,
) -> IncomingUpdate:
    """An owner message in the bot's own chat, as a handler would build it (`forward`: he forwarded it)."""
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
        forward=forward,
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
async def send_outbox(deps: SharedDeps, state: SenderState | None = None) -> int:
    """One pass of the outbox sender (`run_sender` makes one every 0.25 s): whatever was queued and is due goes."""
    return await outbox_service.send_due(deps, state)


async def drain_outbox(
    deps: SharedDeps, clock: FakeClock, *, seconds: float, step_s: float = 0.5, state: SenderState | None = None
) -> int:
    """Passes of the sender across `seconds` of virtual time, `step_s` apart: retries, parked chats and spaced edits
    get their turn. Pass the `state` of earlier passes to keep their rate windows. Returns the calls made."""
    state = SenderState() if state is None else state
    calls = await outbox_service.send_due(deps, state)
    elapsed = 0.0
    while elapsed < seconds:
        await clock.advance(step_s)
        elapsed += step_s
        calls += await outbox_service.send_due(deps, state)
    return calls


async def add_outbox_row(
    db: Database,
    *,
    payload: JsonValue,
    at: datetime,
    op: str = "send_text",
    kind: str = "notice",
    chat_id: int = DEV_GROUP_ID,
    dedupe_key: str = "notice:raw:0",
    bot: BotName = BotName.DEV,
) -> int:
    """Write an `outbox_messages` row directly: no payload check, no scrubbing (rows an older or buggy writer left)."""
    async with db.uow() as s:
        row_id = await outbox_store.add_outbox_message(
            s,
            bot=bot.value,
            chat_id=chat_id,
            op=op,
            kind=kind,
            payload=payload,
            dedupe_key=dedupe_key,
            coalesce_key=None,
            priority=2,
            next_attempt_at=at,
            reply_to_message_id=None,
            disable_notification=False,
            project_id=None,
            turn_id=None,
            created_at=at,
        )
    assert row_id is not None
    return row_id


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
    forward: ForwardSource | None = None,
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
        content=Content(
            text=text, caption=caption, attachment=_attachment(update_id, part_kind, duration_s), forward=forward
        ),
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
GATE_SESSION_KEY: Final = "agent-session-7"


def make_gate_scope(
    root: Path,
    project: str = "BonviHR",
    *,
    policy: Policy = Policy.FULL,
    session_key: str = GATE_SESSION_KEY,
    turn_id: int | None = 31,
    dirty_before_turn: bool | None = False,
    turn_started_at: datetime | None = None,
) -> GateScope:
    """The scope `turns` would build for a session of `project` whose folder is `root / project`."""
    return GateScope(
        project_id=project,
        project_root=root / project,
        session_key=session_key,
        policy=policy,
        turn_id=turn_id,
        dirty_before_turn=dirty_before_turn,
        turn_started_at=turn_started_at,
    )


def make_bash_call(command: str, *, tool_use_id: str = "toolu_01", cwd: str | None = None) -> ToolCall:
    """A main-thread Bash call as the Claude guard hands it to `SessionSpec.guard`."""
    return ToolCall("Bash", {"command": command, "description": "x"}, tool_use_id, None, "sess-1", cwd=cwd)


async def make_decision(
    s: Session,
    *,
    at: datetime,
    project: str = "BonviHR",
    status: str = "pending",
    command: str = "ssh prod 'docker compose up -d'",
    gate_class: str | None = "server_write",
    ttl_min: int = 120,
) -> Decision:
    """A gate decision row about a Bash command, in `status` (inside the caller's unit of work)."""
    row = await gate_store.add_decision(
        s,
        kind="gate",
        project_id=project,
        turn_id=31,
        gate_class=gate_class,
        tool_name="Bash",
        input_sha256=gate_domain.input_sha256("Bash", {"command": command}),
        input_redacted=command,
        target="prod",
        payload={"reason": "remote_command"},
        created_at=at,
        expires_at=gate_domain.decision_expires_at(at, ttl_min),
    )
    row.status = status
    await s.flush()
    return row


# --- voice ---
# A second key of each speech kind (fake, real shape), for "a newer key replaces the old one".
SECOND_SPEECH_KEYS: Final[dict[str, str]] = {
    "google_api_key": "AIza" + "SyZ9y8X7w6V5u4T3s2R1q0P9o8N7m6L5k4J",
    "openrouter_api_key": "sk-or-v1-" + "fedcba9876543210" * 4,
}


async def store_secret(
    db: Database, keystore: InMemoryKeystore, kind: str, value: str, *, bot: BotName = BotName.DEV, update_id: int = 1
) -> SecretRef:
    """A secret the owner pasted, stored the way intake stores it (one unit of work, assigned to `bot`)."""
    origin = make_owner_origin(bot, update_id=update_id)
    async with db.uow() as s:
        [ref] = await vault_service.add_secrets(
            s, keystore, origin=origin, secrets=[NewSecret(kind=kind, value=value)], at=DEFAULT_START
        )
    return ref


def make_transcript(
    text: str = "BonviHR'da login sahifasini tuzat",
    *,
    provider: str = "google",
    model: str = "gemini-3.5-transcribe",
    audio_ms: int = 60_000,
    latency_ms: int = 1_800,
    cost_usd_micros: int | None = None,
    units: Mapping[str, int] | None = None,
) -> Transcript:
    """A provider's answer for one voice note (a 60 s note by default)."""
    return Transcript(
        text=text,
        provider=provider,
        provider_path="chat_completions" if provider == "openrouter" else "interactions",
        model=model,
        audio_ms=audio_ms,
        latency_ms=latency_ms,
        cost_usd_micros=cost_usd_micros,
        units=dict(units or {}),
    )


# --- routing ---
ROUTING_FIXTURES: Final = Path(__file__).parent / "fixtures" / "llm" / "routing"
# The owner's ~/Documents/Projects folders (read-only listing, 2026-10-24; `BONVI_SERVER.md` is a file, not a project).
OWNER_PROJECT_NAMES: Final = (
    "AI",
    "AI-Phone",
    "AIAgents",
    "AgentsRoom",
    "BS",
    "Bonvi-platform",
    "BonviAGI",
    "BonviCall",
    "BonviCall Clone Archive",
    "BonviFinance",
    "BonviGo",
    "BonviHR",
    "BonviImport",
    "BonviImzo",
    "BrokerApp",
    "ClaudeManager",
    "FactoryIncome",
    "GasStation",
    "HRMentor",
    "HRMentor-wt-agent-api",
    "ImportAgents",
    "MegaPlus ERP",
    "MegaPlus Platform",
    "NUR-Project",
    "OvozYoz",
    "PrivateBusiness",
    "Russian-Speaking",
    "TezTel",
    "Wooden",
    "WunderkindAIAgent",
    "WunderkindEmployeesBot",
    "WunderkindLC",
    "WunderkindLMS",
    "WunderkindOlympics",
    "WunderkindTestPortal",
    "Yordamchi",
    "zinnur-v2",
)


def make_project_views(
    *names: str,
    at: datetime = DEFAULT_START,
    aliases: Mapping[str, str] | None = None,
    descriptions: Mapping[str, str] | None = None,
    archived: tuple[str, ...] = (),
    missing: tuple[str, ...] = (),
) -> tuple[Project, ...]:
    """Registry views as routing sees them: `*-wt-*` names are worktrees, `… Archive` names archived (both answer
    only to their exact name); `aliases` maps alias → project, as `projects.aliases` does."""
    prefs = ProjectPrefs(aliases=aliases or {}, descriptions=descriptions or {}, archived=list(archived))
    return tuple(
        project_view(
            make_project_record(
                name,
                at=at,
                is_worktree="-wt-" in name,
                is_archive_name=is_archive_name(name),
                missing_since=at if name in missing else None,
            ),
            prefs,
        )
        for name in names
    )


async def add_registered_projects(
    s: Session, *names: str, at: datetime = DEFAULT_START, descriptions: Mapping[str, str] | None = None
) -> None:
    """`projects` rows as the scan writes them, flags inferred from the names as in `make_project_views`."""
    for name in names:
        record = make_project_record(
            name,
            at=at,
            description=(descriptions or {}).get(name, ""),
            is_worktree="-wt-" in name,
            is_archive_name=is_archive_name(name),
        )
        await projects_store.set_project(s, record)


def make_voice_part(update_id: int, *, at_s: float, transcript: str, duration_s: int = 5) -> BurstPart:
    """A voice note whose transcript has arrived (`shared/voice`), as the burst holds it at close."""
    part = make_part(update_id, at_s=at_s, kind=UpdateKind.VOICE, duration_s=duration_s)
    return replace(
        part,
        transcript=burst_domain.TranscriptState.DONE,
        transcript_text=transcript,
        transcript_at=part.at + timedelta(seconds=1),
    )


def make_turn_input(*parts: BurstPart, burst_id: int = 1, reason: CloseReason = CloseReason.QUIET) -> TurnInput:
    """A closed burst of `parts` (a text "salom" when none), as routing receives it."""
    chosen = parts or (make_part(text="salom"),)
    last = max(part.at for part in chosen)
    return burst_domain.turn_input(
        burst_id, DEV_GROUP_ID, chosen, opened_at=chosen[0].at, closed_at=last, reason=reason
    )


async def make_closed_burst(
    s: Session, *parts: BurstPart, bot: BotName = BotName.DEV, reason: CloseReason = CloseReason.QUIET
) -> TurnInput:
    """A `closed` burst row holding `parts`, and the turn input the router reads for it."""
    chosen = parts or (make_part(text="salom"),)
    last = max(part.at for part in chosen)
    row = await burst_store.add_burst(
        s,
        bot=bot.value,
        chat_id=DEV_GROUP_ID if bot is BotName.DEV else OWNER_ID,
        status=BurstStatus.CLOSED.value,
        opened_at=chosen[0].at,
        last_part_at=last,
        closes_at=last,
        closed_at=last,
        close_reason=reason.value,
        input_json=burst_domain.parts_json(chosen, str),
    )
    return burst_domain.turn_input(row.id, row.chat_id, chosen, opened_at=chosen[0].at, closed_at=last, reason=reason)


def make_running_turn(
    project_id: str, *, last_part_at: datetime = DEFAULT_START, message_ids: frozenset[int] = frozenset()
) -> RunningTurn:
    return RunningTurn(last_part_at=last_part_at, project_id=project_id, message_ids=message_ids)


def make_activity(project_id: str, *, at: datetime, kind: ActivityKind = ActivityKind.SPOKEN) -> Activity:
    return Activity(project_id=project_id, at=at, kind=kind)


def routing_reply(
    project_id: str | None,
    confidence: float,
    *,
    intent: str = "instruct_project",
    candidates: Mapping[str, float] | None = None,
    needs_fresh: bool = False,
    is_new_project: bool = False,
) -> dict[str, JsonValue]:
    """A structured routing answer of the `routing.domain.OUTPUT_SCHEMA` shape (queue it on `FakeAssistant`)."""
    others: list[JsonValue] = [{"project_id": name, "confidence": value} for name, value in (candidates or {}).items()]
    return {
        "intent": intent,
        "project_id": project_id,
        "candidates": others,
        "confidence": confidence,
        "needs_fresh": needs_fresh,
        "is_new_project": is_new_project,
    }


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


async def list_setting_rows(db: Database, key: str) -> list[SettingVersion]:
    """Every `setting_versions` row of `key`, newest first."""
    async with db.read() as s:
        return await store.list_setting_history(s, key)


async def list_replies(db: Database, bot: BotName = BotName.DEV) -> list[OutboxMessage]:
    """Every queued outbox row of `bot`, oldest first (the settings replies are `report` rows)."""
    async with db.read() as s:
        return await outbox_store.list_outbox_messages(s, bot.value)


class RecordingAsker:
    """A `settings.service.AskOwner` that records each question and numbers it (the personal bot's conversational
    confirm is P3; the dev bot's asker is `gate.service.ask_setting_change`)."""

    def __init__(self) -> None:
        self.asked: list[PendingChange] = []

    async def __call__(self, change: PendingChange) -> int:
        self.asked.append(change)
        return len(self.asked)


def gate_asker(deps: DevDeps) -> AskOwner:
    """The dev bot's asker, as `bots/dev/app.py` binds it."""
    return partial(gate_service.ask_setting_change, deps)


# --- turns ---
def make_turn_request(
    project: str = "BonviHR",
    text: str = "Login sahifasini tuzat",
    *,
    kind: TurnKind = TurnKind.INSTRUCT,
    merge: bool = False,
    burst_id: int | None = None,
    message_ids: tuple[int, ...] = (),
    last_part_at: datetime | None = None,
) -> TurnRequest:
    """A routed instruction as routing hands it to `turns.service.submit`."""
    return TurnRequest(
        project_id=project,
        text=text,
        kind=kind,
        merge=merge,
        burst_id=burst_id,
        message_ids=message_ids,
        last_part_at=last_part_at,
    )


async def make_turn(
    s: Session,
    project: str = "BonviHR",
    *,
    at: datetime = DEFAULT_START,
    status: TurnStatus = TurnStatus.QUEUED,
    kind: TurnKind = TurnKind.INSTRUCT,
    text: str = "Login sahifasini tuzat",
    delivery_status: str = "pending",
    mode: DeliveryMode = DeliveryMode.NEW_TURN,
) -> Turn:
    """A `turns` row in `status` with its first delivery in `delivery_status` (rows a crash may leave behind)."""
    turn = await turns_store.add_turn(
        s, project_id=project, kind=kind.value, status=status.value, title=text[:120], burst_id=None, at=at
    )
    delivery = await turns_store.add_delivery(
        s,
        turn_id=turn.id,
        mode=mode.value,
        source=DeliverySource.OWNER.value,
        text=text,
        media={"images": [], "message_ids": [], "media_group_ids": [], "last_part_at": None},
        burst_id=None,
        at=at,
    )
    assert delivery is not None
    if delivery_status != "pending":
        delivery.status = delivery_status
    if status is not TurnStatus.QUEUED:
        turn.started_at = at
    await s.flush()
    return turn


def write_transcript(home: Path, session_id: str, *, folder: str = "-Users-owner-Documents-Projects-BonviHR") -> Path:
    """An empty `~/.claude/projects/<folder>/<session_id>.jsonl`: the resume pre-check finds the session by name."""
    path = home / ".claude" / "projects" / folder / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


# --- questions ---
DEFAULT_OPTIONS: Final = (("PostgreSQL", "ishonchli, production uchun"), ("SQLite", "oddiy, bitta fayl"))


def agent_question(
    text: str = "Qaysi bazadan foydalanamiz?",
    options: Sequence[tuple[str, str]] = DEFAULT_OPTIONS,
    *,
    header: str = "Baza",
    multi_select: bool = False,
) -> dict[str, JsonValue]:
    """One `AskUserQuestion` question as the agent sends it (an item of `QuestionAsked.questions`)."""
    choices: list[JsonValue] = [{"label": label, "description": description} for label, description in options]
    return {"question": text, "header": header, "options": choices, "multiSelect": multi_select}


async def make_question(
    s: Session,
    *,
    turn_id: int,
    project: str = "BonviHR",
    tool_use_id: str = "toolu_q1",
    questions: Sequence[Mapping[str, JsonValue]] | None = None,
    at: datetime = DEFAULT_START,
    status: QuestionStatus = QuestionStatus.OPEN,
) -> int:
    """A `questions` row as `open_question` writes it, without its message (inside the caller's unit of work)."""
    asked = questions_domain.parse_questions(questions or (agent_question(),), str)
    row_id = await questions_store.add_question(
        s,
        project_id=project,
        turn_id=turn_id,
        tool_use_id=tool_use_id,
        questions=questions_domain.questions_json(asked),
        status=status.value,
        asked_at=at,
    )
    assert row_id is not None
    return row_id


# --- digest ---
def make_digest_state(turns: TurnsState | None = None) -> DigestState:
    """The dev bot's digest state, as `bots/dev/app.py` builds it next to its `TurnsState`."""
    return DigestState(turns=turns if turns is not None else TurnsState())


def make_report_question(
    text: str = "BonviHR qanday?",
    project: str | None = "BonviHR",
    *,
    intent: Intent | None = None,
    needs_fresh: bool = False,
    burst_id: int | None = None,
) -> ReportQuestion:
    """A report question as routing hands it over (`report_project` with a project, `report_all` without)."""
    return ReportQuestion(
        text=text,
        project_id=project,
        intent=intent or (Intent.REPORT if project is not None else Intent.REPORT_ALL),
        needs_fresh=needs_fresh,
        burst_id=burst_id,
    )


def script_git(
    git: FakeGit,
    folder: Path,
    *,
    head: str | None = "a1b2c3d4e5f6",
    commits: tuple[Commit, ...] = (),
    branch: str | None = "main",
    upstream: str | None = "origin/main",
    ahead: int = 0,
    behind: int = 0,
    changed: int = 0,
    untracked: int = 0,
) -> None:
    """What `git status` and `git log` say for one repo; the head commit is the first of `commits` if it is one."""
    git.statuses[folder] = GitStatus(branch, head, upstream, ahead, behind, changed, untracked)
    git.heads[folder] = head
    git.logs[folder] = list(commits)
    if head is not None and commits and commits[0].sha == head:
        git.commits[(folder, "HEAD")] = commits[0]


def make_turn_summary(
    turn_id: int = 1,
    project: str = "BonviHR",
    *,
    status: TurnStatus = TurnStatus.RUNNING,
    kind: TurnKind = TurnKind.INSTRUCT,
    title: str = "Login sahifasini tuzat",
    queued_at: datetime = DEFAULT_START,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
    natija: str | None = None,
    tests: str | None = None,
) -> TurnSummary:
    return TurnSummary(
        turn_id=turn_id,
        project_id=project,
        kind=kind,
        status=status,
        title=title,
        queued_at=queued_at,
        started_at=started_at,
        ended_at=ended_at,
        natija=natija,
        tests=tests,
    )


def make_server_check(host: str = "web", *, at: datetime, ok: bool = True, summary: str = "") -> ServerCheck:
    return ServerCheck(host=host, at=at, ok=ok, summary=summary)


# --- servers ---
SERVER_HOST: Final = "bonvi-srv"  # the Bonvi VM: reached through the jump-only Windows host (BONVI_SERVER.md)
JUMP_HOST: Final = "server"  # `gate.jump_only_hosts` default: production 1C, never a target
SAMPLE_UPTIME: Final = " 10:15:01 up 12 days,  3:04,  1 user,  load average: 0.52, 0.48, 0.40\n"
SAMPLE_DF: Final = (
    "Filesystem      Size  Used Avail Use% Mounted on\n"
    "tmpfs           1.2G  1.6M  1.2G   1% /run\n"
    "/dev/sda2       147G  118G   22G  85% /\n"
    "tmpfs           5.9G     0  5.9G   0% /dev/shm\n"
    "/dev/sdb1       880G   70G  766G   9% /srv/data\n"
    "overlay         147G  118G   22G  85% /var/lib/docker/overlay2/3f2a/merged\n"
    "/dev/loop0       64M   64M     0 100% /snap/core20/2318\n"
)
SAMPLE_FREE: Final = (
    "               total        used        free      shared  buff/cache   available\n"
    "Mem:           11950        5123        1234         123        5600        6500\n"
    "Swap:           2047           0        2047\n"
)
SAMPLE_CONTAINERS: Final = (
    ("1a2b3c4d5e6f", "hrmentor-api:latest", '"uvicorn app:app --h…"', "2 hours ago", "Up 2 hours (healthy)",
     "127.0.0.1:8290->80/tcp", "hrmentor-api-1"),
    ("2b3c4d5e6f7a", "postgres:16", '"docker-entrypoint.s…"', "3 days ago", "Up 3 days", "5432/tcp",
     "hrmentor-postgres-1"),
)  # fmt: skip


def docker_ps_table(rows: tuple[tuple[str, ...], ...]) -> str:
    """`docker ps` output as the docker CLI pads it (Go tabwriter: each column as wide as its widest cell + 3)."""
    header = ("CONTAINER ID", "IMAGE", "COMMAND", "CREATED", "STATUS", "PORTS", "NAMES")
    table = [header, *rows]
    widths = [max(len(row[index]) for row in table) for index in range(len(header) - 1)]
    lines = [
        "".join(cell.ljust(width + 3) for cell, width in zip(row, widths, strict=False)) + row[-1] for row in table
    ]
    return "\n".join(lines) + "\n"


def remote_ok(host: str, stdout: str, *, exit_code: int = 0) -> RemoteResult:
    return RemoteResult(host=host, exit_code=exit_code, stdout=stdout, stderr="", duration_ms=40)


def script_healthy_server(
    shell: FakeRemoteShell,
    host: str = SERVER_HOST,
    *,
    df: str = SAMPLE_DF,
    containers: tuple[tuple[str, ...], ...] = SAMPLE_CONTAINERS,
    failed_units: str = "",
) -> None:
    """Every probe of a check answers like a healthy Ubuntu VM (`domain.plan_probes` commands)."""
    shell.respond(host, servers_domain.UPTIME_COMMAND, remote_ok(host, SAMPLE_UPTIME))
    shell.respond(host, servers_domain.DISK_COMMAND, remote_ok(host, df))
    shell.respond(host, servers_domain.MEMORY_COMMAND, remote_ok(host, SAMPLE_FREE))
    shell.respond(host, servers_domain.CONTAINERS_COMMAND, remote_ok(host, docker_ps_table(containers)))
    shell.respond(host, servers_domain.UNITS_COMMAND, remote_ok(host, failed_units))


def df_with_root_at(pct: int) -> str:
    """`SAMPLE_DF` with `/` at `pct` percent."""
    return SAMPLE_DF.replace(" 85% /\n", f" {pct}% /\n", 1)


async def list_server_checks(db: Database, host: str | None = None) -> list[ServerCheckRow]:
    """Every `server_checks` row (of one host), oldest first."""
    query = select(ServerCheckRow).order_by(ServerCheckRow.id)
    if host is not None:
        query = query.where(ServerCheckRow.host == host)
    async with db.read() as s:
        return list((await s.scalars(query)).all())


# --- health ---
HEALTH_VERSION: Final = "2026.10.24-abcdef0"
HEALTH_SDK_VERSION: Final = "0.2.173"
HEALTH_PID: Final = 4242


def make_health_state(
    clock: FakeClock,
    *,
    version: str = HEALTH_VERSION,
    sdk_version: str = HEALTH_SDK_VERSION,
    degraded: tuple[str, ...] = (),
    pid: int = HEALTH_PID,
) -> HealthState:
    """The state `health.new_state` builds at start, with a fixed pid (the test process's is not a bot's)."""
    return HealthState(version=version, started_at=clock.now(), pid=pid, sdk_version=sdk_version, degraded=degraded)


async def list_health_events(db: Database, bot: BotName = BotName.DEV) -> list[HealthEvent]:
    """Every `health_events` row of `bot`, oldest first."""
    async with db.read() as s:
        return list(reversed(await health_store.list_health_events(s, bot.value, limit=1000)))


async def add_health_event(
    db: Database, *, at: datetime, kind: str = "started", bot: BotName = BotName.DEV, data: JsonValue = None
) -> None:
    async with db.uow() as s:
        await health_store.add_health_event(s, bot=bot.value, at=at, kind=kind, data=data or {})


def make_media_dir(paths: Paths, name: str, *, files: int = 1) -> Path:
    """A `media/<name>/` cache folder holding `files` small files."""
    folder = paths.media / name
    folder.mkdir(parents=True, exist_ok=True)
    for index in range(files):
        (folder / f"file-{index}.bin").write_bytes(b"x")
    return folder


# --- wiring (bots) ---


# --- artefacts ---
def make_video_limits(**overrides: int) -> VideoLimits:
    """The `web.video_*` defaults as one pass reads them, with fields replaced by name."""
    return replace(artefacts_domain.video_limits(make_default_snapshot(BotName.PERSONAL)), **overrides)


# --- voice_tts ---
TTS_PROVIDERS: Final = ("openrouter", "google", "edge")


def make_speech_audio(
    *,
    provider: str = "openrouter",
    model: str = "google/gemini-3.8-flash-tts",
    duration_ms: int | None = 24_000,
    data: bytes = b"OggS synthetic opus note",
    cost_usd_micros: int | None = None,
    units: Mapping[str, int] | None = None,
) -> SpeechAudio:
    """One finished voice note as an adapter returns it (a 24 s note by default)."""
    return SpeechAudio(
        data=data,
        mime="audio/ogg",
        provider=provider,
        model=model,
        duration_ms=duration_ms,
        cost_usd_micros=cost_usd_micros,
        units=dict(units if units is not None else {"characters": 380, "requests": 1}),
    )


def make_synthesizers(clock: FakeClock | None = None) -> dict[str, FakeSynthesizer]:
    """One scripted fake per TTS provider, keyed as `SharedDeps.synthesizers` expects."""
    return {provider: FakeSynthesizer(provider=provider, clock=clock) for provider in TTS_PROVIDERS}


# --- integrations ---
INTEGRATION_URL: Final = "https://hr.bonvi.example.uz/mcp"
INTEGRATION_OAUTH_URL: Final = "https://lc.wunderkind.example.uz/mcp"
INTEGRATION_TOKEN: Final = "bonvi-hr-token-" + "Q7v9Lr2Tz8Wn4Kp6"


def make_mcp_tools() -> list[McpTool]:
    """One read-only and one write tool, as the owner's HR server exposes them."""
    return [
        McpTool("employees_list", "List employees", read_only=True),
        McpTool("salary_set", "Change a salary", read_only=False),
    ]


async def vault_secret(deps: SharedDeps, kind: str, value: str, *, update_id: int = 900) -> SecretRef:
    """A key the owner pasted, as intake vaults it before any feature sees the message (SPEC §3.2.1 step 3)."""
    origin = make_owner_origin(deps.bot, update_id=update_id)
    async with deps.db.uow() as s:
        (ref,) = await vault_service.add_secrets(
            s, deps.keystore, origin=origin, secrets=[NewSecret(kind=kind, value=value)], at=deps.clock.now()
        )
    return ref


def outbox_text(row: OutboxMessage) -> str:
    assert isinstance(row.payload_json, dict)
    text = row.payload_json.get("text")
    assert isinstance(text, str)
    return text
