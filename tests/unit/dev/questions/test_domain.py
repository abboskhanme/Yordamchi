"""questions.domain: the owner's message, what an answer chose, and which question it answers (SPEC §3.2.2; UC-12)."""

from dataclasses import replace
from datetime import timedelta

import pytest

from tests.factories import agent_question, make_part, make_turn_input, make_voice_part
from tests.fakes.clock import DEFAULT_START
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.dev.questions import domain
from yordamchi.dev.questions.domain import (
    AgentQuestion,
    Ambiguous,
    Answer,
    Bound,
    NotAnAnswer,
    OpenQuestion,
    Option,
    OutOfRange,
    ReplyTarget,
)
from yordamchi.shared.burst.domain import TranscriptState

DB = domain.parse_questions((agent_question(),), str)
DB_QUESTION = DB[0].text
STACK = domain.parse_questions(
    (
        agent_question(),
        agent_question(
            "Qaysi framework?", (("FastAPI (Recommended)", "tez, async"), ("Django", "")), header="Framework"
        ),
    ),
    str,
)
EXTRAS = domain.parse_questions(
    (
        agent_question(
            "Nimalarni qo'shamiz?",
            (("Redis", "kesh"), ("Celery", "fon vazifalari"), ("Sentry", "xatolar")),
            header="Qo'shimcha",
            multi_select=True,
        ),
    ),
    str,
)
FREE = domain.parse_questions((agent_question("Port raqami nechchi bo'lsin?", ()),), str)


def chosen(text: str, questions: tuple[AgentQuestion, ...] = DB, **kwargs: tuple[str, ...]) -> Answer:
    answer = domain.answer_of(text, questions, **kwargs)
    assert isinstance(answer, Answer), answer
    return answer


def open_question(
    question_id: int, project: str, *, minutes: int = 0, questions: tuple[AgentQuestion, ...] = DB
) -> OpenQuestion:
    return OpenQuestion(
        id=question_id,
        project_id=project,
        turn_id=question_id * 10,
        tool_use_id=f"toolu_{question_id}",
        asked_at=DEFAULT_START + timedelta(minutes=minutes),
        questions=questions,
    )


# --- the owner's message (no buttons, the project first, options numbered) ----------------------------------------
def test_one_question_starts_with_the_project_and_numbers_its_options_with_descriptions() -> None:
    text = domain.question_text("BonviHR", DB)
    assert text.splitlines() == [
        "BonviHR · Savol: Qaysi bazadan foydalanamiz?",
        "1. PostgreSQL — ishonchli, production uchun",
        "2. SQLite — oddiy, bitta fayl",
        S.AGENT_QUESTION_HINT(),
    ]


def test_several_questions_are_numbered_with_their_headers_and_a_multi_select_says_so() -> None:
    text = domain.question_text("BonviHR", (*STACK, *EXTRAS))
    lines = text.splitlines()
    assert lines[0] == "BonviHR · 3 ta savol:"
    assert lines[1] == "1) Baza: Qaysi bazadan foydalanamiz?"
    assert "2) Framework: Qaysi framework?" in lines
    assert "2. Django" in lines  # an option without a description has no dash
    assert S.AGENT_QUESTION_MULTI() in lines
    assert lines[-1] == S.AGENT_QUESTIONS_HINT()


def test_an_unreadable_question_still_reaches_the_owner() -> None:
    assert domain.question_text("BonviHR", ()).splitlines()[0] == "BonviHR · Savol: (savol matni kelmadi)"


def test_parsing_skips_malformed_entries_and_scrubs_every_text() -> None:
    raw: list[dict[str, JsonValue]] = [
        {"question": "  Qaysi   kalit? sk-1 ", "header": "Kalit", "options": [{"label": "A sk-2"}, {"x": 1}, "y"]},
        {"header": "no question"},
        {"question": 7},
    ]
    parsed = domain.parse_questions(raw, lambda text: text.replace("sk-", "••"))
    assert parsed == (AgentQuestion("Qaysi kalit? ••1", "Kalit", (Option("A ••2", ""),), multi_select=False),)
    assert domain.questions_from_json(domain.questions_json(parsed)) == parsed


# --- what the answer chose ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("said", ["2", "2-chi", "2chisi", "ikkinchisi", "ikkinchi variant", "#2", "ha, 2", "two"])
def test_a_number_in_any_form_picks_that_option(said: str) -> None:
    answer = chosen(said)
    assert answer.answers == {DB_QUESTION: "SQLite"}
    assert answer.chose


def test_the_last_option_has_a_word_of_its_own() -> None:
    assert chosen("oxirgisi").answers == {DB_QUESTION: "SQLite"}


@pytest.mark.parametrize(
    ("said", "label"),
    [("SQLite", "SQLite"), ("postgres", "PostgreSQL"), ("Postgersql", "PostgreSQL"), ("sqlite bo'lsin", "SQLite")],
)
def test_an_option_text_exactly_or_loosely_picks_it(said: str, label: str) -> None:
    assert chosen(said).answers == {DB_QUESTION: label}


@pytest.mark.parametrize("said", ["postgres emas, boshqasi", "MySQL ishlat", "1 yoki 2", "1 2"])
def test_anything_else_goes_to_the_agent_as_said(said: str) -> None:
    answer = chosen(said)
    assert answer.answers == {DB_QUESTION: said}
    assert not answer.chose


def test_a_number_past_the_options_is_out_of_range_not_a_guess() -> None:
    assert domain.answer_of("5", DB) == OutOfRange(2)


def test_a_question_without_options_takes_a_number_as_words() -> None:
    assert chosen("8080", FREE).answers == {FREE[0].text: "8080"}


@pytest.mark.parametrize("said", ["1, 3", "1 va 3", "Redis va Sentry", "redis, sentry"])
def test_a_multi_select_takes_several_options(said: str) -> None:
    assert chosen(said, EXTRAS).answers == {EXTRAS[0].text: "Redis, Sentry"}


@pytest.mark.parametrize(
    ("said", "framework"),
    [
        ("2\n1", "FastAPI (Recommended)"),
        ("2; fastapi", "FastAPI (Recommended)"),
        ("1) 2\n2) 1", "FastAPI (Recommended)"),
        ("2 1", "FastAPI (Recommended)"),
        ("sqlite, django", "Django"),
    ],
)
def test_several_questions_take_one_answer_each_in_order(said: str, framework: str) -> None:
    answer = chosen(said, STACK)
    assert answer.answers == {DB_QUESTION: "SQLite", STACK[1].text: framework}
    assert answer.chose


def test_several_questions_with_one_free_answer_give_it_to_each() -> None:
    answer = chosen("o'zing hal qil", STACK)
    assert answer.answers == {DB_QUESTION: "o'zing hal qil", STACK[1].text: "o'zing hal qil"}


def test_the_project_named_in_the_answer_is_not_part_of_the_choice() -> None:
    assert chosen("BonviHR'ga 2", names=("BonviHR",)).answers == {DB_QUESTION: "SQLite"}
    assert chosen("bonvihr savoliga: postgres", names=("BonviHR",)).answers == {DB_QUESTION: "PostgreSQL"}


def test_answers_round_trip_and_read_as_the_agent_message_block() -> None:
    answer = chosen("2\n1", STACK)
    assert domain.answer_from_json(domain.answer_json(answer)) == answer
    assert domain.answers_block(answer) == (
        "«Qaysi bazadan foydalanamiz?»: SQLite\n«Qaysi framework?»: FastAPI (Recommended)"
    )
    assert domain.answer_from_json(None) == Answer({}, "", chose=False)


# --- what the owner said ------------------------------------------------------------------------------------------
def test_the_answer_text_keeps_texts_and_transcripts_but_not_an_unheard_voice_note() -> None:
    failed = replace(
        make_voice_part(2, at_s=1, transcript="[ovoz tushunilmadi: shovqin]"), transcript=TranscriptState.FAILED
    )
    item = make_turn_input(make_part(1, text="Javob:"), make_voice_part(3, at_s=2, transcript="ikkinchisi"), failed)
    assert domain.answer_text(item) == "Javob:\nikkinchisi"
    assert domain.spoken_text(item) == "ikkinchisi"
    assert domain.reply_to_of(item) is None
    assert domain.reply_to_of(make_turn_input(make_part(1, text="2", reply_to_message_id=77))) == 77


# --- which question -----------------------------------------------------------------------------------------------
BONVI = open_question(1, "BonviHR")
HR = open_question(2, "HRMentor", minutes=1)


def test_a_reply_binds_to_the_question_replied_to_whatever_else_is_open() -> None:
    assert domain.bind_answer("2", [BONVI, HR], reply=ReplyTarget(question_id=1, project_id="BonviHR")) == Bound(BONVI)


def test_a_reply_to_a_question_no_longer_open_is_not_an_answer() -> None:
    assert domain.bind_answer("2", [HR], reply=ReplyTarget(question_id=9, project_id="BonviHR")) == NotAnAnswer()


def test_the_only_open_item_takes_any_answer() -> None:
    assert domain.bind_answer("o'zing hal qil", [BONVI]) == Bound(BONVI)


def test_a_pending_decision_means_free_words_are_not_guessed_as_the_answer() -> None:
    assert domain.bind_answer("keyinroq", [BONVI], decisions_pending=True) == NotAnAnswer()
    assert domain.bind_answer("2", [BONVI], decisions_pending=True) == Bound(BONVI)


def test_a_bare_number_with_two_projects_waiting_is_never_guessed() -> None:
    assert domain.bind_answer("2", [BONVI, HR]) == Ambiguous(("BonviHR", "HRMentor"))
    assert domain.bind_answer("ikkinchisi", [HR, BONVI]) == Ambiguous(("BonviHR", "HRMentor"))  # asked order


def test_an_answer_that_fits_one_question_only_binds_to_it() -> None:
    three = open_question(3, "HRMentor", minutes=2, questions=EXTRAS)
    assert domain.bind_answer("Sentry", [BONVI, three]) == Bound(three)
    assert domain.bind_answer("3", [BONVI, three]) == Bound(three)  # BonviHR's question has two options


def test_a_named_project_takes_its_newest_open_question() -> None:
    older = open_question(3, "HRMentor", minutes=-5)
    assert domain.bind_answer("HRMentor 2", [BONVI, older, HR], mentioned=("HRMentor",), names=("HRMentor",)) == Bound(
        HR
    )


def test_a_name_alone_or_a_project_without_questions_is_not_an_answer() -> None:
    assert domain.bind_answer("HRMentor", [BONVI, HR], mentioned=("HRMentor",), names=("HRMentor",)) == NotAnAnswer()
    assert domain.bind_answer("OvozYoz 2", [BONVI], mentioned=("OvozYoz",), names=("OvozYoz",)) == NotAnAnswer()


def test_two_named_projects_with_questions_ask_which() -> None:
    named = ("BonviHR", "HRMentor")
    assert domain.bind_answer("BonviHR va HRMentor: 2", [BONVI, HR], mentioned=named, names=named) == Ambiguous(named)


def test_a_reply_to_another_message_of_a_project_takes_its_newest_question() -> None:
    assert domain.bind_answer("2", [BONVI, HR], reply=ReplyTarget(project_id="HRMentor")) == Bound(HR)
    assert domain.bind_answer("2", [BONVI, HR], reply=ReplyTarget(project_id="OvozYoz")) == NotAnAnswer()
    assert domain.bind_answer("2", [BONVI, HR], reply=ReplyTarget()) == Ambiguous(("BonviHR", "HRMentor"))


def test_free_words_with_two_projects_waiting_go_to_routing() -> None:
    assert domain.bind_answer("login sahifasini tuzat", [BONVI, HR]) == NotAnAnswer()


def test_one_projects_questions_take_free_words_on_the_newest() -> None:
    newer = open_question(3, "BonviHR", minutes=3)
    assert domain.bind_answer("o'zing hal qil", [BONVI, newer]) == Bound(newer)
    assert domain.bind_answer("anything", []) == NotAnAnswer()


def test_the_which_question_names_the_projects_and_quotes_the_answer() -> None:
    assert domain.which_question(("BonviHR", "HRMentor"), "2") == "«2» — BonviHR savoligami yoki HRMentor savoligami?"
    many = domain.which_question(("A1", "B2", "C3"), "ikkinchisi")
    assert many == "«ikkinchisi» — qaysi loyiha savoliga: A1, B2, C3? Loyiha nomini yozing."


def test_the_confirmation_names_the_project_echoes_a_voice_note_and_says_when_it_went_as_a_message() -> None:
    answer = chosen("2")
    assert domain.answered_text("BonviHR", answer, spoken="", held=True) == "→ BonviHR: javob «SQLite»"
    voiced = domain.answered_text("BonviHR", answer, spoken="ikkinchisi", held=False)
    assert voiced == "Ovoz: «ikkinchisi» → BonviHR: javob «SQLite» " + S.QUESTION_ANSWERED_AS_MESSAGE()


def test_retention_keeps_ninety_days() -> None:
    assert domain.question_cutoff(DEFAULT_START) == DEFAULT_START - timedelta(days=90)
