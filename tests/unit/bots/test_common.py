"""bots/common: `guarded()` around every turn and job, the update translation `OwnerOnly` judges, and the polling
backoff schedule (CONVENTIONS §6, §7; SPEC §2.8; NFR-REL-03). The feed_update and polling tests are integration
tests (`tests/integration/test_owner_only.py`, `test_polling.py`)."""

import asyncio
import json
import logging
import re

import pytest
from aiogram.types import Update

from tests.factories import (
    DEV_GROUP_ID,
    FAKE_KEYS,
    FOREIGN_GROUP_ID,
    OWNER_ID,
    STRANGER_ID,
    Fakes,
    tg_callback,
    tg_inline_query,
    tg_member_change,
    tg_message,
)
from yordamchi.bots import common
from yordamchi.bots.common import DEFAULT_POLL, PollConfig, backoff_s, guarded
from yordamchi.core import aio
from yordamchi.core import strings as S
from yordamchi.core.errors import CallTimedOut, RateLimited, SettingUnknown
from yordamchi.core.log import JsonFormatter
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.access.domain import Arrival, ChatKind, EventType

GOOGLE = FAKE_KEYS["google_api_key"]
INCIDENT = re.compile(r"#([0-9a-f]{8})\)")


async def _answer() -> int:
    return 42


# --- guarded() ------------------------------------------------------------------------------------------------------
async def test_guarded_returns_the_result_and_tells_nobody(dev_deps: DevDeps, fakes: Fakes) -> None:
    assert await guarded(dev_deps, _answer(), job="digest_refresh") == 42
    assert fakes.messenger.requests == []


@pytest.mark.parametrize(
    "error",
    [SettingUnknown(key="burst.nope"), RateLimited(provider="google", retry_after=3.0)],
    ids=["domain_error", "external_error"],
)
async def test_an_expected_error_is_a_warning_and_its_catalog_message(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture, error: SettingUnknown | RateLimited
) -> None:
    async def failing() -> int:
        raise error

    with caplog.at_level(logging.INFO):
        assert await guarded(dev_deps, failing(), job="turn", chat_id=DEV_GROUP_ID) is None

    [sent] = fakes.messenger.sent("send_text")
    assert (sent.chat_id, sent.payload["text"]) == (DEV_GROUP_ID, S.ERRORS[error.code](**error.params))
    [record] = [record for record in caplog.records if record.getMessage() == "job_failed"]
    assert record.levelno == logging.WARNING
    assert not record.exc_info  # an expected error carries no traceback
    line = json.loads(JsonFormatter(process="dev", version="test").format(record))
    assert (line["job"], line["error_code"]) == ("turn", error.code.value)


async def test_a_bug_is_an_error_with_a_scrubbed_traceback_and_an_incident_for_the_owner(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    async def crashing() -> int:
        msg = f"unexpected state while handling key {GOOGLE}"
        raise RuntimeError(msg)

    with caplog.at_level(logging.INFO):
        assert await guarded(dev_deps, crashing(), job="router", chat_id=DEV_GROUP_ID) is None

    [record] = [record for record in caplog.records if record.getMessage() == "job_crashed"]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    line = JsonFormatter(process="dev", version="test").format(record)
    assert "RuntimeError" in line  # the traceback is there …
    assert GOOGLE not in line  # … scrubbed
    incident = json.loads(line)["incident"]
    [sent] = fakes.messenger.sent("send_text")
    assert sent.payload["text"] == S.ERR_INTERNAL(incident=incident)
    match = INCIDENT.search(str(sent.payload["text"]))
    assert match is not None
    assert match.group(1) == incident
    assert "RuntimeError" not in str(sent.payload["text"])  # never a class name or a traceback to the owner


async def test_a_job_without_a_chat_reports_to_the_bots_home_chat(
    dev_deps: DevDeps, personal_deps: PersonalDeps, fakes: Fakes
) -> None:
    async def failing() -> None:
        raise CallTimedOut(operation="stt")

    await guarded(dev_deps, failing(), job="stt")
    await guarded(personal_deps, failing(), job="stt")
    assert [request.chat_id for request in fakes.messenger.sent("send_text")] == [DEV_GROUP_ID, OWNER_ID]


async def test_a_notice_that_cannot_be_sent_is_logged_and_guarded_still_returns(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    async def failing() -> int:
        raise SettingUnknown(key="x")

    fakes.messenger.fail_next("send_text", CallTimedOut(operation="sendMessage"))
    with caplog.at_level(logging.WARNING):
        assert await guarded(dev_deps, failing(), job="turn") is None
    assert {record.getMessage() for record in caplog.records} >= {"job_failed", "owner_notice_failed"}


async def test_cancellation_is_never_swallowed_even_when_a_library_disguises_it(dev_deps: DevDeps) -> None:
    started = asyncio.Event()

    async def stubborn() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            msg = "the library turned the cancel into this"
            raise RuntimeError(msg) from None

    task = aio.spawn("guarded", guarded(dev_deps, stubborn(), job="turn"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_a_plain_cancellation_passes_through(dev_deps: DevDeps, fakes: Fakes) -> None:
    async def cancelled() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await guarded(dev_deps, cancelled(), job="turn")
    assert fakes.messenger.requests == []


# --- the update translation -----------------------------------------------------------------------------------------
def _arrival(raw: dict[str, object], bot: BotName = BotName.DEV) -> Arrival:
    return common.arrival_of(Update.model_validate(raw), bot)


def test_a_message_carries_its_sender_chat_and_kind() -> None:
    assert _arrival(tg_message(5)) == Arrival(
        bot=BotName.DEV,
        update_id=5,
        event=EventType.MESSAGE,
        user_id=OWNER_ID,
        chat_id=DEV_GROUP_ID,
        chat_kind=ChatKind.SUPERGROUP,
    )
    edit = _arrival(tg_message(6, chat_id=OWNER_ID, field="edited_message"), BotName.PERSONAL)
    assert (edit.event, edit.chat_kind, edit.chat_id) == (EventType.EDITED_MESSAGE, ChatKind.PRIVATE, OWNER_ID)


def test_a_channel_post_has_no_sender() -> None:
    post = _arrival(tg_message(7, user_id=None, chat_id=FOREIGN_GROUP_ID, chat_type="channel", field="channel_post"))
    assert (post.event, post.user_id, post.chat_kind) == (EventType.OTHER, None, ChatKind.CHANNEL)


def test_a_membership_change_says_whether_the_bot_is_still_there() -> None:
    added = _arrival(tg_member_change(8, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID))
    assert (added.event, added.user_id, added.bot_present) == (EventType.MY_CHAT_MEMBER, STRANGER_ID, True)
    kicked = tg_member_change(9, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID, old="member", new="kicked")
    assert _arrival(kicked).bot_present is False
    left = tg_member_change(10, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID, old="member", new="left")
    assert _arrival(left).bot_present is False


def test_callbacks_and_inline_queries_are_never_served() -> None:
    callback = _arrival(tg_callback(11, user_id=OWNER_ID, chat_id=DEV_GROUP_ID))
    assert (callback.event, callback.user_id, callback.chat_id) == (EventType.OTHER, OWNER_ID, DEV_GROUP_ID)
    inline = _arrival(tg_inline_query(12, user_id=STRANGER_ID))
    assert (inline.event, inline.user_id, inline.chat_id) == (EventType.OTHER, STRANGER_ID, None)


def test_an_update_of_an_unknown_type_is_other_with_no_ids() -> None:
    unknown = _arrival({"update_id": 13})
    assert (unknown.event, unknown.user_id, unknown.chat_id) == (EventType.OTHER, None, None)


# --- the polling backoff --------------------------------------------------------------------------------------------
def test_the_backoff_doubles_from_1_s_and_stops_at_30_s() -> None:
    assert [backoff_s(failures, DEFAULT_POLL) for failures in range(1, 9)] == [1, 2, 4, 8, 16, 30, 30, 30]


def test_the_backoff_survives_an_outage_of_days() -> None:
    assert backoff_s(100_000, DEFAULT_POLL) == DEFAULT_POLL.max_backoff_s


def test_jitter_only_shortens_a_wait() -> None:
    assert backoff_s(3, DEFAULT_POLL, spread=0.8) == pytest.approx(3.2)
    assert backoff_s(9, DEFAULT_POLL, spread=1.0) == 30


def test_the_defaults_resume_within_60_s_of_connectivity() -> None:
    """NFR-REL-03: the worst case is a probe that hangs on a dead connection, then the longest wait."""
    assert DEFAULT_POLL.probe_budget_s + DEFAULT_POLL.max_backoff_s <= 60
    assert DEFAULT_POLL.long_poll_s == 25  # SPEC §2.8; the watchdog's 120 s heartbeat limit relies on it
    assert DEFAULT_POLL.long_poll_s + DEFAULT_POLL.request_grace_s < 120
    assert DEFAULT_POLL.allowed_updates == ("message", "edited_message", "my_chat_member")
    assert PollConfig().jitter < 1
