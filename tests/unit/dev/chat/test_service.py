"""chat.service on a real SQLite file, `FakeClock` and `FakeAssistant` (owner, 2026-10-24; SPEC §3.2.6 `chat`).

One answer per `chat` burst: the chat Assistant's reply as a priority-0 reply to the owner's message, with its usage
row; a timeout, Claude being unavailable or an empty reply gives the one catalog line instead. Never a report, never
a project, never the executor.
"""

from dataclasses import replace
from typing import cast

from tests.factories import Fakes, list_replies, make_closed_burst, make_part, make_voice_part
from tests.owner_text import shown
from yordamchi import prompts
from yordamchi.core import strings as S
from yordamchi.core.errors import CallTimedOut, ProviderUnavailable, RateLimited
from yordamchi.dev.chat import domain, service
from yordamchi.dev.deps import DevDeps
from yordamchi.ports.agent import AgentRuntime
from yordamchi.ports.assistant import AssistantReply
from yordamchi.shared.burst.domain import TurnInput
from yordamchi.shared.cost import store as cost_store
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.outbox.models import OutboxMessage

QUESTION = "Python'da decorator nima?"
ANSWER = "**Decorator** — boshqa funksiyani o'rab, unga yangi xatti-harakat qo'shadigan funksiya."


class ExecutorTripwire:
    """The chat path's executor: any use of it fails the test (a chat answer never touches a project session)."""

    def __getattr__(self, name: str) -> object:
        msg = f"the chat path touched the executor: {name}"
        raise AssertionError(msg)


def text_of(row: OutboxMessage) -> str:
    payload = row.payload_json
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


async def _burst(deps: DevDeps, *, voice: bool = False) -> TurnInput:
    part = make_voice_part(1, at_s=0, transcript=QUESTION) if voice else make_part(1, text=QUESTION)
    async with deps.db.uow() as s:
        return await make_closed_burst(s, part)


async def test_a_general_question_gets_the_model_s_answer_as_a_quick_reply(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with(
        "chat",
        AssistantReply(text=ANSWER, provider="claude", model="sonnet", duration_ms=900, cost_usd_micros=1200),
    )
    deps = replace(dev_deps, agents=cast(AgentRuntime, ExecutorTripwire()))
    item = await _burst(deps)

    answer = await service.answer_chat(deps, item)

    assert (answer.text, answer.provider, answer.error_code) == (ANSWER, "claude", None)
    [request] = fakes.assistant.requests
    assert (request.kind, request.model, request.effort, request.output_schema) == ("chat", "sonnet", "low", None)
    assert request.system_prompt == prompts.render(domain.SYSTEM_PROMPT)
    assert request.instructions.startswith("Current instructions from the owner:")
    assert QUESTION in request.prompt
    [row] = await list_replies(deps.db)
    assert (row.kind, row.priority, row.op) == (Kind.REPORT.value, 0, "send_text")
    assert row.dedupe_key == f"report:chat-{item.burst_id}:0"
    assert row.reply_to_message_id == item.parts[0].message_id
    assert row.project_id is None
    assert shown(text_of(row)) == shown(ANSWER)
    async with deps.db.read() as s:
        [usage] = await cost_store.list_usage_records(s)
    assert (usage.source, usage.model, usage.cost_usd_micros, usage.project_id) == ("chat", "sonnet", 1200, None)


async def test_no_report_line_and_no_project_in_the_answer(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("chat", ANSWER)
    await service.answer_chat(dev_deps, await _burst(dev_deps, voice=True))
    [row] = await list_replies(dev_deps.db)
    text = shown(text_of(row))
    assert "Ma'lumot" not in text
    assert "Tekshiryapman" not in text


async def test_a_timeout_gets_the_one_short_line(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("chat", CallTimedOut(operation="assistant_chat"))
    item = await _burst(dev_deps)

    answer = await service.answer_chat(dev_deps, item)

    assert (answer.text, answer.provider, answer.error_code) == (S.CHAT_FAILED(), None, "call_timed_out")
    [row] = await list_replies(dev_deps.db)
    assert (row.kind, row.priority) == (Kind.REPORT.value, 0)
    assert text_of(row) == S.CHAT_FAILED()
    async with dev_deps.db.read() as s:
        assert await cost_store.list_usage_records(s) == []


async def test_claude_unavailable_or_out_of_limit_gets_the_same_line(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("chat", ProviderUnavailable(provider="claude"), RateLimited(provider="claude"))
    for update_id in (1, 2):
        async with dev_deps.db.uow() as s:
            item = await make_closed_burst(s, make_part(update_id, text=QUESTION))
        assert (await service.answer_chat(dev_deps, item)).text == S.CHAT_FAILED()
    assert [text_of(row) for row in await list_replies(dev_deps.db)] == [S.CHAT_FAILED()] * 2


async def test_an_empty_answer_gets_the_line_and_its_usage_is_still_recorded(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("chat", AssistantReply(text="  ", provider="claude", model="sonnet", duration_ms=5))
    answer = await service.answer_chat(dev_deps, await _burst(dev_deps))
    assert (answer.text, answer.error_code) == (S.CHAT_FAILED(), service.EMPTY)
    async with dev_deps.db.read() as s:
        assert len(await cost_store.list_usage_records(s)) == 1


async def test_a_redelivered_burst_is_answered_once(dev_deps: DevDeps, fakes: Fakes) -> None:
    fakes.assistant.reply_with("chat", ANSWER, "ikkinchi javob")
    item = await _burst(dev_deps)
    await service.answer_chat(dev_deps, item)
    await service.answer_chat(dev_deps, item)
    assert len(await list_replies(dev_deps.db)) == 1  # the outbox key `report:chat-<burst>:0` was queued before


async def test_the_client_is_warmed_with_the_profile_every_ask_uses(dev_deps: DevDeps, fakes: Fakes) -> None:
    await service.warm_chat(dev_deps)
    fakes.assistant.reply_with("chat", ANSWER)
    await service.answer_chat(dev_deps, await _burst(dev_deps))
    assert fakes.assistant.warmed == [fakes.assistant.requests[0].profile]
