"""UC-01 acceptance at the Bot API edge: both dispatchers via `Dispatcher.feed_update`, with the real
`TelegramMessenger` on the recording aiogram session (CONVENTIONS §11; SPEC §3.2.1 step 2; NFR-SEC-01).

A dropped update must produce 0 outgoing requests and 0 inbox rows, and reach no handler. A foreign chat produces
exactly one request, `leaveChat`. The handlers here stand in for `bots/<bot>/telegram.py` (T34): each turns the event
into an `IncomingUpdate` and awaits only `inbox.service.accept_update` (A7), so an admitted update visibly writes a row
and acknowledges, which proves the harness would see a leak.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.dispatcher.middlewares.error import ErrorsMiddleware
from aiogram.dispatcher.middlewares.user_context import UserContextMiddleware
from aiogram.types import CallbackQuery, ChatMemberUpdated, InlineQuery, Message, Update

from tests.factories import (
    DEV_BOT_TOKEN,
    DEV_GROUP_ID,
    FOREIGN_GROUP_ID,
    OWNER_ID,
    PERSONAL_BOT_TOKEN,
    PERSONAL_GROUP_ID,
    STRANGER_ID,
    TelegramJson,
    send_outbox,
    tg_callback,
    tg_inline_query,
    tg_member_change,
    tg_message,
)
from tests.fakes.telegram_session import FIXTURES, RecordingSession
from yordamchi.adapters.telegram.messenger import TelegramMessenger
from yordamchi.bots import common
from yordamchi.bots.common import OwnerOnly
from yordamchi.core import strings as S
from yordamchi.core.errors import SettingUnknown
from yordamchi.core.log import JsonFormatter
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.access import service as access
from yordamchi.shared.access.domain import OwnerOrigin
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import IncomingUpdate, UpdateKind
from yordamchi.shared.inbox.models import InboundUpdate

SECRET_TEXT = "maxfiy matn: buni hech kim ko'rmasin"


@dataclass
class Edge:
    """One bot's edge: its deps (with the real Telegram adapter), dispatcher, aiogram bot and recording session."""

    deps: SharedDeps
    dispatcher: Dispatcher
    bot: Bot
    session: RecordingSession
    reached: list[str]

    async def feed(self, raw: TelegramJson) -> object:
        update = Update.model_validate(raw, context={"bot": self.bot})
        return await self.dispatcher.feed_update(self.bot, update)

    async def rows(self) -> list[InboundUpdate]:
        async with self.deps.db.read() as s:
            return await inbox_store.list_inbound_updates(s, self.deps.bot.value)

    def methods(self) -> list[str]:
        return [request.api_method for request in self.session.requests]


def _incoming(bot: BotName, origin: OwnerOrigin, message: Message, kind: UpdateKind) -> IncomingUpdate:
    return IncomingUpdate(
        bot=bot,
        update_id=origin.update_id,
        chat_id=message.chat.id,
        kind=kind,
        user_id=origin.user_id,
        message_id=message.message_id,
        text=message.text,
    )


def _intake_router(deps: SharedDeps, reached: list[str]) -> Router:
    """What T34's `telegram.py` will hold: translate, one service call, return. Every handler records that it ran,
    and `origin` is optional here (T34's will require it), so a leak past `OwnerOnly` shows even where nothing would be
    sent or stored."""
    router = Router()

    @router.message()
    async def on_message(message: Message, origin: OwnerOrigin | None = None) -> None:
        reached.append("message" if origin else "message_without_origin")
        if origin:
            await inbox.accept_update(deps, origin, _incoming(deps.bot, origin, message, UpdateKind.TEXT))

    @router.edited_message()
    async def on_edit(message: Message, origin: OwnerOrigin | None = None) -> None:
        reached.append("edited_message" if origin else "edited_message_without_origin")
        if origin:
            await inbox.accept_update(deps, origin, _incoming(deps.bot, origin, message, UpdateKind.EDITED))

    @router.my_chat_member()
    async def on_member(change: ChatMemberUpdated, origin: OwnerOrigin | None = None) -> None:
        reached.append("my_chat_member" if origin else "my_chat_member_without_origin")
        if not origin:
            return
        member = IncomingUpdate(
            bot=deps.bot,
            update_id=origin.update_id,
            chat_id=change.chat.id,
            kind=UpdateKind.MEMBER,
            user_id=origin.user_id,
        )
        await inbox.accept_update(deps, origin, member)

    @router.channel_post()
    @router.edited_channel_post()
    async def on_post(post: Message) -> None:
        reached.append("channel_post")

    @router.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        reached.append("callback_query")

    @router.inline_query()
    async def on_inline(query: InlineQuery) -> None:
        reached.append("inline_query")

    return router


def _edge(deps: SharedDeps, token: str, *routers: Router) -> Edge:
    session = RecordingSession()
    messenger = TelegramMessenger(token, session=session)
    wired = replace(deps, messenger=messenger)
    reached: list[str] = []
    dispatcher = common.build_dispatcher(wired, _intake_router(wired, reached), *routers)
    return Edge(wired, dispatcher, messenger.bot, session, reached)


@pytest.fixture
def dev(dev_deps: DevDeps) -> Edge:
    return _edge(dev_deps, DEV_BOT_TOKEN)


@pytest.fixture
def personal(personal_deps: PersonalDeps) -> Edge:
    return _edge(personal_deps, PERSONAL_BOT_TOKEN)


def _fixture(name: str) -> TelegramJson:
    raw: dict[str, Any] = json.loads((FIXTURES / "updates" / f"{name}.json").read_text(encoding="utf-8"))
    return raw


async def _assert_nothing_happened(edge: Edge) -> None:
    assert edge.methods() == []
    assert await edge.rows() == []
    assert edge.reached == []


# --- strangers and the owner elsewhere: nothing at all --------------------------------------------------------------
DEV_DROPPED: dict[str, Callable[[], TelegramJson]] = {
    "stranger_dm": lambda: tg_message(1, user_id=STRANGER_ID, chat_id=STRANGER_ID, text=SECRET_TEXT),
    "stranger_command_dm": lambda: tg_message(2, user_id=STRANGER_ID, chat_id=STRANGER_ID, text="/start"),
    "stranger_in_the_dev_group": lambda: tg_message(3, user_id=STRANGER_ID, text=SECRET_TEXT),
    "stranger_edit_in_the_dev_group": lambda: tg_message(4, user_id=STRANGER_ID, field="edited_message"),
    "owner_in_the_dev_bots_dm": lambda: tg_message(5, chat_id=OWNER_ID),
    "owner_edit_in_the_dev_bots_dm": lambda: tg_message(6, chat_id=OWNER_ID, field="edited_message"),
    "channel_post_without_sender_in_the_dev_group": lambda: tg_message(7, user_id=None, field="channel_post"),
    "owner_callback_in_the_dev_group": lambda: tg_callback(8, user_id=OWNER_ID, chat_id=DEV_GROUP_ID),
    "stranger_callback_without_message": lambda: tg_callback(9, user_id=STRANGER_ID),
    "stranger_inline_query": lambda: tg_inline_query(10, user_id=STRANGER_ID),
    "owner_inline_query": lambda: tg_inline_query(11, user_id=OWNER_ID),
    "stranger_starts_the_bot": lambda: tg_member_change(12, user_id=STRANGER_ID, chat_id=STRANGER_ID),
    "stranger_promotes_the_bot_in_the_dev_group": lambda: tg_member_change(
        13, user_id=STRANGER_ID, chat_id=DEV_GROUP_ID, old="member", new="administrator"
    ),
    "bot_kicked_from_a_foreign_group": lambda: _fixture("my_chat_member_kicked"),
}


@pytest.mark.parametrize("case", DEV_DROPPED, ids=str)
async def test_the_dev_bot_drops_it_silently(dev: Edge, case: str) -> None:
    await dev.feed(DEV_DROPPED[case]())
    await _assert_nothing_happened(dev)


async def test_a_stranger_is_logged_by_numeric_ids_and_never_by_content(
    dev: Edge, caplog: pytest.LogCaptureFixture
) -> None:
    formatter = JsonFormatter(process="dev", version="test")
    with caplog.at_level(logging.DEBUG):
        await dev.feed(tg_message(20, user_id=STRANGER_ID, chat_id=STRANGER_ID, text=SECRET_TEXT))
    lines = [formatter.format(record) for record in caplog.records]
    [dropped] = [json.loads(line) for line in lines if '"event": "update_dropped"' in line]
    assert (dropped["user_id"], dropped["chat_id"], dropped["reason"]) == (STRANGER_ID, STRANGER_ID, "stranger")
    assert all(SECRET_TEXT not in line and "Stranger" not in line for line in lines)
    await _assert_nothing_happened(dev)


# --- foreign chats: left at once, nothing posted --------------------------------------------------------------------
# case → (the chat that must be left, the update)
DEV_LEFT: dict[str, tuple[int, Callable[[], TelegramJson]]] = {
    "added_to_a_foreign_group": (-1009876543210, lambda: _fixture("my_chat_member_added_to_foreign_group")),
    "promoted_in_a_foreign_group": (
        FOREIGN_GROUP_ID,
        lambda: tg_member_change(30, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID, old="member", new="administrator"),
    ),
    "stranger_in_a_foreign_group": (
        FOREIGN_GROUP_ID,
        lambda: tg_message(31, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID),
    ),
    "owner_in_a_foreign_group": (FOREIGN_GROUP_ID, lambda: tg_message(32, chat_id=FOREIGN_GROUP_ID)),
    "owner_edit_in_a_foreign_group": (
        FOREIGN_GROUP_ID,
        lambda: tg_message(33, chat_id=FOREIGN_GROUP_ID, field="edited_message"),
    ),
    "owner_in_a_foreign_basic_group": (-4004004, lambda: tg_message(34, chat_id=-4004004, chat_type="group")),
    "channel_post": (
        FOREIGN_GROUP_ID,
        lambda: tg_message(35, user_id=None, chat_id=FOREIGN_GROUP_ID, chat_type="channel", field="channel_post"),
    ),
    "edited_channel_post": (
        FOREIGN_GROUP_ID,
        lambda: tg_message(
            36, user_id=None, chat_id=FOREIGN_GROUP_ID, chat_type="channel", field="edited_channel_post"
        ),
    ),
    "owner_callback_in_a_foreign_group": (
        FOREIGN_GROUP_ID,
        lambda: tg_callback(37, user_id=OWNER_ID, chat_id=FOREIGN_GROUP_ID),
    ),
    "the_personal_bots_group": (PERSONAL_GROUP_ID, lambda: tg_message(38, chat_id=PERSONAL_GROUP_ID)),
}


@pytest.mark.parametrize("case", DEV_LEFT, ids=str)
async def test_the_dev_bot_leaves_a_foreign_chat_and_posts_nothing_there(dev: Edge, case: str) -> None:
    chat_id, update = DEV_LEFT[case]
    await dev.feed(update())

    [request] = dev.session.requests
    assert (request.api_method, request.params) == ("leaveChat", {"chat_id": chat_id})
    assert request.timeout is not None
    assert request.timeout <= 10  # UC-01: left within 10 s
    assert await dev.rows() == []
    assert dev.reached == []


async def test_a_foreign_group_that_writes_again_is_left_again(dev: Edge) -> None:
    await dev.feed(tg_message(40, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID))
    await dev.feed(tg_message(41, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID))
    assert dev.methods() == ["leaveChat", "leaveChat"]


# --- the owner in the bot's own chat: admitted (the harness sees a real intake) -------------------------------------
async def test_the_owner_in_the_dev_group_reaches_the_inbox_and_is_acknowledged(dev: Edge) -> None:
    await dev.feed(tg_message(50, text="BonviHR'da login sahifasini tuzat"))

    assert dev.reached == ["message"]
    [row] = await dev.rows()
    assert (row.update_id, row.chat_id, row.user_id, row.kind, row.status) == (
        50,
        DEV_GROUP_ID,
        OWNER_ID,
        "text",
        "received",
    )
    await send_outbox(dev.deps)  # acks are durable outbox rows since T15; one sender pass delivers them
    assert sorted(dev.methods()) == ["sendChatAction"]  # `ui.seen_reaction` is empty by default: typing is the ack
    dev.session.assert_no_reply_markup()


async def test_the_owners_edit_and_membership_change_are_admitted(dev: Edge) -> None:
    await dev.feed(tg_message(51, field="edited_message"))
    await dev.feed(tg_member_change(52, user_id=OWNER_ID, chat_id=DEV_GROUP_ID, old="member", new="administrator"))

    assert dev.reached == ["edited_message", "my_chat_member"]
    assert [(row.update_id, row.kind, row.status) for row in await dev.rows()] == [
        (51, "edited", "received"),
        (52, "member", "ignored"),
    ]


# --- the personal bot -----------------------------------------------------------------------------------------------
PERSONAL_DROPPED: dict[str, Callable[[], TelegramJson]] = {
    "stranger_dm": lambda: tg_message(60, user_id=STRANGER_ID, chat_id=STRANGER_ID, text=SECRET_TEXT),
    "stranger_in_the_personal_group": lambda: tg_message(61, user_id=STRANGER_ID, chat_id=PERSONAL_GROUP_ID),
    "stranger_callback_in_the_dm": lambda: tg_callback(62, user_id=STRANGER_ID, chat_id=STRANGER_ID),
    "owner_callback_in_the_dm": lambda: tg_callback(63, user_id=OWNER_ID, chat_id=OWNER_ID),
}


@pytest.mark.parametrize("case", PERSONAL_DROPPED, ids=str)
async def test_the_personal_bot_drops_it_silently(personal: Edge, case: str) -> None:
    await personal.feed(PERSONAL_DROPPED[case]())
    await _assert_nothing_happened(personal)


async def test_the_personal_bot_leaves_any_other_group(personal: Edge) -> None:
    for update_id, chat_id in ((70, DEV_GROUP_ID), (71, FOREIGN_GROUP_ID)):
        await personal.feed(tg_message(update_id, chat_id=chat_id))
    assert [(r.api_method, r.params["chat_id"]) for r in personal.session.requests] == [
        ("leaveChat", DEV_GROUP_ID),
        ("leaveChat", FOREIGN_GROUP_ID),
    ]
    assert await personal.rows() == []


async def test_the_personal_bot_admits_the_owner_in_the_dm_and_in_its_group(personal: Edge) -> None:
    await personal.feed(tg_message(80, chat_id=OWNER_ID))
    await personal.feed(tg_message(81, chat_id=PERSONAL_GROUP_ID))
    assert [(row.update_id, row.chat_id) for row in await personal.rows()] == [(80, OWNER_ID), (81, PERSONAL_GROUP_ID)]
    assert personal.reached == ["message", "message"]


# --- failures: told to the owner, never a crash ---------------------------------------------------------------------
def _failing_router(error: Exception) -> Router:
    router = Router()

    @router.message()
    async def broken(message: Message, origin: OwnerOrigin) -> None:
        raise error

    return router


async def test_a_failed_intake_is_told_to_the_owner_in_that_chat(dev_deps: DevDeps) -> None:
    session = RecordingSession()
    messenger = TelegramMessenger(DEV_BOT_TOKEN, session=session)
    wired = replace(dev_deps, messenger=messenger)
    dispatcher = common.build_dispatcher(wired, _failing_router(SettingUnknown(key="burst.nope")))
    edge = Edge(wired, dispatcher, messenger.bot, session, [])

    await edge.feed(tg_message(90))
    [sent] = session.requests
    assert (sent.api_method, sent.params["chat_id"]) == ("sendMessage", DEV_GROUP_ID)
    assert sent.params["text"] == S.ERR_SETTING_UNKNOWN(key="burst.nope")


async def test_an_error_in_the_access_check_is_logged_and_handled(
    dev: Edge, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(deps: SharedDeps, arrival: object) -> object:
        msg = "settings table is gone"
        raise RuntimeError(msg)

    monkeypatch.setattr(access, "admit_update", broken)
    with caplog.at_level(logging.ERROR):
        handled = await dev.feed(tg_message(91))

    assert handled is True  # `@dp.errors()` reports it handled, so polling goes on
    [record] = [record for record in caplog.records if record.getMessage() == "update_failed"]
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    await _assert_nothing_happened(dev)  # fail closed: no handler ran


def test_owner_only_is_the_first_middleware_of_ours_and_aiogram_keeps_no_fsm(dev: Edge) -> None:
    errors, user_context, owner_only = dev.dispatcher.update.outer_middleware  # no FSMContextMiddleware
    assert isinstance(errors, ErrorsMiddleware)
    assert isinstance(user_context, UserContextMiddleware)  # reads ids only
    assert isinstance(owner_only, OwnerOnly)
