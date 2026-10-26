"""`bots/dev/telegram.py`: an admitted aiogram message → the `IncomingUpdate` the inbox takes (kind, text, caption,
the largest photo, album, reply, forward origin), and `runtime`'s small pure helpers."""

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from aiogram.types import Message

from tests.factories import DEV_GROUP_ID, TELEGRAM_DATE, make_owner_origin, tg_chat, tg_user
from yordamchi.bots.dev import runtime, telegram
from yordamchi.core.paths import BotName
from yordamchi.shared.inbox.domain import Attachment, ForwardSource, UpdateKind

ORIGIN = make_owner_origin(BotName.DEV, update_id=77)


def _message(**body: Any) -> Message:  # noqa: ANN401  # Bot API JSON
    raw = {
        "message_id": 501,
        "date": TELEGRAM_DATE,
        "chat": tg_chat(DEV_GROUP_ID),
        "from": tg_user(ORIGIN.user_id),
        **body,
    }
    return Message.model_validate(raw)


def test_a_text_message_carries_its_ids_and_words() -> None:
    update = telegram.incoming_of(ORIGIN, _message(text="BonviHR'da login sahifasini tuzat"))
    assert (update.bot, update.update_id, update.chat_id, update.user_id) == (
        BotName.DEV,
        77,
        DEV_GROUP_ID,
        ORIGIN.user_id,
    )
    assert (update.kind, update.message_id, update.text) == (UpdateKind.TEXT, 501, "BonviHR'da login sahifasini tuzat")
    assert update.attachment is None


# --- forum topics (D4 revised 2026-10-26) ----------------------------------------------------------------------------
FORUM_CHAT: Final = {**tg_chat(DEV_GROUP_ID), "is_forum": True}
TOPIC_ROOT: Final = {
    "message_id": 310,
    "date": TELEGRAM_DATE,
    "chat": FORUM_CHAT,
    "forum_topic_created": {"name": "BonviHR", "icon_color": 0x6FB9F0},
}


def test_a_topic_message_carries_its_topic_and_its_implicit_reply_to_the_topic_is_none() -> None:
    message = _message(
        text="testlarini ham yoz",
        chat=FORUM_CHAT,
        message_thread_id=310,
        is_topic_message=True,
        reply_to_message=TOPIC_ROOT,  # how Telegram presents every message of a topic
    )
    update = telegram.incoming_of(ORIGIN, message)
    assert (update.thread_id, update.is_forum, update.reply_to_message_id) == (310, True, None)


def test_a_real_reply_inside_a_topic_stays_a_reply() -> None:
    reply = {"message_id": 420, "date": TELEGRAM_DATE, "chat": FORUM_CHAT, "text": "BonviHR · Savol: …"}
    message = _message(text="2", chat=FORUM_CHAT, message_thread_id=310, is_topic_message=True, reply_to_message=reply)
    assert telegram.incoming_of(ORIGIN, message).reply_to_message_id == 420


def test_a_reply_in_general_has_no_topic_and_a_flat_group_is_no_forum() -> None:
    reply = {"message_id": 420, "date": TELEGRAM_DATE, "chat": FORUM_CHAT, "text": "→ BonviHR"}
    general = telegram.incoming_of(
        ORIGIN, _message(text="shu", chat=FORUM_CHAT, message_thread_id=420, reply_to_message=reply)
    )
    assert (general.thread_id, general.is_forum, general.reply_to_message_id) == (None, True, 420)
    flat = telegram.incoming_of(ORIGIN, _message(text="salom"))
    assert (flat.thread_id, flat.is_forum) == (None, False)


@pytest.mark.parametrize(
    ("service", "closed"),
    [
        pytest.param({"forum_topic_closed": {}}, True, id="closed"),
        pytest.param({"forum_topic_reopened": {}}, False, id="reopened"),
        pytest.param({"forum_topic_edited": {"name": "BonviHR!"}}, None, id="renamed"),
    ],
)
def test_a_topic_service_message_is_no_request(service: dict[str, Any], closed: bool | None) -> None:
    message = _message(chat=FORUM_CHAT, message_thread_id=310, is_topic_message=True, **service)
    update = telegram.incoming_of(ORIGIN, message)
    assert (update.kind, update.thread_id, update.topic_closed) == (UpdateKind.SERVICE, 310, closed)


def test_a_link_only_text_is_a_link() -> None:
    assert telegram.incoming_of(ORIGIN, _message(text="https://example.uz/x")).kind is UpdateKind.LINK


def test_a_photo_keeps_the_largest_size_its_caption_album_and_reply() -> None:
    sizes = [
        {"file_id": "small", "file_unique_id": "s", "width": 90, "height": 60, "file_size": 1_000},
        {"file_id": "large", "file_unique_id": "l", "width": 1280, "height": 853, "file_size": 90_000},
    ]
    reply = {"message_id": 400, "date": TELEGRAM_DATE, "chat": tg_chat(DEV_GROUP_ID), "text": "BonviHR · Natija"}
    update = telegram.incoming_of(
        ORIGIN, _message(photo=sizes, caption="shu xato", media_group_id="album-1", reply_to_message=reply)
    )
    assert update.kind is UpdateKind.PHOTO
    assert update.attachment == Attachment("large", "image/jpeg", 90_000)
    assert (update.caption, update.media_group_id, update.reply_to_message_id) == ("shu xato", "album-1", 400)


@pytest.mark.parametrize(
    ("body", "kind", "attachment"),
    [
        (
            {"voice": {"file_id": "v", "file_unique_id": "v", "duration": 4, "mime_type": "audio/ogg"}},
            UpdateKind.VOICE,
            Attachment("v", "audio/ogg", None, 4),
        ),
        (
            {"audio": {"file_id": "a", "file_unique_id": "a", "duration": 60, "file_name": "song.mp3"}},
            UpdateKind.AUDIO,
            Attachment("a", None, None, 60, "song.mp3"),
        ),
        (
            {"video": {"file_id": "w", "file_unique_id": "w", "width": 1, "height": 1, "duration": 5}},
            UpdateKind.VIDEO,
            Attachment("w", None, None, 5, None),
        ),
        (
            {"document": {"file_id": "d", "file_unique_id": "d", "file_name": "log.txt", "mime_type": "text/plain"}},
            UpdateKind.DOCUMENT,
            Attachment("d", "text/plain", None, None, "log.txt"),
        ),
    ],
)
def test_each_file_kind_is_read_with_its_attachment(
    body: dict[str, Any], kind: UpdateKind, attachment: Attachment
) -> None:
    update = telegram.incoming_of(ORIGIN, _message(**body))
    assert (update.kind, update.attachment) == (kind, attachment)


def test_an_edit_is_an_edit_and_a_sticker_is_other() -> None:
    assert telegram.incoming_of(ORIGIN, _message(text="tuzatildi", edit_date=TELEGRAM_DATE), edited=True).kind is (
        UpdateKind.EDITED
    )
    sticker = {"file_id": "st", "file_unique_id": "st", "type": "regular", "width": 1, "height": 1}
    sticker |= {"is_animated": False, "is_video": False}
    assert telegram.incoming_of(ORIGIN, _message(sticker=sticker)).kind is UpdateKind.OTHER


def test_a_message_carries_when_the_owner_sent_it_and_an_edit_when_he_edited_it() -> None:
    sent = datetime.fromtimestamp(TELEGRAM_DATE, tz=UTC)
    update = telegram.incoming_of(ORIGIN, _message(text="BonviHR'da login sahifasini tuzat"))
    assert update.sent_at == sent
    assert update.sent_at is not None
    assert update.sent_at.tzinfo is UTC  # UTC-aware, as every instant here
    edited = _message(text="tuzatildi", edit_date=TELEGRAM_DATE + 300)
    assert telegram.incoming_of(ORIGIN, edited, edited=True).sent_at == sent + timedelta(seconds=300)
    assert telegram.incoming_of(ORIGIN, _message(text="tuzatildi"), edited=True).sent_at == sent  # no edit_date


def test_a_forwarded_message_says_where_it_came_from_and_the_owners_own_does_not() -> None:
    forwarded = _message(
        text="buni qil", forward_origin={"type": "user", "date": TELEGRAM_DATE, "sender_user": tg_user(5)}
    )
    assert telegram.incoming_of(ORIGIN, forwarded).forward is ForwardSource.USER
    assert telegram.incoming_of(ORIGIN, _message(text="o'zim yozdim")).forward is None


def test_the_retention_run_waits_for_the_next_local_time() -> None:
    now = datetime(2026, 10, 24, 20, 0, tzinfo=UTC)  # 01:00 in Tashkent (UTC+5)
    assert runtime.seconds_until(now, "04:30", "Asia/Tashkent") == 3.5 * 3600
    assert runtime.seconds_until(now, "00:30", "Asia/Tashkent") == 23.5 * 3600  # today's has passed
