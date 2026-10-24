"""T36: "Qayta ishga tushdim …" within 60 s of an unclean exit, even when the outbox is full (UC-06 AC; SPEC §2.5
step 8, §2.7; NFR-CAP-02).

The dev group takes at most 20 messages a minute, and after a burst of work the outbox can hold more replies than
that. The restart notice is what tells the owner the bot came back and asks "davom ettiraymi?": it must not wait
behind the backlog.
"""

from datetime import timedelta
from typing import Final

from tests.factories import DEV_GROUP_ID
from tests.fault.harness import outbox_ledger, world
from yordamchi.core import redact
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind

BACKLOG: Final = 45  # results queued for the dev group: more than two minutes of its 20/min budget
NOTICE_WITHIN_S: Final = 60.0


async def test_the_restart_notice_comes_within_60_s_even_behind_a_full_outbox() -> None:
    async with world("BonviHR") as here:
        first = await here.start()
        await here.advance(2.0)
        for index in range(BACKLOG):  # finished work reporting back, as after many turns ended at once
            text = redact.scrubbed(f"BonviHR · hisobot {index:02d}: tayyor")
            await outbox.enqueue_reply(
                first.deps, chat_id=DEV_GROUP_ID, text=text, kind=Kind.RESULT, ref=f"backlog-{index}", max_parts=3
            )
        await here.advance(30.0)  # the group's budget for this minute is spent; most of the backlog still waits
        assert 15 <= len(here.telegram.posted("BonviHR · hisobot")) <= 21
        await here.crash()

        await here.advance(5.0)  # launchd restarts the agent
        second = await here.start()
        took = await here.within(
            NOTICE_WITHIN_S + 30, lambda: len(here.telegram.posted("Qayta ishga tushdim")) >= 2, what="restart notice"
        )
        assert took <= NOTICE_WITHIN_S, f"'Qayta ishga tushdim' took {took} s behind the backlog"
        [notice] = [post for post in here.telegram.posted("Qayta ishga tushdim") if post.at >= second.started_at]
        assert "kutilmaganda uzildi" in notice.text
        assert notice.at - second.started_at <= timedelta(seconds=NOTICE_WITHIN_S)

        # The backlog itself still goes out in full, within Telegram's limits.
        await here.within(600, lambda: len(here.telegram.posted("BonviHR · hisobot")) >= BACKLOG, what="backlog")
        ledger = await outbox_ledger(here)
        assert ledger.lost == ()
        assert here.telegram.violations == []
