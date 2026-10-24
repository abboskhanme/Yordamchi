"""T36 network faults (NFR-REL-03, NFR-REL-06, NFR-CAP-02; T32b; SPEC §2.8, §4.6).

The bot runs against the fake Telegram of `tests/fault/harness.py`, which knows what it really posted whatever the
bot heard back:
- outbound faults: 5xx from Telegram or its proxy, timeouts, a connection that drops after Telegram posted, and 429
  with `retry_after` → no message lost, duplicates ≤ 2 %, `retry_after` and the per-chat/per-group limits honoured;
- a 10-minute outage (no connection at all): the heartbeat keeps showing a live bot that is offline, not a dead one
  (the watchdog must not restart it); polling resumes ≤ 60 s after the network is back; what the owner wrote and what
  finished meanwhile flows; "Internet uzilgan edi HH:MM–HH:MM" is told once.

Two bugs were found here. BUG-1 (a held backlog joined into one burst) is fixed and its test passes; BUG-2 is
kept as a strict xfail, so it turns into a failure the day it is fixed.
"""

import json
import random
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Final

import pytest
from aiogram.methods import TelegramMethod

from tests.fakes.agent import Pause, ScriptItem, ToolStep, done_result
from tests.fault.harness import (
    MESSAGE_METHODS,
    Fault,
    World,
    delivered_to,
    delivery_counts,
    drain,
    outbox_ledger,
    record,
    release_paused,
    tokens_in,
    world,
)
from yordamchi.core import strings as S
from yordamchi.core import timefmt
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.ports.agent import AgentInput

PROJECTS: Final = ("BonviHR", "HRMentor", "Markaz", "OvozYoz")
SEED: Final = 36_03
INSTRUCTIONS: Final = 36
POSTING_FAULTS_AT: Final = frozenset({25, 70})
TIMEOUT_EVERY: Final = 6  # the message sends whose answer is lost after Telegram posted them
MAX_DUPLICATE_RATE: Final = 0.02  # NFR-REL-06
OUTAGE: Final = timedelta(minutes=10)  # NFR-REL-03
RESUME_WITHIN_S: Final = 60.0
WATCHDOG_STALE_S: Final = 120.0  # SPEC §2.8: no poll attempt for this long → the watchdog restarts the bot
TIMEZONE: Final = "Asia/Tashkent"  # `ui.timezone`


def finishes(message: AgentInput) -> tuple[ScriptItem, ...]:
    """A turn that reads a file and reports the messages it worked on."""
    names = " ".join(tokens_in(message.text)) or "ish"
    return ToolStep("Read", {"file_path": "README.md"}), done_result(f"Natija: {names} bajarildi.")


def keeps_working(message: AgentInput) -> tuple[ScriptItem, ...]:
    """A turn that works until the test releases it."""
    names = " ".join(tokens_in(message.text)) or "ish"
    return ToolStep("Read", {"file_path": "README.md"}), Pause(), done_result(f"Natija: {names} bajarildi.")


class OutboundFaults:
    """Seeded faults on what the bot sends (never on `getUpdates`): a lone 5xx (the next request goes through, as
    after a proxy hiccup), a 429 with `retry_after`, a timeout on every `TIMEOUT_EVERY`-th idempotent call (edits,
    pins, reactions, typing), and on the sends numbered in `posting` an answer lost after Telegram posted (reset or
    timeout)."""

    def __init__(self, rng: random.Random, *, posting: frozenset[int]) -> None:
        self.rng = rng
        self.posting = posting
        self.sends = 0
        self.idempotent = 0
        self.faults: Counter[Fault] = Counter()
        self._last_failed = False

    def __call__(self, method: TelegramMethod[Any]) -> Fault | None:
        api_method = method.__api_method__
        if api_method == "getUpdates":
            return None
        fault: Fault | None = None
        if api_method in MESSAGE_METHODS:
            self.sends += 1
            if self.sends in self.posting:
                fault = self.rng.choice((Fault.RESET, Fault.TIMEOUT_POSTED))
        else:
            self.idempotent += 1
        if fault is None and not self._last_failed:
            roll = self.rng.random()
            if api_method not in MESSAGE_METHODS and self.idempotent % TIMEOUT_EVERY == 0:
                fault = Fault.TIMEOUT
            elif roll < 0.10:
                fault = Fault.SERVER_ERROR
            elif roll < 0.13:
                fault = Fault.FLOOD
        self._last_failed = fault is not None
        if fault is not None:
            self.faults[fault] += 1
        return fault


async def test_outbound_faults_lose_no_message_and_honour_retry_after(request: pytest.FixtureRequest) -> None:
    rng = random.Random(SEED)  # noqa: S311  # a seeded simulation, so a failure replays; not a secret
    async with world(*PROJECTS, script=finishes) as here:
        faults = OutboundFaults(random.Random(SEED + 1), posting=POSTING_FAULTS_AT)  # noqa: S311  # likewise
        here.telegram.plan = faults
        await here.start()
        await here.advance(3.0)
        tokens: dict[str, str] = {}
        for index in range(INSTRUCTIONS):
            project = PROJECTS[index % len(PROJECTS)]
            token = f"#{index:02d}"
            tokens[token] = project
            here.say(f"{project}'da hisobot sahifasini qo'sh {token}")
            await here.advance(rng.uniform(6.0, 14.0))
        await drain(here)

        for fault, count in faults.faults.items():
            record(request, f"faults_{fault.value}", count)
        assert faults.faults[Fault.SERVER_ERROR] >= 10
        assert faults.faults[Fault.FLOOD] >= 3
        assert faults.faults[Fault.TIMEOUT] >= 3
        assert faults.sends >= max(POSTING_FAULTS_AT)  # both posting faults happened
        # Every instruction ran once, in its own project, and its result reached the owner.
        assert delivery_counts(here, tokens) == dict.fromkeys(tokens, 1)
        assert {token: found for token, found in delivered_to(here).items() if token in tokens} == {
            token: {project} for token, project in tokens.items()
        }
        for token, project in tokens.items():
            assert here.telegram.posted(f"{project} · Natija: {token} bajarildi."), token
        # NFR-REL-06: nothing lost; duplicates only where Telegram posted and the answer was lost.
        ledger = await outbox_ledger(here)
        assert (ledger.lost, ledger.pending) == ((), ())
        assert len(ledger.duplicates) <= len(POSTING_FAULTS_AT)
        record(request, "posts", ledger.posted)
        record(request, "duplicates", len(ledger.duplicates))
        assert ledger.duplicate_rate <= MAX_DUPLICATE_RATE, ledger.duplicates
        # NFR-CAP-02: no request to a chat Telegram told to wait, none over 1/s per chat or 20/min per group.
        assert here.telegram.violations == []


# --- a 10-minute outage --------------------------------------------------------------------------------------------
def heartbeat(here: World) -> dict[str, JsonValue]:
    raw = json.loads(here.paths.heartbeat(BotName.DEV).read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return raw


def _at(raw: JsonValue) -> datetime:
    assert isinstance(raw, str)
    return datetime.fromisoformat(raw)


def first_call(here: World, api_method: str, *, after: datetime, failed: bool) -> datetime:
    return next(
        call.at
        for call in here.telegram.calls
        if call.api_method == api_method and call.at >= after and (call.fault is not None) == failed
    )


async def test_a_ten_minute_outage_is_offline_not_dead_and_everything_flows_after_it(  # noqa: PLR0915
    request: pytest.FixtureRequest,
) -> None:
    async with world("BonviHR", "HRMentor", script=keeps_working) as here:
        first = await here.start()
        await here.advance(3.0)
        here.say("BonviHR'da login sahifasini tuzat #01")
        await here.advance(4.0)
        here.say("HRMentor'da hisobot sahifasini qo'sh #02")
        await here.within(30, lambda: sum(delivery_counts(here, ["#01", "#02"]).values()) == 2, what="both work")
        await here.advance(61.0, step=1.0)  # a quiet minute: the group's message budget is free again

        went_down = here.clock.now()
        here.telegram.down = True  # Wi-Fi off
        beats: list[tuple[datetime, dict[str, JsonValue]]] = []
        for minute in range(10):
            if minute == 2:
                await release_paused(here)  # both turns finish while the Mac is offline: their results must wait
            if minute == 4:
                here.say("BonviHR'da testlarini yoz #03")  # Telegram holds these for the bot
            if minute == 5:
                here.say("BonviHR'da README'ni yangila #04")
            for _ in range(4):
                await here.advance(15.0, step=1.0)
                beats.append((here.clock.now(), heartbeat(here)))
        came_back = went_down + OUTAGE
        assert here.clock.now() >= came_back
        here.telegram.down = False

        # --- polling resumes ≤ 60 s after the network is back (NFR-REL-03) -------------------------------------
        def polled() -> bool:
            calls = here.telegram.calls
            return any(c.api_method == "getUpdates" and c.at >= came_back and c.fault is None for c in calls)

        resumed = await here.within(RESUME_WITHIN_S + 30, polled, what="polling resumed", step=0.5)
        record(request, "resumed_after_s", resumed)
        assert resumed <= RESUME_WITHIN_S
        await here.within(RESUME_WITHIN_S, lambda: delivery_counts(here, ["#03"])["#03"] == 1, what="#03 delivered")
        await drain(here)

        # --- offline, not dead (T32b): every heartbeat a fresh failed attempt, kind "network" ------------------
        assert len(beats) == 40
        for at, beat in beats:
            attempt = _at(beat["last_poll_attempt_at"])
            assert (at - attempt).total_seconds() <= WATCHDOG_STALE_S / 2, (at, beat)
            assert beat["last_poll_error_kind"] == "network", (at, beat)
            assert beat["pid"] == first.app.health.pid
            assert beat["mode"] == "normal"
        assert all(int(str(beat["consecutive_poll_failures"])) >= 1 for _, beat in beats)
        assert heartbeat(here)["last_poll_error_kind"] == "none"

        # --- told once, with the outage's times; nothing else reported as a sleep or a restart -----------------
        gone_from = first_call(here, "getUpdates", after=went_down, failed=True)
        back_at = first_call(here, "getUpdates", after=came_back, failed=False)
        told = S.NETWORK_GAP(from_=timefmt.hhmm(gone_from, TIMEZONE), to=timefmt.hhmm(back_at, TIMEZONE))
        assert [post.text for post in here.telegram.posted("Internet uzilgan edi")] == [told]
        assert not here.telegram.posted("Mac uxlagan edi")
        assert len(here.telegram.posted("Qayta ishga tushdim")) == 1  # the first start's only: no restart
        assert len(here.processes) == 1

        # --- what finished offline and what the owner wrote offline both flowed, once each ----------------------
        assert here.telegram.posted("BonviHR · Natija: #01 bajarildi.")
        assert here.telegram.posted("HRMentor · Natija: #02 bajarildi.")
        assert delivery_counts(here, ["#01", "#02", "#03", "#04"]) == {"#01": 1, "#02": 1, "#03": 1, "#04": 1}
        assert delivered_to(here)["#04"] == {"BonviHR"}
        ledger = await outbox_ledger(here)
        assert (ledger.lost, ledger.pending) == ((), ())
        assert ledger.duplicate_rate <= MAX_DUPLICATE_RATE
        assert here.telegram.violations == []


# --- bugs found here (BUG-1 fixed; BUG-2 a strict xfail) -----------------------------------------------------------
async def test_messages_held_while_offline_stay_apart() -> None:
    """T36 BUG-1: what Telegram held arrives in one getUpdates batch; the owner's send times keep it apart."""
    async with world("BonviHR", "HRMentor", script=finishes) as here:
        await here.start()
        await here.advance(3.0)
        here.telegram.down = True
        await here.advance(60.0, step=1.0)
        here.say("BonviHR'da login sahifasini tuzat #01")
        await here.advance(120.0, step=1.0)  # two minutes later, another project
        here.say("HRMentor'da hisobot sahifasini qo'sh #02")
        await here.advance(120.0, step=1.0)
        here.telegram.down = False
        # `drain` only sees what reached the bot: first let polling resume and take the backlog in.
        await here.within(RESUME_WITHIN_S + 30, lambda: not here.telegram.updates, what="the held updates fetched")
        await drain(here)
        assert delivered_to(here) == {"#01": {"BonviHR"}, "#02": {"HRMentor"}}
        assert not here.telegram.posted("gami?")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T36 BUG-2: a routing echo ('→ X') whose send never left the Mac (no connection) is given up as an unknown "
        "delivery: adapters/telegram/translate._network() marks every TelegramNetworkError delivery_unknown, "
        "connect failures included, and shared/outbox/domain.failure_of() treats any retryable error on a send as "
        "unknown, so AT_MOST_ONCE_KINDS (echo, heartbeat, voice) are dropped although nothing was posted."
    ),
)
async def test_an_echo_whose_send_never_left_the_mac_still_reaches_the_owner() -> None:
    async with world("BonviHR", script=keeps_working) as here:
        refused: list[str] = []

        def refuse_the_first_echo(method: TelegramMethod[Any]) -> Fault | None:
            text = str(getattr(method, "text", "") or "")
            if text.startswith("→ ") and not refused:
                refused.append(text)
                return Fault.DOWN  # the connection was refused: Telegram never saw the request
            return None

        here.telegram.plan = refuse_the_first_echo
        await here.start()
        await here.advance(3.0)
        here.say("BonviHR'da login sahifasini tuzat #01")
        await here.within(30, lambda: bool(refused), what="the echo refused once")
        await here.advance(120.0, step=1.0)
        assert here.telegram.posted("→ BonviHR")
        ledger = await outbox_ledger(here)
        assert ledger.lost == ()
