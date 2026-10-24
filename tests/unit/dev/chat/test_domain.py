"""chat.domain: the ask the chat Assistant gets, its settings and instructions block, and what the owner reads of its
answer (owner, 2026-10-24; SPEC §3.2.6 `chat`, §5.4)."""

import json

from tests.factories import FAKE_KEYS, make_default_snapshot, make_part, make_turn_input, make_voice_part
from tests.fakes.clock import DEFAULT_START
from yordamchi import prompts
from yordamchi.core import redact
from yordamchi.dev.chat import domain
from yordamchi.shared.inbox.domain import ForwardSource, UpdateKind
from yordamchi.shared.settings import defaults

SNAP = make_default_snapshot()
CFG = domain.chat_settings(SNAP)


def test_the_settings_come_from_the_chat_block() -> None:
    assert (CFG.model, CFG.effort, CFG.timeout_s) == ("sonnet", "low", 30.0)
    assert CFG.persona == SNAP[defaults.PERSONA_DEV_CHAT]
    assert (CFG.language, CFG.timezone) == ("uz", "Asia/Tashkent")


def test_the_system_prompt_is_fixed_and_the_persona_travels_per_ask() -> None:
    """SPEC §5.4 (spike S4): the frozen prompt carries no owner-editable text; the context block carries all of it."""
    system = prompts.render(domain.SYSTEM_PROMPT)
    assert CFG.persona not in system
    assert "Current instructions from the owner:" in system  # how to read the per-ask block
    context = prompts.render(domain.CONTEXT_PROMPT, **domain.context_slots(CFG))
    assert context.startswith("Current instructions from the owner:")
    assert CFG.persona in context
    assert "Uzbek (Latin script)" in context
    russian = domain.context_slots(domain.ChatSettings("sonnet", "low", 30.0, "p", "ru", "Asia/Tashkent"))
    assert russian["language"] == "Russian"


def test_the_ask_is_canonical_json_of_the_owner_s_words_and_his_local_time() -> None:
    item = make_turn_input(
        make_part(1, text="buni inglizchaga tarjima qil"),
        make_part(2, at_s=1, text="Ertaga soat 10 da uchrashamiz", forward=ForwardSource.USER),
        make_voice_part(3, at_s=2, transcript="tezroq bo'lsa yaxshi"),
        make_part(4, at_s=3, kind=UpdateKind.PHOTO),
    )
    prompt = domain.chat_prompt(item, now=DEFAULT_START, timezone="Asia/Tashkent", scrub=redact.scrubbed)
    payload = json.loads(prompt)
    assert payload["message"] == [
        {"text": "buni inglizchaga tarjima qil"},
        {"text": "Ertaga soat 10 da uchrashamiz", "forwarded": True},
        {"text": "tezroq bo'lsa yaxshi", "voice": True},
    ]  # the photo has no words: routing sends no media here
    assert payload["now"].endswith("+05:00")
    assert prompt == domain.chat_prompt(item, now=DEFAULT_START, timezone="Asia/Tashkent", scrub=redact.scrubbed)


def test_no_secret_reaches_the_model() -> None:
    key = FAKE_KEYS["anthropic_api_key"]
    item = make_turn_input(make_part(1, text=f"bu kalit to'g'rimi: {key}"))
    prompt = domain.chat_prompt(item, now=DEFAULT_START, timezone="Asia/Tashkent", scrub=redact.scrubbed)
    assert key not in prompt


def test_a_long_part_is_cut() -> None:
    item = make_turn_input(make_part(1, text="so'z " * 2000))
    payload = json.loads(domain.chat_prompt(item, now=DEFAULT_START, timezone="Asia/Tashkent", scrub=redact.scrubbed))
    assert len(payload["message"][0]["text"]) <= domain.PART_MAX


def test_the_answer_is_the_model_s_text_scrubbed_and_within_one_message() -> None:
    assert domain.answer_text("  Decorator — funksiyani o'raydigan funksiya.  ", redact.scrubbed) == (
        "Decorator — funksiyani o'raydigan funksiya."
    )
    key = FAKE_KEYS["anthropic_api_key"]
    assert key not in str(domain.answer_text(f"Kalit: {key}", redact.scrubbed))
    long = domain.answer_text("Bu uzun javob. " * 400, redact.scrubbed)
    assert long is not None
    assert len(long) <= domain.ANSWER_MAX


def test_a_copied_chat_marker_is_dropped_and_an_empty_answer_is_none() -> None:
    assert domain.answer_text("[chat]\nSalom!", redact.scrubbed) == "Salom!"
    assert domain.answer_text("   ", redact.scrubbed) is None
    assert domain.answer_text("[chat]", redact.scrubbed) is None


def test_a_cut_inside_a_code_block_closes_the_block() -> None:
    code = "Misol:\n```python\n" + "x = 1\n" * 1000
    answer = domain.answer_text(code, redact.scrubbed)
    assert answer is not None
    assert answer.count(domain.FENCE) % 2 == 0
    assert answer.endswith(domain.FENCE)
