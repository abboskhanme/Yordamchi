"""The dev bot's chat pipeline, path by path, on the whole app (SPEC §3.2.5, §3.2.6, §6.1; UC-03, UC-10, UC-15;
T31): the interrupt's target, a routing question and its answer, a command that names no project, server hosts by
chat, and a failed job's code in `holat?`."""

from tests.factories import STRANGER_ID
from tests.fakes.agent import Pause, ToolStep
from tests.integration.dev_bot_harness import DevBot, dev_bot, register_projects
from tests.integration.test_dev_bot import OTHER, PROJECT, result, route_reply, turn_status
from yordamchi.bots.dev import jobs
from yordamchi.core import strings as S
from yordamchi.core.errors import ProjectNotFound
from yordamchi.dev.turns.domain import TurnStatus


async def _running(bot: DevBot, project: str, text: str) -> None:
    bot.fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), Pause(), result())
    await bot.say(text)

    async def working() -> bool:
        return await turn_status(bot, project) == TurnStatus.RUNNING.value

    await bot.within(30, working, what=f"{project} running")


async def test_a_stop_with_two_turns_working_asks_which_and_a_named_stop_stops_only_that_one() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        await _running(bot, PROJECT, "BonviHR'da login sahifasini tuzat")
        await _running(bot, OTHER, "HRMentor'da hisobot sahifasini qo'sh")
        await bot.say("to'xta")
        await bot.within(5, lambda: bool(bot.posted("Bir nechta ish ketyapti")), what="which one")
        assert "BonviHR" in bot.posted("Bir nechta ish ketyapti")[0]
        await bot.say("HRMentor'ni to'xtat")
        await bot.within(5, lambda: bool(bot.posted(S.STOPPED(project=OTHER))), what="HRMentor stopped")
        assert await turn_status(bot, PROJECT) == TurnStatus.RUNNING.value
        assert not bot.posted(S.STOPPED(project=PROJECT))


async def test_a_forwarded_stop_or_command_is_material_never_an_order() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await _running(bot, PROJECT, "BonviHR'da login sahifasini tuzat")
        await bot.say("to'xta", forwarded_from=STRANGER_ID)  # someone else's "to'xta" stops nothing (T33)
        await bot.say("holat?", forwarded_from=STRANGER_ID)
        await bot.advance(15)
        assert not bot.posted(S.STOPPED(project=PROJECT))
        assert not bot.posted("Holat · dev bot")
        assert bot.fakes.agents.interrupts == []  # the working turn was never interrupted


async def test_a_routing_question_is_answered_by_number_and_the_work_goes_there() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        unsure = route_reply("instruct_project", PROJECT, confidence=0.5)
        unsure["candidates"] = [{"project_id": PROJECT, "confidence": 0.5}, {"project_id": OTHER, "confidence": 0.45}]
        bot.fakes.assistant.reply_with("routing", unsure)
        bot.fakes.agents.script_turn(result())
        await bot.say("login sahifasidagi xatoni tuzat.")
        await bot.within(10, lambda: bool(bot.posted("BonviHR'gami yoki HRMentor'gami?")), what="the question")
        await bot.say("2")
        await bot.within(10, lambda: bool(bot.posted("→ HRMentor")), what="routed to the pick")

        async def done() -> bool:
            return await turn_status(bot, OTHER) == TurnStatus.DONE.value

        await bot.within(20, done, what="HRMentor's turn ran")
        assert "login sahifasidagi xatoni tuzat" in bot.fakes.agents.sends[0][1].text
        assert await turn_status(bot, PROJECT) is None


async def test_a_phrase_that_names_no_project_is_work_for_routing_not_a_command() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        bot.fakes.assistant.reply_with("routing", route_reply("instruct_project", PROJECT))
        bot.fakes.agents.script_turn(result())
        await bot.say("serverni to'xtat")  # "X'ni to'xtat" with X no project: an instruction
        await bot.within(10, lambda: bool(bot.posted("→ BonviHR")), what="routed as work")
        assert not bot.posted(S.STOP_ALL_NOTHING())


async def test_servers_are_watched_by_chat_and_their_status_is_checked_in_the_background() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await bot.say("bonvi-srv serverini kuzat")
        await bot.within(5, lambda: bool(bot.posted("bonvi-srv: kuzatuvga oldim")), what="host added")
        bot.fakes.assistant.reply_with("routing", route_reply("server_status", None))
        await bot.say("bonvi-srv qanday?")
        await bot.within(10, lambda: bool(bot.posted("Tekshiryapman")), what="checking first")
        await bot.within(30, lambda: bool(bot.posted("bonvi-srv: ")), what="the status line")
        await bot.say("bonvi-srv'ni kuzatma")
        await bot.within(5, lambda: bool(bot.posted(S.SERVERS_HOST_REMOVED(host="bonvi-srv"))), what="removed")


async def test_a_failed_jobs_code_is_noted_for_holat_and_told_to_the_owner() -> None:
    async with dev_bot() as bot:

        async def fails() -> int:
            raise ProjectNotFound(name="Nope")

        assert await jobs.run(bot.deps, bot.app.health, fails(), job="test_job") is None
        assert [note.code for note in bot.app.health.errors] == ["project_not_found"]
        await bot.within(5, lambda: bool(bot.posted("«Nope» degan loyiha topilmadi")), what="told")
        await bot.say("holat?")
        await bot.within(5, lambda: bool(bot.posted("project_not_found")), what="in holat")
