"""access.domain: the owner-only policy of both bots, pure (SPEC §3.2.1 step 2; UC-01, D12, NFR-SEC-01)."""

from dataclasses import replace

import pytest

from tests.factories import (
    DEV_GROUP_ID,
    FOREIGN_GROUP_ID,
    OWNER_ID,
    PERSONAL_GROUP_ID,
    STRANGER_ID,
    make_arrival,
    make_default_snapshot,
)
from yordamchi.core.paths import BotName
from yordamchi.shared.access import domain
from yordamchi.shared.access.domain import (
    AccessPolicy,
    Admit,
    ChatKind,
    Drop,
    DropReason,
    EventType,
    Leave,
    OwnerOrigin,
)
from yordamchi.shared.settings import defaults

DEV = domain.policy_for(make_default_snapshot(BotName.DEV))
PERSONAL = domain.policy_for(make_default_snapshot(BotName.PERSONAL))


def _personal_without_group() -> AccessPolicy:
    snap = make_default_snapshot(BotName.PERSONAL)
    values = {**snap.values, defaults.ACCESS_PERSONAL_GROUP_ID.key: 0}
    return domain.policy_for(replace(snap, values=values))


# --- the policy from settings ---------------------------------------------------------------------------------------
def test_the_dev_bot_serves_only_its_group() -> None:
    policy = domain.policy_for(make_default_snapshot(BotName.DEV))
    assert policy == AccessPolicy(
        bot=BotName.DEV, owner_id=OWNER_ID, chat_ids=frozenset({DEV_GROUP_ID}), home_chat_id=DEV_GROUP_ID
    )


def test_the_personal_bot_serves_the_owners_dm_and_its_optional_group() -> None:
    policy = domain.policy_for(make_default_snapshot(BotName.PERSONAL))
    assert policy == AccessPolicy(
        bot=BotName.PERSONAL,
        owner_id=OWNER_ID,
        chat_ids=frozenset({OWNER_ID, PERSONAL_GROUP_ID}),
        home_chat_id=OWNER_ID,
    )
    assert _personal_without_group().chat_ids == frozenset({OWNER_ID})  # 0 means "no group"


def test_the_policy_follows_the_snapshot_not_the_bootstrap_constants() -> None:
    snap = make_default_snapshot(BotName.DEV)
    values = {**snap.values, defaults.ACCESS_OWNER_ID.key: 777, defaults.ACCESS_DEV_GROUP_ID.key: -100777}
    policy = domain.policy_for(replace(snap, values=values))
    assert (policy.owner_id, policy.chat_ids, policy.home_chat_id) == (777, frozenset({-100777}), -100777)


# --- admit ----------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("event", [EventType.MESSAGE, EventType.EDITED_MESSAGE, EventType.MY_CHAT_MEMBER])
def test_the_owner_in_the_dev_group_is_admitted_with_an_origin_for_that_update(event: EventType) -> None:
    verdict = domain.admit(make_arrival(41, event=event), DEV)
    assert verdict == Admit(OwnerOrigin(bot=BotName.DEV, update_id=41, user_id=OWNER_ID, chat_id=DEV_GROUP_ID))


def test_the_owner_is_admitted_by_the_personal_bot_in_the_dm_and_in_its_group() -> None:
    for chat_id in (OWNER_ID, PERSONAL_GROUP_ID):
        verdict = domain.admit(make_arrival(5, bot=BotName.PERSONAL, chat_id=chat_id), PERSONAL)
        assert verdict == Admit(OwnerOrigin(bot=BotName.PERSONAL, update_id=5, user_id=OWNER_ID, chat_id=chat_id))


@pytest.mark.parametrize(
    ("user_id", "chat_id"),
    [(STRANGER_ID, STRANGER_ID), (STRANGER_ID, DEV_GROUP_ID), (None, DEV_GROUP_ID)],
    ids=["stranger_dm", "stranger_in_the_group", "no_sender_in_the_group"],
)
def test_anyone_but_the_owner_is_dropped(user_id: int | None, chat_id: int) -> None:
    assert domain.admit(make_arrival(user_id=user_id, chat_id=chat_id), DEV) == Drop(DropReason.STRANGER)


def test_a_stranger_in_the_personal_bots_own_chats_is_dropped() -> None:
    for chat_id in (STRANGER_ID, PERSONAL_GROUP_ID):
        arrival = make_arrival(bot=BotName.PERSONAL, user_id=STRANGER_ID, chat_id=chat_id)
        assert domain.admit(arrival, PERSONAL) == Drop(DropReason.STRANGER)


def test_the_owner_elsewhere_is_dropped() -> None:
    """UC-01 AC: the dev bot ignores the owner's own messages in any chat but its group (here: the DM)."""
    assert domain.admit(make_arrival(chat_id=OWNER_ID), DEV) == Drop(DropReason.OWNER_ELSEWHERE)
    edit_in_dm = make_arrival(event=EventType.EDITED_MESSAGE, chat_id=OWNER_ID)
    assert domain.admit(edit_in_dm, DEV) == Drop(DropReason.OWNER_ELSEWHERE)


@pytest.mark.parametrize(
    ("user_id", "event", "chat_kind"),
    [
        (OWNER_ID, EventType.MESSAGE, ChatKind.SUPERGROUP),
        (STRANGER_ID, EventType.MESSAGE, ChatKind.GROUP),
        (OWNER_ID, EventType.EDITED_MESSAGE, ChatKind.SUPERGROUP),
        (None, EventType.OTHER, ChatKind.CHANNEL),
        (STRANGER_ID, EventType.MY_CHAT_MEMBER, ChatKind.SUPERGROUP),
        (STRANGER_ID, EventType.OTHER, ChatKind.SUPERGROUP),
    ],
    ids=["owner", "stranger_basic_group", "owner_edit", "channel_post", "bot_added", "callback_there"],
)
def test_a_foreign_group_or_channel_is_left_whoever_wrote(
    user_id: int | None, event: EventType, chat_kind: ChatKind
) -> None:
    arrival = make_arrival(event=event, user_id=user_id, chat_id=FOREIGN_GROUP_ID, chat_kind=chat_kind)
    assert domain.admit(arrival, DEV) == Leave(chat_id=FOREIGN_GROUP_ID)


def test_each_bot_leaves_the_other_bots_group() -> None:
    dev_group = make_arrival(bot=BotName.PERSONAL, chat_id=DEV_GROUP_ID)
    assert domain.admit(dev_group, PERSONAL) == Leave(chat_id=DEV_GROUP_ID)
    personal_group = make_arrival(chat_id=PERSONAL_GROUP_ID)
    assert domain.admit(personal_group, DEV) == Leave(chat_id=PERSONAL_GROUP_ID)
    assert domain.admit(make_arrival(bot=BotName.PERSONAL, chat_id=PERSONAL_GROUP_ID), _personal_without_group()) == (
        Leave(chat_id=PERSONAL_GROUP_ID)
    )


def test_a_removal_from_a_foreign_chat_leaves_nothing_to_leave() -> None:
    arrival = make_arrival(event=EventType.MY_CHAT_MEMBER, user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID)
    assert domain.admit(replace(arrival, bot_present=False), DEV) == Drop(DropReason.ALREADY_OUT)


def test_a_private_chat_or_an_unknown_chat_type_is_never_left() -> None:
    assert domain.admit(make_arrival(user_id=STRANGER_ID, chat_id=STRANGER_ID), DEV) == Drop(DropReason.STRANGER)
    unknown = make_arrival(user_id=STRANGER_ID, chat_id=FOREIGN_GROUP_ID)
    assert domain.admit(replace(unknown, chat_kind=None), DEV) == Drop(DropReason.STRANGER)


@pytest.mark.parametrize("chat_id", [DEV_GROUP_ID, None], ids=["callback_in_the_group", "inline_query"])
def test_update_kinds_the_bots_never_act_on_are_dropped_even_from_the_owner(chat_id: int | None) -> None:
    arrival = replace(make_arrival(event=EventType.OTHER), chat_id=chat_id, chat_kind=None)
    assert domain.admit(arrival, DEV) == Drop(DropReason.NOT_SERVED)


def test_an_update_is_judged_only_by_its_own_bots_policy() -> None:
    with pytest.raises(ValueError, match="cannot be judged"):
        domain.admit(make_arrival(bot=BotName.PERSONAL), DEV)


# --- membership -----------------------------------------------------------------------------------------------------
def test_a_chat_member_status_says_whether_the_bot_is_in_the_chat() -> None:
    assert all(domain.is_present(status) for status in ("creator", "administrator", "member"))
    assert not any(domain.is_present(status) for status in ("left", "kicked", "restricted"))
    assert domain.is_present("restricted", is_member=True)
