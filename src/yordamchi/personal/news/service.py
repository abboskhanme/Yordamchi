"""News on request (SPEC §3.1 `personal/news`; UC-33; T75).

"bugungi yangiliklar", "moliya yangiliklari", `/yangiliklar` → the assistant's ordinary answer (SPEC §3.2.13), asked
with `prompts/personal/news_request.md` around the owner's words: WebSearch now, at most `news.max_items` items from
the last `news.max_age_h` hours, each with its source and publication time, and every market number with its source
and date. The answer, its voice note and "to'liqroq" are `personal/qa`'s.
"""

from dataclasses import replace
from typing import Final

from yordamchi import prompts
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.qa import domain as qa_domain
from yordamchi.personal.qa.domain import QaQuestion
from yordamchi.shared.burst.domain import TurnInput
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings

PROMPT_REQUEST: Final = "personal/news_request"


async def news_question(deps: PersonalDeps, item: TurnInput) -> QaQuestion:
    """The burst as a news request for `qa.answer`: the owner's words inside the news brief."""
    snap = await settings.snapshot(deps)
    question = qa_domain.question_of(item)
    request = prompts.render(
        PROMPT_REQUEST,
        request=" ".join(question.own_text.split())[:300],
        max_items=str(snap[defaults.NEWS_MAX_ITEMS]),
        max_age_h=str(snap[defaults.NEWS_MAX_AGE_H]),
    )
    return replace(question, own_text=request)
