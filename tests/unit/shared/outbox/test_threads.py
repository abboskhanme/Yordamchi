"""The outbox in a forum dev group (D4 revised 2026-10-26; SPEC §3.2.16): where each row goes, decided when it is
written; typing per topic; and a send into a topic the owner closed or deleted, repaired instead of given up."""

from typing import Final

from tests.factories import DEV_GROUP_ID, Fakes, drain_outbox, send_outbox
from yordamchi.core.errors import CallTimedOut
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.shared.outbox import domain, service, store
from yordamchi.shared.outbox.domain import GENERAL, About, Kind, Thread
from yordamchi.shared.outbox.models import OutboxMessage
from yordamchi.shared.topics import service as topics

GROUP: Final = DEV_GROUP_ID
ABOUT_BONVI: Final = About(project_id="BonviHR", turn_id=7)


async def _open_topic(deps: DevDeps, project: str = "BonviHR") -> int:
    async with deps.db.uow() as s:
        await topics.note_chat(s, chat_id=GROUP, is_forum=True, at=deps.clock.now())
    ensured = await topics.ensure_topic(
        deps, chat_id=GROUP, project_id=project, enabled=True, retry_after_s=60.0, timeout=5.0
    )
    assert ensured.thread_id is not None
    return ensured.thread_id


async def _say(
    deps: DevDeps, text: str, *, ref: str, about: About = service.NO_ABOUT, thread: Thread | None = None
) -> None:
    async with deps.db.uow() as s:
        await service.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=GROUP,
            text=BotText(text),
            kind=Kind.RESULT,
            ref=ref,
            at=deps.clock.now(),
            max_parts=3,
            about=about,
            thread=thread,
        )


async def _row(deps: DevDeps, key: str) -> OutboxMessage:
    async with deps.db.read() as s:
        row = await store.find_outbox_message(s, key)
    assert row is not None, key
    return row


def _sent(fakes: Fakes) -> list[tuple[object, object]]:
    """(text, thread) of every send, in order; the thread is absent for a send to the chat itself."""
    return [(r.payload["text"], r.payload.get("thread_id")) for r in fakes.messenger.sent("send_text")]


async def test_a_row_about_a_project_goes_to_its_open_topic_and_any_other_to_general(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    thread = await _open_topic(dev_deps)
    await _say(dev_deps, "BonviHR · Natija: tayyor", ref="r1", about=ABOUT_BONVI)
    await _say(dev_deps, "HRMentor · Natija: tayyor", ref="r2", about=About(project_id="HRMentor"))  # no topic
    await _say(dev_deps, "Onlayn: 09:00", ref="r3")
    assert (await _row(dev_deps, "result:r1:0")).thread_id == thread  # decided at the write, kept in the row
    await send_outbox(dev_deps)
    await drain_outbox(dev_deps, fakes.clock, seconds=3)
    assert _sent(fakes) == [
        ("BonviHR · Natija: tayyor", thread),
        ("HRMentor · Natija: tayyor", None),
        ("Onlayn: 09:00", None),
    ]


async def test_the_callers_thread_wins_over_the_projects_topic(dev_deps: DevDeps, fakes: Fakes) -> None:
    """An answer goes where the owner asked: "BonviHR qanday?" in General is answered in General."""
    await _open_topic(dev_deps)
    await _say(dev_deps, "BonviHR · hammasi joyida", ref="r1", about=ABOUT_BONVI, thread=GENERAL)
    await _say(dev_deps, "Arzimaydi!", ref="r2", thread=Thread(777))  # small talk in some topic
    await drain_outbox(dev_deps, fakes.clock, seconds=3)
    assert _sent(fakes) == [("BonviHR · hammasi joyida", None), ("Arzimaydi!", 777)]


async def test_a_file_about_a_project_goes_to_its_topic(dev_deps: DevDeps) -> None:
    thread = await _open_topic(dev_deps)
    async with dev_deps.db.uow() as s:
        await service.enqueue_file(
            s,
            bot=dev_deps.bot,
            chat_id=GROUP,
            op=domain.Op.SEND_DOCUMENT,
            path=dev_deps.paths.media / "plan.md",
            kind=Kind.FILE,
            ref="doc-1",
            at=dev_deps.clock.now(),
            max_parts=3,
            about=ABOUT_BONVI,
        )
    assert (await _row(dev_deps, "file:doc-1:0")).thread_id == thread


async def test_edits_deletes_and_reactions_carry_no_thread(dev_deps: DevDeps) -> None:
    await _open_topic(dev_deps)
    async with dev_deps.db.uow() as s:
        await service.enqueue_reaction(
            s, bot=dev_deps.bot, chat_id=GROUP, message_id=5, emoji="👀", ref="u1", at=dev_deps.clock.now()
        )
        await service.enqueue_delete(
            s, bot=dev_deps.bot, chat_id=GROUP, message_id=5, ref="u1", at=dev_deps.clock.now()
        )
    async with dev_deps.db.read() as s:
        rows = await store.list_outbox_messages(s, dev_deps.bot.value)
    assert [row.thread_id for row in rows] == [None, None]


async def test_typing_shows_in_the_topic_written_in_and_a_reply_there_ends_only_that_one(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    now = dev_deps.clock.now()
    async with dev_deps.db.uow() as s:
        await service.request_typing(s, bot=dev_deps.bot, chat_id=GROUP, ref="update-1", at=now, thread=Thread(501))
        await service.request_typing(s, bot=dev_deps.bot, chat_id=GROUP, ref="update-2", at=now)  # General
    await send_outbox(dev_deps)
    hints = [r.payload.get("thread_id") for r in fakes.messenger.sent("chat_action")]
    assert sorted(hints, key=str) == sorted([501, None], key=str)  # two hints: one per topic, never one replacing
    await _say(dev_deps, "Arzimaydi!", ref="r1", thread=Thread(501))
    await drain_outbox(dev_deps, fakes.clock, seconds=1)
    async with dev_deps.db.read() as s:
        rows = [row for row in await store.list_outbox_messages(s, dev_deps.bot.value) if row.op == "chat_action"]
    by_thread = {row.thread_id: row.status for row in rows}
    assert by_thread[501] == "cancelled"  # the reply in that topic ended its hint
    assert by_thread[None] == "pending"  # General's hint still repeats


async def test_stop_typing_in_one_topic_leaves_the_others(dev_deps: DevDeps) -> None:
    now = dev_deps.clock.now()
    async with dev_deps.db.uow() as s:
        await service.request_typing(s, bot=dev_deps.bot, chat_id=GROUP, ref="update-1", at=now, thread=Thread(501))
        await service.request_typing(s, bot=dev_deps.bot, chat_id=GROUP, ref="update-2", at=now, thread=GENERAL)
        assert await service.stop_typing(s, bot=dev_deps.bot, chat_id=GROUP, thread=GENERAL) == 1
        assert await service.stop_typing(s, bot=dev_deps.bot, chat_id=GROUP) == 1  # every topic: the one left


async def test_a_send_into_a_deleted_topic_goes_to_the_projects_new_topic_with_the_rows_behind_it(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    thread = await _open_topic(dev_deps)
    await _say(dev_deps, "BonviHR · birinchi", ref="r1", about=ABOUT_BONVI)
    await _say(dev_deps, "BonviHR · ikkinchi", ref="r2", about=ABOUT_BONVI)
    fakes.messenger.delete_topic(GROUP, thread)  # the owner deleted it after both were queued
    await drain_outbox(dev_deps, fakes.clock, seconds=3)
    assert len(fakes.messenger.sent("create_forum_topic")) == 2  # opened anew, once for both rows
    async with dev_deps.db.read() as s:
        now_thread = await topics.thread_of_project(s, chat_id=GROUP, project_id="BonviHR")
    assert now_thread is not None
    assert now_thread != thread
    assert _sent(fakes) == [("BonviHR · birinchi", now_thread), ("BonviHR · ikkinchi", now_thread)]
    first = await _row(dev_deps, "result:r1:0")
    assert (first.status, first.thread_id) == ("sent", now_thread)


async def test_a_send_into_a_closed_topic_reopens_it_and_goes_there(dev_deps: DevDeps, fakes: Fakes) -> None:
    thread = await _open_topic(dev_deps)
    fakes.messenger.closed_topics.add((GROUP, thread))
    await _say(dev_deps, "BonviHR · Natija: tayyor", ref="r1", about=ABOUT_BONVI)
    await drain_outbox(dev_deps, fakes.clock, seconds=2)
    assert [r.payload["thread_id"] for r in fakes.messenger.sent("reopen_forum_topic")] == [thread]
    assert _sent(fakes) == [("BonviHR · Natija: tayyor", thread)]


async def test_a_send_into_a_gone_topic_no_project_owns_goes_to_general(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.messenger.deleted_topics.add((GROUP, 777))
    await _say(dev_deps, "Arzimaydi!", ref="r1", thread=Thread(777))
    await drain_outbox(dev_deps, fakes.clock, seconds=2)
    assert _sent(fakes) == [("Arzimaydi!", None)]
    assert fakes.messenger.sent("create_forum_topic") == []


async def test_a_repair_telegram_does_not_answer_retries_the_row_later(dev_deps: DevDeps, fakes: Fakes) -> None:
    thread = await _open_topic(dev_deps)
    fakes.messenger.delete_topic(GROUP, thread)
    fakes.messenger.fail_next("create_forum_topic", CallTimedOut(operation="createForumTopic"))
    await _say(dev_deps, "BonviHR · Natija: tayyor", ref="r1", about=ABOUT_BONVI)
    await send_outbox(dev_deps)
    row = await _row(dev_deps, "result:r1:0")
    assert (row.status, row.thread_id, row.last_error_code) == ("pending", thread, "call_timed_out")
    await drain_outbox(dev_deps, fakes.clock, seconds=5)
    assert [text for text, _ in _sent(fakes)] == ["BonviHR · Natija: tayyor"]
    assert (await _row(dev_deps, "result:r1:0")).status == "sent"
