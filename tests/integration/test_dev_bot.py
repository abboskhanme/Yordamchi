"""T34 acceptance: the dev bot end to end on fakes (SPEC §2.5–§2.8, §3.1.1, §3.2, §6.1; UC-01, UC-02, UC-03, UC-05,
UC-06, UC-09, UC-11, UC-12, UC-13, UC-15; NFR-REL-05/07, NFR-SPD-01, NFR-SEC-11, D8).

`dev_bot_harness` runs every loop of the real app on `FakeClock` time, with the real Telegram adapter on the recording
aiogram session (a request carrying `reply_markup` raises there). Latencies are virtual seconds from the owner's
update to the Bot API request that answers it.
"""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Final

from tests.factories import (
    DEV_GROUP_ID,
    FAKE_KEYS,
    FOREIGN_GROUP_ID,
    STRANGER_ID,
    Fakes,
    agent_question,
    make_transcript,
    open_test_db,
    store_secret,
)
from tests.fakes.agent import Pause, ToolStep
from tests.integration.dev_bot_harness import DevBot, dev_bot, register_projects, short_paths, until, visible
from yordamchi.core import control
from yordamchi.core import strings as S
from yordamchi.core.config import SESSION_TOKEN_VAR
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.dev.turns import store as turns_store
from yordamchi.dev.turns.domain import TurnStatus
from yordamchi.ports.agent import QuestionAsked, TurnResult
from yordamchi.ports.git import Commit, FileChange, GitStatus
from yordamchi.shared.inbox import store as inbox_store

PROJECT = "BonviHR"
OTHER = "HRMentor"
FINAL = (
    "Tayyor.\nNatija: Login sahifasidagi xato tuzatildi.\nTestlar: o'tdi (42 passed)\n"
    "<handoff>\nGoal: fix login\nDecisions: none\nState: pushed\nNext: nothing\n</handoff>"
)
SCREENSHOT = b"\x89PNG fake screenshot"
VOICE = b"OggS fake voice"

# The live line of a working turn as the owner sees it (`ui.progress_steps` 0 since 2026-10-25).
LIVE_LINE: Final = " · ⏱ "


def result(text: str = FINAL) -> TurnResult:
    return TurnResult(
        text=text,
        subtype="success",
        is_error=False,
        stop_reason="end_turn",
        cost_usd_micros=1000,
        model_usage={},
        num_turns=2,
        duration_ms=9000,
    )


def route_reply(intent: str, project: str | None, confidence: float = 0.95) -> dict[str, JsonValue]:
    """The routing assistant's structured answer (`routing.domain.OUTPUT_SCHEMA`)."""
    return {
        "intent": intent,
        "project_id": project,
        "candidates": [],
        "confidence": confidence,
        "needs_fresh": False,
        "is_new_project": False,
    }


def session_of(bot: DevBot, project: str) -> str:
    """The fake executor session the project's latest delivery went to."""
    sessions = bot.fakes.agents._sessions
    return next(sid for sid, _, _ in reversed(bot.fakes.agents.sends) if sessions[sid].spec.cwd.name == project)


async def turn_status(bot: DevBot, project: str) -> str | None:
    async with bot.deps.db.read() as s:
        rows = await turns_store.list_project_turns(s, project, limit=1)
    return rows[0].status if rows else None


async def _status_is(bot: DevBot, project: str, status: TurnStatus) -> bool:
    return await turn_status(bot, project) == status.value


def git_pushed(bot: DevBot, folder: Path, sha: str, subject: str, *files: str) -> None:
    """What git shows after the agent committed and pushed: the result line reads it (SPEC §6.2)."""
    bot.fakes.git.statuses[folder] = GitStatus("main", sha, "origin/main", 0, 0, 0, 0)
    bot.fakes.git.logs[folder] = [Commit(sha, subject, bot.clock.now())]
    bot.fakes.git.changes[folder] = [FileChange("M", path) for path in files]


async def test_the_scripted_conversation_runs_end_to_end_on_fakes() -> None:  # noqa: PLR0915  # one conversation
    async with dev_bot() as bot:
        folders = await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT, OTHER)
        await bot.within(10, lambda: bool(bot.requests("pinChatMessage")), what="'Onlayn' posted and pinned")
        await bot.advance(1)
        before = len(bot.requests())

        # --- a stranger gets nothing: no request, no row (UC-01) --------------------------------------------------
        await bot.say("salom, kodni ko'rsat", user_id=STRANGER_ID, chat_id=STRANGER_ID)
        await bot.say("BonviHR'ni o'chir", user_id=STRANGER_ID)
        await bot.advance(2)
        assert len(bot.requests()) == before
        async with bot.deps.db.read() as s:
            assert await inbox_store.list_inbound_updates(s, "dev") == []
        await bot.say("salom", chat_id=FOREIGN_GROUP_ID)  # the owner in a foreign group: the bot leaves it
        assert [r.api_method for r in bot.requests()[before:]] == ["leaveChat"]

        # --- screenshot + text + voice as one burst → routed with the project named → the turn runs ----------------
        bot.fakes.transcriber.script(make_transcript("tugmasi bosilmayapti, shuni ham tekshir"))
        bot.fakes.agents.script_turn(
            ToolStep("Read", {"file_path": "src/app/login.py"}, tool_use_id="t0"),
            Pause(),
            ToolStep("Bash", {"command": "git add -A && git commit -m 'fix login'"}, tool_use_id="t1"),
            ToolStep("Bash", {"command": "git push origin main"}, tool_use_id="t2"),
            result(),
        )
        await bot.photo(SCREENSHOT)
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.voice(VOICE)
        await bot.within(30, lambda: bool(bot.posted("→ BonviHR")), what="routed with the project named")
        echo = bot.posted("→ BonviHR")[0]
        assert echo.startswith("Ovoz: «tugmasi bosilmayapti")  # the voice echo names the project (UC-05)
        await bot.within(10, lambda: bool(bot.posted(LIVE_LINE)), what="the progress line")
        sent = bot.fakes.agents.sends[0][1]
        assert "BonviHR'da login sahifasini tuzat" in sent.text
        assert "tugmasi bosilmayapti" in sent.text
        assert len(sent.images) == 1  # the screenshot reached the agent as an image
        assert sent.images[0].read_bytes() == SCREENSHOT

        # --- a follow-up is merged into the working turn (UC-02) ---------------------------------------------------
        await bot.say("BonviHR: testlarini ham yoz.")
        await bot.within(15, lambda: bool(bot.posted("ishlayotgan topshiriqqa qo'shdim")), what="merged")
        await until(lambda: [mode for _, _, mode in bot.fakes.agents.sends] == ["new_turn", "next"], what="steered")

        await bot.quiet()

        # --- "X qanday?" is answered from the digest within 4 s while X codes (UC-15) -----------------------------
        bot.fakes.assistant.reply_with("routing", route_reply("report_project", PROJECT))
        await bot.say("BonviHR qanday?")
        took = await bot.within(5, lambda: bool(bot.posted("BonviHR · hozir:")), what="the report from the digest")
        assert took <= 4 + 1.0  # 1 s of it is the burst's own quiet window after a complete question
        report = bot.posted("BonviHR · hozir:")[0]
        assert "ishlayapti" in report.splitlines()[0]  # "hozir: «<title>», ishlayapti, N daq"
        assert await turn_status(bot, PROJECT) == TurnStatus.RUNNING.value  # the report never touched the turn

        # --- the result: Natija, commit and push, one progress line, no question asked (UC-11, NFR-SEC-11) --------
        git_pushed(bot, folders[PROJECT], "a1b2c3d4e5", "fix login", "src/app/login.py", "tests/test_login.py")
        await bot.fakes.agents.release(session_of(bot, PROJECT))
        await bot.within(10, lambda: bool(bot.posted("BonviHR · Natija:")), what="the result")
        [final] = bot.posted("BonviHR · Natija:")
        assert final.splitlines()[0] == "BonviHR · Natija: Login sahifasidagi xato tuzatildi."
        assert "Commit: a1b2c3d · push: bajarildi (origin/main)" in final.splitlines()
        progress = [r for r in bot.requests("sendMessage") if LIVE_LINE in visible(r)]
        assert len(progress) == 1  # one progress message, edited in place
        assert not bot.posted("Muhim qaror")
        assert not bot.posted("Savol:")

        await bot.quiet()

        # --- an agent question → the owner's "2" → delivered into the held callback (UC-12) -----------------------
        question = agent_question("Qaysi bazadan foydalanamiz?", [("SQLite", "oddiy"), ("Postgres", "server")])
        bot.fakes.agents.script_turn(QuestionAsked("q1", (question,)), Pause(), result("Natija: Postgres ulandi."))
        await bot.say("HRMentor'da hisobotlar sahifasini qo'sh")
        await bot.within(20, lambda: bool(bot.posted("HRMentor · Savol:")), what="the agent question")
        await bot.say("2")
        await bot.within(10, lambda: bool(bot.fakes.agents.answers), what="the answer delivered")
        assert bot.fakes.agents.answers[0][2] == {"Qaysi bazadan foydalanamiz?": "Postgres"}
        await bot.within(5, lambda: bool(bot.posted("→ HRMentor: javob «Postgres»")), what="the answer confirmed")
        await bot.fakes.agents.release(session_of(bot, OTHER))
        await bot.within(10, lambda: bool(bot.posted("HRMentor · Natija:")), what="the second result")

        await bot.quiet()

        # --- a deploy: exactly one question → "ha" → it runs once (UC-13) ----------------------------------------
        deploy = {"command": "vercel deploy --prod"}
        bot.fakes.agents.script_turn(
            ToolStep("Bash", deploy, tool_use_id="d1"), result("Natija: deploy qarorini kutyapman.")
        )
        bot.fakes.agents.script_turn(ToolStep("Bash", deploy, tool_use_id="d2"), result("Natija: deploy qilindi."))
        await bot.say("BonviHR'ni prod'ga deploy qil")
        await bot.within(20, lambda: bool(bot.posted("Muhim qaror")), what="the decision question")
        await bot.within(10, lambda: _status_is(bot, PROJECT, TurnStatus.WAITING_DECISION), what="the turn waits")
        await bot.say("ha")
        await bot.within(20, lambda: bool(bot.posted("BonviHR · Natija: deploy qilindi.")), what="deployed")
        assert len(bot.posted("Muhim qaror")) == 1
        deploys = [verdict.allow for call, verdict in bot.fakes.agents.guard_calls if call.tool_input == deploy]
        assert deploys == [False, True]  # asked once, then run exactly once on the grant

        await bot.quiet()

        # --- "to'xta" stops the working turn within 3 s (UC-03) ---------------------------------------------------
        bot.fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), Pause(), result())
        await bot.say("BonviHR'da README'ni yangila")
        await bot.within(20, lambda: _status_is(bot, PROJECT, TurnStatus.RUNNING), what="a new turn runs")
        await bot.say("to'xta")
        took = await bot.within(3, lambda: bool(bot.posted(S.STOPPED(project=PROJECT))), what="stopped")
        assert took <= 3

        await bot.quiet()

        # --- a settings change by chat (UC-09) -------------------------------------------------------------------
        change: dict[str, JsonValue] = {
            "outcome": "change",
            "changes": [{"key": "outbox.progress_edit_s", "value": 10}],
            "keys": [],
        }
        bot.fakes.assistant.reply_with("settings", change)
        await bot.say("sozlama: progress xabari har 10 soniyada yangilansin")
        await bot.within(10, lambda: bool(bot.posted("Sozlama o'zgardi")), what="the setting changed")
        assert "outbox.progress_edit_s: 5 s → 10 s" in bot.posted("Sozlama o'zgardi")[0]

        await bot.quiet()

        # --- "holat?" within 3 s (UC-06) -------------------------------------------------------------------------
        await bot.say("holat?")
        took = await bot.within(3, lambda: bool(bot.posted("Holat · dev bot")), what="holat")
        assert took <= 3

        # --- "xarajat?" answers the cost report from local data (UC-07, T51) -----------------------------------
        await bot.say("xarajat?")
        await bot.within(5, lambda: bool(bot.posted(S.COST_HEAD())), what="the cost report")

        # --- later-phase intents answer "not yet", never become work ----------------------------------------------
        await bot.say("integratsiyalar")
        await bot.within(5, lambda: bool(bot.posted(S.NOT_YET(phase="P2"))), what="not yet")

        # --- no button, keyboard or menu, ever (D8); every message went to the dev group ----------------------
        bot.session.assert_no_reply_markup()
        assert not bot.requests("setMyCommands")
        assert {r.params.get("chat_id") for r in bot.requests("sendMessage")} == {DEV_GROUP_ID}


async def test_a_restart_lists_the_cut_off_turn_and_davom_ettir_continues_it() -> None:
    paths, root = short_paths()
    fakes = Fakes()
    db = await open_test_db(paths.db_file(BotName.DEV))
    try:
        await store_secret(db, fakes.keystore, "google_api_key", FAKE_KEYS["google_api_key"])
        async with dev_bot(fakes=fakes, paths=paths, db=db, crashes=True) as first:
            await register_projects(first.deps, fakes, paths, PROJECT)
            fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), Pause(), result())
            await first.say("BonviHR'da login sahifasini tuzat")
            await first.within(20, lambda: _status_is(first, PROJECT, TurnStatus.RUNNING), what="running")
            await until(lambda: fakes.agents.guard_calls, what="the instruction reached the agent")
            await first.advance(1)  # its delivery is recorded as delivered: the turn is mid-work
        # kill -9 happened: no shutdown pass, no clean marker. The next start (UC-06: the notice ≤ 60 s):
        async with dev_bot(fakes=fakes, paths=paths, db=db) as second:
            took = await second.within(60, lambda: bool(second.posted("Qayta ishga tushdim")), what="the notice")
            assert took <= 60
            notice = second.posted("Qayta ishga tushdim")[0]
            assert "Uzilib qolgan ishlar: BonviHR — davom ettiraymi?" in notice
            assert await turn_status(second, PROJECT) == TurnStatus.INTERRUPTED.value  # never re-run (NFR-REL-07)
            fakes.agents.script_turn(result())
            second.next_update = first.next_update  # Telegram's update ids go on across the restart
            await second.say("davom ettir")
            await second.within(10, lambda: bool(second.posted(S.TURN_CONTINUED(project=PROJECT))), what="continued")
            await second.within(20, lambda: _status_is(second, PROJECT, TurnStatus.DONE), what="done")
    finally:
        await db.dispose()
        shutil.rmtree(root, ignore_errors=True)


async def test_the_control_socket_answers_a_shim_and_the_pre_push_hook() -> None:
    async with dev_bot() as bot:
        await register_projects(bot.deps, bot.fakes, bot.paths, PROJECT)
        bot.fakes.agents.script_turn(ToolStep("Read", {"file_path": "README.md"}), Pause(), result())
        await bot.say("BonviHR'da login sahifasini tuzat")
        await bot.within(20, lambda: _status_is(bot, PROJECT, TurnStatus.RUNNING), what="running")
        socket = bot.paths.socket(bot.deps.bot)
        assert socket.stat().st_mode & 0o777 == 0o600
        spec = bot.fakes.agents.opened[-1][0]
        token = spec.env[SESSION_TOKEN_VAR]
        cwd = str(spec.cwd)

        def ask(line: bytes) -> bytes | None:
            return control.request(socket, line, timeout=5.0)

        readonly = control.gate_check_request(control.GateCheck("ssh", ("web", "uptime"), cwd, token))
        answer = control.read_gate_answer(await asyncio.to_thread(ask, readonly))
        assert answer.allow, answer.reason
        writing = control.gate_check_request(control.GateCheck("ssh", ("web", "rm", "-rf", "/srv/app"), cwd, token))
        assert not control.read_gate_answer(await asyncio.to_thread(ask, writing)).allow
        stranger = control.gate_check_request(control.GateCheck("ssh", ("web", "uptime"), cwd, "not-a-session"))
        assert not control.read_gate_answer(await asyncio.to_thread(ask, stranger)).allow

        blocked = control.secret_blocked_request(control.SecretBlocked(cwd, "config.py", 3, "github_token", token))
        secret = control.read_secret_answer(await asyncio.to_thread(ask, blocked))
        assert secret is not None
        assert secret.enforce
        await bot.within(10, lambda: bool(bot.posted("config.py")), what="the owner told about the secret")

        raw = await asyncio.to_thread(ask, b'{"method":"status"}\n')
        status = json.loads(raw or b"{}")
        assert status == {"version": "2026.10.24-test", "mode": "normal", "live_turns": 1, "drained": False}
        malformed = control.read_gate_answer(await asyncio.to_thread(ask, b"not json\n"))
        assert (malformed.allow, malformed.reason) == (False, "bad_request")  # fail closed
        unknown = await asyncio.to_thread(ask, b'{"method":"selftest","nonce":"1"}\n')
        assert json.loads(unknown or b"{}") == {"error_code": "unknown_method"}  # P2's selftest is not served yet
