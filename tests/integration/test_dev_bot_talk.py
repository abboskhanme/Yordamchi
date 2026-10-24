"""T34b: the dev bot on fakes as the owner really talks to it — naturally, in Uzbek with Russian and English mixed in
(SPEC §6, §3.2.5; UC-03, UC-07, UC-09, UC-15 F; the owner's typing rule of 2026-10-24).

- "kut" means "wait, more is coming", never "stop": with work running it holds the window for the next part, gets
  its own reaction, and the turn goes on.
- A settings change in plain words reaches `shared/settings`: a cue word ("ovozingni …") or "sozlama: …" by
  pre-parse; a cue inside work for a named project is work.
- An investigation routed to a project says "Tekshiryapman" at once; "xarajat?" answers the cost report; "X yangi
  sessiya" starts the next turn in a fresh session.
- "yozmoqda…" shows for quick answers until they are sent, never for long work (unless `ack.typing_for_work`).
- A message about none of his projects ("Python'da decorator nima?") gets a plain answer from `dev/chat`: no turn, no
  report, no "Tekshiryapman"; a named project still decides.

Same harness as `test_dev_bot.py`: every loop of the real app on `FakeClock` time, the real Telegram adapter on the
recording session.
"""

import pytest

from tests.factories import make_default_snapshot, make_owner_origin
from tests.fakes.agent import Pause, ToolStep, done_result
from tests.integration.dev_bot_harness import DevBot, dev_bot, register_projects, visible
from yordamchi.core import strings as S
from yordamchi.core.errors import CallTimedOut
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.domain import TurnStatus
from yordamchi.persistence.uow import Session
from yordamchi.shared.outbox import service as outbox_service
from yordamchi.shared.outbox.domain import plain_text
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

PROJECT = "BonviHR"
VOICE_PROJECT = "OvozYoz"
FINAL = (
    "Natija: login tuzatildi.\n<handoff>\nGoal: fix login\nDecisions: none\nState: pushed\nNext: nothing\n</handoff>"
)
SCREENSHOT = b"\x89PNG fake screenshot"
DEFAULTS = make_default_snapshot()
HOLD = DEFAULTS[defaults.UI_HOLD_REACTION]
READ_MARK = DEFAULTS[defaults.UI_READ_MARK]


def route_reply(intent: str, project: str | None, confidence: float = 0.95) -> dict[str, JsonValue]:
    return {
        "intent": intent,
        "project_id": project,
        "candidates": [],
        "confidence": confidence,
        "needs_fresh": False,
        "is_new_project": False,
    }


def change(key: str, value: JsonValue) -> dict[str, JsonValue]:
    """The settings assistant's structured answer for one change."""
    return {"outcome": "change", "changes": [{"key": key, "value": value}], "keys": []}


async def turn_status(bot: DevBot, project: str) -> str | None:
    async with bot.deps.db.read() as s:
        rows = await turns_store.list_project_turns(s, project, limit=1)
    return rows[0].status if rows else None


async def running(bot: DevBot, project: str) -> bool:
    return await turn_status(bot, project) == TurnStatus.RUNNING.value


def reactions_on(bot: DevBot, update_id: int) -> list[str]:
    message_id = str(5000 + update_id)  # the harness's message ids
    return [
        str(r.params.get("reaction"))
        for r in bot.requests("setMessageReaction")
        if str(r.params.get("message_id")) == message_id
    ]


def typing_requests(bot: DevBot) -> list[int]:
    """Where each "typing…" request falls among the bot's requests (its index)."""
    return [index for index, r in enumerate(bot.requests()) if r.api_method == "sendChatAction"]


async def started(bot: DevBot) -> None:
    """The startup messages are out (the restart notice, the pinned "Onlayn"), so what follows is the owner's."""
    await bot.within(10, lambda: bool(bot.requests("pinChatMessage")), what="'Onlayn' posted and pinned")
    await bot.quiet()


def first_index(bot: DevBot, needle: str) -> int:
    return next(
        index
        for index, r in enumerate(bot.requests())
        if r.api_method in {"sendMessage", "sendDocument"} and needle in str(r.params.get("text", ""))
    )


# --- "kut" ----------------------------------------------------------------------------------------------------------
async def test_kut_while_a_turn_runs_holds_for_the_next_part_and_stops_nothing() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        bot.fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), Pause(), done_result(FINAL))
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(20, lambda: running(bot, PROJECT), what="a turn runs")

        kut = await bot.say("kut")
        await bot.within(3, lambda: any(HOLD in r for r in reactions_on(bot, kut)), what="the hold reaction")
        await bot.advance(40)  # he picks a screenshot; meanwhile nothing is stopped
        assert not bot.posted(S.STOPPED(project=PROJECT))
        assert await running(bot, PROJECT)

        await bot.photo(SCREENSHOT)
        await bot.say("BonviHR: shu xato ham")
        await bot.within(20, lambda: bool(bot.posted("ishlayotgan topshiriqqa qo'shdim")), what="merged")
        assert [mode for _, _, mode in bot.fakes.agents.sends] == ["new_turn", "next"]
        steered = bot.fakes.agents.sends[-1][1]
        assert len(steered.images) == 1  # the screenshot sent after "kut" reached the working turn
        assert "kut" not in steered.text
        assert not bot.posted(S.STOPPED(project=PROJECT))


# --- settings in plain words (UC-09) --------------------------------------------------------------------------------
async def test_the_owner_changes_settings_in_plain_words() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, VOICE_PROJECT)

        # a cue word: pre-parse hands it to settings, with no routing ask
        bot.fakes.assistant.reply_with("settings", change("voice.gender", "male"))
        await bot.say("ovozingni erkakka o'zgartir")
        await bot.within(10, lambda: bool(bot.posted("Sozlama o'zgardi")), what="changed by a cue word")
        assert "voice.gender" in bot.posted("Sozlama o'zgardi")[0]
        assert [request.kind for request in bot.fakes.assistant.requests] == ["settings"]
        await bot.quiet()

        # said outright with "sozlama:"; the request alone goes to settings
        bot.fakes.assistant.reply_with("settings", change("burst.after_media_s", 15))
        await bot.say("/sozlamalar rasmdan keyin 15 soniya kut")
        await bot.within(10, lambda: len(bot.posted("Sozlama o'zgardi")) == 2, what="changed by the alias")
        assert bot.fakes.assistant.requests[-1].kind == "settings"
        assert "burst.after_media_s: 10 s → 15 s" in bot.posted("Sozlama o'zgardi")[1]
        snap = await settings.snapshot(bot.deps)
        assert snap[defaults.BURST_AFTER_MEDIA_S] == 15
        assert await turn_status(bot, PROJECT) is None
        await bot.quiet()

        # a cue word inside work for a named project is work for it
        bot.fakes.agents.script_turn(done_result("Natija: tayyor."))
        await bot.say("OvozYoz'da javoblaringni qisqaroq qiladigan rejim qo'sh")
        await bot.within(20, lambda: bool(bot.posted("→ OvozYoz")), what="routed to the named project")
        assert len(bot.posted("Sozlama o'zgardi")) == 2


# --- "Tekshiryapman", "xarajat?", "X yangi sessiya" ----------------------------------------------------------------
async def test_an_investigation_says_tekshiryapman_within_two_seconds_of_routing() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        bot.fakes.assistant.reply_with("routing", route_reply("investigate", PROJECT))
        bot.fakes.agents.script_turn(Pause())
        await bot.say("BonviHR'da login nega ishlamayapti?")
        quiet = DEFAULTS[defaults.BURST_AFTER_COMPLETE_TEXT_S]  # the burst's own window after a complete question
        took = await bot.within(5, lambda: bool(bot.posted("Tekshiryapman")), what="Tekshiryapman")
        assert took <= quiet + 2.0
        assert bot.posted("Tekshiryapman") == ["BonviHR · Tekshiryapman (~3 daqiqa)"]
        assert not bot.posted("→ BonviHR")  # the ack names the project; a bare "→ BonviHR" would repeat it
        await bot.within(10, lambda: bool(bot.fakes.agents.sends), what="the investigation turn started")


async def test_xarajat_answers_the_cost_report_within_three_seconds() -> None:
    async with dev_bot() as bot:
        xarajat = await bot.say("xarajat?")
        quiet = DEFAULTS[defaults.BURST_AFTER_TEXT_S]
        took = await bot.within(6, lambda: bool(bot.posted(S.COST_HEAD())), what="the cost report")
        assert took <= quiet + 3.0
        assert not bot.posted(S.NOT_YET(phase="P2"))
        head = plain_text(S.COST_HEAD())  # as the owner reads it
        [report] = [r for r in bot.requests("sendMessage") if head in visible(r)]
        assert str(5000 + xarajat) in str(report.params.get("reply_parameters"))  # an answer to the question


async def test_yangi_sessiya_starts_the_next_turn_fresh_with_the_handoff_note() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        bot.fakes.agents.script_turn(done_result(FINAL))
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(30, lambda: bool(bot.posted("BonviHR · Natija:")), what="the first result")
        await bot.quiet()

        await bot.say("BonviHR yangi sessiya")
        await bot.within(5, lambda: bool(bot.posted(S.NEW_SESSION_READY(project=PROJECT))), what="the one line")
        bot.fakes.agents.script_turn(Pause())
        await bot.say("BonviHR'da testlarni yoz")
        await bot.within(20, lambda: len(bot.fakes.agents.sends) == 2, what="the next turn")
        assert [resume for _, resume in bot.fakes.agents.opened] == [None, None]  # fresh, not resumed
        seeded = bot.fakes.agents.sends[-1][1].text
        assert seeded.startswith("[Yordamchi] This is a new session")
        assert "Goal: fix login" in seeded


# --- "yozmoqda…" (the owner's rule, 2026-10-24) ---------------------------------------------------------------------
async def test_holat_and_a_report_show_typing_until_the_answer_and_never_stop_it_early(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stops: list[int] = []
    real_stop = outbox_service.stop_typing

    async def counted_stop(s: Session, *, bot: BotName, chat_id: int) -> int:
        stops.append(chat_id)
        return await real_stop(s, bot=bot, chat_id=chat_id)

    monkeypatch.setattr(outbox_service, "stop_typing", counted_stop)
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        await bot.say("holat?")
        await bot.within(5, lambda: bool(bot.posted("Holat · dev bot")), what="holat")
        assert typing_requests(bot), "typing shows from the moment the message is accepted"
        assert min(typing_requests(bot)) < first_index(bot, "Holat · dev bot")
        await bot.quiet()

        before = len(bot.requests())
        bot.fakes.assistant.reply_with("routing", route_reply("report_project", PROJECT))
        await bot.say("BonviHR qanday?")
        await bot.within(6, lambda: bool(bot.posted("BonviHR · hozir")), what="the report")
        assert [index for index in typing_requests(bot) if index >= before]
        assert stops == []  # neither path stops it: the answer itself ends the typing when it is sent


async def test_a_settings_change_keeps_typing_up_while_the_settings_assistant_thinks() -> None:
    async with dev_bot() as bot:
        await started(bot)
        bot.fakes.assistant.reply_with("settings", change("outbox.progress_edit_s", 10))
        await bot.say("sozlama: progress xabari har 10 soniyada yangilansin")
        await bot.within(10, lambda: bool(bot.posted("Sozlama o'zgardi")), what="the change")
        assert min(typing_requests(bot)) < first_index(bot, "Sozlama o'zgardi")


async def test_a_coding_instruction_is_acked_by_its_route_line_then_the_progress_line_and_no_repeated_typing() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        first, later = (ToolStep("Read", {"file_path": f"src/{n}.py"}, tool_use_id=f"t{n}") for n in range(2))
        bot.fakes.agents.script_turn(first, Pause(), later, Pause())
        instruction = await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(20, lambda: bool(bot.posted("BonviHR · ishlayapti")), what="the progress line")
        await bot.advance(2)
        await bot.fakes.agents.release(bot.fakes.agents.sends[-1][0])  # the work goes on: no typing comes back
        await bot.advance(30)
        # owner, 2026-10-24: 👀 on every message looked odd. The read mark sets one and removes it at once (✓✓)
        assert reactions_on(bot, instruction) == [f"[{{'type': 'emoji', 'emoji': '{READ_MARK}'}}]", "[]"]
        assert bot.posted("→ BonviHR")
        assert len(typing_requests(bot)) <= 1  # at most the one hint at intake, never repeated
        assert all(index < first_index(bot, "→ BonviHR") for index in typing_requests(bot))


async def test_the_seen_reaction_comes_back_when_the_owner_sets_one() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        owner = make_owner_origin(update_id=90)
        await settings.record_version(bot.deps, owner, defaults.UI_SEEN_REACTION, "\U0001f440", reason="change")
        bot.fakes.agents.script_turn(Pause())
        instruction = await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(20, lambda: bool(reactions_on(bot, instruction)), what="the seen reaction")
        [reaction] = reactions_on(bot, instruction)
        assert "\U0001f440" in reaction


# --- small talk (owner, 2026-10-24: "Qandeysan?" got a report) -----------------------------------------------------
async def test_how_are_you_gets_an_answer_not_a_report_and_asks_no_model() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        await bot.say("Qandeysan?")
        took = await bot.within(5, lambda: bool(bot.posted(S.CHAT_HOW())), what="the answer")
        assert took <= DEFAULTS[defaults.BURST_AFTER_TEXT_S] + 1.0
        assert bot.fakes.assistant.requests == []  # neither routing nor a report was asked
        assert not bot.posted("Bugun o'zgargan")


async def test_a_greeting_adds_no_project_status_even_while_work_runs() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        bot.fakes.agents.script_turn(Pause())
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(20, lambda: bool(bot.fakes.agents.sends), what="the turn")
        await bot.say("Hi")
        await bot.within(5, lambda: bool(bot.posted(S.CHAT_HELLO())), what="the greeting")
        [reply] = bot.posted(S.CHAT_HELLO())
        assert PROJECT not in reply  # owner: no project status in small talk
        assert "Ma'lumot" not in reply
        assert await running(bot, PROJECT)  # a greeting is never an instruction for the running turn


async def test_a_bare_okay_with_nothing_open_gets_no_reply_and_no_question() -> None:
    """Owner, 2026-10-24: "Hop" got "«Hop» — qaysi loyiha uchun?". With nothing open it needs no answer."""
    async with dev_bot() as bot:
        await started(bot)
        before = len(bot.requests("sendMessage"))
        await bot.say("Hop")
        await bot.advance(10)
        await bot.quiet()
        assert len(bot.requests("sendMessage")) == before
        assert bot.fakes.assistant.requests == []  # routing was never asked


async def test_what_can_you_do_lists_the_bot_s_abilities() -> None:
    async with dev_bot() as bot:
        await started(bot)
        await bot.say("Nimalar qilolasan?")
        await bot.within(5, lambda: bool(bot.posted("Loyihalarda ishlash")), what="the help text")
        await bot.say("rahmat")
        await bot.within(5, lambda: bool(bot.posted(S.CHAT_THANKS())), what="you're welcome")
        assert bot.fakes.assistant.requests == []


async def test_typing_for_work_switched_on_shows_typing_while_the_turn_works() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        bot.fakes.assistant.reply_with("settings", change("ack.typing_for_work", True))
        await bot.say("sozlama: ish paytida ham yozmoqda ko'rsat")
        await bot.within(10, lambda: bool(bot.posted("Sozlama o'zgardi")), what="switched on")
        assert (await settings.snapshot(bot.deps))[defaults.ACK_TYPING_FOR_WORK] is True
        await bot.quiet()

        first, later = (ToolStep("Read", {"file_path": f"src/{n}.py"}, tool_use_id=f"t{n}") for n in range(2))
        bot.fakes.agents.script_turn(first, Pause(), later, Pause())
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(20, lambda: bool(bot.posted("BonviHR · ishlayapti")), what="the progress line")
        await bot.advance(2)  # "→ BonviHR" and the progress line are out
        progress = len(bot.requests())
        await bot.fakes.agents.release(bot.fakes.agents.sends[-1][0])  # the work goes on: one more step
        await bot.advance(10)
        assert [index for index in typing_requests(bot) if index >= progress], "typing renewed while it works"


# --- chat: not only about projects (owner, 2026-10-24) ---------------------------------------------------------------
# "nimadir so'rasam faqat projectni ichidan qidirishga harakat qilyapti": a general question became "X · Tekshiryapman".
CHAT_ANSWER = "**Decorator** — boshqa funksiyani o'rab, unga qo'shimcha ish qo'shadigan funksiya. Masalan, `@cache`."


async def test_a_general_question_gets_a_plain_answer_with_no_turn_and_no_report() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        bot.fakes.agents.script_turn(Pause())
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(20, lambda: bool(bot.fakes.agents.sends), what="the BonviHR turn")
        await bot.quiet()
        before = len(bot.requests())

        bot.fakes.assistant.reply_with("routing", route_reply("chat", None, 0.95))
        bot.fakes.assistant.reply_with("chat", CHAT_ANSWER)
        question = await bot.say("Python'da decorator nima?")
        took = await bot.within(10, lambda: bool(bot.posted("Decorator")), what="the chat answer")

        assert took <= DEFAULTS[defaults.BURST_AFTER_COMPLETE_TEXT_S] + 3.0
        [answer] = [r for r in bot.requests("sendMessage") if "Decorator" in visible(r)]
        assert str(5000 + question) in str(answer.params.get("reply_parameters"))  # an answer to his question
        assert not bot.posted("Tekshiryapman")
        assert not bot.posted("Ma'lumot")
        assert not bot.posted("→ BonviHR · ")
        assert PROJECT not in visible(answer)
        kinds = [request.kind for request in bot.fakes.assistant.requests]
        assert kinds[-2:] == ["routing", "chat"]
        assert "report" not in kinds
        assert len(bot.fakes.agents.sends) == 1  # no new turn, and nothing steered into the running one
        assert await running(bot, PROJECT)
        assert [index for index in typing_requests(bot) if before <= index < first_index(bot, "Decorator")]


async def test_a_chat_answer_that_times_out_gets_one_short_line() -> None:
    async with dev_bot() as bot:
        await started(bot)
        bot.fakes.assistant.reply_with("routing", route_reply("chat", None, 0.9))
        bot.fakes.assistant.reply_with("chat", CallTimedOut(operation="assistant_chat"))
        await bot.say("dollar kursi qancha bo'lishi mumkin?")
        await bot.within(10, lambda: bool(bot.posted(S.CHAT_FAILED())), what="the short line")
        await bot.advance(5)
        assert len(bot.posted(S.CHAT_FAILED())) == 1
        assert not bot.posted("Bugun o'zgargan")  # never a project report instead
        assert not bot.posted("Ma'lumot")
        assert not bot.posted("Ichki xato")  # handled, never the guarded() incident line
        assert not bot.posted("vaqtida tugamadi")  # nor the generic timeout error
        assert bot.fakes.agents.sends == []


async def test_a_question_that_names_a_project_still_goes_to_the_project() -> None:
    """Even when the routing model says `chat`, a named project decides: the question is about BonviHR."""
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        await started(bot)
        bot.fakes.assistant.reply_with("routing", route_reply("chat", None, 0.9))
        bot.fakes.assistant.reply_with("report", "Decorator'lar `auth.py` da kirishni tekshirish uchun ishlatilgan.")
        await bot.say("BonviHR'da decorator qanday ishlatilgan?")
        await bot.within(10, lambda: bool(bot.posted("BonviHR")), what="the BonviHR answer")
        kinds = [request.kind for request in bot.fakes.assistant.requests]
        assert "chat" not in kinds
        assert kinds[0] == "routing"
        assert any(text.startswith(PROJECT) for text in bot.texts())
