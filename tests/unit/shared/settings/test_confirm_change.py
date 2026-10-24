"""`settings.service.confirm_change` (T77; SPEC §3.2.15 step 3, UC-09 F): the personal bot's conversational confirm.
The owner's "ha" writes the waiting change and its one-line restatement in one unit of work; a value today's
registry refuses changes nothing."""

import pytest

from tests.factories import list_replies, list_setting_rows, make_owner_origin
from yordamchi.core.errors import SettingInvalid
from yordamchi.core.paths import BotName
from yordamchi.personal.deps import PersonalDeps
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import ChangeItem, PendingChange
from yordamchi.shared.settings.service import OutcomeKind


def _change(value: str) -> PendingChange:
    return PendingChange(
        reason="change",
        items=(ChangeItem(key=defaults.MODELS_QA.key, value=value, old="sonnet"),),
        restated=f"models.qa: sonnet → {value}",
        origin_update_id=7,
    )


async def test_ha_writes_the_change_and_restates_it(personal_deps: PersonalDeps) -> None:
    origin = make_owner_origin(BotName.PERSONAL, update_id=8)
    outcome = await settings.confirm_change(personal_deps, origin, _change("best"))
    assert outcome.kind is OutcomeKind.APPLIED
    assert outcome.keys == (defaults.MODELS_QA.key,)
    snap = await settings.snapshot(personal_deps)
    assert snap[defaults.MODELS_QA] == "best"
    [row] = await list_setting_rows(personal_deps.db, defaults.MODELS_QA.key)
    assert row.origin_update_id == 8  # the update that said "ha"
    [reply] = await list_replies(personal_deps.db, BotName.PERSONAL)
    assert reply.chat_id == origin.chat_id


async def test_a_value_the_registry_refuses_changes_nothing(personal_deps: PersonalDeps) -> None:
    origin = make_owner_origin(BotName.PERSONAL, update_id=8)
    with pytest.raises(SettingInvalid):
        await settings.confirm_change(personal_deps, origin, _change("gpt-9"))
    assert await list_setting_rows(personal_deps.db, defaults.MODELS_QA.key) == []
    assert await list_replies(personal_deps.db, BotName.PERSONAL) == []
