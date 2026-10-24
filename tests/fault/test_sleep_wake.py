"""T36 sleep and wake (NFR-REL-04; SPEC §2.8 "Sleep and wake").

The Mac sleeps: the clock jumps and nothing of the bot runs meanwhile, while Telegram holds what the owner writes. On
wake the bot must resume within 60 s, tell "Mac uxlagan edi HH:MM–HH:MM, N ta xabar qayta ishlanmoqda" (a gap of
≥ 5 min) and work through the backlog. A sleep is neither a restart nor a network outage.
"""

from datetime import timedelta
from typing import Final

import pytest

from tests.fakes.agent import ScriptItem, ToolStep, done_result
from tests.fault.harness import delivered_to, delivery_counts, drain, outbox_ledger, tokens_in, world
from yordamchi.core import strings as S
from yordamchi.core import timefmt
from yordamchi.ports.agent import AgentInput

TIMEZONE: Final = "Asia/Tashkent"  # `ui.timezone`
SLEEP: Final = timedelta(minutes=20)
RESUME_WITHIN_S: Final = 60.0


def finishes(message: AgentInput) -> tuple[ScriptItem, ...]:
    names = " ".join(tokens_in(message.text)) or "ish"
    return ToolStep("Read", {"file_path": "README.md"}), done_result(f"Natija: {names} bajarildi.")


async def test_a_mac_that_slept_says_so_and_works_through_the_backlog() -> None:
    async with world("BonviHR", script=finishes) as here:
        bot = await here.start()
        await here.advance(40.0)
        last_tick = bot.app.health.last_tick_at  # the bot's last sign of life before the lid closed
        assert last_tick is not None
        asleep = here.clock.now()
        # Asleep: nothing runs. The owner writes three times; Telegram holds the updates.
        backlog = ((6, "login sahifasini tuzat #01"), (8, "testlarini yoz #02"), (12, "README'ni yangila #03"))
        for minutes, text in backlog:
            here.clock.set(asleep + timedelta(minutes=minutes))
            here.say(f"BonviHR'da {text}")
        woke = asleep + SLEEP
        here.clock.set(woke)

        took = await here.within(
            RESUME_WITHIN_S + 30, lambda: bool(here.telegram.posted("Mac uxlagan edi")), what="wake"
        )
        assert took <= RESUME_WITHIN_S
        told = S.WAKE_GAP(from_=timefmt.hhmm(last_tick, TIMEZONE), to=timefmt.hhmm(woke, TIMEZONE), n=3)
        assert [post.text for post in here.telegram.posted("Mac uxlagan edi")] == [told]
        resumed = next(call.at for call in here.telegram.calls if call.api_method == "getUpdates" and call.at >= woke)
        assert resumed - woke <= timedelta(seconds=RESUME_WITHIN_S)

        tokens = ["#01", "#02", "#03"]
        ran = dict.fromkeys(tokens, 1)
        await here.within(RESUME_WITHIN_S, lambda: delivery_counts(here, tokens) == ran, what="the backlog ran")
        await drain(here)
        assert delivery_counts(here, tokens) == dict.fromkeys(tokens, 1)
        results = here.telegram.posted("BonviHR · Natija:")
        assert all(any(token in post.text for post in results) for token in tokens)
        # A sleep is neither an outage nor a restart.
        assert not here.telegram.posted("Internet uzilgan edi")
        assert len(here.telegram.posted("Qayta ishga tushdim")) == 1
        ledger = await outbox_ledger(here)
        assert (ledger.lost, ledger.pending, ledger.duplicates) == ((), (), ())
        assert here.telegram.violations == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "T36 BUG-1 after a sleep: the backlog arrives in one getUpdates batch and shared/burst/domain.place() windows "
        "it by receipt time, so instructions for three projects written minutes apart become one burst and one "
        "routing question instead of three turns (see test_network.py::test_messages_held_while_offline_stay_apart)."
    ),
)
async def test_a_backlog_for_three_projects_after_a_sleep_goes_to_each_project() -> None:
    async with world("BonviHR", "HRMentor", "Markaz", script=finishes) as here:
        await here.start()
        await here.advance(40.0)
        asleep = here.clock.now()
        for minutes, project, token in ((6, "BonviHR", "#01"), (9, "HRMentor", "#02"), (12, "Markaz", "#03")):
            here.clock.set(asleep + timedelta(minutes=minutes))
            here.say(f"{project}'da login sahifasini tuzat {token}")
        here.clock.set(asleep + SLEEP)
        await drain(here)
        assert delivered_to(here) == {"#01": {"BonviHR"}, "#02": {"HRMentor"}, "#03": {"Markaz"}}
