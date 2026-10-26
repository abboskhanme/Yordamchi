"""`bots/dev/intents.py`: the typed aliases and exact phrases of SPEC §6, natural language first, no menu."""

import pytest

from yordamchi.bots.dev.intents import EARLY, NOT_BUILT, Command, Intent, command_of


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # show_health (UC-06): words, the alias, a voice transcript's spelling
        ("holat?", Command(Intent.SHOW_HEALTH)),
        ("Holat", Command(Intent.SHOW_HEALTH)),
        ("/holat", Command(Intent.SHOW_HEALTH)),
        ("/holat@yordamchi_dev_bot", Command(Intent.SHOW_HEALTH)),
        # settings (UC-09)
        ("sozlamalaring qanday?", Command(Intent.SHOW_SETTINGS)),
        ("/sozlamalar", Command(Intent.SHOW_SETTINGS)),
        ("oldingi holatiga qaytar", Command(Intent.REVERT_SETTING)),
        (
            "oldingi holatiga qaytar outbox.progress_edit_s",
            Command(Intent.REVERT_SETTING, argument="outbox.progress_edit_s"),
        ),
        (
            "sozlama: javoblar qisqaroq bo'lsin",
            Command(Intent.CHANGE_SETTING, argument="sozlama: javoblar qisqaroq bo'lsin"),
        ),
        ("Settings: answer in Russian", Command(Intent.CHANGE_SETTING, argument="Settings: answer in Russian")),
        (
            "/sozlamalar rasmdan keyin 15 soniya kut",
            Command(Intent.CHANGE_SETTING, argument="rasmdan keyin 15 soniya kut"),
        ),
        # settings in natural language (T34b): the owner speaking to the bot about itself, anywhere in the message
        (
            "ovozingni ayolga o'zgartir",
            Command(Intent.CHANGE_SETTING, argument="ovozingni ayolga o'zgartir", implicit=True),
        ),
        (
            "Javoblaringni qisqaroq qil.",
            Command(Intent.CHANGE_SETTING, argument="Javoblaringni qisqaroq qil", implicit=True),
        ),
        (
            "iltimos, gaplaringni iliqroq qil",
            Command(Intent.CHANGE_SETTING, argument="iltimos, gaplaringni iliqroq qil", implicit=True),
        ),
        (
            "o’zingni qisqaroq tut",  # a curly apostrophe, as a phone keyboard types it
            Command(Intent.CHANGE_SETTING, argument="o'zingni qisqaroq tut", implicit=True),
        ),
        # small talk (owner, 2026-10-24: "Qandeysan?" got a report): answered as such, the owner's spelling too
        ("Qandeysan?", Command(Intent.GREETING, argument="how")),
        ("qandaysiz", Command(Intent.GREETING, argument="how")),
        ("Salom, qandaysan?", Command(Intent.GREETING, argument="how")),
        ("yaxshimisiz", Command(Intent.GREETING, argument="how")),
        ("как дела?", Command(Intent.GREETING, argument="how")),
        ("Salom!", Command(Intent.GREETING, argument="hello")),
        ("Salomlarim", Command(Intent.GREETING, argument="hello")),  # the owner's own words, 2026-10-24
        ("xayrli tong", Command(Intent.GREETING, argument="hello")),
        ("hi", Command(Intent.GREETING, argument="hello")),
        ("Assalomu alaykum", Command(Intent.GREETING, argument="salam")),
        ("Assalamu alaykum", Command(Intent.GREETING, argument="salam")),  # the owner's spelling, 2026-10-24
        ("Rahmat!", Command(Intent.THANKS)),
        ("katta rahmat 🙏", Command(Intent.THANKS)),
        ("spasibo", Command(Intent.THANKS)),
        ("Nimalar qilolasan?", Command(Intent.HELP)),
        ("Nimalar qo'lingdan keladi", Command(Intent.HELP)),
        ("qo'lingizdan nima keladi?", Command(Intent.HELP)),
        ("nima qila olasiz", Command(Intent.HELP)),
        ("qanday yordam bera olasan?", Command(Intent.HELP)),
        ("yordam", Command(Intent.HELP)),
        ("/yordam", Command(Intent.HELP)),
        ("/help", Command(Intent.HELP)),
        ("Hop", Command(Intent.ACKNOWLEDGE)),  # the owner's own word, 2026-10-24
        ("xo'p", Command(Intent.ACKNOWLEDGE)),
        ("ok 👍", Command(Intent.ACKNOWLEDGE)),
        ("tushunarli, rahmatsiz", None),  # more than an okay: routing reads it
        # projects (UC-18)
        ("loyihalar", Command(Intent.LIST_PROJECTS)),
        ("/loyihalar", Command(Intent.LIST_PROJECTS)),
        ("BonviHR'ni 'hr' deb ham tani", Command(Intent.ALIAS_PROJECT, project="BonviHR", argument="hr")),
        ("BonviHR’ni «bonvi» deb tani", Command(Intent.ALIAS_PROJECT, project="BonviHR", argument="bonvi")),
        ("OldCRM'ni arxivla", Command(Intent.ARCHIVE_PROJECT, project="OldCRM")),
        ("OldCRM'ni arxivdan chiqar", Command(Intent.UNARCHIVE_PROJECT, project="OldCRM")),
        ("BonviHR tavsifi: HR tizimi", Command(Intent.DESCRIBE_PROJECT, project="BonviHR", argument="HR tizimi")),
        # voice terms (UC-05)
        ("bu so'zni to'g'ri yoz: Wunderkind", Command(Intent.ADD_TERM, argument="Wunderkind")),
        # reports (UC-15)
        ("/hisobot", Command(Intent.REPORT)),
        ("/hisobot BonviHR", Command(Intent.REPORT, project="BonviHR")),
        # turns (UC-03, NFR-COST-03, NFR-REL-05/07)
        ("/stop", Command(Intent.STOP_PROJECT)),
        ("BonviHR'ni to'xtat", Command(Intent.STOP_PROJECT, project="BonviHR")),
        ("serverni to'xtat", Command(Intent.STOP_PROJECT, project="server")),  # the router lets it through
        ("davom ettir", Command(Intent.CONTINUE_TURN)),
        ("BonviHR'ni davom ettir", Command(Intent.CONTINUE_TURN, project="BonviHR")),
        ("qayta yubor", Command(Intent.RETRY_DELIVERY)),
        ("HRMentor'ga qayta yubor", Command(Intent.RETRY_DELIVERY, project="HRMentor")),
        # servers (T31)
        ("bonvi-srv serverini kuzat", Command(Intent.WATCH_SERVER, argument="bonvi-srv")),
        ("bonvi-srv'ni kuzatma", Command(Intent.UNWATCH_SERVER, argument="bonvi-srv")),
        # cost (UC-07, T51)
        ("xarajat?", Command(Intent.SHOW_COST)),
        ("/xarajat", Command(Intent.SHOW_COST)),
        # later phases answer S.NOT_YET
        ("integratsiyalar", Command(Intent.INTEGRATIONS)),
        ("Bonvi HR'ga ulan: https://hr.example.uz/mcp", Command(Intent.INTEGRATIONS)),
        ("yangi loyiha: maktab uchun bot", Command(Intent.NEW_PROJECT)),
        ("BonviHR sessiyasi", Command(Intent.MAC_HANDOFF, project="BonviHR")),
        ("/sessiya BonviHR", Command(Intent.MAC_HANDOFF, project="BonviHR")),
        ("BonviHR'ni yopib tur", Command(Intent.PARK_PROJECT, project="BonviHR")),
        ("BonviHR'ni davom ettiramiz", Command(Intent.RESUME_PROJECT, project="BonviHR")),
        ("o'zingga ovozli javob qo'sh", Command(Intent.SELF_UPDATE)),
        ("BonviHR yangi sessiya", Command(Intent.NEW_SESSION, project="BonviHR")),
        # bare, in a project's forum topic: that project's (D4 revised 2026-10-26)
        ("yangi sessiya", Command(Intent.NEW_SESSION)),
        ("Yangi sessiya och.", Command(Intent.NEW_SESSION)),
    ],
)
def test_each_phrase_names_its_intent(text: str, expected: Command) -> None:
    assert command_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "BonviHR'da login sahifasini tuzat",
        "BonviHR'ning sozlamalar sahifasini tuzat",  # "sozlama" inside work for a project
        "sozlamalar sahifasiga dark mode qo'sh",  # a part of some app, not the bot's settings
        "Settings page'dagi tugmani tuzat",
        "rasmdan keyin 15 soniya kut",  # no cue: routing's `change_setting` intent decides it
        "xatoni o'zing top va tuzat",  # "do it yourself" is an instruction
        "holatni tekshir va testlarni ishga tushir",
        "Salom, BonviHR'da login xatosini tuzat",  # a greeting in front of work is work
        "rahmat, endi testlarni ham yoz",
        "yordam ber: BonviHR testlari yiqilyapti",
        "BonviHR qanday?",  # a report question: routing's fast path
        "/start",
        "ha",
        "2",
        "",
        "   ",
        None,
    ],
)
def test_project_messages_and_answers_are_not_commands(text: str | None) -> None:
    assert command_of(text) is None


def test_only_commands_that_can_never_be_an_answer_come_before_binding() -> None:
    assert {Intent.SHOW_HEALTH, Intent.SHOW_SETTINGS, Intent.LIST_PROJECTS} <= EARLY
    assert Intent.CONTINUE_TURN not in EARLY  # "davom ettir" may answer an agent's question
    assert Intent.CHANGE_SETTING not in EARLY
    assert {Intent.GREETING, Intent.THANKS, Intent.HELP} <= EARLY  # "rahmat" never answers an agent's question
    assert Intent.ACKNOWLEDGE not in EARLY  # "xo'p" may answer an open question or decision


def test_every_later_phase_intent_is_answered_not_yet() -> None:
    later = {
        Intent.INTEGRATIONS,
        Intent.NEW_PROJECT,
        Intent.MAC_HANDOFF,
        Intent.PARK_PROJECT,
        Intent.RESUME_PROJECT,
        Intent.SELF_UPDATE,
    }
    assert later <= NOT_BUILT
    assert not Command(Intent.SELF_UPDATE).built  # its drain and probation are wired in P2
    assert Command(Intent.SELF_UPDATE).phase == "P2"
    assert Command(Intent.SHOW_HEALTH).built


def test_the_cost_report_and_a_new_session_are_wired() -> None:
    assert Command(Intent.SHOW_COST).built
    assert Command(Intent.SHOW_COST).early  # "xarajat?" can never answer an agent's question
    assert Command(Intent.NEW_SESSION, project="BonviHR").built
