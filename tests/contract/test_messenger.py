import inspect

import pytest

from tests.fakes.messenger import FakeMessenger
from yordamchi.core import strings as S
from yordamchi.ports.messenger import ChatAction, Messenger

CHAT = -1001234567890
TIMEOUT = 5.0


class MessengerContract:
    @pytest.fixture
    def messenger(self) -> Messenger:
        raise NotImplementedError

    def test_no_method_accepts_reply_markup(self, messenger: Messenger) -> None:
        """D8 / UC-12: buttons cannot even be expressed."""
        for name, method in inspect.getmembers(messenger, inspect.ismethod):
            if not name.startswith("_"):
                assert "reply_markup" not in inspect.signature(method).parameters, name

    async def test_send_returns_increasing_message_ids_per_chat(self, messenger: Messenger) -> None:
        first = await messenger.send_text(CHAT, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        second = await messenger.send_text(
            CHAT, S.HEALTH_ONLINE(time="09:05"), reply_to=first.message_id, timeout=TIMEOUT
        )
        assert first.chat_id == CHAT
        assert first.message_id > 0
        assert second.message_id > first.message_id

    async def test_edit_react_action_pin_accept_a_sent_message(self, messenger: Messenger) -> None:
        sent = await messenger.send_text(CHAT, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        await messenger.edit_text(CHAT, sent.message_id, S.HEALTH_ONLINE(time="09:05"), timeout=TIMEOUT)
        await messenger.react(CHAT, sent.message_id, "\U0001f440", timeout=TIMEOUT)
        await messenger.chat_action(CHAT, ChatAction.TYPING, timeout=TIMEOUT)
        await messenger.pin(CHAT, sent.message_id, timeout=TIMEOUT)

    async def test_delete_reports_whether_it_worked(self, messenger: Messenger) -> None:
        sent = await messenger.send_text(CHAT, S.HEALTH_ONLINE(time="09:00"), timeout=TIMEOUT)
        assert isinstance(await messenger.delete(CHAT, sent.message_id, timeout=TIMEOUT), bool)


class TestFakeMessenger(MessengerContract):
    @pytest.fixture
    def messenger(self) -> Messenger:
        return FakeMessenger()

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
