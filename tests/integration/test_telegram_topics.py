"""`TelegramMessenger` and forum topics (D4 revised 2026-10-26; SPEC §3.2.16), on the recording aiogram session.

What goes on the wire for a send or a typing hint into a topic, the bot's own topic calls, and Telegram's refusals
about topics as the `ExternalError`s the outbox repairs (`TopicGone`, `TopicClosed`) or falls back on
(`TopicsUnavailable`). Still never a keyboard (D8).
"""

from pathlib import Path
from typing import Final

import pytest

from tests.fakes.telegram_session import RecordingSession, bad_request
from yordamchi.adapters.telegram.messenger import TelegramMessenger
from yordamchi.core import strings as S
from yordamchi.core.errors import ProviderUnavailable, TopicClosed, TopicGone, TopicsUnavailable
from yordamchi.ports.messenger import ChatAction, ForumTopic, OutboundFile

TOKEN: Final = "123456:TEST-token-not-a-real-one-000000000"
GROUP: Final = -1001234567890
TIMEOUT: Final = 5.0
TEXT: Final = S.STOPPED(project="BonviHR")


@pytest.fixture
def session() -> RecordingSession:
    recording = RecordingSession()
    recording.topics.forum = True
    return recording


@pytest.fixture
def messenger(session: RecordingSession) -> TelegramMessenger:
    adapter = TelegramMessenger(TOKEN, session=session)
    adapter.retry_base_delay_s = 0.001
    adapter.retry_max_delay_s = 0.002
    return adapter


async def test_a_project_topic_is_opened_with_its_name_and_colour(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    topic = await messenger.create_forum_topic(GROUP, "BonviHR", icon_color=0x6FB9F0, timeout=TIMEOUT)
    assert topic == ForumTopic(thread_id=501, name="BonviHR", icon_color=0x6FB9F0)
    [request] = session.calls("createForumTopic")
    assert request.params == {"chat_id": GROUP, "name": "BonviHR", "icon_color": 0x6FB9F0}


async def test_sends_and_typing_into_a_topic_carry_its_thread_and_nothing_else_does(
    messenger: TelegramMessenger, session: RecordingSession, tmp_path: Path
) -> None:
    thread = (await messenger.create_forum_topic(GROUP, "BonviHR", icon_color=0x6FB9F0, timeout=TIMEOUT)).thread_id
    sent = await messenger.send_text(GROUP, TEXT, thread_id=thread, timeout=TIMEOUT)
    await messenger.chat_action(GROUP, ChatAction.TYPING, thread_id=thread, timeout=TIMEOUT)
    await messenger.edit_text(GROUP, sent.message_id, TEXT, timeout=TIMEOUT)
    small = tmp_path / "plan.md"
    small.write_bytes(b"# Reja\n")
    file = OutboundFile(small, filename="plan.md")
    await messenger.send_document(GROUP, file, thread_id=thread, timeout=TIMEOUT)
    await messenger.send_photo(GROUP, file, thread_id=thread, timeout=TIMEOUT)
    await messenger.send_voice(GROUP, file, thread_id=thread, timeout=TIMEOUT)
    await messenger.send_video(GROUP, file, thread_id=thread, timeout=TIMEOUT)
    await messenger.send_text(GROUP, TEXT, timeout=TIMEOUT)  # General

    threads = [(r.api_method, r.params.get("message_thread_id")) for r in session.requests[1:]]
    assert threads == [
        ("sendMessage", thread),
        ("sendChatAction", thread),
        ("editMessageText", None),  # an edit names its message
        ("sendDocument", thread),
        ("sendPhoto", thread),
        ("sendVoice", thread),
        ("sendVideo", thread),
        ("sendMessage", None),
    ]
    session.assert_no_reply_markup()


async def test_reopening_an_open_topic_counts_as_reopened(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    thread = (await messenger.create_forum_topic(GROUP, "BonviHR", icon_color=0x6FB9F0, timeout=TIMEOUT)).thread_id
    await messenger.reopen_forum_topic(GROUP, thread, timeout=TIMEOUT)  # TOPIC_NOT_MODIFIED: fine
    session.topics.close(GROUP, thread)
    await messenger.reopen_forum_topic(GROUP, thread, timeout=TIMEOUT)
    assert (GROUP, thread) not in session.topics.closed


async def test_a_deleted_or_closed_topic_is_its_own_refusal(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    thread = (await messenger.create_forum_topic(GROUP, "BonviHR", icon_color=0x6FB9F0, timeout=TIMEOUT)).thread_id
    session.topics.close(GROUP, thread)
    with pytest.raises(TopicClosed) as closed:
        await messenger.send_text(GROUP, TEXT, thread_id=thread, timeout=TIMEOUT)
    session.topics.delete(GROUP, thread)
    with pytest.raises(TopicGone) as gone:
        await messenger.send_text(GROUP, TEXT, thread_id=thread, timeout=TIMEOUT)
    with pytest.raises(TopicGone):
        await messenger.reopen_forum_topic(GROUP, thread, timeout=TIMEOUT)
    assert not closed.value.retryable
    assert not gone.value.retryable
    assert len(session.calls("sendMessage")) == 2  # refused, never retried


@pytest.mark.parametrize(
    ("description", "reason"),
    [
        pytest.param("the chat is not a forum", "not_forum", id="topics_off"),
        pytest.param("not enough rights to create a topic", "no_right", id="no_manage_topics_right"),
    ],
)
async def test_a_group_where_no_topic_can_be_opened(
    messenger: TelegramMessenger, session: RecordingSession, description: str, reason: str
) -> None:
    session.script("createForumTopic", bad_request(description))
    with pytest.raises(TopicsUnavailable) as refused:
        await messenger.create_forum_topic(GROUP, "BonviHR", icon_color=0x6FB9F0, timeout=TIMEOUT)
    assert refused.value.reason == reason
    assert not refused.value.retryable


async def test_not_enough_rights_outside_the_topic_calls_stays_a_plain_refusal(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("pinChatMessage", bad_request("not enough rights to pin a message"))
    with pytest.raises(ProviderUnavailable):
        await messenger.pin(GROUP, 5, timeout=TIMEOUT)
