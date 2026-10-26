"""outbox.service on a real SQLite file with `FakeMessenger`: durable sends and their links, dedupe, the split and the
`.md` file, order and rate limits, parking on `retry_after`, restart recovery, unknown deliveries, progress
coalescing, typing, scrubbing before the call, the keyboard guard, bounded calls, the sender loop, shutdown flush and
retention (SPEC §2.6, §2.7, §3.2.1, §4.6; NFR-REL-06, NFR-CAP-02/03, NFR-SEC-03, UC-11, UC-12)."""

import asyncio
import itertools
import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Final

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    FAKE_KEYS,
    OWNER_ID,
    UNKNOWN_FORMAT_KEY,
    Fakes,
    add_outbox_row,
    drain_outbox,
    send_outbox,
)
from tests.fakes.messenger import FakeMessenger
from yordamchi.core import aio, redact
from yordamchi.core import strings as S
from yordamchi.core.errors import CallTimedOut, MarkupRejected, ProviderUnavailable, RateLimited
from yordamchi.core.json_value import JsonValue
from yordamchi.core.text import BotText
from yordamchi.dev.deps import DevDeps
from yordamchi.ports.messenger import ChatAction, OutboundFile, SentMessage
from yordamchi.shared.outbox import domain, service, store
from yordamchi.shared.outbox.domain import About, Kind, Op, PayloadInvalid, SenderConfig, Status
from yordamchi.shared.outbox.models import OutboxMessage
from yordamchi.shared.outbox.service import SenderState

GROUP = DEV_GROUP_ID
PRIVATE = OWNER_ID
OTHER_GROUP = -1005555555555
TEXT_TIMEOUT = domain.DEFAULT_CONFIG.text_timeout_s
FILE_TIMEOUT = domain.DEFAULT_CONFIG.file_timeout_s


async def _enqueue(
    deps: DevDeps,
    text: str,
    *,
    ref: str = "turn-1",
    kind: Kind = Kind.RESULT,
    chat_id: int = GROUP,
    max_parts: int = 3,
    reply_to: int | None = None,
    disable_notification: bool = False,
    about: About = service.NO_ABOUT,
) -> tuple[int, ...]:
    async with deps.db.uow() as s:
        return await service.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat_id,
            text=BotText(text),
            kind=kind,
            ref=ref,
            at=deps.clock.now(),
            max_parts=max_parts,
            reply_to=reply_to,
            disable_notification=disable_notification,
            about=about,
        )


async def _rows(deps: DevDeps) -> list[OutboxMessage]:
    async with deps.db.read() as s:
        return await store.list_outbox_messages(s, deps.bot.value)


async def _row(deps: DevDeps, key: str) -> OutboxMessage:
    async with deps.db.read() as s:
        row = await store.find_outbox_message(s, key)
    assert row is not None, key
    return row


def _texts(fakes: Fakes, method: str = "send_text") -> list[object]:
    return [request.payload["text"] for request in fakes.messenger.sent(method)]


async def _until(condition: Callable[[], bool], fakes: Fakes) -> None:
    """Let a background sender run: advance virtual time and yield real time until `condition` holds."""
    for _ in range(200):
        if condition():
            return
        await fakes.clock.advance(0.25)
        await asyncio.sleep(0.01)
    pytest.fail("the sender did not get there")


# --- a message, end to end -----------------------------------------------------------------------------------------
async def test_a_queued_text_is_sent_once_with_its_options_and_linked(dev_deps: DevDeps, fakes: Fakes) -> None:
    about = About(project_id="BonviHR", turn_id=17)
    [row_id] = await _enqueue(dev_deps, "BonviHR · Natija: tayyor", reply_to=55, disable_notification=True, about=about)
    assert fakes.messenger.requests == []  # nothing is sent inside the caller's transaction

    assert await send_outbox(dev_deps) == 1
    [request] = fakes.messenger.sent()
    assert (request.chat_id, request.payload) == (
        GROUP,
        {
            "text": "BonviHR · Natija: tayyor",
            "reply_to": 55,
            "parse_mode": None,
            "disable_notification": True,
            "timeout": TEXT_TIMEOUT,
        },
    )
    [row] = await _rows(dev_deps)
    assert (row.id, row.status, row.attempts, row.sent_at, row.priority) == (
        row_id,
        "sent",
        1,
        fakes.clock.now(),
        domain.DEFAULT_PRIORITY[Kind.RESULT],
    )
    link = await service.find_message_link(dev_deps, chat_id=GROUP, message_id=101)
    assert link is not None
    assert (link.kind, link.about, link.created_at) == (Kind.RESULT, about, fakes.clock.now())
    assert await service.find_message_link(dev_deps, chat_id=GROUP, message_id=999) is None
    delivery = await service.get_delivery(dev_deps, "result:turn-1:0")
    assert delivery is not None
    assert (delivery.status, delivery.message_id) == (Status.SENT, 101)
    assert await send_outbox(dev_deps) == 0  # sent once


async def test_the_same_ref_queued_twice_is_one_message(dev_deps: DevDeps, fakes: Fakes) -> None:
    assert len(await _enqueue(dev_deps, "birinchi")) == 1
    assert await _enqueue(dev_deps, "ikkinchi, xuddi shu ref") == ()
    await drain_outbox(dev_deps, fakes.clock, seconds=3)
    assert _texts(fakes) == ["birinchi"]


async def test_a_long_text_goes_as_parts_in_order_one_second_apart(dev_deps: DevDeps, fakes: Fakes) -> None:
    text = "\n\n".join(f"{index}-band " + "so'z " * 600 for index in range(2))
    ids = await _enqueue(dev_deps, text, reply_to=40, about=About(project_id="BonviHR"))
    assert len(ids) == 2

    state = SenderState()
    assert await send_outbox(dev_deps, state) == 1  # the second part waits for the per-chat second
    await drain_outbox(dev_deps, fakes.clock, seconds=1, state=state)
    first, second = fakes.messenger.sent()
    assert str(first.payload["text"]).startswith("0-band")
    assert str(second.payload["text"]).startswith("1-band")
    assert (first.payload["reply_to"], second.payload["reply_to"]) == (40, None)
    rows = await _rows(dev_deps)
    assert [row.dedupe_key for row in rows] == ["result:turn-1:0", "result:turn-1:1"]
    sent_at = [row.sent_at for row in rows]
    assert sent_at[0] is not None
    assert sent_at[1] is not None
    assert sent_at[1] - sent_at[0] >= timedelta(seconds=1)
    for message_id in (101, 102):
        link = await service.find_message_link(dev_deps, chat_id=GROUP, message_id=message_id)
        assert link is not None
        assert link.about.project_id == "BonviHR"


class _ReadingMessenger(FakeMessenger):
    """Keeps the bytes of each uploaded document: the outbox removes its `.md` file after the call."""

    def __init__(self) -> None:
        super().__init__()
        self.documents: list[bytes] = []

    async def send_document(
        self,
        chat_id: int,
        document: OutboundFile,
        *,
        caption: BotText | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        thread_id: int | None = None,
        timeout: float,
    ) -> SentMessage:
        self.documents.append(document.path.read_bytes())
        return await super().send_document(
            chat_id,
            document,
            caption=caption,
            reply_to=reply_to,
            disable_notification=disable_notification,
            thread_id=thread_id,
            timeout=timeout,
        )


async def test_a_text_longer_than_max_parts_goes_as_one_md_file(dev_deps: DevDeps, fakes: Fakes) -> None:
    messenger = _ReadingMessenger()
    deps = replace(dev_deps, messenger=messenger)
    text = "BonviHR · Natija: to'liq hisobot\n\n" + "\n\n".join("band " * 700 for _ in range(5))
    [row_id] = await _enqueue(deps, text, ref="turn-9", max_parts=3)

    await send_outbox(deps)
    [request] = messenger.sent("send_document")
    assert request.payload["filename"] == "result-turn-9.md"
    assert request.payload["mime"] == "text/markdown"
    assert request.payload["caption"] == S.OUTBOX_TEXT_IN_FILE(summary="BonviHR · Natija: to'liq hisobot")
    assert request.payload["timeout"] == FILE_TIMEOUT
    assert messenger.documents == [text.encode()]
    assert not await asyncio.to_thread(Path(str(request.payload["path"])).exists)  # the cache copy is gone
    assert messenger.sent() == []
    assert (await _row(deps, "result:turn-9:0")).id == row_id
    assert await service.find_message_link(deps, chat_id=GROUP, message_id=101) is not None


async def test_code_blocks_go_as_html_and_plain_text_as_typed(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _enqueue(dev_deps, "BonviHR · xato:\n```py\nif a < b:\n```", ref="turn-1")
    await _enqueue(dev_deps, "BonviHR · a < b & c", ref="turn-2", chat_id=PRIVATE)
    await send_outbox(dev_deps)
    by_chat = {request.chat_id: request.payload for request in fakes.messenger.sent()}
    assert by_chat[GROUP]["parse_mode"] == "HTML"
    assert by_chat[GROUP]["text"] == 'BonviHR · xato:\n<pre><code class="language-py">if a &lt; b:</code></pre>'
    assert (by_chat[PRIVATE]["parse_mode"], by_chat[PRIVATE]["text"]) == (None, "BonviHR · a < b & c")


async def test_markup_goes_as_html_with_every_value_escaped(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _enqueue(dev_deps, "**BonviHR · Natija:** a < b & `make check`\n- tayyor")
    await send_outbox(dev_deps)
    [request] = fakes.messenger.sent()
    assert request.payload["parse_mode"] == "HTML"
    assert request.payload["text"] == "<b>BonviHR · Natija:</b> a &lt; b &amp; <code>make check</code>\n• tayyor"


MARKED: Final = "**BonviHR · Natija:** [PR](https://github.com/x/y/pull/7) tayyor"
STRIPPED: Final = "BonviHR · Natija: PR (https://github.com/x/y/pull/7) tayyor"


async def test_a_text_telegram_cannot_parse_is_sent_once_more_as_plain_text(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    fakes.messenger.fail_next("send_text", MarkupRejected(provider="telegram"))
    [row_id] = await _enqueue(dev_deps, MARKED, reply_to=55)
    with caplog.at_level(logging.WARNING):
        assert await send_outbox(dev_deps) == 1
    [request] = fakes.messenger.sent()  # the refused HTML posted nothing; the plain resend did
    assert (request.payload["text"], request.payload["parse_mode"], request.payload["reply_to"]) == (
        STRIPPED,
        None,
        55,
    )
    row = await _row(dev_deps, "result:turn-1:0")
    assert (row.id, row.status, row.attempts, row.last_error_code) == (row_id, "sent", 1, None)
    assert await service.find_message_link(dev_deps, chat_id=GROUP, message_id=101) is not None
    [record] = [record for record in caplog.records if record.getMessage() == "outbox_markup_rejected"]
    assert record.levelno == logging.WARNING


async def test_the_plain_resend_happens_once_and_a_second_refusal_gives_up(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.messenger.fail_next("send_text", MarkupRejected(provider="telegram"))
    fakes.messenger.fail_next("send_text", MarkupRejected(provider="telegram"))
    await _enqueue(dev_deps, MARKED, ref="turn-1")
    await _enqueue(dev_deps, "keyingisi", ref="turn-2")
    await drain_outbox(dev_deps, fakes.clock, seconds=3)
    assert _texts(fakes) == ["keyingisi"]
    row = await _row(dev_deps, "result:turn-1:0")
    assert (row.status, row.attempts, row.last_error_code) == ("failed", 1, "markup_rejected")


async def test_a_plain_text_refused_as_markup_is_not_sent_again(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.messenger.fail_next("send_text", MarkupRejected(provider="telegram"))
    await _enqueue(dev_deps, "oddiy matn")
    await drain_outbox(dev_deps, fakes.clock, seconds=3)
    assert fakes.messenger.sent() == []
    row = await _row(dev_deps, "result:turn-1:0")
    assert (row.status, row.last_error_code) == ("failed", "markup_rejected")


async def test_an_edit_telegram_cannot_parse_is_made_once_more_as_plain_text(dev_deps: DevDeps, fakes: Fakes) -> None:
    async with dev_deps.db.uow() as s:
        await service.enqueue_edit(
            s,
            bot=dev_deps.bot,
            chat_id=GROUP,
            text=BotText("**Onlayn:** 14:05"),
            kind=Kind.HEARTBEAT,
            ref="heartbeat",
            part="edit1",
            at=dev_deps.clock.now(),
            message_id=77,
        )
    fakes.messenger.fail_next("edit_text", MarkupRejected(provider="telegram"))
    assert await send_outbox(dev_deps) == 1
    [edit] = fakes.messenger.sent("edit_text")
    assert (edit.payload["message_id"], edit.payload["text"], edit.payload["parse_mode"]) == (77, "Onlayn: 14:05", None)
    assert (await _row(dev_deps, "heartbeat:heartbeat:edit1")).status == "sent"


async def test_priority_zero_goes_first_and_chats_do_not_wait_for_each_other(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _enqueue(dev_deps, "eslatma", kind=Kind.NOTICE, ref="n-1")
    await _enqueue(dev_deps, "BonviHR · Savol: 1) ha 2) yo'q", kind=Kind.QUESTION, ref="q-1")
    await _enqueue(dev_deps, "shaxsiy", kind=Kind.REPORT, ref="r-1", chat_id=PRIVATE)
    await send_outbox(dev_deps)
    assert sorted(_texts(fakes), key=str) == sorted(["BonviHR · Savol: 1) ha 2) yo'q", "shaxsiy"], key=str)
    await drain_outbox(dev_deps, fakes.clock, seconds=1)
    assert [request.payload["text"] for request in fakes.messenger.sent() if request.chat_id == GROUP] == [
        "BonviHR · Savol: 1) ha 2) yo'q",
        "eslatma",
    ]


# --- durability (NFR-REL-06, SPEC §2.7) ----------------------------------------------------------------------------
async def test_a_restart_resends_what_was_in_flight_and_sends_what_was_pending(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _enqueue(dev_deps, "uchib ketayotgan edi", ref="turn-1")
    await _enqueue(dev_deps, "navbatda edi", ref="turn-2", chat_id=PRIVATE)
    in_flight = await _row(dev_deps, "result:turn-1:0")
    async with dev_deps.db.uow() as s:
        assert await store.claim_message(s, in_flight.id, at=fakes.clock.now()) is not None
    await dev_deps.db.dispose()  # kill -9: the next start opens the same file

    state = SenderState()
    assert await service.recover_queue(dev_deps, state) == 1
    await send_outbox(dev_deps, state)
    assert sorted(str(text) for text in _texts(fakes)) == ["navbatda edi", "uchib ketayotgan edi"]
    assert {row.status for row in await _rows(dev_deps)} == {"sent"}
    assert (await _row(dev_deps, "result:turn-1:0")).attempts == 2  # the resend reused the same row


async def test_recovery_brings_back_the_rate_windows(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _enqueue(dev_deps, "bir")
    await send_outbox(dev_deps)
    state = SenderState()
    await service.recover_queue(dev_deps, state)
    now = fakes.clock.now()
    assert not state.may_call(GROUP, now + timedelta(seconds=0.5))
    assert state.may_call(GROUP, now + timedelta(seconds=1))
    assert state.may_call(PRIVATE, now)


async def test_retry_after_parks_the_chat_durably_while_other_chats_go_on(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.messenger.fail_next("send_text", RateLimited(provider="telegram", retry_after=7))
    await _enqueue(dev_deps, "birinchi", ref="turn-1")
    await _enqueue(dev_deps, "ikkinchi", ref="turn-2")
    await _enqueue(dev_deps, "boshqa chat", ref="turn-3", chat_id=PRIVATE)
    state = SenderState()
    await send_outbox(dev_deps, state)
    assert _texts(fakes) == ["boshqa chat"]
    parked_until = fakes.clock.now() + timedelta(seconds=7)
    for key in ("result:turn-1:0", "result:turn-2:0"):
        row = await _row(dev_deps, key)
        assert (row.status, row.next_attempt_at) == ("pending", parked_until)
    assert (await _row(dev_deps, "result:turn-1:0")).attempts == 0  # a 429 is not the message's fault

    await fakes.clock.advance(1)
    await _enqueue(dev_deps, "park paytida", ref="turn-4")  # queued after the 429: held back as well
    await send_outbox(dev_deps, state)
    await send_outbox(dev_deps)  # even a fresh sender (a restart) honours the parked rows
    assert _texts(fakes) == ["boshqa chat"]

    await drain_outbox(dev_deps, fakes.clock, seconds=10, step_s=1, state=state)
    in_group = [request.payload["text"] for request in fakes.messenger.sent() if request.chat_id == GROUP]
    assert in_group == ["birinchi", "ikkinchi", "park paytida"]


def test_a_call_counts_from_when_it_ended_so_the_next_one_keeps_its_second(fakes: Fakes) -> None:
    """The adapter retries a 5xx inside one call: the request Telegram counted went out up to the call's end (T36)."""
    state = SenderState()
    started = fakes.clock.now()
    state.record_call(GROUP, started)
    state.settle_call(GROUP, started, started + timedelta(seconds=0.6))
    assert not state.may_call(GROUP, started + timedelta(seconds=1))
    assert state.may_call(GROUP, started + timedelta(seconds=1.6))
    assert state.chat_sent[GROUP] == [started + timedelta(seconds=0.6)]  # one call, not two
    assert state.all_sent == [started + timedelta(seconds=0.6)]


async def test_one_counted_call_per_second_per_chat(dev_deps: DevDeps, fakes: Fakes) -> None:
    for index in range(3):
        await _enqueue(dev_deps, f"xabar {index}", ref=f"turn-{index}")
    state = SenderState()
    assert await send_outbox(dev_deps, state) == 1
    await drain_outbox(dev_deps, fakes.clock, seconds=3, step_s=0.25, state=state)
    sent_at = sorted(row.sent_at for row in await _rows(dev_deps) if row.sent_at is not None)
    assert len(sent_at) == 3
    assert all(later - earlier >= timedelta(seconds=1) for earlier, later in itertools.pairwise(sent_at))


# --- failures ------------------------------------------------------------------------------------------------------
async def test_a_send_whose_delivery_is_unknown_is_resent_under_the_same_row(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.messenger.fail_next("send_text", CallTimedOut(operation="sendMessage"))
    await _enqueue(dev_deps, "natija")
    await send_outbox(dev_deps)
    row = await _row(dev_deps, "result:turn-1:0")
    assert (row.status, row.attempts, row.last_error_code) == ("pending", 1, "call_timed_out")
    await drain_outbox(dev_deps, fakes.clock, seconds=2)
    assert _texts(fakes) == ["natija"]
    assert len(await _rows(dev_deps)) == 1


async def test_an_echo_whose_delivery_is_unknown_is_not_resent(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.messenger.fail_next("send_text", ProviderUnavailable(provider="telegram"))
    await _enqueue(dev_deps, "→ BonviHR", kind=Kind.ECHO, ref="route-3")
    await drain_outbox(dev_deps, fakes.clock, seconds=5)
    assert fakes.messenger.sent() == []
    row = await _row(dev_deps, "echo:route-3:0")
    assert (row.status, row.last_error_code) == ("failed", "delivery_unknown")


async def test_a_refused_message_fails_and_the_next_one_still_goes(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.messenger.fail_next("send_text", ProviderUnavailable(provider="telegram", retryable=False))
    await _enqueue(dev_deps, "rad etiladi", ref="turn-1")
    await _enqueue(dev_deps, "keyingisi", ref="turn-2")
    await drain_outbox(dev_deps, fakes.clock, seconds=2)
    assert _texts(fakes) == ["keyingisi"]
    row = await _row(dev_deps, "result:turn-1:0")
    assert (row.status, row.last_error_code) == ("failed", "provider_unavailable")


class _HangingMessenger(FakeMessenger):
    async def send_text(
        self,
        chat_id: int,
        text: BotText,
        *,
        reply_to: int | None = None,
        parse_mode: object = None,
        disable_notification: bool = False,
        thread_id: int | None = None,
        timeout: float,
    ) -> SentMessage:
        await asyncio.Event().wait()  # an adapter that ignores its own timeout
        raise AssertionError


async def test_every_call_is_bounded_even_when_the_adapter_hangs(dev_deps: DevDeps) -> None:
    deps = replace(dev_deps, messenger=_HangingMessenger())
    await _enqueue(deps, "natija")
    state = SenderState(config=SenderConfig(text_timeout_s=0.01, hard_grace_s=0.01))
    assert await service.send_due(deps, state) == 1
    row = await _row(deps, "result:turn-1:0")
    assert (row.status, row.last_error_code) == ("pending", "call_timed_out")


class _BuggyMessenger(FakeMessenger):
    async def react(self, chat_id: int, message_id: int, emoji: str, *, timeout: float) -> None:
        raise TypeError(emoji)


async def test_a_bug_on_one_row_fails_that_row_and_is_raised(dev_deps: DevDeps) -> None:
    deps = replace(dev_deps, messenger=_BuggyMessenger())
    async with deps.db.uow() as s:
        await service.enqueue_reaction(
            s, bot=deps.bot, chat_id=GROUP, message_id=9, emoji="\U0001f440", ref="update-9", at=deps.clock.now()
        )
    with pytest.raises(ExceptionGroup) as raised:
        await send_outbox(deps)
    assert raised.group_contains(TypeError)
    row = await _row(deps, "ack:update-9:react")
    assert (row.status, row.last_error_code) == ("failed", "internal")  # a restart does not repeat it


async def test_a_row_this_release_cannot_read_is_failed_once(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    await add_outbox_row(dev_deps.db, op="send_fax", payload={"text": "x"}, at=fakes.clock.now())
    with caplog.at_level(logging.ERROR):
        await send_outbox(dev_deps)
        await send_outbox(dev_deps)
    assert [record.getMessage() for record in caplog.records].count("outbox_row_unreadable") == 1
    assert (await _row(dev_deps, "notice:raw:0")).status == "failed"
    assert fakes.messenger.requests == []


# --- scrubbing and the keyboard guard (NFR-SEC-03, UC-12) ----------------------------------------------------------
async def test_a_value_the_vault_learned_after_queueing_is_masked_before_sending(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    await _enqueue(dev_deps, f"BonviHR · Natija: {UNKNOWN_FORMAT_KEY} ishlatildi")
    stored = (await _row(dev_deps, "result:turn-1:0")).payload_json
    assert isinstance(stored, dict)
    assert UNKNOWN_FORMAT_KEY in str(stored["text"])  # no pattern knows it, and the vault did not yet
    redact.set_known_secrets([UNKNOWN_FORMAT_KEY])  # stored in the vault meanwhile

    await send_outbox(dev_deps)
    [text] = _texts(fakes)
    assert UNKNOWN_FORMAT_KEY not in str(text)
    assert text == f"BonviHR · Natija: {redact.mask(UNKNOWN_FORMAT_KEY)} ishlatildi"


async def test_a_stored_key_pattern_is_masked_even_in_a_row_written_unscrubbed(dev_deps: DevDeps, fakes: Fakes) -> None:
    google = FAKE_KEYS["google_api_key"]
    await add_outbox_row(dev_deps.db, payload={"text": f"kalit: {google}"}, at=fakes.clock.now())
    await send_outbox(dev_deps)
    [text] = _texts(fakes)
    assert google not in str(text)
    assert redact.MASK in str(text)


async def test_queued_text_is_scrubbed_before_it_is_written(dev_deps: DevDeps) -> None:
    google = FAKE_KEYS["google_api_key"]
    await _enqueue(dev_deps, f"kalit: {google}")
    stored = (await _row(dev_deps, "result:turn-1:0")).payload_json
    assert google not in str(stored)


async def test_a_row_carrying_a_keyboard_is_refused_and_nothing_is_sent(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    keyboard: JsonValue = {"inline_keyboard": [[{"text": "Ha", "callback_data": "approve"}]]}
    await add_outbox_row(dev_deps.db, payload={"text": "Bajaraymi?", "reply_markup": keyboard}, at=fakes.clock.now())
    await _enqueue(dev_deps, "oddiy xabar", kind=Kind.NOTICE, ref="n-1")
    with caplog.at_level(logging.ERROR):
        await drain_outbox(dev_deps, fakes.clock, seconds=2)
    assert _texts(fakes) == ["oddiy xabar"]  # the refused row does not block the chat
    assert "outbox_payload_refused" in [record.getMessage() for record in caplog.records]
    row = await _row(dev_deps, "notice:raw:0")
    assert (row.status, row.last_error_code) == ("failed", "payload_invalid")
    fakes.messenger.assert_no_reply_markup()


async def test_enqueueing_a_payload_its_op_does_not_allow_writes_nothing(dev_deps: DevDeps) -> None:
    with pytest.raises(PayloadInvalid, match="one target"):
        async with dev_deps.db.uow() as s:
            await service.enqueue_edit(
                s,
                bot=dev_deps.bot,
                chat_id=GROUP,
                text=BotText("yangi"),
                kind=Kind.PROGRESS,
                ref="7",
                part="edit1",
                at=dev_deps.clock.now(),
            )
    assert await _rows(dev_deps) == []


# --- progress coalescing (UC-11) -----------------------------------------------------------------------------------
async def _progress(deps: DevDeps, text: str, *, turn_id: int = 7, every_s: float = 5.0) -> int | None:
    async with deps.db.uow() as s:
        return await service.show_progress(
            s,
            bot=deps.bot,
            chat_id=GROUP,
            turn_id=turn_id,
            text=BotText(text),
            at=deps.clock.now(),
            every_s=every_s,
            project_id="BonviHR",
        )


async def test_progress_posts_once_then_edits_at_most_every_5_s_keeping_the_newest(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    state = SenderState()
    await _progress(dev_deps, "BonviHR · ishlayapti 0 daq · 1 qadam")
    await send_outbox(dev_deps, state)
    [post] = fakes.messenger.sent()
    for steps in (2, 3, 4):
        await fakes.clock.advance(0.5)
        await _progress(dev_deps, f"BonviHR · ishlayapti 0 daq · {steps} qadam")
        await send_outbox(dev_deps, state)
    assert fakes.messenger.sent("edit_text") == []  # not within 5 s of the post

    await drain_outbox(dev_deps, fakes.clock, seconds=4, step_s=0.25, state=state)
    await _progress(dev_deps, "BonviHR · ishlayapti 0 daq · 5 qadam")
    await drain_outbox(dev_deps, fakes.clock, seconds=6, step_s=0.25, state=state)

    edits = fakes.messenger.sent("edit_text")
    assert [request.payload["text"] for request in edits] == [
        "BonviHR · ishlayapti 0 daq · 4 qadam",
        "BonviHR · ishlayapti 0 daq · 5 qadam",
    ]
    assert {request.payload["message_id"] for request in edits} == {101}
    assert post.payload["text"] == "BonviHR · ishlayapti 0 daq · 1 qadam"
    rows = await _rows(dev_deps)
    assert [row.status for row in rows if row.op == "edit_text"] == ["superseded", "superseded", "sent", "sent"]
    times = sorted(row.sent_at for row in rows if row.sent_at is not None)
    assert all(later - earlier >= timedelta(seconds=5) for earlier, later in itertools.pairwise(times))
    link = await service.find_message_link(dev_deps, chat_id=GROUP, message_id=101)
    assert link is not None
    assert link.about == About(project_id="BonviHR", turn_id=7)


async def test_a_newer_progress_text_rides_along_while_the_first_post_is_queued(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    first = await _progress(dev_deps, "1 qadam")
    assert await _progress(dev_deps, "2 qadam") == first
    await drain_outbox(dev_deps, fakes.clock, seconds=6)
    assert _texts(fakes) == ["2 qadam"]
    assert fakes.messenger.sent("edit_text") == []


# --- typing --------------------------------------------------------------------------------------------------------
async def _typing(deps: DevDeps, ref: str, chat_id: int = GROUP) -> int | None:
    async with deps.db.uow() as s:
        return await service.request_typing(s, bot=deps.bot, chat_id=chat_id, ref=ref, at=deps.clock.now())


async def test_typing_repeats_every_4_s_for_at_most_60_s(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _typing(dev_deps, "update-1")
    await drain_outbox(dev_deps, fakes.clock, seconds=70, step_s=1)
    typing = fakes.messenger.sent("chat_action")
    assert len(typing) == 15  # at 0, 4, … 56 s
    assert {request.payload["action"] for request in typing} == {ChatAction.TYPING}
    assert (await _row(dev_deps, "ack:update-1:chat_action")).status == "sent"


async def test_typing_is_deduped_per_chat_and_ends_with_the_reply(dev_deps: DevDeps, fakes: Fakes) -> None:
    state = SenderState()
    await _typing(dev_deps, "update-1")
    await send_outbox(dev_deps, state)
    await fakes.clock.advance(1)
    await _typing(dev_deps, "update-2")  # a second message 1 s later: the hint is still showing
    await send_outbox(dev_deps, state)
    assert len(fakes.messenger.sent("chat_action")) == 1
    assert (await _row(dev_deps, "ack:update-1:chat_action")).status == "superseded"

    await fakes.clock.advance(1)
    await _enqueue(dev_deps, "BonviHR · Natija: tayyor")
    await send_outbox(dev_deps, state)
    await drain_outbox(dev_deps, fakes.clock, seconds=20, state=state)
    assert len(fakes.messenger.sent("chat_action")) == 1
    assert (await _row(dev_deps, "ack:update-2:chat_action")).status == "cancelled"


async def test_stop_typing_ends_the_hint(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _typing(dev_deps, "update-1")
    await _typing(dev_deps, "update-2", chat_id=PRIVATE)
    async with dev_deps.db.uow() as s:
        assert await service.stop_typing(s, bot=dev_deps.bot, chat_id=GROUP) == 1
    await drain_outbox(dev_deps, fakes.clock, seconds=5)
    assert {request.chat_id for request in fakes.messenger.sent("chat_action")} == {PRIVATE}


async def test_stop_typing_also_ends_a_hint_the_sender_has_in_flight(dev_deps: DevDeps, fakes: Fakes) -> None:
    """T34b: a burst routed to a turn stops the intake's typing from another task; a hint being sent at that instant
    finishes its call but is never re-armed (race-free)."""
    await _typing(dev_deps, "update-1")
    row = await _row(dev_deps, "ack:update-1:chat_action")
    async with dev_deps.db.uow() as s:
        assert await store.claim_message(s, row.id, at=fakes.clock.now()) is not None  # the sender took it
        assert await service.stop_typing(s, bot=dev_deps.bot, chat_id=GROUP) == 1
    await drain_outbox(dev_deps, fakes.clock, seconds=20)
    assert fakes.messenger.sent("chat_action") == []
    assert (await _row(dev_deps, "ack:update-1:chat_action")).status == "cancelled"


# --- deletes, pins, edits by id, files -----------------------------------------------------------------------------
async def test_the_heartbeat_is_posted_pinned_and_edited_in_place(dev_deps: DevDeps, fakes: Fakes) -> None:
    now = fakes.clock.now()
    await _enqueue(dev_deps, "Onlayn: 14:00", kind=Kind.HEARTBEAT, ref="online")
    async with dev_deps.db.uow() as s:
        await service.enqueue_pin(s, bot=dev_deps.bot, chat_id=GROUP, ref="online", at=now, target="heartbeat:online:0")
    await send_outbox(dev_deps)
    [pin] = fakes.messenger.sent("pin")
    assert pin.payload["message_id"] == 101
    delivery = await service.get_delivery(dev_deps, "heartbeat:online:0")
    assert delivery is not None
    assert delivery.message_id == 101

    async with dev_deps.db.uow() as s:
        await service.enqueue_edit(
            s,
            bot=dev_deps.bot,
            chat_id=GROUP,
            text=BotText("Onlayn: 14:05"),
            kind=Kind.HEARTBEAT,
            ref="online",
            part="edit-1405",
            at=fakes.clock.now(),
            message_id=delivery.message_id,
        )
    await drain_outbox(dev_deps, fakes.clock, seconds=1)
    [edit] = fakes.messenger.sent("edit_text")
    assert (edit.payload["message_id"], edit.payload["text"]) == (101, "Onlayn: 14:05")


async def test_a_delete_telegram_refuses_is_recorded_as_not_deleted(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    fakes.messenger.undeletable.add((GROUP, 5))
    async with dev_deps.db.uow() as s:
        await service.enqueue_delete(
            s, bot=dev_deps.bot, chat_id=GROUP, message_id=5, ref="update-5", at=fakes.clock.now()
        )
    with caplog.at_level(logging.WARNING):
        await send_outbox(dev_deps)
    row = await _row(dev_deps, "ack:update-5:delete")
    assert (row.status, row.last_error_code) == ("failed", "not_deleted")
    assert "outbox_delete_refused" in [record.getMessage() for record in caplog.records]


async def _file(deps: DevDeps, path: Path, *, ref: str, caption: str | None = None) -> tuple[int, ...]:
    async with deps.db.uow() as s:
        return await service.enqueue_file(
            s,
            bot=deps.bot,
            chat_id=PRIVATE,
            op=Op.SEND_VOICE,
            path=path,
            kind=Kind.VOICE,
            ref=ref,
            at=deps.clock.now(),
            max_parts=3,
            caption=None if caption is None else BotText(caption),
            duration_s=12,
            about=About(project_id="BonviHR"),
        )


async def test_a_file_from_another_feature_is_sent_with_its_caption(
    dev_deps: DevDeps, fakes: Fakes, tmp_path: Path
) -> None:
    voice = tmp_path / "answer.ogg"
    voice.write_bytes(b"OggS")
    await _file(dev_deps, voice, ref="answer-1", caption="ovozli javob")
    await send_outbox(dev_deps)
    [request] = fakes.messenger.sent("send_voice")
    assert request.payload == {
        "path": str(voice),
        "filename": None,
        "mime": None,
        "caption": "ovozli javob",
        "reply_to": None,
        "duration_s": 12,
        "timeout": FILE_TIMEOUT,
    }
    assert voice.exists()  # another feature's file is never removed by the outbox


async def test_a_caption_goes_with_its_markers_stripped(dev_deps: DevDeps, fakes: Fakes, tmp_path: Path) -> None:
    """The Messenger port takes no parse mode for captions: their markup would show as typed."""
    voice = tmp_path / "answer.ogg"
    voice.write_bytes(b"OggS")
    await _file(dev_deps, voice, ref="answer-1", caption="**BonviHR** · `README.md` a < b")
    await send_outbox(dev_deps)
    [request] = fakes.messenger.sent("send_voice")
    assert request.payload["caption"] == "BonviHR · README.md a < b"


async def test_a_missing_file_fails_without_a_call(dev_deps: DevDeps, fakes: Fakes, tmp_path: Path) -> None:
    await _file(dev_deps, tmp_path / "gone.ogg", ref="answer-2")
    await send_outbox(dev_deps)
    assert fakes.messenger.requests == []
    row = await _row(dev_deps, "voice:answer-2:0")
    assert (row.status, row.last_error_code) == ("failed", "file_missing")


async def test_a_caption_over_1024_keeps_its_start_and_the_rest_follows(
    dev_deps: DevDeps, fakes: Fakes, tmp_path: Path
) -> None:
    voice = tmp_path / "long.ogg"
    voice.write_bytes(b"OggS")
    caption = "\n\n".join(f"{index}-izoh " + "so'z " * 120 for index in range(3))
    ids = await _file(dev_deps, voice, ref="answer-3", caption=caption)
    assert len(ids) >= 2
    await drain_outbox(dev_deps, fakes.clock, seconds=3)
    [request] = fakes.messenger.sent("send_voice")
    assert domain.tg_len(str(request.payload["caption"])) <= domain.CAPTION_LIMIT
    assert str(request.payload["caption"]).startswith("0-izoh")
    assert str(_texts(fakes)[0]).startswith("1-izoh")


# --- the sender loop and shutdown ----------------------------------------------------------------------------------
async def test_the_sender_runs_under_forever_and_stops_when_cancelled(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _enqueue(dev_deps, "birinchi", ref="turn-1")
    task = aio.spawn("outbox", aio.forever("outbox", lambda: service.run_sender(dev_deps)))
    try:
        await _until(lambda: len(fakes.messenger.sent()) == 1, fakes)
        await _enqueue(dev_deps, "ikkinchi", ref="turn-2")
        await _until(lambda: len(fakes.messenger.sent()) == 2, fakes)
    finally:
        task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _texts(fakes) == ["birinchi", "ikkinchi"]


async def test_shutdown_flushes_only_urgent_rows(dev_deps: DevDeps, fakes: Fakes) -> None:
    await _enqueue(dev_deps, "BonviHR · Savol", kind=Kind.QUESTION, ref="q-1")
    await _enqueue(dev_deps, "BonviHR · Natija", kind=Kind.RESULT, ref="t-1", chat_id=PRIVATE)
    await _enqueue(dev_deps, "eslatma", kind=Kind.NOTICE, ref="n-1", chat_id=OTHER_GROUP)
    assert await service.flush_queue(dev_deps, max_priority=1, within_s=5) == 2
    assert sorted(str(text) for text in _texts(fakes)) == ["BonviHR · Natija", "BonviHR · Savol"]
    assert (await _row(dev_deps, "notice:n-1:0")).status == "pending"  # kept for the next start


# --- retention (SPEC §4.2) -----------------------------------------------------------------------------------------
async def test_retention_deletes_finished_rows_after_30_days_and_links_after_90(
    dev_deps: DevDeps, fakes: Fakes
) -> None:
    start = fakes.clock.now()
    fakes.clock.set(start - timedelta(days=91))
    await _enqueue(dev_deps, "juda eski", ref="t-1")
    await send_outbox(dev_deps)  # row and link at -91 d
    fakes.clock.set(start - timedelta(days=31))
    await _enqueue(dev_deps, "eski", ref="t-2", chat_id=PRIVATE)
    await send_outbox(dev_deps)  # row and link at -31 d
    await _enqueue(dev_deps, "hali kutyapti", ref="t-3", chat_id=OTHER_GROUP)  # pending: never touched
    async with dev_deps.db.uow() as s:
        await store.postpone_chat(s, bot="dev", chat_id=OTHER_GROUP, until=start + timedelta(days=1))
    fakes.clock.set(start - timedelta(days=3))
    await _enqueue(dev_deps, "yangi", ref="t-4")
    await send_outbox(dev_deps)
    fakes.clock.set(start)

    assert await service.apply_retention(dev_deps) == 3  # two rows, one link
    assert sorted(row.dedupe_key for row in await _rows(dev_deps)) == ["result:t-3:0", "result:t-4:0"]
    assert await service.find_message_link(dev_deps, chat_id=GROUP, message_id=101) is None
    assert await service.find_message_link(dev_deps, chat_id=PRIVATE, message_id=101) is not None
    assert await service.find_message_link(dev_deps, chat_id=GROUP, message_id=102) is not None
    assert await service.apply_retention(dev_deps) == 0


# --- enqueue_reply (T34: an answer with nothing else to commit) -------------------------------------------------
async def test_a_reply_is_queued_in_its_own_unit_of_work_once_per_ref(dev_deps: DevDeps) -> None:
    from yordamchi.core import strings as S
    from yordamchi.shared.outbox.domain import About, Kind

    text = S.STOP_ALL_NOTHING()
    first = await service.enqueue_reply(
        dev_deps, chat_id=DEV_GROUP_ID, text=text, kind=Kind.NOTICE, ref="burst-7", max_parts=3, about=About("X")
    )
    again = await service.enqueue_reply(
        dev_deps, chat_id=DEV_GROUP_ID, text=text, kind=Kind.NOTICE, ref="burst-7", max_parts=3
    )
    assert len(first) == 1
    assert again == ()
    delivery = await service.get_delivery(dev_deps, "notice:burst-7:0")
    assert delivery is not None
    assert delivery.status.value == "pending"
