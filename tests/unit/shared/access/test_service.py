"""access.service on a real SQLite file with fakes: drops leave no trace but an id-only log line, a foreign chat is
left at once and audited, and the policy is read from settings at use time (SPEC §3.2.1 step 2; UC-01)."""

import json
import logging

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    FOREIGN_GROUP_ID,
    OWNER_ID,
    STRANGER_ID,
    Fakes,
    make_arrival,
    make_owner_origin,
)
from yordamchi.core.errors import ProviderUnavailable
from yordamchi.core.log import JsonFormatter
from yordamchi.core.paths import BotName
from yordamchi.dev.deps import DevDeps
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.access import domain, service
from yordamchi.shared.access.domain import Admit, Drop, DropReason, EventType, Leave, OwnerOrigin
from yordamchi.shared.audit import store as audit_store
from yordamchi.shared.audit.models import AuditEvent
from yordamchi.shared.inbox import store as inbox_store
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

NEW_OWNER = 515151


async def _audit(deps: DevDeps | PersonalDeps) -> list[AuditEvent]:
    async with deps.db.read() as s:
        return await audit_store.list_audit_events(s)


async def _inbox_rows(deps: DevDeps | PersonalDeps) -> int:
    async with deps.db.read() as s:
        return len(await inbox_store.list_inbound_updates(s, deps.bot.value))


# --- admit ----------------------------------------------------------------------------------------------------------
async def test_the_owner_is_admitted_and_nothing_else_happens(dev_deps: DevDeps, fakes: Fakes) -> None:
    verdict = await service.admit_update(dev_deps, make_arrival(9))
    assert verdict == Admit(OwnerOrigin(bot=BotName.DEV, update_id=9, user_id=OWNER_ID, chat_id=DEV_GROUP_ID))
    assert fakes.messenger.requests == []
    assert await _audit(dev_deps) == []


async def test_the_personal_bot_admits_the_owners_dm(personal_deps: PersonalDeps, fakes: Fakes) -> None:
    verdict = await service.admit_update(personal_deps, make_arrival(3, bot=BotName.PERSONAL))
    assert verdict == Admit(OwnerOrigin(bot=BotName.PERSONAL, update_id=3, user_id=OWNER_ID, chat_id=OWNER_ID))
    assert fakes.messenger.requests == []


# --- drop -----------------------------------------------------------------------------------------------------------
async def test_a_stranger_gets_nothing_and_is_logged_by_numeric_ids_only(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    arrival = make_arrival(17, user_id=STRANGER_ID, chat_id=STRANGER_ID)
    with caplog.at_level(logging.INFO):
        verdict = await service.admit_update(dev_deps, arrival)

    assert verdict == Drop(DropReason.STRANGER)
    assert fakes.messenger.requests == []
    assert (await _audit(dev_deps), await _inbox_rows(dev_deps)) == ([], 0)
    [record] = [record for record in caplog.records if record.getMessage() == "update_dropped"]
    line = json.loads(JsonFormatter(process="dev", version="test").format(record))
    assert {key: line[key] for key in ("bot", "update_id", "update_type", "user_id", "chat_id", "reason")} == {
        "bot": "dev",
        "update_id": 17,
        "update_type": "message",
        "user_id": STRANGER_ID,
        "chat_id": STRANGER_ID,
        "reason": "stranger",
    }


async def test_the_owner_in_the_dev_bots_dm_is_dropped_silently(dev_deps: DevDeps, fakes: Fakes) -> None:
    verdict = await service.admit_update(dev_deps, make_arrival(chat_id=OWNER_ID))
    assert verdict == Drop(DropReason.OWNER_ELSEWHERE)
    assert fakes.messenger.requests == []


# --- leave ----------------------------------------------------------------------------------------------------------
async def test_a_foreign_group_is_left_at_once_and_audited(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    arrival = make_arrival(21, event=EventType.MY_CHAT_MEMBER, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID)
    with caplog.at_level(logging.INFO):
        verdict = await service.admit_update(dev_deps, arrival)

    assert verdict == Leave(chat_id=FOREIGN_GROUP_ID)
    [leave] = fakes.messenger.requests
    assert (leave.method, leave.chat_id, leave.payload) == ("leave", FOREIGN_GROUP_ID, {"timeout": 8.0})
    assert domain.LEAVE_TIMEOUT_S < 10  # UC-01: gone within 10 s
    [event] = await _audit(dev_deps)
    assert (event.bot, event.actor, event.kind, event.ref_type, event.ref_id, event.at) == (
        "dev",
        "system",
        "access_dropped",
        "chat",
        str(FOREIGN_GROUP_ID),
        fakes.clock.now(),
    )
    assert event.data_json == {
        "update_id": 21,
        "update_type": "my_chat_member",
        "user_id": STRANGER_ID,
        "chat_id": FOREIGN_GROUP_ID,
        "left": True,
        "error_code": None,
    }
    assert "foreign_chat_left" in {record.getMessage() for record in caplog.records}
    assert await _inbox_rows(dev_deps) == 0


async def test_a_failed_leave_is_logged_and_audited_never_raised(
    dev_deps: DevDeps, fakes: Fakes, caplog: pytest.LogCaptureFixture
) -> None:
    fakes.messenger.fail_next("leave", ProviderUnavailable(provider="telegram"))
    with caplog.at_level(logging.WARNING):
        verdict = await service.admit_update(dev_deps, make_arrival(22, chat_id=FOREIGN_GROUP_ID))

    assert verdict == Leave(chat_id=FOREIGN_GROUP_ID)
    [event] = await _audit(dev_deps)
    assert isinstance(event.data_json, dict)
    assert (event.data_json["left"], event.data_json["error_code"]) == (False, "provider_unavailable")
    assert "foreign_chat_leave_failed" in {record.getMessage() for record in caplog.records}


# --- the policy is data ---------------------------------------------------------------------------------------------
async def test_a_changed_owner_applies_from_the_next_update(dev_deps: DevDeps, fakes: Fakes) -> None:
    origin = make_owner_origin(update_id=900)
    await settings.record_version(dev_deps, origin, defaults.ACCESS_OWNER_ID, NEW_OWNER, reason="change")

    assert await service.admit_update(dev_deps, make_arrival(30)) == Drop(DropReason.STRANGER)
    verdict = await service.admit_update(dev_deps, make_arrival(31, user_id=NEW_OWNER))
    assert verdict == Admit(OwnerOrigin(bot=BotName.DEV, update_id=31, user_id=NEW_OWNER, chat_id=DEV_GROUP_ID))
    assert fakes.messenger.requests == []


async def test_the_home_chat_is_the_dev_group_or_the_owners_dm(dev_deps: DevDeps, personal_deps: PersonalDeps) -> None:
    assert await service.resolve_home_chat(dev_deps) == DEV_GROUP_ID
    assert await service.resolve_home_chat(personal_deps) == OWNER_ID
