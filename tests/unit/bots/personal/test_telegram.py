"""`bots/personal/telegram.py`: an admitted aiogram message → the `IncomingUpdate` the inbox takes."""

from typing import Any

from aiogram.types import Message

from tests.factories import OWNER_ID, TELEGRAM_DATE, make_owner_origin, tg_chat, tg_user
from yordamchi.bots.personal import telegram
from yordamchi.core.paths import BotName
from yordamchi.shared.inbox.domain import UpdateKind

ORIGIN = make_owner_origin(BotName.PERSONAL, update_id=91)


def _message(**body: Any) -> Message:  # noqa: ANN401  # Bot API JSON
    raw = {"message_id": 601, "date": TELEGRAM_DATE, "chat": tg_chat(OWNER_ID), "from": tg_user(OWNER_ID), **body}
    return Message.model_validate(raw)


def test_a_text_with_a_link_preview_is_read_without_a_crash() -> None:
    """The owner's message of 2026-10-31 (a key, a link, a request) crashed intake: aiogram fills the unset fields of
    `link_preview_options` with a `Default` sentinel that does not serialise."""
    text = "https://crm.example.uz/ mana bu tizim uchun api key"
    update = telegram.incoming_of(ORIGIN, _message(text=text, link_preview_options={"url": "https://crm.example.uz/"}))
    assert (update.kind, update.text, update.forward) == (UpdateKind.TEXT, text, None)
