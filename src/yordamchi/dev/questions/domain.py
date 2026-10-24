"""Agent questions and the owner's answers, pure (SPEC §3.2.2, §4.2 `questions`; UC-12; spike S3).

An agent asks through `AskUserQuestion`: 1–4 questions, each with a short `header`, options (`label`, `description`)
and `multiSelect`. The owner gets one text message (`question_text`): the project first, the options numbered "1."
"2." with their descriptions, never a button. He answers in the group however he likes, by reply or not.

**Which question** (`bind_answer`), after the gate had its turn at the message (a decision binds first):
1. the question he replied to (one no longer open: not an answer, the reply routes as a message);
2. else a project he named: its newest open question (a name alone answers nothing; two named → ask);
3. else the project of another bot message he replied to: its newest open question;
4. else the only open item in the group (one question, no gate decision pending): any words;
5. else answer-shaped words — options he picks ("2", an option's text), or a bare number no option takes — go to
   the newest of those questions when they are one project's. Across two projects nothing is guessed: he is asked
   which (`Ambiguous` → `which_question`), and routing's clarification reads that answer;
6. else his own words, when one project's questions are all that is open. Any other free text is not an answer:
   routing takes it, content first.

**What he chose** (`answer_of`): "2", "2-chi", "ikkinchisi" → that option's label; "1, 3" for a multi-select; an
option's text, exactly or loosely ("postgres" for "PostgreSQL"); otherwise his own words, as he said them. A number
past the options is told (`OutOfRange`), never guessed. Several questions take one answer per line (or `;`, `,`), in
order; anything else goes to each of them as said.

A question asked inside a subagent never reaches this module: the guard denies it and tells the subagent to hand the
question to the main agent, which asks it with `AskUserQuestion` itself (S3). So the owner sees it as any other.
"""

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.text import BotText, join
from yordamchi.dev.projects.domain import shorten
from yordamchi.dev.routing.domain import EXCERPT_MAX, VOICE_ECHO_MAX, fold, tokens, within_one_typo
from yordamchi.shared.burst.domain import TranscriptState, TurnInput

QUESTION_KEEP: Final = timedelta(days=90)  # SPEC §4.2 `questions`: 90 d
PREFIX_MIN: Final = 4  # "postgres" picks "PostgreSQL": a word that starts another counts from this length
TYPO_MIN: Final = 6  # one typo is forgiven in words this long (routing's rule for names)
_UNHEARD: Final = frozenset({TranscriptState.PENDING, TranscriptState.FAILED})  # a "not understood" marker
_NUMBER: Final = re.compile(
    r"#?(\d{1,2})(?:-?(?:chi|chisi|chisini|chisiga|nchi|nchisi|inchi|inchisi|si|ni|ga|variant|varianti))?"
)
_ORDINAL_ROOTS: Final = {
    "birinchi": 1,
    "ikkinchi": 2,
    "uchinchi": 3,
    "to'rtinchi": 4,
    "beshinchi": 5,
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "перв": 1,
    "втор": 2,
    "трет": 3,
    "четв": 4,
    "пят": 5,
}
_LAST_ROOTS: Final = ("oxirgi", "last", "последн")
_LAST: Final = -1
_NUMBER_WORDS: Final = {
    "bir": 1,
    "ikki": 2,
    "uch": 3,
    "to'rt": 4,
    "besh": 5,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "один": 1,
    "два": 2,
    "три": 3,
    "четыре": 4,
}
# Words around a choice that add nothing to it ("ha, 2", "ikkinchisi bo'lsin", "1 va 3", "BonviHR savoliga: 2").
# Negations and "or" are not here: "2 emas", "1 yoki 2" are the owner's own words.
_FILLER: Final = frozenset(
    {
        "ha",
        "xa",
        "ok",
        "okay",
        "mayli",
        "shu",
        "o'sha",
        "bu",
        "variant",
        "varianti",
        "variantni",
        "variantini",
        "javob",
        "javobim",
        "bo'lsin",
        "tanlayman",
        "tanladim",
        "ishlat",
        "ishlating",
        "qil",
        "qiling",
        "kerak",
        "iltimos",
        "albatta",
        "yaxshi",
        "menimcha",
        "savol",
        "savolga",
        "savoliga",
        "uchun",
        "ga",
        "va",
        "option",
        "the",
        "use",
        "please",
        "and",
        "go",
        "with",
        "choose",
        "pick",
        "вариант",
        "давай",
        "и",
        "да",
    }
)
_LINES: Final = re.compile(r"\s*\n\s*")
_SEMICOLONS: Final = re.compile(r"\s*;\s*")
_COMMAS: Final = re.compile(r"\s*,\s*")
_CHOICE_JOINERS: Final = re.compile(r"\s*(?:,|;|\bva\b|\band\b|\bи\b)\s*", re.IGNORECASE)
_QUESTION_MARKER: Final = re.compile(r"^\s*\d{1,2}\)\s*(?=\S)")  # "1) 2": the owner numbered his lines like ours


class QuestionStatus(StrEnum):
    OPEN = "open"  # waiting for the owner, also once the hold elapsed and the agent was told to go on (parked)
    ANSWERED = "answered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"  # its turn ended or is gone: nothing can take an answer any more


TERMINAL: Final = frozenset({QuestionStatus.ANSWERED, QuestionStatus.EXPIRED, QuestionStatus.CANCELLED})


class AnswerMode(StrEnum):
    CALLBACK = "callback"  # into the held `AskUserQuestion` callback
    MESSAGE = "message"  # the callback was gone (hold elapsed, restart): the turn's next input


# --- the question -----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Option:
    label: str = field(repr=False)
    description: str = field(default="", repr=False)


@dataclass(frozen=True, slots=True)
class AgentQuestion:
    """One of an `AskUserQuestion`'s 1–4 questions, its texts already scrubbed (`questions_json` ✱)."""

    text: str = field(repr=False)
    header: str = field(default="", repr=False)
    options: tuple[Option, ...] = ()
    multi_select: bool = False


def _str(raw: JsonValue) -> str:
    return " ".join(raw.split()) if isinstance(raw, str) else ""


def _items(raw: JsonValue) -> list[JsonObject]:
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def parse_questions(raw: Iterable[Mapping[str, JsonValue]], scrub: Callable[[str], str]) -> tuple[AgentQuestion, ...]:
    """The agent's `questions` input, every text scrubbed. A malformed entry is skipped, never raised on: the agent's
    turn is waiting, and the owner still gets whatever could be read."""
    found: list[AgentQuestion] = []
    for item in raw:
        text = _str(item.get("question"))
        if not text:
            continue
        options = tuple(
            Option(scrub(label), scrub(_str(option.get("description"))))
            for option in _items(item.get("options"))
            if (label := _str(option.get("label")))
        )
        header = _str(item.get("header"))
        found.append(AgentQuestion(scrub(text), scrub(header), options, item.get("multiSelect") is True))
    return tuple(found)


def questions_json(questions: Sequence[AgentQuestion]) -> JsonValue:
    """`questions.questions_json`, in the agent's own shape."""
    return [
        {
            "question": question.text,
            "header": question.header,
            "options": [{"label": option.label, "description": option.description} for option in question.options],
            "multiSelect": question.multi_select,
        }
        for question in questions
    ]


def questions_from_json(raw: JsonValue) -> tuple[AgentQuestion, ...]:
    return parse_questions(_items(raw), str)


def _option_lines(question: AgentQuestion) -> list[BotText]:
    lines = [
        S.AGENT_QUESTION_OPTION_DESCRIBED(number=number, label=option.label, description=option.description)
        if option.description
        else S.AGENT_QUESTION_OPTION(number=number, label=option.label)
        for number, option in enumerate(question.options, start=1)
    ]
    if question.multi_select and len(question.options) > 1:
        lines.append(S.AGENT_QUESTION_MULTI())
    return lines


def question_text(project: str, questions: Sequence[AgentQuestion]) -> BotText:
    """SPEC §3.2.2 step 3: "BonviHR · Savol: …", then "1. <label> — <description>" per option and how to answer."""
    if len(questions) <= 1:
        asked = questions[0].text if questions else S.AGENT_QUESTION_NO_TEXT()
        options = _option_lines(questions[0]) if questions else []
        return join([S.AGENT_QUESTION(project=project, question=asked), *options, S.AGENT_QUESTION_HINT()])
    lines = [S.AGENT_QUESTIONS(project=project, count=len(questions))]
    for number, question in enumerate(questions, start=1):
        if question.header:
            lines.append(S.AGENT_QUESTION_ITEM_HEADED(number=number, header=question.header, question=question.text))
        else:
            lines.append(S.AGENT_QUESTION_ITEM(number=number, question=question.text))
        lines.extend(_option_lines(question))
    return join([*lines, S.AGENT_QUESTIONS_HINT()])


# --- what the owner said ----------------------------------------------------------------------------------------
def answer_text(item: TurnInput) -> str:
    """What the owner said in the burst in his own words, one line per part: texts, captions and transcripts. A voice
    note that was not understood is left out (its marker is no answer), and so is what he forwarded: someone else's
    words answer no question and approve no decision (NFR-SEC-09, `TurnInput.own_parts`)."""
    said = (part.text for part in item.own_parts if part.transcript not in _UNHEARD)
    return "\n".join(text.strip() for text in said if text and text.strip())


def spoken_text(item: TurnInput) -> str:
    """The transcripts alone, for the voice echo (UC-05)."""
    said = (part.transcript_text for part in item.parts if part.transcript is TranscriptState.DONE)
    return " ".join(" ".join(text.split()) for text in said if text)


def reply_to_of(item: TurnInput) -> int | None:
    """The bot message the burst replies to (its first reply in the owner's own words)."""
    return item.own_reply_to


def _without_names(text: str, names: Iterable[str]) -> str:
    """`text` folded, with the project names it addresses taken out ("BonviHR'ga 2" → "'ga 2")."""
    folded = fold(text)
    for name in sorted({fold(name) for name in names if name.strip()}, key=len, reverse=True):
        folded = re.sub(rf"(?<![\w']){re.escape(name)}", " ", folded)
    return folded


def _words(text: str, names: Iterable[str]) -> list[str]:
    return list(tokens(_without_names(text, names)))


def _content(words: Iterable[str]) -> list[str]:
    return [word for word in words if word not in _FILLER]


def _number(word: str) -> int | None:
    if (match := _NUMBER.fullmatch(word)) is not None:
        return int(match.group(1))
    if word.startswith(_LAST_ROOTS):
        return _LAST
    for root, number in _ORDINAL_ROOTS.items():
        if word.startswith(root):
            return number
    return _NUMBER_WORDS.get(word)


def _numbers(content: Sequence[str]) -> list[int] | None:
    """The numbers the words are, when they are nothing but numbers."""
    numbers = [_number(word) for word in content]
    if not numbers or any(number is None for number in numbers):
        return None
    return [number for number in numbers if number is not None]


def _word_matches(said: str, option_word: str) -> bool:
    if said == option_word:
        return True
    shorter = min(len(said), len(option_word))
    if shorter >= PREFIX_MIN and (said.startswith(option_word) or option_word.startswith(said)):
        return True
    return shorter >= TYPO_MIN and within_one_typo(said, option_word)


def _option_words(option: Option) -> list[str]:
    return _content(tokens(option.label))


def _label_index(text: str, question: AgentQuestion, names: Iterable[str]) -> int | None:
    """The option `text` names: its label exactly, or loosely — every word said belongs to that option and to no
    other ("postgres" → "PostgreSQL (recommended)"; "postgres emas" names none)."""
    said = _content(_words(text, names))
    if not said:
        return None
    exact = [index for index, option in enumerate(question.options) if _option_words(option) == said]
    if len(exact) == 1:
        return exact[0]
    loose = [
        index
        for index, option in enumerate(question.options)
        if (words := _option_words(option)) and all(any(_word_matches(w, o) for o in words) for w in said)
    ]
    return loose[0] if len(loose) == 1 else None


@dataclass(frozen=True, slots=True)
class OutOfRange:
    """A number past the question's options: told, the question stays open."""

    count: int


@dataclass(frozen=True, slots=True)
class _Pick:
    value: str
    chose: bool


def _by_numbers(said: str, question: AgentQuestion, numbers: Sequence[int]) -> _Pick | OutOfRange:
    """ "2" → its label; "1, 3" → both labels for a multi-select; a number past the options is out of range."""
    options = question.options
    if any(number != _LAST and not 1 <= number <= len(options) for number in numbers):
        return OutOfRange(len(options))
    chosen = list(dict.fromkeys(len(options) if number == _LAST else number for number in numbers))
    if len(chosen) == 1 or question.multi_select:
        return _Pick(", ".join(options[number - 1].label for number in chosen), chose=True)
    return _Pick(said, chose=False)  # "1 2" for a single choice: his words, as said


def _labels(said: str, question: AgentQuestion, names: Iterable[str]) -> list[str]:
    """The options his words name: one, or for a multi-select each part of "PostgreSQL va Redis"."""
    spellings = tuple(names)
    if (index := _label_index(said, question, spellings)) is not None:
        return [question.options[index].label]
    if not question.multi_select:
        return []
    parts = [part for part in _CHOICE_JOINERS.split(said) if part.strip()]
    indexes = [_label_index(part, question, spellings) for part in parts]
    if len(parts) < 2 or any(index is None for index in indexes):
        return []
    return list(dict.fromkeys(question.options[index].label for index in indexes if index is not None))


def _pick(text: str, question: AgentQuestion, names: Iterable[str]) -> _Pick | OutOfRange:
    """One question's answer from one segment of the owner's words."""
    said = text.strip()
    if not question.options:
        return _Pick(said, chose=False)
    numbers = _numbers(_content(_words(said, names)))
    if numbers is not None:
        return _by_numbers(said, question, numbers)
    labels = _labels(said, question, names)
    return _Pick(", ".join(labels), chose=True) if labels else _Pick(said, chose=False)


@dataclass(frozen=True, slots=True)
class Answer:
    """The owner's answer as the agent gets it: `answers` maps each question's text to the chosen label(s) or his
    own words (the `AskUserQuestion` result shape); `shown` is what he is told went."""

    answers: Mapping[str, str] = field(repr=False)
    shown: str = field(repr=False)
    chose: bool  # every question got an option (a number or its text), not free words


def _segments(text: str, count: int, names: Iterable[str]) -> list[str] | None:
    """One answer per question: lines, else `;`, else `,`, else as many bare numbers as questions."""
    for splitter in (_LINES, _SEMICOLONS, _COMMAS):
        parts = [part for part in splitter.split(text.strip()) if part]
        if len(parts) == count:
            return [_QUESTION_MARKER.sub("", part) for part in parts]
    content = _content(_words(text, names))
    if len(content) == count and _numbers(content) is not None:
        return content
    return None


def answer_of(text: str, questions: Sequence[AgentQuestion], *, names: Iterable[str] = ()) -> Answer | OutOfRange:
    """What the owner chose (module docstring). `names` are the project names he addressed, left out of the choice."""
    spellings = tuple(names)
    said = text.strip()
    if not questions:
        return Answer({}, said, chose=False)
    if len(questions) == 1:
        segments: list[str] | None = [said]
    else:
        segments = _segments(said, len(questions), spellings)
    if segments is None:
        return Answer({question.text: said for question in questions}, said, chose=False)
    picks: list[_Pick] = []
    for segment, question in zip(segments, questions, strict=True):
        pick = _pick(segment, question, spellings)
        if isinstance(pick, OutOfRange):
            return pick
        picks.append(pick)
    answers = {question.text: pick.value for question, pick in zip(questions, picks, strict=True)}
    return Answer(answers, "; ".join(pick.value for pick in picks), chose=all(pick.chose for pick in picks))


def picks_option(text: str, questions: Sequence[AgentQuestion], *, names: Iterable[str] = ()) -> bool:
    """The words choose an option of these questions (a number in range, or an option's text)."""
    answer = answer_of(text, questions, names=names)
    return isinstance(answer, Answer) and answer.chose and any(question.options for question in questions)


def is_bare_number(text: str, *, names: Iterable[str] = ()) -> bool:
    """ "2", "ikkinchisi", "1, 3": numbers and nothing else, answer-shaped whatever the options."""
    return _numbers(_content(_words(text, names))) is not None


def says_more_than_names(text: str, names: Iterable[str]) -> bool:
    """Anything besides the project names ("BonviHR" alone answers nothing)."""
    return bool(_words(text, names))


def answer_json(answer: Answer) -> JsonValue:
    """`questions.answer_json` ✱ (the texts are the owner's redacted words and the scrubbed labels)."""
    return {"answers": dict(answer.answers), "shown": answer.shown, "chose": answer.chose}


def answer_from_json(raw: JsonValue) -> Answer:
    data = raw if isinstance(raw, dict) else {}
    answers = data.get("answers")
    pairs = (
        {key: value for key, value in answers.items() if isinstance(value, str)} if isinstance(answers, dict) else {}
    )
    shown = data.get("shown")
    return Answer(pairs, shown if isinstance(shown, str) else "; ".join(pairs.values()), data.get("chose") is True)


def answers_block(answer: Answer) -> str:
    """The answers for the agent when the callback is gone (`prompts/dev/questions_answer.md`): «question»: answer."""
    return "\n".join(f"«{question}»: {value}" for question, value in answer.answers.items())


# --- which question ---------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OpenQuestion:
    """A `questions` row still waiting for the owner, as binding sees it."""

    id: int
    project_id: str
    turn_id: int
    tool_use_id: str
    asked_at: datetime
    questions: tuple[AgentQuestion, ...]


@dataclass(frozen=True, slots=True)
class ReplyTarget:
    """What the bot message the owner replied to is about (`message_links`)."""

    question_id: int | None = None
    project_id: str | None = None


@dataclass(frozen=True, slots=True)
class Bound:
    question: OpenQuestion


@dataclass(frozen=True, slots=True)
class Ambiguous:
    """Answer-shaped, but it fits questions of several projects: ask which (`which_question`), in this order."""

    projects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NotAnAnswer:
    """Not for an open question: routing takes the message."""


type Binding = Bound | Ambiguous | NotAnAnswer


def newest(questions: Iterable[OpenQuestion]) -> OpenQuestion:
    return max(questions, key=lambda question: (question.asked_at, question.id))


def _projects(questions: Iterable[OpenQuestion]) -> tuple[str, ...]:
    ordered = sorted(questions, key=lambda question: (question.asked_at, question.id))
    return tuple(dict.fromkeys(question.project_id for question in ordered))


def bind_answer(  # noqa: PLR0911  # one return per binding rule
    text: str,
    open_questions: Sequence[OpenQuestion],
    *,
    reply: ReplyTarget | None = None,
    mentioned: Sequence[str] = (),
    names: Sequence[str] = (),
    decisions_pending: bool = False,
) -> Binding:
    """Which open question an owner message answers (module docstring, rules 1–6). `mentioned` are the projects it
    names, `names` their spellings (ids and aliases); `decisions_pending`: a gate decision waits too, so a question is
    not the only open item."""
    if not open_questions:
        return NotAnAnswer()
    if reply is not None and reply.question_id is not None:
        replied = [question for question in open_questions if question.id == reply.question_id]
        return Bound(replied[0]) if replied else NotAnAnswer()  # an answered question: the reply routes as a message
    waiting_projects = _projects(open_questions)
    if mentioned:
        if not says_more_than_names(text, names):
            return NotAnAnswer()
        waiting = [project for project in dict.fromkeys(mentioned) if project in waiting_projects]
        if len(waiting) == 1:
            return Bound(newest(question for question in open_questions if question.project_id == waiting[0]))
        return Ambiguous(tuple(waiting)) if waiting else NotAnAnswer()
    if reply is not None and reply.project_id is not None:
        theirs = [question for question in open_questions if question.project_id == reply.project_id]
        return Bound(newest(theirs)) if theirs else NotAnAnswer()
    if len(open_questions) == 1 and not decisions_pending:
        return Bound(open_questions[0])
    picked = [question for question in open_questions if picks_option(text, question.questions, names=names)]
    pool = picked or (list(open_questions) if is_bare_number(text, names=names) else [])
    if pool:
        projects = _projects(pool)
        return Bound(newest(pool)) if len(projects) == 1 else Ambiguous(projects)
    if len(waiting_projects) == 1 and not decisions_pending:
        return Bound(newest(open_questions))
    return NotAnAnswer()


def which_question(projects: Sequence[str], answer: str) -> BotText:
    """UC-12 F: "«2» — BonviHR savoligami yoki HRMentor savoligami?". The order is what "2" or "birinchisi" means
    (routing's clarification reads the reply)."""
    quoted = shorten(" ".join(answer.split()), EXCERPT_MAX)
    if len(projects) == 2:
        return S.QUESTION_WHICH_TWO(answer=quoted, first=projects[0], second=projects[1])
    return S.QUESTION_WHICH_MANY(answer=quoted, projects=", ".join(projects))


def answered_text(project: str, answer: Answer, *, spoken: str, held: bool) -> BotText:
    """The owner's confirmation: what went to which project; a voice answer is echoed (UC-05); an answer that went in
    as a message says so."""
    shown = shorten(" ".join(answer.shown.split()), EXCERPT_MAX)
    if spoken:
        echo = shorten(spoken, VOICE_ECHO_MAX)
        head = S.QUESTION_ANSWERED_VOICE(text=echo, project=project, answer=shown)
    else:
        head = S.QUESTION_ANSWERED(project=project, answer=shown)
    return head if held else join([head, S.QUESTION_ANSWERED_AS_MESSAGE()], " ")


# --- retention --------------------------------------------------------------------------------------------------
def question_cutoff(now: datetime) -> datetime:
    return now - QUESTION_KEEP
