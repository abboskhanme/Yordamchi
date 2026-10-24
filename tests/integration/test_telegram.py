"""`TelegramMessenger` on the recording aiogram session and the synthetic Bot API fixtures (CONVENTIONS §4, §11).

The network is closed: every request lands in `RecordingSession`, which fails on any `reply_markup` (UC-12) and
answers through aiogram's own response parsing, so the fixture statuses and bodies raise the real exceptions.
"""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import aiohttp
import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage, TelegramMethod
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, Update
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from tests.fakes.telegram_session import FIXTURES, Answer, RecordingSession, ReplyMarkupSent
from yordamchi.adapters.telegram import messenger as messenger_module
from yordamchi.adapters.telegram.messenger import TelegramMessenger
from yordamchi.adapters.telegram.translate import (
    INBOUND_MAX_BYTES,
    OUTBOUND_MAX_BYTES,
    OUTBOUND_PHOTO_MAX_BYTES,
    TelegramFileTooLarge,
)
from yordamchi.core import strings as S
from yordamchi.core.errors import (
    AuthFailed,
    CallTimedOut,
    ConfigInvalid,
    ExternalError,
    ProviderUnavailable,
    RateLimited,
)
from yordamchi.core.redact import LOG_FIELDS_ATTR
from yordamchi.ports.messenger import ChatAction, OutboundFile

TOKEN = "123456:TEST-token-not-a-real-one-000000000"
DEV_GROUP = -1001234567890
TIMEOUT = 5.0
TEXT = S.HEALTH_ONLINE(time="09:00")


@pytest.fixture
def session() -> RecordingSession:
    return RecordingSession()


@pytest.fixture
def messenger(session: RecordingSession) -> TelegramMessenger:
    adapter = TelegramMessenger(TOKEN, session=session)
    adapter.retry_base_delay_s = 0.001  # keep transient-retry tests fast; the policy itself is core.retry's
    adapter.retry_max_delay_s = 0.002
    return adapter


def _update_fixture(name: str) -> dict[str, Any]:
    return cast(dict[str, Any], _load_update_fixture(name))


def _album_fixture() -> list[dict[str, Any]]:
    return cast(list[dict[str, Any]], _load_update_fixture("album"))


def _load_update_fixture(name: str) -> object:
    return json.loads((FIXTURES / "updates" / f"{name}.json").read_text(encoding="utf-8"))


def _network_error(message: str) -> Callable[[TelegramMethod[Any]], BaseException]:
    def make(method: TelegramMethod[Any]) -> BaseException:
        return TelegramNetworkError(method=method, message=message)

    return make


def _sparse_file(path: Path, size: int) -> OutboundFile:
    with path.open("wb") as handle:
        handle.truncate(size)
    return OutboundFile(path=path, filename=path.name)


def _response_error(status: int) -> aiohttp.ClientResponseError:
    info = aiohttp.RequestInfo(
        URL(f"https://api.telegram.org/file/bot{TOKEN}/voice/file_42.oga"),
        "GET",
        CIMultiDictProxy(CIMultiDict()),
    )
    return aiohttp.ClientResponseError(info, (), status=status, message="Not Found")


# --- inbound fixtures (what the inbox, burst and access features will receive) ---------------------------------
def test_album_items_share_one_media_group_and_arrive_as_separate_updates() -> None:
    bot = Bot(TOKEN, session=RecordingSession())
    updates = [Update.model_validate(raw, context={"bot": bot}) for raw in _album_fixture()]
    messages = [update.message for update in updates]
    assert all(message is not None for message in messages)
    groups = {message.media_group_id for message in messages if message is not None}
    assert groups == {"13912345678901234"}
    assert [update.update_id for update in updates] == [880001, 880002, 880003]
    captions = [message.caption for message in messages if message is not None]
    assert captions == ["shu xatolarni ko'r", None, None]


def test_voice_photo_and_document_updates_carry_what_the_download_needs() -> None:
    bot = Bot(TOKEN, session=RecordingSession())
    voice = Update.model_validate(_update_fixture("voice"), context={"bot": bot}).message
    assert voice is not None
    assert voice.voice is not None
    assert (voice.voice.duration, voice.voice.mime_type) == (14, "audio/ogg")
    photo = Update.model_validate(_update_fixture("photo"), context={"bot": bot}).message
    assert photo is not None
    assert photo.photo is not None
    assert max(photo.photo, key=lambda size: size.width).width == 1280
    big = Update.model_validate(_update_fixture("document_too_large"), context={"bot": bot}).message
    assert big is not None
    assert big.document is not None
    assert big.document.file_size is not None
    assert big.document.file_size > INBOUND_MAX_BYTES


def test_my_chat_member_fixtures_describe_join_promotion_and_kick() -> None:
    bot = Bot(TOKEN, session=RecordingSession())
    statuses = {}
    for name in (
        "my_chat_member_added_to_foreign_group",
        "my_chat_member_promoted_in_dev_group",
        "my_chat_member_kicked",
    ):
        update = Update.model_validate(_update_fixture(name), context={"bot": bot})
        assert update.my_chat_member is not None
        change = update.my_chat_member
        statuses[name] = (change.chat.id, change.old_chat_member.status, change.new_chat_member.status)
    assert statuses == {
        "my_chat_member_added_to_foreign_group": (-1009876543210, "left", "member"),
        "my_chat_member_promoted_in_dev_group": (DEV_GROUP, "member", "administrator"),
        "my_chat_member_kicked": (-1009876543210, "member", "kicked"),
    }
    promoted = Update.model_validate(_update_fixture("my_chat_member_promoted_in_dev_group"), context={"bot": bot})
    assert promoted.my_chat_member is not None
    assert getattr(promoted.my_chat_member.new_chat_member, "can_delete_messages", False) is True


# --- UC-12: no reply_markup, ever ---------------------------------------------------------------------------------
async def test_recording_session_fails_any_request_with_reply_markup(session: RecordingSession) -> None:
    """The guard itself works: the adapter tests below would catch a keyboard."""
    bot = Bot(TOKEN, session=session)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="ha", callback_data="yes")]])
    for markup in (keyboard, ReplyKeyboardRemove()):
        with pytest.raises(ReplyMarkupSent):
            await bot(SendMessage(chat_id=DEV_GROUP, text="x", reply_markup=markup))
    assert session.requests == []


async def test_every_operation_reaches_telegram_without_reply_markup(
    messenger: TelegramMessenger, session: RecordingSession, tmp_path: Path
) -> None:
    session.add_file("AwACAgIAAxkBAAIB_voice_0001", b"OggS-voice", file_path="voice/file_42.oga")
    sent = await messenger.send_text(DEV_GROUP, TEXT, reply_to=7, parse_mode="HTML", timeout=TIMEOUT)
    await messenger.edit_text(DEV_GROUP, sent.message_id, TEXT, timeout=TIMEOUT)
    await messenger.react(DEV_GROUP, sent.message_id, "\U0001f440", timeout=TIMEOUT)
    await messenger.chat_action(DEV_GROUP, ChatAction.TYPING, timeout=TIMEOUT)
    await messenger.pin(DEV_GROUP, sent.message_id, timeout=TIMEOUT)
    assert await messenger.delete(DEV_GROUP, sent.message_id, timeout=TIMEOUT) is True
    await messenger.send_draft(100000001, 1, TEXT, timeout=TIMEOUT)
    small = _sparse_file(tmp_path / "f.bin", 1024)
    await messenger.send_photo(DEV_GROUP, small, caption=TEXT, timeout=TIMEOUT)
    await messenger.send_document(DEV_GROUP, small, timeout=TIMEOUT)
    await messenger.send_voice(DEV_GROUP, small, duration_s=3, timeout=TIMEOUT)
    await messenger.send_video(DEV_GROUP, small, timeout=TIMEOUT)
    assert await messenger.download("AwACAgIAAxkBAAIB_voice_0001", max_bytes=1000, timeout=TIMEOUT) == b"OggS-voice"
    await messenger.leave(-1009876543210, timeout=TIMEOUT)

    assert [request.api_method for request in session.requests] == [
        "sendMessage",
        "editMessageText",
        "setMessageReaction",
        "sendChatAction",
        "pinChatMessage",
        "deleteMessage",
        "sendMessageDraft",
        "sendPhoto",
        "sendDocument",
        "sendVoice",
        "sendVideo",
        "getFile",
        "leaveChat",
    ]
    session.assert_no_reply_markup()


async def test_send_text_wire_format(messenger: TelegramMessenger, session: RecordingSession) -> None:
    sent = await messenger.send_text(
        DEV_GROUP, TEXT, reply_to=41, parse_mode="HTML", disable_notification=True, timeout=7.5
    )
    request = session.calls("sendMessage")[0]
    assert request.params == {
        "chat_id": DEV_GROUP,
        "text": "Onlayn: 09:00",
        "parse_mode": "HTML",
        "disable_notification": True,
        # A deleted reply target must not fail the send.
        "reply_parameters": {"message_id": 41, "allow_sending_without_reply": True},
    }
    assert request.timeout == 8  # aiogram's own HTTP timeout, rounded up from the call's budget
    assert (sent.chat_id, sent.message_id) == (DEV_GROUP, 1001)


async def test_react_sets_one_emoji_reaction_not_a_button(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    await messenger.react(DEV_GROUP, 55, "\U0001f440", timeout=TIMEOUT)
    assert session.calls("setMessageReaction")[0].params == {
        "chat_id": DEV_GROUP,
        "message_id": 55,
        "reaction": [{"type": "emoji", "emoji": "\U0001f440"}],
    }


async def test_pin_is_silent_and_chat_action_is_typing(messenger: TelegramMessenger, session: RecordingSession) -> None:
    await messenger.pin(DEV_GROUP, 9, timeout=TIMEOUT)
    await messenger.chat_action(DEV_GROUP, ChatAction.TYPING, timeout=TIMEOUT)
    assert session.calls("pinChatMessage")[0].params == {
        "chat_id": DEV_GROUP,
        "message_id": 9,
        "disable_notification": True,
    }
    assert session.calls("sendChatAction")[0].params == {"chat_id": DEV_GROUP, "action": "typing"}


async def test_files_are_uploaded_as_multipart_with_caption_and_duration(
    messenger: TelegramMessenger, session: RecordingSession, tmp_path: Path
) -> None:
    voice = _sparse_file(tmp_path / "answer.ogg", 2048)
    sent = await messenger.send_voice(DEV_GROUP, voice, caption=TEXT, duration_s=4, reply_to=3, timeout=TIMEOUT)
    request = session.calls("sendVoice")[0]
    assert list(request.uploads.values()) == ["answer.ogg"]
    assert str(request.params["voice"]).startswith("attach://")
    assert (request.params["caption"], request.params["duration"]) == ("Onlayn: 09:00", 4)
    assert sent.message_id == 1001


async def test_send_draft_streams_into_a_private_chat(messenger: TelegramMessenger, session: RecordingSession) -> None:
    await messenger.send_draft(100000001, 42, TEXT, timeout=TIMEOUT)
    assert session.calls("sendMessageDraft")[0].params == {
        "chat_id": 100000001,
        "draft_id": 42,
        "text": "Onlayn: 09:00",
    }


# --- NFR-CAP-02: 429 and retry_after ------------------------------------------------------------------------------
async def test_retry_after_longer_than_the_budget_raises_rate_limited_at_once(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("sendMessage", Answer.fixture("too_many_requests"))
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(RateLimited) as caught:
        await messenger.send_text(DEV_GROUP, TEXT, timeout=TIMEOUT)
    assert caught.value.retry_after == 7.0  # the outbox parks this chat for 7 s
    assert caught.value.retryable is True
    assert len(session.calls("sendMessage")) == 1
    assert loop.time() - started < 1.0


async def test_retry_after_within_the_budget_is_waited_then_retried(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("sendMessage", Answer.fixture("too_many_requests", retry_after=1))
    loop = asyncio.get_running_loop()
    started = loop.time()
    sent = await messenger.send_text(DEV_GROUP, TEXT, timeout=TIMEOUT)
    assert loop.time() - started >= 1.0
    assert len(session.calls("sendMessage")) == 2
    assert sent.message_id == 1001


async def test_429_without_parameters_reads_retry_after_from_the_description(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("editMessageText", Answer.fixture("too_many_requests_no_parameters"))
    with pytest.raises(RateLimited) as caught:
        await messenger.edit_text(DEV_GROUP, 5, TEXT, timeout=2.0)
    assert caught.value.retry_after == 3.0


# --- error mapping ------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("fixture", ["internal_server_error", "bad_gateway"])
async def test_server_errors_are_retried(messenger: TelegramMessenger, session: RecordingSession, fixture: str) -> None:
    session.script("sendMessage", Answer.fixture(fixture), Answer.fixture(fixture))
    sent = await messenger.send_text(DEV_GROUP, TEXT, timeout=TIMEOUT)
    assert len(session.calls("sendMessage")) == 3
    assert sent.message_id == 1001


async def test_persistent_server_errors_give_up_retryable_after_the_attempts(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("pinChatMessage", *[Answer.fixture("bad_gateway")] * 5)
    with pytest.raises(ProviderUnavailable) as caught:
        await messenger.pin(DEV_GROUP, 9, timeout=TIMEOUT)
    assert caught.value.retryable is True
    assert caught.value.params == {"provider": "telegram"}
    assert len(session.calls("pinChatMessage")) == messenger.retry_attempts


@pytest.mark.parametrize(
    ("fixture", "error_type"),
    [
        ("cant_parse_entities", ProviderUnavailable),
        ("reaction_invalid", ProviderUnavailable),
        ("chat_migrated", ProviderUnavailable),
        ("forbidden_bot_kicked", ProviderUnavailable),
        ("conflict", ProviderUnavailable),
        ("unauthorized", AuthFailed),
    ],
)
async def test_client_errors_are_never_retried(
    messenger: TelegramMessenger, session: RecordingSession, fixture: str, error_type: type[ExternalError]
) -> None:
    session.script("setMessageReaction", Answer.fixture(fixture))
    with pytest.raises(error_type) as caught:
        await messenger.react(DEV_GROUP, 9, "\U0001f440", timeout=TIMEOUT)
    assert caught.value.retryable is False
    assert len(session.calls("setMessageReaction")) == 1


async def test_an_unknown_send_outcome_is_not_resent(messenger: TelegramMessenger, session: RecordingSession) -> None:
    """A network error after the request left may hide a delivered message: the outbox decides, not a blind retry."""
    session.script("sendMessage", _network_error("ServerDisconnectedError: Server disconnected"))
    with pytest.raises(ProviderUnavailable) as caught:
        await messenger.send_text(DEV_GROUP, TEXT, timeout=TIMEOUT)
    assert caught.value.retryable is True
    assert len(session.calls("sendMessage")) == 1


async def test_an_unreadable_answer_to_a_send_is_not_resent(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("sendMessage", Answer.fixture("proxy_html_502"))
    with pytest.raises(ProviderUnavailable) as caught:
        await messenger.send_text(DEV_GROUP, TEXT, timeout=TIMEOUT)
    assert caught.value.retryable is True
    assert len(session.calls("sendMessage")) == 1


async def test_idempotent_calls_are_retried_after_network_errors(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("editMessageText", _network_error("ClientOSError: Connection reset by peer"))
    session.script("deleteMessage", Answer.fixture("proxy_html_502"))
    await messenger.edit_text(DEV_GROUP, 5, TEXT, timeout=TIMEOUT)
    assert await messenger.delete(DEV_GROUP, 5, timeout=TIMEOUT) is True
    assert len(session.calls("editMessageText")) == 2
    assert len(session.calls("deleteMessage")) == 2


async def test_aiogram_request_timeout_maps_to_call_timed_out(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("sendMessage", _network_error("Request timeout error"))
    with pytest.raises(CallTimedOut) as caught:
        await messenger.send_text(DEV_GROUP, TEXT, timeout=TIMEOUT)
    assert caught.value.params == {"operation": "sendMessage"}


async def test_a_hung_request_ends_at_the_call_timeout(messenger: TelegramMessenger, session: RecordingSession) -> None:
    session.hang = True
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(CallTimedOut):
        await messenger.chat_action(DEV_GROUP, ChatAction.TYPING, timeout=0.05)
    assert loop.time() - started < 1.0


async def test_an_unchanged_edit_counts_as_done(messenger: TelegramMessenger, session: RecordingSession) -> None:
    session.script("editMessageText", Answer.fixture("message_not_modified"))
    await messenger.edit_text(DEV_GROUP, 5, TEXT, timeout=TIMEOUT)
    assert len(session.calls("editMessageText")) == 1


@pytest.mark.parametrize("fixture", ["message_cant_be_deleted", "message_to_delete_not_found"])
async def test_delete_reports_false_when_telegram_refuses(
    messenger: TelegramMessenger, session: RecordingSession, fixture: str
) -> None:
    session.script("deleteMessage", Answer.fixture(fixture))
    assert await messenger.delete(DEV_GROUP, 5, timeout=TIMEOUT) is False
    assert len(session.calls("deleteMessage")) == 1


@pytest.mark.parametrize("fixture", ["forbidden_not_member", "forbidden_bot_kicked"])
async def test_leaving_a_chat_the_bot_is_not_in_counts_as_left(
    messenger: TelegramMessenger, session: RecordingSession, fixture: str
) -> None:
    session.script("leaveChat", Answer.fixture(fixture))
    await messenger.leave(-1009876543210, timeout=TIMEOUT)


# --- NFR-CAP-04: 20 MB in, 50 MB out ------------------------------------------------------------------------------
async def test_download_streams_the_file_from_get_file(messenger: TelegramMessenger, session: RecordingSession) -> None:
    session.script("getFile", Answer.fixture("get_file_voice"))
    session.add_file("AwACAgIAAxkBAAIB_voice_0001", b"OggS" * 1000, file_path="voice/file_42.oga")
    data = await messenger.download("AwACAgIAAxkBAAIB_voice_0001", max_bytes=INBOUND_MAX_BYTES, timeout=TIMEOUT)
    assert data == b"OggS" * 1000
    assert session.streamed == ["voice/file_42.oga"]


async def test_download_refuses_a_file_over_20_mb_before_fetching_it(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    big = _update_fixture("document_too_large")["message"]["document"]
    session.add_file(big["file_id"], b"", file_path="documents/file_7.gz", reported_size=big["file_size"])
    with pytest.raises(TelegramFileTooLarge) as caught:
        await messenger.download(big["file_id"], max_bytes=100 * 1024 * 1024, timeout=TIMEOUT)
    assert (caught.value.direction, caught.value.limit_bytes) == ("in", INBOUND_MAX_BYTES)
    assert caught.value.size_bytes == 26214400
    assert caught.value.retryable is False
    assert session.streamed == []


async def test_download_honours_a_smaller_max_bytes(messenger: TelegramMessenger, session: RecordingSession) -> None:
    session.add_file("f1", b"x" * 2000, file_path="documents/f1.bin")
    with pytest.raises(TelegramFileTooLarge) as caught:
        await messenger.download("f1", max_bytes=1000, timeout=TIMEOUT)
    assert caught.value.limit_bytes == 1000


async def test_get_file_too_big_maps_to_the_inbound_limit(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.script("getFile", Answer.fixture("file_is_too_big"))
    with pytest.raises(TelegramFileTooLarge) as caught:
        await messenger.download("BQACAgIAAxkBAAIB_doc_big1", max_bytes=INBOUND_MAX_BYTES, timeout=TIMEOUT)
    assert caught.value.direction == "in"
    assert len(session.calls("getFile")) == 1


async def test_download_without_file_size_stops_at_the_limit(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.add_file("f2", b"y" * 300_000, file_path="documents/f2.bin", reported_size=None)
    with pytest.raises(TelegramFileTooLarge):
        await messenger.download("f2", max_bytes=100_000, timeout=TIMEOUT)
    assert session.streamed == ["documents/f2.bin"]  # aborted mid-stream, not retried


async def test_download_without_a_file_path_is_refused(messenger: TelegramMessenger, session: RecordingSession) -> None:
    session.script("getFile", Answer.json(200, {"ok": True, "result": {"file_id": "f3", "file_unique_id": "u3"}}))
    with pytest.raises(TelegramFileTooLarge):
        await messenger.download("f3", max_bytes=INBOUND_MAX_BYTES, timeout=TIMEOUT)


async def test_download_retries_a_dropped_connection(messenger: TelegramMessenger, session: RecordingSession) -> None:
    session.add_file("f4", b"voice", file_path="voice/file_42.oga")
    session.fail_stream(aiohttp.ClientConnectionError("Connection reset by peer"))
    assert await messenger.download("f4", max_bytes=1000, timeout=TIMEOUT) == b"voice"
    assert session.streamed == ["voice/file_42.oga", "voice/file_42.oga"]


async def test_download_does_not_retry_a_4xx_from_the_file_endpoint(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.add_file("f5", b"voice", file_path="voice/file_42.oga")
    session.fail_stream(_response_error(404))
    with pytest.raises(ProviderUnavailable) as caught:
        await messenger.download("f5", max_bytes=1000, timeout=TIMEOUT)
    assert caught.value.retryable is False
    assert len(session.streamed) == 1


async def test_download_retries_a_5xx_from_the_file_endpoint(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.add_file("f6", b"voice", file_path="voice/file_42.oga")
    session.fail_stream(_response_error(502))
    assert await messenger.download("f6", max_bytes=1000, timeout=TIMEOUT) == b"voice"


async def test_download_lets_a_non_transport_error_through(
    messenger: TelegramMessenger, session: RecordingSession
) -> None:
    session.add_file("f7", b"voice", file_path="voice/file_42.oga")
    session.fail_stream(KeyError("bug"))
    with pytest.raises(KeyError):
        await messenger.download("f7", max_bytes=1000, timeout=TIMEOUT)


async def test_outbound_files_over_50_mb_are_refused_before_any_request(
    messenger: TelegramMessenger, session: RecordingSession, tmp_path: Path
) -> None:
    too_big = _sparse_file(tmp_path / "big.zip", OUTBOUND_MAX_BYTES + 1)
    for send in (messenger.send_document, messenger.send_video, messenger.send_voice):
        with pytest.raises(TelegramFileTooLarge) as caught:
            await send(DEV_GROUP, too_big, timeout=TIMEOUT)
        assert (caught.value.direction, caught.value.size_bytes) == ("out", OUTBOUND_MAX_BYTES + 1)
    assert session.requests == []


async def test_a_file_of_exactly_50_mb_is_sent(
    messenger: TelegramMessenger, session: RecordingSession, tmp_path: Path
) -> None:
    await messenger.send_document(DEV_GROUP, _sparse_file(tmp_path / "edge.zip", OUTBOUND_MAX_BYTES), timeout=TIMEOUT)
    assert len(session.calls("sendDocument")) == 1


async def test_photos_over_10_mb_are_refused(
    messenger: TelegramMessenger, session: RecordingSession, tmp_path: Path
) -> None:
    with pytest.raises(TelegramFileTooLarge) as caught:
        await messenger.send_photo(
            DEV_GROUP, _sparse_file(tmp_path / "shot.png", OUTBOUND_PHOTO_MAX_BYTES + 1), timeout=TIMEOUT
        )
    assert caught.value.limit_bytes == OUTBOUND_PHOTO_MAX_BYTES
    assert session.requests == []


async def test_413_from_telegram_is_an_outbound_too_large_and_not_retried(
    messenger: TelegramMessenger, session: RecordingSession, tmp_path: Path
) -> None:
    session.script("sendVideo", Answer.fixture("request_entity_too_large"))
    with pytest.raises(TelegramFileTooLarge) as caught:
        await messenger.send_video(DEV_GROUP, _sparse_file(tmp_path / "clip.mp4", 4096), timeout=TIMEOUT)
    assert caught.value.direction == "out"
    assert len(session.calls("sendVideo")) == 1


# --- construction, secrets and logs -------------------------------------------------------------------------------
def test_a_malformed_token_is_a_config_error_without_the_token() -> None:
    with pytest.raises(ConfigInvalid) as caught:
        TelegramMessenger("not a token", session=RecordingSession())
    assert caught.value.params == {"key": "bot_token", "problem": "malformed"}
    assert caught.value.__cause__ is None


def test_no_bot_is_created_at_import_time() -> None:
    assert not [name for name, value in vars(messenger_module).items() if isinstance(value, Bot)]


async def test_errors_and_logs_never_carry_the_token_or_the_text(
    messenger: TelegramMessenger, session: RecordingSession, caplog: pytest.LogCaptureFixture
) -> None:
    leak = f"ClientConnectorError: Cannot connect to host api.telegram.org/bot{TOKEN}/sendMessage"
    session.script("sendMessage", _network_error(leak))
    caplog.set_level(logging.INFO)
    with pytest.raises(ProviderUnavailable) as caught:
        await messenger.send_text(DEV_GROUP, TEXT, timeout=TIMEOUT)
    error = caught.value
    assert error.__cause__ is None
    assert error.__context__ is None
    assert TOKEN not in f"{error} {error!r} {error.params}"
    dumped = " ".join(f"{record.getMessage()} {getattr(record, LOG_FIELDS_ATTR, {})}" for record in caplog.records)
    assert TOKEN not in dumped
    assert "Onlayn" not in dumped


async def test_each_call_writes_one_log_line_with_outcome_and_duration(
    messenger: TelegramMessenger, session: RecordingSession, caplog: pytest.LogCaptureFixture
) -> None:
    session.script("sendMessage", Answer.fixture("bad_gateway"))
    session.script("deleteMessage", Answer.fixture("message_cant_be_deleted"))
    session.script("pinChatMessage", Answer.fixture("unauthorized"))
    caplog.set_level(logging.INFO)
    await messenger.send_text(DEV_GROUP, TEXT, timeout=TIMEOUT)
    await messenger.delete(DEV_GROUP, 5, timeout=TIMEOUT)
    with pytest.raises(AuthFailed):
        await messenger.pin(DEV_GROUP, 5, timeout=TIMEOUT)
    lines = [
        (record.getMessage(), getattr(record, LOG_FIELDS_ATTR))
        for record in caplog.records
        if record.name == messenger_module.__name__
    ]
    assert [(event, fields["method"], fields["outcome"], fields["attempts"]) for event, fields in lines] == [
        ("telegram_call_finished", "sendMessage", "ok", 2),
        ("telegram_call_finished", "deleteMessage", "refused_message_cant_be_deleted", 1),
        ("telegram_call_failed", "pinChatMessage", "auth_failed", 1),
    ]
    assert all(isinstance(fields["duration_ms"], int) for _, fields in lines)
    assert lines[2][1]["reason"] == "unauthorized"
    assert all(fields["chat_id"] == DEV_GROUP for _, fields in lines)


async def test_close_closes_the_session(session: RecordingSession) -> None:
    adapter = TelegramMessenger(TOKEN, session=session)
    assert adapter.bot.session is session
    await adapter.close()
