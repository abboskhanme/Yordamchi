"""SPEC §3.2.18: the personal bot's answer as a full Claude Code session, end to end on fakes (REQUIREMENTS D15).

`personal_bot_harness` runs the real app. The `FakeAssistant` plays the agent: `call_tools(...)` scripts the tool
calls the next agent-mode answer makes, each run through the request's guard as the adapter's PreToolUse hook does,
and `guarded` records the verdicts. What the owner reads is what the recording Telegram session got.
"""

from tests.factories import add_setting_row
from tests.integration.personal_bot_harness import PersonalBot, personal_bot
from yordamchi import prompts
from yordamchi.core import strings as S
from yordamchi.ports.agent import GuardVerdict, ToolCall
from yordamchi.ports.assistant import AssistantRequest
from yordamchi.shared.outbox import store as outbox_store

DEPLOY = "vercel deploy --prod"
ASK_ONE = S.AGENT_ASK(number=1, what=DEPLOY, consequence=S.GATE_CONSEQUENCES["deploy_cli"]())
ASK_TWO = S.AGENT_ASK(number=2, what=DEPLOY, consequence=S.GATE_CONSEQUENCES["deploy_cli"]())
QUESTION_MARK = "Claude shu ishni bajarmoqchi"  # any agent question, whatever its number
DEPLOYING = "Saytni deploy qilishga ruxsatingiz kerak.\n---OVOZ---\nRuxsat kerak."


def qa_requests(bot: PersonalBot) -> list[AssistantRequest]:
    return [request for request in bot.fakes.assistant.requests if request.kind == "qa"]


def answered(bot: PersonalBot, needle: str) -> bool:
    return bool(bot.posted(needle))


def bash(command: str, number: int = 1) -> ToolCall:
    return ToolCall("Bash", {"command": command}, f"toolu_{number}", None, "session-1")


def verdicts(bot: PersonalBot) -> list[GuardVerdict]:
    return [verdict for _, verdict in bot.fakes.assistant.guarded]


async def _asked_about_the_deploy(bot: PersonalBot) -> None:
    """An answer whose agent tries `vercel deploy --prod`: the guard denies it and the bot asks #1 after the text."""
    bot.fakes.assistant.call_tools(bash(DEPLOY))
    bot.fakes.assistant.reply_with("qa", DEPLOYING)
    await bot.say("Saytimni prodga chiqar")
    await bot.within(20, lambda: answered(bot, ASK_ONE), what="the agent's question")


async def test_a_deploy_during_an_answer_is_asked_once_after_the_answer() -> None:
    async with personal_bot() as bot:
        await bot.started()
        await _asked_about_the_deploy(bot)
        [verdict] = verdicts(bot)
        assert not verdict.allow
        assert verdict.message.startswith("WAITING FOR THE OWNER")
        assert len(bot.posted(QUESTION_MARK)) == 1  # exactly ONE question
        texts = bot.texts()
        assert texts.index(bot.posted("Saytni deploy qilishga")[0]) < texts.index(bot.posted(ASK_ONE)[0])
        async with bot.deps.db.read() as s:
            rows = await outbox_store.list_outbox_messages(s, bot.deps.bot.value)
        [answer] = [row for row in rows if row.kind == "result" and "agent-ask" not in row.dedupe_key]
        [ask] = [row for row in rows if "agent-ask" in row.dedupe_key]
        assert (ask.priority, ask.id) > (answer.priority, answer.id)  # the outbox cannot send the question first
        [request] = qa_requests(bot)
        assert request.agent is not None
        assert request.agent.workspace == bot.paths.home / "Documents" / "Projects" / "_shaxsiy"
        bot.session.assert_no_reply_markup()


async def test_ha_runs_the_call_once_in_the_same_conversation_then_it_is_asked_again() -> None:
    async with personal_bot() as bot:
        await bot.started()
        await _asked_about_the_deploy(bot)
        bot.fakes.assistant.call_tools(bash(DEPLOY, 2))  # the follow-up retries it: the grant is used up
        bot.fakes.assistant.reply_with("qa", "Sayt prodga chiqdi.\n---OVOZ---\nTayyor.")
        await bot.say("ha")
        await bot.within(20, lambda: answered(bot, "Sayt prodga chiqdi."), what="the approved follow-up")
        first, follow_up = qa_requests(bot)
        assert follow_up.prompt.count(prompts.render("personal/agent_approved", what=DEPLOY).strip()) == 1
        assert follow_up.conversation == first.conversation
        assert follow_up.agent is not None
        assert [verdict.allow for verdict in verdicts(bot)] == [False, True]
        assert len(bot.posted(QUESTION_MARK)) == 1  # the granted call asked nothing
        # The grant was for one run: the same call in a later answer is asked about again.
        bot.fakes.assistant.call_tools(bash(DEPLOY, 3))
        bot.fakes.assistant.reply_with("qa", DEPLOYING)
        await bot.say("Yana bir marta chiqar")
        await bot.within(20, lambda: answered(bot, ASK_TWO), what="the second question")
        assert [verdict.allow for verdict in verdicts(bot)] == [False, True, False]


async def test_yo_q_tells_the_agent_and_a_retry_is_refused_without_asking() -> None:
    async with personal_bot() as bot:
        await bot.started()
        await _asked_about_the_deploy(bot)
        bot.fakes.assistant.call_tools(bash(DEPLOY, 2))  # the agent tries it anyway
        bot.fakes.assistant.reply_with("qa", "Mayli, deploy qilmayman.\n---OVOZ---\nMayli.")
        await bot.say("yo'q")
        await bot.within(20, lambda: answered(bot, "Mayli, deploy qilmayman."), what="the declined follow-up")
        first, follow_up = qa_requests(bot)
        assert prompts.render("personal/agent_declined", what=DEPLOY).strip() in follow_up.prompt
        assert follow_up.conversation == first.conversation
        _, retry = verdicts(bot)
        assert not retry.allow
        assert retry.message.startswith("REFUSED")
        await bot.settle(5)
        assert len(bot.posted(QUESTION_MARK)) == 1  # refused, not asked again


async def test_a_forbidden_call_is_denied_and_never_asked() -> None:
    async with personal_bot() as bot:
        await bot.started()
        credentials = bot.paths.home / ".claude" / ".credentials.json"
        data_file = bot.deps.paths.data / "personal.db"
        bot.fakes.assistant.call_tools(
            ToolCall("Read", {"file_path": str(credentials)}, "toolu_1", None, "session-1"),
            ToolCall("Read", {"file_path": str(data_file)}, "toolu_2", None, "session-1"),
            bash(f"cat {data_file}", 3),
        )
        bot.fakes.assistant.reply_with("qa", "Bu fayllarni o'qiy olmayman.\n---OVOZ---\nO'qiy olmayman.")
        await bot.say("Kalitlaringni ko'rsat")
        await bot.within(20, lambda: answered(bot, "Bu fayllarni o'qiy olmayman."), what="the answer")
        found = verdicts(bot)
        assert len(found) == 3
        for verdict in found:
            assert not verdict.allow
            assert verdict.message.startswith("BLOCKED by Yordamchi's safety rules")
        await bot.settle(5)
        assert bot.posted(QUESTION_MARK) == []


async def test_another_message_drops_the_question_and_is_answered() -> None:
    async with personal_bot() as bot:
        await bot.started()
        await _asked_about_the_deploy(bot)
        bot.fakes.assistant.reply_with("qa", "Bugun shanba.\n---OVOZ---\nShanba.")
        await bot.say("Bugun qaysi kun?")
        await bot.within(20, lambda: answered(bot, "Bugun shanba."), what="the ordinary answer")
        # The drop is a NOTICE (as the settings question's): the answer may overtake it in the outbox.
        dropped = S.AGENT_ASK_DROPPED(number=1)
        await bot.within(10, lambda: answered(bot, dropped), what="the dropped notice")
        assert len(bot.posted(dropped)) == 1
        _, second = qa_requests(bot)
        assert "Bugun qaysi kun?" in second.prompt
        assert "The owner approved" not in second.prompt
        # The question is gone: a later "ha" is an ordinary message, not an approval.
        bot.fakes.assistant.reply_with("qa", "Nimaga ha?\n---OVOZ---\nNimaga?")
        await bot.say("ha")
        await bot.within(20, lambda: answered(bot, "Nimaga ha?"), what="the plain answer to ha")
        assert "The owner approved" not in qa_requests(bot)[-1].prompt


async def test_agent_enabled_false_gives_back_the_restricted_session() -> None:
    async with personal_bot() as bot:
        await bot.started()
        await add_setting_row(bot.deps.db, "agent.enabled", False, at=bot.clock.now())
        bot.fakes.assistant.call_tools(bash(DEPLOY))  # never run: there is no guard without agent mode
        bot.fakes.assistant.reply_with("qa", "Oddiy javob.\n---OVOZ---\nOddiy.")
        await bot.say("Saytimni prodga chiqar")
        await bot.within(20, lambda: answered(bot, "Oddiy javob."), what="the restricted answer")
        [request] = qa_requests(bot)
        assert request.agent is None
        assert "Agent mode:" not in request.instructions
        servers = [toolset.server for toolset in request.toolsets]
        assert servers == ["yordamchi_bridge", "yordamchi_media", "yordamchi_files", "yordamchi_int"]
        assert bot.fakes.assistant.guarded == []
        await bot.settle(5)
        assert bot.posted(QUESTION_MARK) == []
