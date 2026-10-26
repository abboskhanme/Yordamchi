"""The `Messenger` contract, run against `FakeMessenger` and `TelegramMessenger` (CONVENTIONS §4, §11).

The real adapter runs twice: offline on the recording aiogram session (always), and against the real Bot API
(`-m live`, opt-in). The live run needs a test bot and a chat it may write to, pin in and delete from:

    YORDAMCHI_LIVE_TELEGRAM_TOKEN=<test bot token> YORDAMCHI_LIVE_TELEGRAM_CHAT_ID=<chat id> uv run pytest -m live

Without both variables the live class is skipped.
"""

import inspect
import os
from collections.abc import AsyncIterator

import pytest

from tests.fakes.messenger import FakeMessenger
from tests.fakes.telegram_session import RecordingSession
from yordamchi.adapters.telegram.messenger import TelegramMessenger
from yordamchi.core import strings as S
from yordamchi.core.errors import TopicClosed, TopicGone, TopicsUnavailable
from yordamchi.ports.messenger import ChatAction, Messenger

CHAT = -1001234567890
TIMEOUT = 5.0
LIVE_TOKEN_VAR = "YORDAMCHI_LIVE_TELEGRAM_TOKEN"
LIVE_CHAT_VAR = "YORDAMCHI_LIVE_TELEGRAM_CHAT_ID"


class MessengerContract:
    @pytest.fixture
    def messenger(self) -> Messenger:
        raise NotImplementedError

    @pytest.fixture
    def chat(self) -> int:
        return CHAT

    def test_no_method_accepts_reply_markup(self, messenger: Messenger) -> None:
        """D8 / UC-12: buttons cannot even be expressed."""
        for name, method in inspect.getmembers(messenger, inspect.ismethod):
            if not name.startswith("_"):
                assert "reply_markup" not in inspect.signature(method).parameters, name

    async def test_send_returns_increasing_message_ids_per_chat(self, messenger: Messenger, chat: int) -> None:
        first = await messenger.send_text(chat, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        second = await messenger.send_text(
            chat, S.HEALTH_ONLINE(time="09:05"), reply_to=first.message_id, timeout=TIMEOUT
        )
        assert first.chat_id == chat
        assert first.message_id > 0
        assert second.message_id > first.message_id

    async def test_edit_react_action_pin_accept_a_sent_message(self, messenger: Messenger, chat: int) -> None:
        sent = await messenger.send_text(chat, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        await messenger.edit_text(chat, sent.message_id, S.HEALTH_ONLINE(time="09:05"), timeout=TIMEOUT)
        await messenger.react(chat, sent.message_id, "\U0001f440", timeout=TIMEOUT)
        await messenger.chat_action(chat, ChatAction.TYPING, timeout=TIMEOUT)
        await messenger.pin(chat, sent.message_id, timeout=TIMEOUT)

    async def test_delete_reports_whether_it_worked(self, messenger: Messenger, chat: int) -> None:
        sent = await messenger.send_text(chat, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        assert isinstance(await messenger.delete(chat, sent.message_id, timeout=TIMEOUT), bool)

    async def test_an_unchanged_edit_is_not_an_error(self, messenger: Messenger, chat: int) -> None:
        """Outbox redelivery may repeat an edit; the second one must not fail."""
        sent = await messenger.send_text(chat, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        await messenger.edit_text(chat, sent.message_id, S.HEALTH_ONLINE(time="09:05"), timeout=TIMEOUT)
        await messenger.edit_text(chat, sent.message_id, S.HEALTH_ONLINE(time="09:05"), timeout=TIMEOUT)


class TopicsContract:
    """Forum topics (D4 revised 2026-10-26): the bot opens a topic, sends and types into it, and reopening an open one
    is no error. Offline only: the live contract's chat need not be a forum."""

    @pytest.fixture
    def forum(self) -> Messenger:
        raise NotImplementedError

    async def test_a_topic_opens_and_takes_sends_and_typing(self, forum: Messenger) -> None:
        topic = await forum.create_forum_topic(CHAT, "BonviHR", icon_color=0x6FB9F0, timeout=TIMEOUT)
        assert (topic.name, topic.icon_color) == ("BonviHR", 0x6FB9F0)
        assert topic.thread_id > 0
        sent = await forum.send_text(CHAT, S.STOPPED(project="BonviHR"), thread_id=topic.thread_id, timeout=TIMEOUT)
        assert sent.chat_id == CHAT
        await forum.chat_action(CHAT, ChatAction.TYPING, thread_id=topic.thread_id, timeout=TIMEOUT)
        await forum.reopen_forum_topic(CHAT, topic.thread_id, timeout=TIMEOUT)  # open already: counts as reopened


class TestFakeMessenger(MessengerContract, TopicsContract):
    @pytest.fixture
    def messenger(self) -> Messenger:
        return FakeMessenger()

    @pytest.fixture
    def forum(self) -> Messenger:
        return FakeMessenger()

    async def test_a_deleted_or_closed_topic_refuses_a_send_as_telegram_does(self) -> None:
        messenger = FakeMessenger()
        thread = (await messenger.create_forum_topic(CHAT, "BonviHR", icon_color=0x6FB9F0, timeout=TIMEOUT)).thread_id
        messenger.closed_topics.add((CHAT, thread))
        with pytest.raises(TopicClosed):
            await messenger.send_text(CHAT, S.STOPPED(project="BonviHR"), thread_id=thread, timeout=TIMEOUT)
        messenger.delete_topic(CHAT, thread)
        with pytest.raises(TopicGone):
            await messenger.send_text(CHAT, S.STOPPED(project="BonviHR"), thread_id=thread, timeout=TIMEOUT)
        messenger.not_forum.add(CHAT)
        with pytest.raises(TopicsUnavailable):
            await messenger.create_forum_topic(CHAT, "HRMentor", icon_color=0x6FB9F0, timeout=TIMEOUT)

    async def test_records_every_request_and_scripted_failures(self) -> None:
        messenger = FakeMessenger()
        messenger.fail_next("send_text", RuntimeError("telegram down"))
        with pytest.raises(RuntimeError):
            await messenger.send_text(CHAT, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        await messenger.leave(-100555, timeout=TIMEOUT)
        assert [(request.method, request.chat_id) for request in messenger.requests] == [("leave", -100555)]
        messenger.assert_no_reply_markup()

    async def test_undeletable_messages_report_false(self) -> None:
        messenger = FakeMessenger()
        sent = await messenger.send_text(CHAT, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        messenger.undeletable.add((CHAT, sent.message_id))
        assert await messenger.delete(CHAT, sent.message_id, timeout=TIMEOUT) is False


class TestTelegramMessengerRecorded(MessengerContract, TopicsContract):
    """The real adapter, offline: every request goes through the recording session, which fails on reply_markup."""

    @pytest.fixture
    def session(self) -> RecordingSession:
        return RecordingSession()

    @pytest.fixture
    def messenger(self, session: RecordingSession) -> Messenger:
        return TelegramMessenger("123456:TEST-token-not-a-real-one-000000000", session=session)

    @pytest.fixture
    def forum(self, session: RecordingSession) -> Messenger:
        session.topics.forum = True
        return TelegramMessenger("123456:TEST-token-not-a-real-one-000000000", session=session)

    async def test_the_contract_sent_no_reply_markup(self, messenger: Messenger, session: RecordingSession) -> None:
        await messenger.send_text(CHAT, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        assert session.requests
        session.assert_no_reply_markup()


def _live_value(name: str) -> str:
    # Opt-in test configuration, not a bootstrap value: core.config does not own it.
    return os.environ.get(name, "")  # noqa: TID251


@pytest.mark.live
class TestTelegramMessengerLive(MessengerContract):
    """The real adapter against the real Bot API. Needs a test bot; never the owner's production bots."""

    @pytest.fixture
    def chat(self) -> int:
        value = _live_value(LIVE_CHAT_VAR)
        if not value:
            pytest.skip(f"{LIVE_CHAT_VAR} is not set")
        return int(value)

    @pytest.fixture
    async def messenger(self, chat: int) -> AsyncIterator[Messenger]:
        del chat  # resolved first, so a missing chat skips before a session is opened
        token = _live_value(LIVE_TOKEN_VAR)
        if not token:
            pytest.skip(f"{LIVE_TOKEN_VAR} is not set")
        adapter = TelegramMessenger(token)
        yield adapter
        await adapter.close()
