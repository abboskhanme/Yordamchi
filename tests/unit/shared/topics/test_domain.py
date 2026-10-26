"""`shared/topics/domain.py`: the forum-topic rules, pure (D4 revised 2026-10-26; SPEC §3.2.16)."""

from datetime import timedelta
from typing import Final

import pytest

from tests.fakes.clock import DEFAULT_START
from yordamchi.shared.topics.domain import (
    ICON_COLORS,
    NAME_MAX,
    ChatTopics,
    KnownTopic,
    Refusal,
    Step,
    TopicPlan,
    icon_color_of,
    plan_repair,
    plan_topic,
    refused_lately,
    topic_link,
    topic_name,
    topic_of,
)

CHAT: Final = -1001234567890
NOW: Final = DEFAULT_START
RETRY: Final = timedelta(minutes=30)
FORUM: Final = ChatTopics(chat_id=CHAT, is_forum=True)


def _plan(topic: KnownTopic | None, chat: ChatTopics | None = FORUM, *, enabled: bool = True) -> TopicPlan:
    return plan_topic(topic, chat, chat_id=CHAT, enabled=enabled, now=NOW, retry_after=RETRY)


def test_a_project_always_gets_the_same_one_of_telegrams_six_colours() -> None:
    colours = {icon_color_of(name) for name in ("BonviHR", "HRMentor", "Yordamchi", "OvozYoz", "WunderkindLC")}
    assert colours <= set(ICON_COLORS)
    assert len(colours) > 1  # spread, not one colour for all
    assert icon_color_of("BonviHR") == icon_color_of("BonviHR")
    assert icon_color_of("BonviHR") == 0xFB6F5F  # fixed across processes: sha256, never Python's salted hash()


def test_the_topic_is_named_after_the_project_within_telegrams_limit() -> None:
    assert topic_name("BonviHR") == "BonviHR"
    assert len(topic_name("X" * 300)) == NAME_MAX


@pytest.mark.parametrize(
    ("thread", "is_topic", "expected"),
    [
        pytest.param(77, True, 77, id="a_topic_message"),
        pytest.param(77, False, None, id="a_reply_chain_in_general"),
        pytest.param(None, False, None, id="general"),
    ],
)
def test_only_a_topic_message_has_a_topic(thread: int | None, is_topic: bool, expected: int | None) -> None:
    assert topic_of(thread, is_topic_message=is_topic) == expected


def test_the_link_opens_the_topic_in_a_supergroup() -> None:
    assert topic_link(CHAT, 501) == "https://t.me/c/1234567890/501"
    assert topic_link(-4123456789, 501) is None  # a basic group has no t.me/c link
    assert topic_link(424242, 501) is None


def test_an_open_topic_is_used_and_a_closed_one_reopened() -> None:
    assert _plan(KnownTopic("BonviHR", CHAT, 501)) == TopicPlan(Step.USE, 501)
    closed = KnownTopic("BonviHR", CHAT, 501, closed_at=NOW - timedelta(hours=1))
    assert _plan(closed) == TopicPlan(Step.REOPEN, 501)


def test_a_project_with_no_topic_in_this_chat_gets_one() -> None:
    assert _plan(None).step is Step.CREATE
    assert _plan(KnownTopic("BonviHR", -1009999999999, 501)).step is Step.CREATE  # the dev group changed


@pytest.mark.parametrize(
    ("chat", "enabled", "reason"),
    [
        pytest.param(FORUM, False, "disabled", id="setting_off"),
        pytest.param(None, True, "not_forum", id="never_seen"),
        pytest.param(ChatTopics(CHAT, is_forum=False), True, "not_forum", id="topics_off"),
        pytest.param(
            ChatTopics(CHAT, is_forum=True, refused_at=NOW - timedelta(minutes=5), refusal=Refusal.NO_RIGHT),
            True,
            "refused",
            id="refused_lately",
        ),
    ],
)
def test_no_topic_means_general(chat: ChatTopics | None, enabled: bool, reason: str) -> None:
    assert _plan(KnownTopic("BonviHR", CHAT, 501), chat, enabled=enabled) == TopicPlan(Step.NONE, reason=reason)


def test_a_refusal_for_want_of_the_right_is_asked_again_after_the_wait() -> None:
    lately = ChatTopics(CHAT, is_forum=True, refused_at=NOW - timedelta(minutes=29), refusal=Refusal.NO_RIGHT)
    long_ago = ChatTopics(CHAT, is_forum=True, refused_at=NOW - timedelta(minutes=31), refusal=Refusal.NO_RIGHT)
    not_forum_then = ChatTopics(CHAT, is_forum=True, refused_at=NOW - timedelta(minutes=1), refusal=Refusal.NOT_FORUM)
    assert refused_lately(lately, now=NOW, retry_after=RETRY)
    assert not refused_lately(long_ago, now=NOW, retry_after=RETRY)
    assert not refused_lately(not_forum_then, now=NOW, retry_after=RETRY)  # it is a forum now: try at once
    assert _plan(None, long_ago).step is Step.CREATE


def test_a_closed_topic_is_reopened_and_a_gone_one_opened_anew() -> None:
    topic = KnownTopic("BonviHR", CHAT, 501)
    assert plan_repair(topic, closed=True) == TopicPlan(Step.REOPEN, 501)
    assert plan_repair(topic, closed=False) == TopicPlan(Step.CREATE)
    assert plan_repair(None, closed=False) == TopicPlan(Step.NONE, reason="unknown_thread")  # the owner's own topic
