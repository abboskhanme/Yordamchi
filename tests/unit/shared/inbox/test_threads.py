"""Intake in a forum dev group (D4 revised 2026-10-26; SPEC §3.2.16): the topic a message was written in is kept with
it, its typing hint and masked reply go there, and what it says of the chat's topics reaches `shared/topics`."""

from dataclasses import replace
from typing import Final

from tests.factories import DEV_GROUP_ID, FAKE_KEYS, Fakes, make_update, origin_of
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.inbox import domain, service
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.inbox.domain import IncomingUpdate, UpdateKind, UpdateStatus
from yordamchi.shared.outbox import store as outbox_store
from yordamchi.shared.topics import service as topics
from yordamchi.shared.topics import store as topics_store

TOPIC: Final = 501


def _in_topic(update: IncomingUpdate) -> IncomingUpdate:
    return replace(update, message_thread_id=TOPIC, is_topic_message=True, is_forum=True)


async def test_a_topic_message_keeps_its_topic_for_the_burst(dev_deps: DevDeps) -> None:
    update = _in_topic(make_update(1, text="testlarini ham yoz"))
    await service.accept_update(dev_deps, origin_of(update), update)
    [stored] = await service.list_received_updates(dev_deps, limit=10)
    assert stored.thread_id == TOPIC


async def test_a_reply_in_general_has_a_thread_id_but_no_topic(dev_deps: DevDeps) -> None:
    update = replace(make_update(1, text="shu"), message_thread_id=4321, is_topic_message=False, is_forum=True)
    assert update.thread_id is None
    await service.accept_update(dev_deps, origin_of(update), update)
    [stored] = await service.list_received_updates(dev_deps, limit=10)
    assert stored.thread_id is None
    async with dev_deps.db.read() as s:
        row = await inbox_store.get_inbound_update(s, "dev", 1)
    assert row is not None
    assert isinstance(row.payload_json, dict)
    assert "thread_id" not in row.payload_json  # a General message keeps the shape it always had


async def test_typing_and_the_masked_reply_go_to_the_topic(dev_deps: DevDeps) -> None:
    update = _in_topic(make_update(1, text=f"kalit: {FAKE_KEYS['google_api_key']}"))
    await service.accept_update(dev_deps, origin_of(update), update)
    async with dev_deps.db.read() as s:
        rows = await outbox_store.list_outbox_messages(s, "dev")
    threads = {row.op: row.thread_id for row in rows}
    assert threads["delete"] is None  # a delete names its message
    assert threads["send_text"] == TOPIC
    assert threads["chat_action"] == TOPIC


async def test_each_group_message_tells_whether_the_chat_is_a_forum(dev_deps: DevDeps) -> None:
    for update_id, is_forum in ((1, True), (2, True), (3, False)):
        update = replace(make_update(update_id, text="salom"), is_forum=is_forum)
        await service.accept_update(dev_deps, origin_of(update), update)
        async with dev_deps.db.read() as s:
            chat = await topics_store.get_topic_chat(s, DEV_GROUP_ID)
        assert chat is not None
        assert chat.is_forum is is_forum


async def test_a_topic_service_message_is_kept_for_the_record_and_closes_the_project_topic(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    async with dev_deps.db.uow() as s:
        await topics.note_chat(s, chat_id=DEV_GROUP_ID, is_forum=True, at=fakes.clock.now())
        await topics_store.set_project_topic(
            s, project_id="BonviHR", chat_id=DEV_GROUP_ID, thread_id=TOPIC, at=fakes.clock.now()
        )
    closed = replace(
        _in_topic(make_update(1, text=None, kind=UpdateKind.SERVICE)), topic_closed=True, message_id=TOPIC + 9
    )
    accepted = await service.accept_update(dev_deps, origin_of(closed), closed)
    assert accepted.status is UpdateStatus.IGNORED  # no request: never a burst
    assert await service.list_received_updates(dev_deps, limit=10) == ()
    async with dev_deps.db.read() as s:
        assert await topics.thread_of_project(s, chat_id=DEV_GROUP_ID, project_id="BonviHR") is None
        assert await outbox_store.list_outbox_messages(s, "dev") == []  # no read mark, no typing: nothing to answer

    reopened = replace(closed, update_id=2, topic_closed=False, message_id=TOPIC + 10)
    await service.accept_update(dev_deps, origin_of(reopened), reopened)
    async with dev_deps.db.read() as s:
        assert await topics.thread_of_project(s, chat_id=DEV_GROUP_ID, project_id="BonviHR") == TOPIC


def test_the_topic_is_stored_beside_the_content_and_read_back() -> None:
    stored = domain.payload_json(domain.Content(text="salom"), lambda text: text, thread_id=TOPIC)
    assert domain.thread_of(stored) == TOPIC
    assert domain.thread_of(domain.payload_json(domain.Content(text="salom"), lambda text: text)) is None
    assert domain.thread_of(None) is None
