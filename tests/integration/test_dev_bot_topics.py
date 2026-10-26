"""Bot-managed forum topics in the dev group, end to end on fakes (owner, 2026-10-26; D4 revised; SPEC §3.2.16).

"Split the group into topics, but I won't open topics by hand: you open a topic for each of my projects
automatically. In General I ask general questions." The real dev app runs on the recording aiogram session, whose
`ForumTopics` is Telegram's side: a forum where the bot may manage topics (`bot.forum()`), unless a test says
otherwise. Messages say which topic they are in (`thread_id`); `in_topic`/`in_general` read what went where.
"""

from datetime import UTC, datetime
from typing import Final

from tests.factories import DEV_GROUP_ID, agent_question, make_owner_origin
from tests.fakes.agent import Pause, done_result
from tests.integration.dev_bot_harness import DevBot, dev_bot, register_projects
from yordamchi.core import strings as S
from yordamchi.dev.routing import store as routing_store
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.domain import TurnStatus
from yordamchi.ports.agent import QuestionAsked
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.topics import store as topics_store
from yordamchi.shared.topics.domain import ICON_COLORS

PROJECT: Final = "BonviHR"
OTHER: Final = "HRMentor"
FINAL: Final = (
    "Natija: login tuzatildi.\n<handoff>\nGoal: fix login\nDecisions: none\nState: pushed\nNext: nothing\n</handoff>"
)
LIVE_LINE: Final = " · ⏱ "  # the live line of a working turn as the owner sees it
POINTER: Final = "mavzusida davom etadi"
EPOCH: Final = datetime(2000, 1, 1, tzinfo=UTC)


async def started(bot: DevBot) -> None:
    """The startup messages are out, so what follows is the owner's."""
    await bot.within(10, lambda: bool(bot.requests("pinChatMessage")), what="'Onlayn' posted and pinned")
    await bot.quiet()


async def topic_of(bot: DevBot, project: str) -> int | None:
    async with bot.deps.db.read() as s:
        row = await topics_store.get_project_topic(s, project)
    return None if row is None else row.thread_id


async def turn_status(bot: DevBot, project: str) -> str | None:
    async with bot.deps.db.read() as s:
        rows = await turns_store.list_project_turns(s, project, limit=1)
    return rows[0].status if rows else None


async def first_work(bot: DevBot, project: str) -> int:
    """The owner's first instruction for `project`, from General, done; returns the topic it opened."""
    bot.fakes.agents.script_turn(done_result(FINAL))
    await bot.say(f"{project}'da login sahifasini tuzat")
    await bot.within(30, lambda: bool(bot.posted(f"{project} · Natija:")), what=f"{project}'s first result")
    thread = await topic_of(bot, project)
    assert thread is not None
    await bot.quiet()
    return thread


async def test_work_from_general_goes_on_in_the_projects_own_topic_with_one_line_in_general() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        await started(bot)
        bot.fakes.agents.script_turn(Pause(), done_result(FINAL))
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(30, lambda: bool(bot.posted(LIVE_LINE)), what="the live line")

        [created] = bot.requests("createForumTopic")  # its first work opened the project's topic, and only it
        assert created.params["name"] == PROJECT
        assert created.params["icon_color"] in ICON_COLORS
        thread = await topic_of(bot, PROJECT)
        assert thread is not None
        assert bot.in_topic(thread, "→ BonviHR")  # the echo
        assert bot.in_topic(thread, LIVE_LINE)  # the live line
        [pointer] = bot.in_general("→ BonviHR")
        assert POINTER in pointer
        link = next(r for r in bot.requests("sendMessage") if POINTER in str(r.params.get("text")))
        assert f"https://t.me/c/{str(DEV_GROUP_ID).removeprefix('-100')}/{thread}" in str(link.params["text"])

        await bot.fakes.agents.release(bot.fakes.agents.sends[-1][0])
        await bot.within(30, lambda: bool(bot.posted("BonviHR · Natija:")), what="the result")
        assert bot.in_topic(thread, "BonviHR · Natija:")
        assert not bot.in_general("Natija")
        assert [text for text in bot.in_general() if PROJECT in text] == [pointer]  # one line, nothing else
        assert not bot.requests("createForumTopic")[1:]  # nothing opened for HRMentor, which had no work

        await bot.quiet()
        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("BonviHR'da testlarni yoz")
        await bot.within(30, lambda: len(bot.posted("BonviHR · Natija:")) == 2, what="the second result")
        assert len(bot.requests("createForumTopic")) == 1  # later work reuses the topic
        assert len(bot.in_topic(thread, "BonviHR · Natija:")) == 2


async def test_a_message_in_a_project_topic_goes_to_that_project_with_no_routing_model() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        await started(bot)
        thread = await first_work(bot, PROJECT)
        asked = len(bot.fakes.assistant.requests)

        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("endi testlarini ham yoz", thread_id=thread)  # no project named
        await bot.within(30, lambda: len(bot.posted("BonviHR · Natija:")) == 2, what="the topic's work done")
        assert len(bot.fakes.assistant.requests) == asked  # no routing ask, no question
        assert "endi testlarini ham yoz" in bot.fakes.agents.sends[-1][1].text
        assert not bot.posted(S.WHICH_PROJECT_NAME())
        assert not bot.in_topic(thread, "→ BonviHR")[1:]  # only the first echo: in its own topic "→ X" says nothing
        async with bot.deps.db.read() as s:
            decisions = await routing_store.list_route_decisions_since(s, EPOCH, limit=50)
        assert (decisions[0].method, decisions[0].project_id) == ("topic", PROJECT)  # the newest: recorded as any


async def test_small_talk_in_a_project_topic_is_answered_in_that_topic() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        thread = await first_work(bot, PROJECT)
        await bot.say("rahmat", thread_id=thread)
        await bot.within(5, lambda: bool(bot.posted(S.CHAT_THANKS())), what="the small-talk answer")
        assert bot.in_topic(thread, S.CHAT_THANKS())
        assert not bot.in_general(S.CHAT_THANKS())


async def test_a_bare_stop_in_a_topic_stops_that_project_only() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        await started(bot)
        bot.fakes.agents.script_turn(Pause())
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(30, lambda: bool(bot.posted(LIVE_LINE)), what="BonviHR works")
        bot.fakes.agents.script_turn(Pause())
        await bot.say("HRMentor'da hisobotni tuzat")
        await bot.within(30, lambda: len(bot.posted(LIVE_LINE)) == 2, what="HRMentor works too")
        thread = await topic_of(bot, PROJECT)
        assert thread is not None

        await bot.say("to'xta", thread_id=thread)  # two turns work; no project named, none replied to
        await bot.within(5, lambda: bool(bot.posted(S.STOPPED(project=PROJECT))), what="BonviHR stopped")
        assert bot.in_topic(thread, S.STOPPED(project=PROJECT))
        assert not bot.posted(S.WHICH_RUNNING(projects=f"{PROJECT}, {OTHER}"))
        await bot.advance(3)
        assert await turn_status(bot, OTHER) == TurnStatus.RUNNING.value


async def test_an_answer_in_a_project_topic_binds_to_that_projects_question_only() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        await started(bot)
        asked = agent_question("Qaysi bazadan foydalanamiz?", [("SQLite", "oddiy"), ("Postgres", "server")])
        bot.fakes.agents.script_turn(QuestionAsked("q1", (asked,)), Pause(), done_result(FINAL))
        await bot.say("BonviHR'da bazani ula")
        await bot.within(20, lambda: bool(bot.posted("BonviHR · Savol:")), what="BonviHR's question")
        other = agent_question("Qaysi formatda?", [("PDF", "chop etish"), ("Excel", "jadval")])
        bot.fakes.agents.script_turn(QuestionAsked("q2", (other,)), Pause(), done_result(FINAL))
        await bot.say("HRMentor'da hisobot eksportini qo'sh")
        await bot.within(20, lambda: bool(bot.posted("HRMentor · Savol:")), what="HRMentor's question")
        thread = await topic_of(bot, PROJECT)
        assert thread is not None
        assert bot.in_topic(thread, "BonviHR · Savol:")  # the question is asked in the project's topic

        await bot.say("2", thread_id=thread)  # two questions open; in General "2" would be asked about
        await bot.within(10, lambda: bool(bot.fakes.agents.answers), what="the answer delivered")
        assert bot.fakes.agents.answers[0][2] == {"Qaysi bazadan foydalanamiz?": "Postgres"}
        assert not bot.posted("BonviHR savoligami yoki HRMentor savoligami")


async def test_two_topics_written_at_once_never_merge_into_one_burst() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        await started(bot)
        first = await first_work(bot, PROJECT)
        second = await first_work(bot, OTHER)
        sends = len(bot.fakes.agents.sends)

        bot.fakes.agents.script_turn(done_result(FINAL))
        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("README'ni yangila", thread_id=first)
        await bot.say("CHANGELOG'ni yangila", thread_id=second)  # inside the first one's burst window
        await bot.within(30, lambda: len(bot.fakes.agents.sends) == sends + 2, what="both delivered")
        delivered = {
            bot.fakes.agents._sessions[sid].spec.cwd.name: msg.text for sid, msg, _ in bot.fakes.agents.sends[sends:]
        }
        assert "README'ni yangila" in delivered[PROJECT]
        assert "CHANGELOG" not in delivered[PROJECT]
        assert "CHANGELOG'ni yangila" in delivered[OTHER]
        assert "README" not in delivered[OTHER]


async def test_a_topic_the_owner_deleted_is_opened_anew_on_the_next_message() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        thread = await first_work(bot, PROJECT)
        bot.session.topics.delete(DEV_GROUP_ID, thread)  # the owner deleted it; Telegram says nothing until a send
        before = len(bot.requests())

        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("BonviHR'da testlarni yoz")
        await bot.within(30, lambda: len(bot.posted("BonviHR · Natija:")) == 2, what="the second result")
        renewed = await topic_of(bot, PROJECT)
        assert renewed is not None
        assert renewed != thread
        assert len(bot.requests("createForumTopic")) == 2
        assert bot.in_topic(renewed, "BonviHR · Natija:")
        # The one send Telegram refused ("message thread not found"); nothing more went to the old topic.
        refused = [r for r in bot.requests()[before:] if r.params.get("message_thread_id") == thread]
        assert [r.api_method for r in refused] == ["sendMessage"]


async def test_a_topic_the_owner_closed_is_reopened_for_new_work() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        thread = await first_work(bot, PROJECT)
        bot.session.topics.close(DEV_GROUP_ID, thread)
        await bot.topic_closed(thread)  # Telegram's service message in that topic
        await bot.advance(3)
        assert not bot.posted(S.NOT_YET(phase="P2"))  # a service message is no request

        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("BonviHR'da testlarni yoz")
        await bot.within(30, lambda: len(bot.posted("BonviHR · Natija:")) == 2, what="the second result")
        assert [r.params["message_thread_id"] for r in bot.requests("reopenForumTopic")] == [thread]
        assert await topic_of(bot, PROJECT) == thread
        assert len(bot.in_topic(thread, "BonviHR · Natija:")) == 2


async def test_a_group_with_topics_off_works_as_the_flat_group_it_was() -> None:
    async with dev_bot() as bot:  # the group is no forum: Telegram's default here
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(30, lambda: bool(bot.posted("BonviHR · Natija:")), what="the result")
        assert not bot.requests("createForumTopic")  # never even asked
        assert bot.in_general("→ BonviHR") == ["→ BonviHR"]  # the echo where it always was, no pointer
        assert bot.in_general("BonviHR · Natija:")
        assert not [r for r in bot.requests() if "message_thread_id" in r.params]


async def test_topics_switched_off_by_setting_keep_everything_in_general() -> None:
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        thread = await first_work(bot, PROJECT)
        owner = make_owner_origin(update_id=90)
        await settings.record_version(bot.deps, owner, defaults.TOPICS_ENABLED, False, reason="change")

        await bot.say("salom")  # any message: the setting applies from the next one
        await bot.within(5, lambda: bool(bot.posted(S.CHAT_HELLO())), what="the greeting")
        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("BonviHR'da testlarni yoz")
        await bot.within(30, lambda: len(bot.posted("BonviHR · Natija:")) == 2, what="the second result")
        assert len(bot.in_general("BonviHR · Natija:")) == 1  # the second one, in General
        assert len(bot.in_topic(thread, "BonviHR · Natija:")) == 1  # the first one stays where it was
        assert len(bot.requests("createForumTopic")) == 1


async def test_without_the_manage_topics_right_the_work_goes_on_in_general_and_the_owner_is_told_once() -> None:
    async with dev_bot() as bot:
        bot.forum(can_manage=False)
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        await started(bot)
        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(30, lambda: bool(bot.posted("BonviHR · Natija:")), what="the result")
        assert bot.in_general("BonviHR · Natija:")
        assert len(bot.posted(S.TOPICS_NO_RIGHT())) == 1
        await bot.quiet()

        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("HRMentor'da hisobotni tuzat")
        await bot.within(30, lambda: bool(bot.posted("HRMentor · Natija:")), what="HRMentor's result")
        assert len(bot.requests("createForumTopic")) == 1  # refused lately: not asked again yet
        assert len(bot.posted(S.TOPICS_NO_RIGHT())) == 1


async def test_hamma_loyihalarga_topic_och_opens_one_topic_per_project_once() -> None:
    """Owner, 2026-10-26: "birinchi martaga barcha loyihalarim uchun topic ochvolish". Archived names and worktree
    copies get none; a second ask opens nothing new; no session opens (a topic is not work)."""
    async with dev_bot() as bot:
        bot.forum()
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER, "OldCRM Archive", "HRMentor-wt-api")
        await started(bot)
        await bot.say("hamma loyihalarga topic och")
        await bot.within(5, lambda: bool(bot.posted(S.TOPICS_ALL_STARTED(count=2, minutes=1))), what="the start")
        done = S.TOPICS_ALL_DONE(opened=2, existing=0)
        await bot.within(30, lambda: bool(bot.posted(done)), what="the topics")
        assert sorted(str(r.params["name"]) for r in bot.requests("createForumTopic")) == sorted([PROJECT, OTHER])
        assert await topic_of(bot, PROJECT) is not None
        assert bot.fakes.agents.opened == []  # a topic is not work: no session opened

        await bot.say("topiclarni och")
        again = S.TOPICS_ALL_DONE(opened=0, existing=2)
        await bot.within(30, lambda: bool(bot.posted(again)), what="nothing new")
        assert len(bot.requests("createForumTopic")) == 2


async def test_hamma_loyihalarga_topic_och_in_a_flat_group_says_why_not() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        await bot.say("hamma loyihalarga topic och")
        await bot.within(30, lambda: bool(bot.posted(S.TOPICS_ALL_UNAVAILABLE())), what="why not")
        assert bot.requests("createForumTopic") == []
