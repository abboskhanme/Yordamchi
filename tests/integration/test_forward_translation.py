"""`inbox.domain.forward_of` on what aiogram hands the dev handler (T34): `Message.model_validate(<Bot API JSON>)`,
then `message.model_dump(mode="json", exclude_none=True)`. A forwarded message keeps its provenance through aiogram;
the owner's own message and a reply to one come out unforwarded."""

import pytest
from aiogram.types import Message

from tests.factories import DEV_GROUP_ID, OWNER_ID, TELEGRAM_DATE, tg_chat, tg_user
from yordamchi.shared.inbox import domain
from yordamchi.shared.inbox.domain import ForwardSource

STRANGER = {"id": 200000002, "is_bot": False, "first_name": "Aziz"}


def _message(**extra: object) -> dict[str, object]:
    return {
        "message_id": 41,
        "date": TELEGRAM_DATE,
        "chat": tg_chat(DEV_GROUP_ID),
        "from": tg_user(OWNER_ID),
        "text": "ha",
        **extra,
    }


@pytest.mark.parametrize(
    ("origin", "source"),
    [
        ({"type": "user", "date": TELEGRAM_DATE, "sender_user": STRANGER}, ForwardSource.USER),
        ({"type": "hidden_user", "date": TELEGRAM_DATE, "sender_user_name": "Aziz"}, ForwardSource.HIDDEN_USER),
        (
            {
                "type": "channel",
                "date": TELEGRAM_DATE,
                "chat": {"id": -1001112223334, "type": "channel"},
                "message_id": 3,
            },
            ForwardSource.CHANNEL,
        ),
        (
            {"type": "chat", "date": TELEGRAM_DATE, "sender_chat": {"id": -1005556667778, "type": "supergroup"}},
            ForwardSource.CHAT,
        ),
    ],
    ids=["user", "hidden_user", "channel", "chat"],
)
def test_aiogram_keeps_the_forward_origin(origin: dict[str, object], source: ForwardSource) -> None:
    message = Message.model_validate(_message(forward_origin=origin))
    assert domain.forward_of(message.model_dump(mode="json", exclude_none=True)) is source
    assert domain.forward_of(message.model_dump(mode="json")) is source


def test_the_owners_own_message_and_his_reply_are_not_forwarded() -> None:
    reply = _message(reply_to_message=_message(message_id=40, text="Qo'llaymi? (ha / yo'q)"))
    for raw in (_message(), reply):
        message = Message.model_validate(raw)
        assert domain.forward_of(message.model_dump(mode="json", exclude_none=True)) is None
        assert domain.forward_of(message.model_dump(mode="json")) is None
