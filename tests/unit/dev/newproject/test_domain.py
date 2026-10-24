"""newproject.domain (SPEC §3.2.8, §3.2.6 step 4; UC-14): the description, the owner's words, the name and its
collisions, which draft a message belongs to, and what the owner reads. Pure: values in, values out."""

from datetime import timedelta

import pytest

from tests.factories import make_part, make_turn_input, make_voice_part
from tests.fakes.clock import DEFAULT_START
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.core.text import join
from yordamchi.dev.newproject import domain
from yordamchi.dev.newproject.domain import DraftPart, DraftStatus, OpenDraft, Pick, PlanAnswer, PlanVerdict

AT = DEFAULT_START


# --- the description --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("tamom", ("", True)),
        ("Boshla!", ("", True)),
        ("  boshlang  ", ("", True)),
        ("Shu. Tamom.", ("Shu.", True)),
        ("Kutubxona boti\ntamom", ("Kutubxona boti", True)),
        ("ma'lumotlar bazasidan boshla", ("ma'lumotlar bazasidan boshla", False)),  # a word before it: description
        ("tamom bo'ldi", ("tamom bo'ldi", False)),
        ("kitob bron qilish", ("kitob bron qilish", False)),
    ],
)
def test_tamom_or_boshla_closes_only_as_its_own_message_line_or_sentence(text: str, expected: tuple[str, bool]) -> None:
    assert domain.split_close(text) == expected


def test_a_burst_becomes_one_part_labelled_as_the_agent_reads_it() -> None:
    item = make_turn_input(
        make_part(1, at_s=0, text="Kutubxona uchun telegram bot."),
        make_voice_part(2, at_s=5, transcript="kitoblarni bron qilish ham kerak"),
        burst_id=7,
    )
    burst = domain.part_of(item)
    assert burst.closes is False
    assert burst.part == DraftPart(
        text="Kutubxona uchun telegram bot.\n\n[voice note, transcribed] kitoblarni bron qilish ham kerak",
        at=AT + timedelta(seconds=5),
        burst_id=7,
    )


def test_a_closing_last_part_closes_and_leaves_its_word_out() -> None:
    item = make_turn_input(make_part(1, text="Faqat admin panel."), make_part(2, at_s=2, text="tamom"))
    burst = domain.part_of(item)
    assert burst.closes is True
    assert burst.part is not None
    assert burst.part.text == "Faqat admin panel."


def test_a_burst_of_only_the_close_word_adds_no_part() -> None:
    assert domain.part_of(make_turn_input(make_voice_part(1, at_s=0, transcript="Tamom."))) == domain.Burst(None, True)


def test_parts_round_trip_through_json_and_malformed_entries_are_dropped() -> None:
    parts = (DraftPart("bir", AT, 3), DraftPart("ikki", AT + timedelta(seconds=9)))
    assert domain.parts_from_json(domain.parts_json(parts)) == parts
    raw: list[JsonValue] = [*domain.parts_json(parts), {"text": 1}, {"text": "x", "at": "not a date"}, "junk"]
    assert domain.parts_from_json(raw) == parts
    assert domain.parts_from_json(None) == ()


def test_the_description_keeps_arrival_order_and_skips_empty_parts() -> None:
    parts = [DraftPart("birinchi", AT), DraftPart("  ", AT), DraftPart("ikkinchi", AT)]
    assert domain.description(parts) == "birinchi\n\nikkinchi"


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["yangi loyiha"], False),
        (["Yangi loyiha ochamiz!", "[voice note, not understood]"], False),
        (["yangi loyiha: kutubxona boti"], True),
        ([], False),
    ],
)
def test_a_description_needs_more_than_the_trigger_words(texts: list[str], expected: bool) -> None:
    assert domain.has_content(DraftPart(text, AT) for text in texts) is expected


def test_silence_closes_exactly_at_the_setting() -> None:
    assert not domain.silent(AT, AT + timedelta(seconds=119.9), silence_s=120)
    assert domain.silent(AT, AT + timedelta(seconds=120), silence_s=120)


# --- the owner's words ----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yo'q", True),
        ("Yoq.", True),
        ("bekor qil", True),
        ("Bekor qiling!", True),
        ("yo'q, login ham kerak", False),
        ("ha", False),
        ("kitob bron qilish", False),
    ],
)
def test_a_bare_no_or_bekor_qil_cancels(text: str, expected: bool) -> None:
    assert domain.is_cancel(text) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ha", PlanAnswer(PlanVerdict.APPROVE)),
        ("Ha, boshla", PlanAnswer(PlanVerdict.APPROVE)),
        ("KitobBot ha", PlanAnswer(PlanVerdict.APPROVE)),
        ("yo'q", PlanAnswer(PlanVerdict.CANCEL)),
        ("o'zgartir: Django bo'lsin", PlanAnswer(PlanVerdict.REVISE, "Django bo'lsin")),
        ("KitobBot o‘zgartiring — mobil ham", PlanAnswer(PlanVerdict.REVISE, "mobil ham")),
        ("ozgartir", PlanAnswer(PlanVerdict.REVISE, "")),
        ("ha, lekin Django bo'lsin", PlanAnswer(PlanVerdict.REVISE, "ha, lekin Django bo'lsin")),
        ("yo'q, mobil ham kerak", PlanAnswer(PlanVerdict.REVISE, "yo'q, mobil ham kerak")),
        ("mobil versiya ham kerak", PlanAnswer(PlanVerdict.REVISE, "mobil versiya ham kerak")),
    ],
)
def test_the_answer_to_the_plan(text: str, expected: PlanAnswer) -> None:
    assert domain.plan_answer(text, names=["KitobBot"]) == expected


# --- the name ---------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("yangi loyiha: kutubxona uchun telegram bot", "KutubxonaTelegramBot"),
        ("Yangi loyiha: «Oila Byudjeti» — oilaviy xarajatlar", "OilaByudjeti"),
        ('yangi loyiha "Kitob Bron" kerak', "KitobBron"),
        ("yangi loyiha, nomi: TezTaxi. Taksi chaqirish ilovasi", "TezTaxi"),
        ("TezTaxi nomli loyiha ochamiz", "TezTaxi"),
        ("yangi loyiha: o'quv markazi uchun CRM", "OquvMarkaziCRM"),
        ("Yangi loyiha ochamiz: KUTUBXONA tizimi", "KutubxonaTizimi"),
        ("янги лойиҳа: дўкон боти", "DokonBoti"),  # Uzbek Cyrillic is transliterated
        ("new project: 3d printer queue", "PrinterQueue"),
        ("yangi loyiha", "YangiLoyiha"),
        ("[voice note, not understood]", "YangiLoyiha"),
    ],
)
def test_the_name_comes_from_the_owners_own_words(text: str, expected: str) -> None:
    assert domain.propose_name(text) == expected


def test_a_name_is_latin_pascal_case_and_never_longer_than_the_limit() -> None:
    name = domain.propose_name("yangi loyiha: " + "juda" * 30 + " uzun nom")
    assert len(name) == domain.NAME_MAX
    assert name.isascii()
    assert name.isalnum()
    assert name[0].isupper()


def test_a_taken_name_continues_with_the_first_free_number_case_insensitively() -> None:
    assert next(domain.name_candidates("KitobBot", {"BonviHR"})) == "KitobBot"
    assert next(domain.name_candidates("BonviHR", {"bonvihr"})) == "BonviHR2"
    candidates = domain.name_candidates("BonviHR", {"BONVIHR", "BonviHR2", "bonvihr4"})
    assert [next(candidates), next(candidates)] == ["BonviHR3", "BonviHR5"]


# --- which draft a message belongs to -------------------------------------------------------------------------------
COLLECTING = OpenDraft(1, DraftStatus.COLLECTING, None)
AWAITING = OpenDraft(2, DraftStatus.AWAITING_APPROVAL, "KitobBot")
PLANNING = OpenDraft(3, DraftStatus.PLANNING, "TezTaxi")
BUILDING = OpenDraft(4, DraftStatus.BUILDING, "OilaByudjeti")


@pytest.mark.parametrize(
    ("drafts", "named", "targeted", "reply", "expected"),
    [
        ([], (), (), None, None),
        ([COLLECTING], (), (), None, Pick(1)),
        ([COLLECTING], ("BonviHR",), (), None, Pick(1)),  # "BonviHR kabi bo'lsin": still the description
        ([COLLECTING], ("BonviHR",), ("BonviHR",), None, None),  # "BonviHR'da login tuzat": work elsewhere
        ([COLLECTING], (), (), "BonviHR", None),  # a reply to BonviHR's message is about BonviHR
        ([AWAITING], (), (), None, Pick(2)),
        ([AWAITING], ("KitobBot",), (), None, Pick(2)),
        ([AWAITING], (), (), "KitobBot", Pick(2)),
        ([AWAITING], ("BonviHR",), (), None, None),
        ([AWAITING, PLANNING], (), (), None, Pick(choices=("KitobBot", "TezTaxi"))),
        ([AWAITING, PLANNING], ("TezTaxi",), (), None, Pick(3)),
        ([COLLECTING, AWAITING], ("KitobBot",), (), None, Pick(2)),
        ([COLLECTING, AWAITING], (), (), None, Pick(1)),
        ([BUILDING], (), (), None, None),  # an approved project's messages are routed as usual
        ([BUILDING], ("OilaByudjeti",), (), None, None),
    ],
)
def test_which_draft_takes_a_message(
    drafts: list[OpenDraft], named: tuple[str, ...], targeted: tuple[str, ...], reply: str | None, expected: Pick | None
) -> None:
    assert domain.pick_draft(drafts, named=named, targeted=targeted, reply_project=reply) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Yangi loyiha: «TezTaxi» taksi boti", True),
        ("  yangi  loyiha ochamiz", True),
        ("New project: printer queue", True),
        ("янги лойиҳа: дўкон", True),
        ("yangi loyihaga o'xshash bo'lsin", False),  # a word that only starts like it
        ("KitobBot'ga yangi loyiha sahifasi qo'sh", False),  # not at the start: work on a project
        ("ha", False),
    ],
)
def test_a_message_that_opens_a_project(text: str, expected: bool) -> None:
    assert domain.starts_project(text) is expected


def test_a_new_project_message_is_never_taken_by_a_waiting_plan() -> None:
    assert domain.pick_draft([AWAITING], starts_new=True) is None
    assert domain.pick_draft([AWAITING], named=("KitobBot",), starts_new=True) is None
    assert domain.pick_draft([COLLECTING, AWAITING], starts_new=True) == Pick(1)  # the description being collected


# --- plan documents and texts ---------------------------------------------------------------------------------------
def test_plan_docs_are_the_markdown_files_in_name_order_within_the_limit() -> None:
    names = ["SPEC.md", "notes.txt", ".hidden.md", "REQUIREMENTS.md", *(f"adr-{n:02}.md" for n in range(20))]
    chosen = domain.plan_docs(names)
    assert chosen[:2] == ("REQUIREMENTS.md", "SPEC.md")
    assert len(chosen) == domain.DOCS_MAX
    assert all(name.endswith(".md") and not name.startswith(".") for name in chosen)


def test_the_collecting_reply_names_the_silence_in_whole_minutes() -> None:
    assert domain.collecting_text(silence_s=120) == S.NEWPROJECT_COLLECTING(minutes=2)
    assert domain.collecting_text(silence_s=30) == S.NEWPROJECT_COLLECTING(minutes=1)


def test_the_planning_message_says_the_new_name_and_the_repo() -> None:
    text = domain.planning_text(
        "BonviHR2",
        path="~/Documents/Projects/BonviHR2",
        wanted="BonviHR",
        plan_max_min=14,
        repo_url="https://github.com/abboskhanme/BonviHR2",
        repo_problem=None,
    )
    assert text == join(
        [
            S.NEWPROJECT_PLANNING(project="BonviHR2", path="~/Documents/Projects/BonviHR2", minutes=14),
            S.NEWPROJECT_RENAMED(wanted="BonviHR", project="BonviHR2"),
            S.NEWPROJECT_REPO(url="https://github.com/abboskhanme/BonviHR2"),
        ]
    )
    assert text.startswith("BonviHR2")


def test_the_planning_message_without_a_repo_says_why() -> None:
    problem = S.NEWPROJECT_REPO_FAILED(why=S.NEWPROJECT_GITHUB_LOGGED_OUT())
    text = domain.planning_text(
        "KitobBot", path="~/P/KitobBot", wanted="KitobBot", plan_max_min=14, repo_url=None, repo_problem=problem
    )
    assert text == join([S.NEWPROJECT_PLANNING(project="KitobBot", path="~/P/KitobBot", minutes=14), problem])


def test_the_plan_message_is_the_summary_the_documents_and_one_question() -> None:
    text = domain.plan_ready_text(
        "KitobBot", natija="Telegram bot, FastAPI, 3 bosqich.", docs=["SPEC.md"], repo_url="https://x/KitobBot"
    )
    assert text == join(
        [
            S.NEWPROJECT_PLAN_READY(project="KitobBot", summary="Telegram bot, FastAPI, 3 bosqich."),
            S.NEWPROJECT_PLAN_DOCS(names="SPEC.md"),
            S.NEWPROJECT_REPO(url="https://x/KitobBot"),
            S.NEWPROJECT_PLAN_ASK(),
        ]
    )
    bare = domain.plan_ready_text("KitobBot", natija=None, docs=[], repo_url=None)
    assert S.TURN_RESULT_NO_NATIJA() in bare
    assert S.NEWPROJECT_PLAN_NO_DOCS() in bare
    assert bare.endswith(S.NEWPROJECT_PLAN_ASK())


def test_which_draft_question_lists_the_projects() -> None:
    assert domain.which_text(["KitobBot", "TezTaxi"]) == S.NEWPROJECT_WHICH(projects="KitobBot, TezTaxi")


def test_a_finished_draft_is_kept_400_days() -> None:
    assert domain.draft_cutoff(AT) == AT - timedelta(days=400)
