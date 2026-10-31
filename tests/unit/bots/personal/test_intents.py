"""`bots/personal/intents.py`: the personal bot's typed aliases and exact phrases (SPEC §6; T77). Anything that is
none of them is a question for the assistant (None)."""

import pytest

from yordamchi.bots.personal import intents
from yordamchi.bots.personal.intents import Answer, Command, Intent


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("holat?", Command(Intent.SHOW_HEALTH)),
        ("/holat", Command(Intent.SHOW_HEALTH)),
        ("/holat@OrvionX_AI_Bot", Command(Intent.SHOW_HEALTH)),
        ("Sozlamalaring qanday?", Command(Intent.SHOW_SETTINGS)),
        ("/sozlamalar", Command(Intent.SHOW_SETTINGS)),
        ("/sozlamalar ovozni o'chir", Command(Intent.CHANGE_SETTING, argument="ovozni o'chir")),
        ("sozlama: javoblar qisqaroq", Command(Intent.CHANGE_SETTING, argument="sozlama: javoblar qisqaroq")),
        ("javoblaring qisqaroq bo'lsin", Command(Intent.CHANGE_SETTING, argument="javoblaring qisqaroq bo'lsin")),
        ("ovozingni erkakka o'zgartir", Command(Intent.CHANGE_SETTING, argument="ovozingni erkakka o'zgartir")),
        ("ovozni o'chir", Command(Intent.CHANGE_SETTING, argument="ovozni o'chir")),
        ("oldingi holatiga qaytar", Command(Intent.REVERT_SETTING)),
        ("oldingi holatiga qaytar voice.enabled", Command(Intent.REVERT_SETTING, argument="voice.enabled")),
        ("bu so'zni to'g'ri yoz: Wunderkind", Command(Intent.ADD_TERM, argument="Wunderkind")),
        ("xarajat?", Command(Intent.SHOW_COST)),
        ("/xarajat", Command(Intent.SHOW_COST)),
        ("to'liqroq", Command(Intent.MORE)),
        ("To‘liqroq!", Command(Intent.MORE)),  # a curly apostrophe, as phones type it
        ("yangi suhbat", Command(Intent.NEW_CONVERSATION)),
        ("/start", Command(Intent.HELP)),
        ("/yordam", Command(Intent.HELP)),
        ("bugungi yangiliklar", Command(Intent.NEWS)),
        ("moliya yangiliklari", Command(Intent.NEWS)),
        ("so'nggi yangiliklarni aytib ber", Command(Intent.NEWS)),
        ("integratsiyalar", Command(Intent.INTEGRATIONS)),
        ("ovozing yoqmadi", Command(Intent.VOICE_SAMPLES)),
        ("Bonvi HR integratsiyasini o'chir", Command(Intent.INTEGRATION_REMOVE, argument="Bonvi HR")),
        ("OpenWeather integratsiyasini qaytar", Command(Intent.INTEGRATION_RESTORE, argument="OpenWeather")),
        ("Markaz'ga qayta ulan", Command(Intent.INTEGRATION_RECONNECT, argument="Markaz")),
    ],
)
def test_a_phrase_names_its_intent(text: str, expected: Command) -> None:
    assert intents.command_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Kvant kompyuter nima?",
        "BonviHR qanday?",  # the assistant's bridge fast path, not a command
        "Nima yangiliklar?",  # a greeting
        "javobing noto'g'ri, qayta o'yla",  # about one answer: the assistant's business
        "sozlamalar sahifasini qayerdan topaman?",  # some app's settings page
        "o'zingni tanishtir",
        "https://example.com/maqola",
        "Bonvi HR'ga ulan: https://hr.example.uz/mcp",  # connecting: the assistant fills in the details
        "mana OpenWeather API kaliti: [kalit: generic_secret ••••abcd], ulan",
        "/unknown",
        "",
        "   ",
    ],
)
def test_anything_else_is_a_question(text: str) -> None:
    assert intents.command_of(text) is None


def test_every_intent_is_built_and_conversation_ones_wait_in_line() -> None:
    assert all(Command(intent).built for intent in Intent)
    for intent in (Intent.NEWS, Intent.VOICE_SAMPLES):  # answers: they take their turn in the work line
        assert Command(intent).conversation
    for intent in (Intent.INTEGRATIONS, Intent.INTEGRATION_REMOVE, Intent.INTEGRATION_RESTORE):
        assert Command(intent).needs_origin
    assert Command(Intent.MORE).conversation
    assert Command(Intent.NEW_CONVERSATION).conversation
    assert not Command(Intent.SHOW_HEALTH).conversation
    assert Command(Intent.CHANGE_SETTING).needs_origin
    assert not Command(Intent.SHOW_HEALTH).needs_origin


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ha", Answer.YES),
        ("Ha.", Answer.YES),
        ("mayli", Answer.YES),
        ("bo‘pti", Answer.YES),
        ("yo'q", Answer.NO),
        ("kerakmas", Answer.NO),
        ("ha, lekin keyinroq", None),
        ("Kvant kompyuter nima?", None),
        (None, None),
    ],
)
def test_the_owners_word_on_a_waiting_change(text: str | None, expected: Answer | None) -> None:
    assert intents.answer_of(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ikkinchisi", 2),
        ("2", 2),
        ("2-si", 2),
        ("Birinchisi!", 1),
        ("uchinchi ovoz yoqdi", 3),
        ("to'rtinchisi", 4),
        ("salom", None),
        ("2 ta savolim bor", None),
        ("", None),
    ],
)
def test_the_owners_pick_among_the_samples(text: str, expected: int | None) -> None:
    assert intents.sample_choice_of(text) == expected
