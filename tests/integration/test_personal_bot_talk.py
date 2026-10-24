"""T77: the personal bot as the owner talks to it after the first question (SPEC §3.2.13–§3.2.15, §6; UC-03, UC-09,
UC-30, UC-32, UC-34; D8).

- "to'xta" stops the answer being written (its voice note too), and it is never answered later;
- "to'liqroq" sends the stored full version of a shortened answer; "yangi suhbat" starts a fresh conversation;
- a settings change by chat applies to the next answer; one that weakens a safeguard waits for one "ha";
- "X saytining skrinshoti": the assistant calls its `yordamchi_media` tool, and the images follow the answer;
- `/start` gets the hello; the owner in a foreign group makes the bot leave it.

Same harness as `test_personal_bot.py`.
"""

import asyncio
from collections.abc import AsyncIterator

from tests.factories import FOREIGN_GROUP_ID, OWNER_ID, Fakes
from tests.fakes.assistant import FakeAssistant
from tests.integration.personal_bot_harness import PersonalBot, personal_bot
from tests.integration.test_personal_bot import qa_requests
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.ports.assistant import AssistantDelta, AssistantReply, AssistantRequest
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings


def change(key: str, value: JsonValue) -> dict[str, JsonValue]:
    """The settings assistant's structured answer for one change."""
    return {"outcome": "change", "changes": [{"key": key, "value": value}], "keys": []}


class SlowAssistant(FakeAssistant):
    """A `FakeAssistant` whose answers wait for `release` while streaming: an answer being written."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()
        self.started = asyncio.Event()

    async def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        self.started.set()
        yield AssistantDelta(text="Yozyapman ")
        await self.release.wait()
        async for item in super().stream(request, idle_timeout=idle_timeout):
            yield item


class ToolCallingAssistant(FakeAssistant):
    """A `FakeAssistant` that calls one in-process tool of the request before it answers, as Claude would."""

    def __init__(self, tool: str, arguments: dict[str, JsonValue]) -> None:
        super().__init__()
        self.tool = tool
        self.arguments = arguments
        self.outputs: list[str] = []

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        if request.kind == "qa":
            for toolset in request.toolsets:
                for tool in toolset.tools:
                    if tool.name == self.tool:
                        self.outputs.append((await tool.handler(self.arguments)).text)
        return await super().ask(request, timeout=timeout)


async def test_to_xta_stops_the_answer_being_written_and_it_never_comes_later() -> None:
    assistant = SlowAssistant()
    async with personal_bot(fakes=Fakes(assistant=assistant)) as bot:
        await bot.started()
        assistant.reply_with("qa", "Uzun javob.\n---OVOZ---\nUzun javob.")
        await bot.say("Menga O'zbekiston tarixini batafsil aytib ber.")
        await bot.within(10, assistant.started.is_set, what="the answer is being written")
        took = await _stop(bot)
        assert took <= 3  # UC-03
        assistant.release.set()
        await bot.advance(10)
        assert not bot.posted("Uzun javob.")  # the stopped answer never arrives
        assert not bot.requests("sendVoice")
        await bot.say("to'xta")
        await bot.within(3, lambda: bool(bot.posted(S.PERSONAL_NOTHING_TO_STOP())), what="nothing to stop")


async def test_hammasini_to_xtat_also_drops_the_questions_waiting_behind_it() -> None:
    assistant = SlowAssistant()
    async with personal_bot(fakes=Fakes(assistant=assistant)) as bot:
        await bot.started()
        assistant.reply_with("qa", "Birinchi javob.\n---OVOZ---\nBir.", "Ikkinchi javob.\n---OVOZ---\nIkki.")
        await bot.say("Birinchi savolim: kitob tavsiya qil.")
        await bot.within(10, assistant.started.is_set, what="the first answer is being written")
        await bot.say("Ikkinchi savolim: film tavsiya qil.")
        await bot.advance(3)  # it waits behind the first one
        await bot.say("hammasini to'xtat")
        await bot.within(3, lambda: bool(bot.posted(S.PERSONAL_STOPPED())), what="stopped")
        assistant.release.set()
        await bot.advance(10)
        assert not bot.posted("Birinchi javob.")
        assert not bot.posted("Ikkinchi javob.")
        assert len(qa_requests(bot)) == 0  # the first ask never got its reply; the second was never asked


async def _stop(bot: PersonalBot) -> float:
    await bot.say("to'xta")
    return await bot.within(3, lambda: bool(bot.posted(S.PERSONAL_STOPPED())), what="stopped")


async def test_to_liqroq_sends_the_full_version_and_yangi_suhbat_starts_afresh() -> None:
    async with personal_bot() as bot:
        await bot.started()
        full = "To'liq javob: " + "juda batafsil tushuntirish. " * 20
        bot.fakes.assistant.reply_with("qa", f"Qisqa javob.\n---OVOZ---\nQisqa.\n---TOLIQ---\n{full}")
        await bot.say("Fotosintez qanday ishlaydi?")
        await bot.within(10, lambda: bool(bot.posted("Qisqa javob.")), what="the short answer")
        assert S.QA_MORE_HINT() in bot.posted("Qisqa javob.")[0]
        await bot.say("to'liqroq")
        await bot.within(10, lambda: bool(bot.posted("To'liq javob: juda batafsil")), what="the full version")
        await bot.say("yangi suhbat")
        await bot.within(10, lambda: bool(bot.posted(S.QA_NEW_CONVERSATION())), what="a new conversation")
        assert [kind for kind, _ in bot.fakes.assistant.resets] == ["qa"]
        assert len(qa_requests(bot)) == 1  # neither command was a question for the model


async def test_a_settings_change_by_chat_applies_to_the_next_answer() -> None:
    async with personal_bot() as bot:
        await bot.started()
        bot.fakes.assistant.reply_with("settings", change("persona.personal_verbosity", "long"))
        await bot.say("javoblaring batafsilroq bo'lsin")
        await bot.within(10, lambda: bool(bot.posted("Sozlama o'zgardi")), what="the change")
        assert "persona.personal_verbosity" in bot.posted("Sozlama o'zgardi")[0]
        bot.fakes.assistant.reply_with("qa", "Javob.\n---OVOZ---\nJavob.")
        await bot.say("Quyosh nega issiq?")
        await bot.within(10, lambda: bool(bot.posted("Javob.")), what="the next answer")
        [request] = qa_requests(bot)
        assert "Verbosity: long" in request.instructions  # from the next message on (UC-34)


async def test_a_weakening_change_waits_for_one_ha() -> None:
    async with personal_bot() as bot:
        await bot.started()
        bot.fakes.assistant.reply_with("settings", change("models.qa", "best"))
        await bot.say("sozlama: javoblar uchun eng kuchli modelni ishlat")
        await bot.within(10, lambda: bool(bot.posted("Muhim qaror #1")), what="the one question")
        snap = await settings.snapshot(bot.deps)
        assert snap[defaults.MODELS_QA] == "sonnet"  # nothing changed before the "ha"
        await bot.say("ha")
        await bot.within(10, lambda: bool(bot.posted("Sozlama o'zgardi")), what="applied on ha")
        snap = await settings.snapshot(bot.deps)
        assert snap[defaults.MODELS_QA] == "best"
        assert qa_requests(bot) == []  # "ha" was the answer, not a question


async def test_a_screenshot_request_runs_the_media_tool_and_the_images_follow_the_answer() -> None:
    assistant = ToolCallingAssistant("screenshot", {"url": "https://example.com"})
    async with personal_bot(fakes=Fakes(assistant=assistant)) as bot:
        await bot.started()
        assistant.reply_with("qa", "Skrinshot tayyorlanmoqda, hozir yuboraman.\n---OVOZ---\nSkrinshot hozir keladi.")
        await bot.say("example.com saytining skrinshotini ol")
        await bot.within(15, lambda: bool(bot.requests("sendPhoto")), what="the screenshot photo")
        await bot.within(5, lambda: bool(bot.requests("sendDocument")), what="the full-page file")
        assert '"status": "started"' in assistant.outputs[0]
        assert bot.fakes.web.calls == [("screenshot", "https://example.com")]
        [photo] = bot.requests("sendPhoto")
        assert photo.params.get("caption") == S.ARTEFACT_SCREENSHOT(site="example.com")
        assert {r.params.get("chat_id") for r in bot.requests("sendPhoto", "sendDocument")} == {OWNER_ID}
        bot.session.assert_no_reply_markup()


async def test_start_gets_the_hello_and_a_foreign_group_is_left() -> None:
    async with personal_bot() as bot:
        await bot.started()
        await bot.say("/start")
        await bot.within(5, lambda: bool(bot.posted("Salom! Men shaxsiy yordamchingizman")), what="the hello")
        before = len(bot.session.requests)
        await bot.say("salom", chat_id=FOREIGN_GROUP_ID)
        assert bot.methods()[before:] == ["leaveChat"]
        assert qa_requests(bot) == []
