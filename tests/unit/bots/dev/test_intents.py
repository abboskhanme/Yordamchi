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
        # later phases answer S.NOT_YET
        ("xarajat?", Command(Intent.SHOW_COST)),
        ("/xarajat", Command(Intent.SHOW_COST)),
        ("integratsiyalar", Command(Intent.INTEGRATIONS)),
        ("Bonvi HR'ga ulan: https://hr.example.uz/mcp", Command(Intent.INTEGRATIONS)),
        ("yangi loyiha: maktab uchun bot", Command(Intent.NEW_PROJECT)),
        ("BonviHR sessiyasi", Command(Intent.MAC_HANDOFF, project="BonviHR")),
        ("/sessiya BonviHR", Command(Intent.MAC_HANDOFF, project="BonviHR")),
        ("BonviHR'ni yopib tur", Command(Intent.PARK_PROJECT, project="BonviHR")),
        ("BonviHR'ni davom ettiramiz", Command(Intent.RESUME_PROJECT, project="BonviHR")),
        ("o'zingga ovozli javob qo'sh", Command(Intent.SELF_UPDATE)),
        ("BonviHR yangi sessiya", Command(Intent.NEW_SESSION, project="BonviHR")),
    ],
)
def test_each_phrase_names_its_intent(text: str, expected: Command) -> None:
    assert command_of(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "BonviHR'da login sahifasini tuzat",
        "BonviHR'ning sozlamalar sahifasini tuzat",  # "sozlama" inside work for a project
        "holatni tekshir va testlarni ishga tushir",
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


def test_every_later_phase_intent_is_answered_not_yet() -> None:
    later = {
        Intent.SHOW_COST,
        Intent.INTEGRATIONS,
        Intent.NEW_PROJECT,
        Intent.MAC_HANDOFF,
        Intent.PARK_PROJECT,
        Intent.RESUME_PROJECT,
        Intent.SELF_UPDATE,
    }
    assert later <= NOT_BUILT
    assert not Command(Intent.SHOW_COST).built
    assert Command(Intent.SHOW_COST).phase == "P2"
    assert Command(Intent.NEW_SESSION).phase == "P1"  # a P1 row whose turns API is still to come
    assert Command(Intent.SHOW_HEALTH).built
