"""Forward provenance in intake (NFR-SEC-09; T33 bug: a forwarded message was indistinguishable from the owner's own
words). `inbox.domain.forward_of` is the Telegram -> `IncomingUpdate` translation the dev handler (T34) calls; the
source is stored with the redacted content and read back by every later stage."""

import pytest

from tests.factories import make_update, origin_of
from yordamchi.core.json_value import JsonValue
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.inbox import domain, service
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import Content, ForwardSource

SENDER = {"id": 200000002, "is_bot": False, "first_name": "Aziz"}
CHANNEL = {"id": -1001112223334, "type": "channel", "title": "Yangiliklar"}
GROUP = {"id": -1005556667778, "type": "supergroup", "title": "Jamoa"}


@pytest.mark.parametrize(
    ("message", "source"),
    [
        ({"forward_origin": {"type": "user", "date": 1, "sender_user": SENDER}}, ForwardSource.USER),
        ({"forward_origin": {"type": "hidden_user", "date": 1, "sender_user_name": "Aziz"}}, ForwardSource.HIDDEN_USER),
        ({"forward_origin": {"type": "chat", "date": 1, "sender_chat": GROUP}}, ForwardSource.CHAT),
        ({"forward_origin": {"type": "channel", "date": 1, "chat": CHANNEL, "message_id": 7}}, ForwardSource.CHANNEL),
        ({"forward_origin": {"type": "a_type_from_a_newer_api", "date": 1}}, ForwardSource.UNKNOWN),
        ({"forward_origin": {"date": 1}}, ForwardSource.UNKNOWN),
        ({"forward_origin": "garbled"}, ForwardSource.UNKNOWN),
        ({"forward_from": SENDER, "forward_date": 1}, ForwardSource.USER),
        ({"forward_sender_name": "Aziz", "forward_date": 1}, ForwardSource.HIDDEN_USER),
        ({"forward_from_chat": CHANNEL, "forward_date": 1}, ForwardSource.CHANNEL),
        ({"forward_from_chat": GROUP, "forward_date": 1}, ForwardSource.CHAT),
        ({"forward_from_chat": "garbled"}, ForwardSource.UNKNOWN),
        ({"is_automatic_forward": True}, ForwardSource.CHANNEL),
        ({"forward_date": 1}, ForwardSource.UNKNOWN),
    ],
    ids=lambda value: str(value)[:48],
)
def test_a_forwarded_message_says_where_it_came_from(message: dict[str, object], source: ForwardSource) -> None:
    assert domain.forward_of({"message_id": 9, "text": "ha", **message}) is source


def test_the_owners_own_message_is_not_forwarded() -> None:
    own = {
        "message_id": 9,
        "text": "ha",
        "reply_to_message": {"message_id": 8, "text": "Qo'llaymi? (ha / yo'q)"},
        "quote": {"text": "ha", "position": 0},  # quoting a message is the owner's own words
        "forward_origin": None,
        "is_automatic_forward": False,
    }
    assert domain.forward_of(own) is None


def test_the_forward_survives_the_payload_and_the_owners_own_payload_keeps_its_shape() -> None:
    forwarded = Content(text="ha", forward=ForwardSource.USER)
    stored = domain.payload_json(forwarded, str)
    assert stored["forward"] == "user"
    assert domain.content_from_json(stored) == forwarded
    assert domain.content_from_json(stored).forwarded
    assert "forward" not in domain.payload_json(Content(text="ha"), str)
    assert not domain.content_from_json(domain.payload_json(Content(text="ha"), str)).forwarded


@pytest.mark.parametrize("raw", [5, "something_else", True], ids=repr)
def test_an_unreadable_stored_forward_still_counts_as_forwarded(raw: JsonValue) -> None:
    content = domain.content_from_json({"text": "ha", "forward": raw})
    assert content.forward is ForwardSource.UNKNOWN


async def test_intake_stores_the_forward_with_the_update(dev_deps: DevDeps) -> None:
    forwarded = make_update(31, text="ha", forward=ForwardSource.CHANNEL)
    own = make_update(32, text="ha")
    for update in (forwarded, own):
        await service.accept_update(dev_deps, origin_of(update), update)
    received = {update.update_id: update for update in await service.list_received_updates(dev_deps, limit=10)}
    assert received[31].content.forward is ForwardSource.CHANNEL
    assert received[32].content.forward is None
    async with dev_deps.db.read() as s:
        rows = {row.update_id: row for row in await inbox_store.list_inbound_updates(s, dev_deps.bot.value)}
    assert isinstance(rows[31].payload_json, dict)
    assert rows[31].payload_json["forward"] == "channel"
    assert isinstance(rows[32].payload_json, dict)
    assert "forward" not in rows[32].payload_json
