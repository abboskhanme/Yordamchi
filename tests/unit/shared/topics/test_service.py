"""`shared/topics/service.py` on a real SQLite file and the fake Messenger (D4 revised 2026-10-26; SPEC §3.2.16)."""

from typing import Final

import pytest

from tests.factories import DEV_GROUP_ID, Fakes
from yordamchi.core.errors import CallTimedOut
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.topics import service as topics
from yordamchi.shared.topics import store
from yordamchi.shared.topics.domain import icon_color_of
from yordamchi.shared.topics.service import Ensured

CHAT: Final = DEV_GROUP_ID
RETRY_S: Final = 1800.0
TIMEOUT: Final = 5.0


async def _forum(deps: DevDeps, *, is_forum: bool = True) -> None:
    async with deps.db.uow() as s:
        await topics.note_chat(s, chat_id=CHAT, is_forum=is_forum, at=deps.clock.now())


async def _ensure(deps: DevDeps, project: str = "BonviHR", *, enabled: bool = True) -> Ensured:
    return await topics.ensure_topic(
        deps, chat_id=CHAT, project_id=project, enabled=enabled, retry_after_s=RETRY_S, timeout=TIMEOUT
    )


async def _thread(deps: DevDeps, project: str = "BonviHR") -> int | None:
    async with deps.db.read() as s:
        return await topics.thread_of_project(s, chat_id=CHAT, project_id=project)


def _created(fakes: Fakes) -> list[dict[str, object]]:
    return [request.payload for request in fakes.messenger.sent("create_forum_topic")]


async def test_a_projects_first_work_opens_its_topic_and_later_work_reuses_it(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _forum(dev_deps)
    first = await _ensure(dev_deps)
    assert first.thread_id is not None
    assert not first.refused
    assert _created(fakes) == [{"name": "BonviHR", "icon_color": icon_color_of("BonviHR"), "timeout": TIMEOUT}]
    assert await _thread(dev_deps) == first.thread_id
    assert await topics.project_of_thread(dev_deps, chat_id=CHAT, thread_id=first.thread_id) == "BonviHR"

    again = await _ensure(dev_deps)
    assert again == first
    assert len(_created(fakes)) == 1  # no second topic for the same project


async def test_no_topic_is_opened_where_topics_are_off_or_the_setting_is(dev_deps: DevDeps, fakes: Fakes) -> None:
    assert await _ensure(dev_deps) == Ensured(None)  # the chat was never seen as a forum
    await _forum(dev_deps, is_forum=False)
    assert await _ensure(dev_deps) == Ensured(None)
    await _forum(dev_deps)
    assert await _ensure(dev_deps, enabled=False) == Ensured(None)
    assert _created(fakes) == []


async def test_a_topic_of_a_chat_that_stopped_being_a_forum_takes_nothing(dev_deps: DevDeps) -> None:
    await _forum(dev_deps)
    assert (await _ensure(dev_deps)).thread_id is not None
    await _forum(dev_deps, is_forum=False)  # the owner turned Topics off
    assert await _thread(dev_deps) is None


async def test_without_the_right_the_work_goes_to_general_and_the_refusal_is_told_once(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await _forum(dev_deps)
    fakes.messenger.no_topic_right.add(CHAT)
    assert await _ensure(dev_deps) == Ensured(None, refused=True)
    assert await _ensure(dev_deps, "HRMentor") == Ensured(None)  # within `topics.retry_after_min`: not asked
    assert len(_created(fakes)) == 1
    await fakes.clock.advance(RETRY_S + 1)
    assert await _ensure(dev_deps) == Ensured(None)  # asked again, refused again, but not told again
    assert len(_created(fakes)) == 2

    fakes.messenger.no_topic_right.clear()  # the owner gave the right
    await fakes.clock.advance(RETRY_S + 1)
    assert (await _ensure(dev_deps)).thread_id is not None
    async with dev_deps.db.read() as s:
        chat = await store.get_topic_chat(s, CHAT)
    assert chat is not None
    assert (chat.refused_at, chat.refusal) == (None, None)


async def test_telegram_not_answering_never_holds_the_work(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _forum(dev_deps)
    fakes.messenger.fail_next("create_forum_topic", CallTimedOut(operation="createForumTopic"))
    assert await _ensure(dev_deps) == Ensured(None)
    assert (await _ensure(dev_deps)).thread_id is not None  # the next work opens it


async def test_a_topic_the_owner_closed_is_reopened_by_the_next_work(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _forum(dev_deps)
    thread = (await _ensure(dev_deps)).thread_id
    assert thread is not None
    fakes.messenger.closed_topics.add((CHAT, thread))
    async with dev_deps.db.uow() as s:
        await topics.note_topic_event(s, chat_id=CHAT, thread_id=thread, closed=True, at=dev_deps.clock.now())
    assert await _thread(dev_deps) is None  # nothing is posted into a closed topic
    assert await _ensure(dev_deps) == Ensured(thread)
    assert [r.payload["thread_id"] for r in fakes.messenger.sent("reopen_forum_topic")] == [thread]
    assert await _thread(dev_deps) == thread


async def test_a_closed_topic_that_is_gone_meanwhile_is_opened_anew(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _forum(dev_deps)
    thread = (await _ensure(dev_deps)).thread_id
    assert thread is not None
    async with dev_deps.db.uow() as s:
        await topics.note_topic_event(s, chat_id=CHAT, thread_id=thread, closed=True, at=dev_deps.clock.now())
    fakes.messenger.delete_topic(CHAT, thread)
    renewed = (await _ensure(dev_deps)).thread_id
    assert renewed is not None
    assert renewed != thread


@pytest.mark.parametrize("closed", [True, False], ids=["closed", "gone"])
async def test_a_failed_send_reopens_or_replaces_the_topic(dev_deps: DevDeps, fakes: Fakes, closed: bool) -> None:
    await _forum(dev_deps)
    thread = (await _ensure(dev_deps)).thread_id
    assert thread is not None
    if not closed:
        fakes.messenger.delete_topic(CHAT, thread)
    repaired = await topics.repair_thread(dev_deps, chat_id=CHAT, thread_id=thread, closed=closed, timeout=TIMEOUT)
    assert repaired is not None
    assert (repaired == thread) is closed
    assert await _thread(dev_deps) == repaired
    assert await topics.project_of_thread(dev_deps, chat_id=CHAT, thread_id=repaired) == "BonviHR"


async def test_a_thread_no_project_owns_is_repaired_to_general(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _forum(dev_deps)
    assert await topics.repair_thread(dev_deps, chat_id=CHAT, thread_id=999, closed=False, timeout=TIMEOUT) is None
    assert _created(fakes) == []


async def test_a_gone_topic_that_cannot_be_replaced_is_forgotten(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _forum(dev_deps)
    thread = (await _ensure(dev_deps)).thread_id
    assert thread is not None
    fakes.messenger.delete_topic(CHAT, thread)
    fakes.messenger.no_topic_right.add(CHAT)
    assert await topics.repair_thread(dev_deps, chat_id=CHAT, thread_id=thread, closed=False, timeout=TIMEOUT) is None
    async with dev_deps.db.read() as s:
        assert await store.get_project_topic(s, "BonviHR") is None


async def test_a_repair_that_telegram_does_not_answer_propagates(dev_deps: DevDeps, fakes: Fakes) -> None:
    """The sender retries the row later: a network failure is no reason to send it to General."""
    await _forum(dev_deps)
    thread = (await _ensure(dev_deps)).thread_id
    assert thread is not None
    fakes.messenger.fail_next("create_forum_topic", CallTimedOut(operation="createForumTopic"))
    with pytest.raises(CallTimedOut):
        await topics.repair_thread(dev_deps, chat_id=CHAT, thread_id=thread, closed=False, timeout=TIMEOUT)
    assert await _thread(dev_deps) == thread  # unchanged


async def test_topics_set_aside_take_nothing_until_the_next_work_reopens_them(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _forum(dev_deps)
    first = (await _ensure(dev_deps)).thread_id
    second = (await _ensure(dev_deps, "HRMentor")).thread_id
    assert first is not None
    assert second is not None
    assert await topics.set_topics_aside(dev_deps, chat_id=CHAT) == 2
    assert await _thread(dev_deps) is None
    assert await _thread(dev_deps, "HRMentor") is None
    assert await topics.set_topics_aside(dev_deps, chat_id=CHAT) == 0
    assert await _ensure(dev_deps) == Ensured(first)  # on again: reopened (it was open on Telegram's side)
    assert await _thread(dev_deps, "HRMentor") is None  # only with its own next work
