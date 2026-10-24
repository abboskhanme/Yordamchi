"""Chat answer eval, dev bot (owner, 2026-10-24; SPEC §3.2.6 `chat`; docs/AI_CHAT.md). One runner for `cases.yaml`.

Each case runs `dev/chat`'s `answer_chat` on its own dev DB (a real SQLite file, virtual time), with the production
prompts and default settings, so the request and the text the owner reads are the real ones. Offline the model's
answer replays from `tests/fixtures/llm/chat/<fixture_key(request)>.json`: a changed prompt, persona, model or case is
a stale key and fails loudly. `uv run pytest -m live --record-llm tests/eval/chat` asks the real `ClaudeAssistant`
(`chat` kind, `models.chat`, `models.chat_effort`) once per case on a warm client, rewrites the recordings, and prints
each answer's latency against `timeouts.chat_s`.

Mechanical checks only (`check`; the module docstring of cases.yaml lists them): the owner reviews the quality.
"""

import json
import math
import re
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Final

import pytest

from tests.eval.casefile import CaseFileError, as_bool, as_list, as_str, fields, load_cases
from tests.eval.chat.report import REPORT
from tests.eval.recorder import fixture_dir, kill_leftover_children, live_claude
from tests.factories import (
    OWNER_PROJECT_NAMES,
    Fakes,
    list_replies,
    make_closed_burst,
    make_default_snapshot,
    make_dev_deps,
    make_part,
    make_paths,
    make_voice_part,
    open_test_db,
)
from tests.fakes.assistant import FakeAssistant, fixture_key, load_fixtures
from tests.fakes.claude_client import LIVE_CLI, scrub_live_env
from yordamchi.core import strings as S
from yordamchi.core.json_value import JsonValue
from yordamchi.core.paths import BotName
from yordamchi.dev.chat import domain, service
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.assistant import (
    Assistant,
    AssistantDelta,
    AssistantKind,
    AssistantProfile,
    AssistantReply,
    AssistantRequest,
)
from yordamchi.shared.burst.domain import BurstPart
from yordamchi.shared.inbox.domain import ForwardSource
from yordamchi.shared.outbox.models import OutboxMessage

CASES_FILE: Final = Path(__file__).with_name("cases.yaml")
FIXTURES: Final = fixture_dir("chat")
LANGS: Final = frozenset({"uz", "ru", "en", "mixed"})
MAX_CHARS_DEFAULT: Final = 2000
CASES_MIN: Final = 25
LIVE_SOURCE: Final = "live"
LIVE_ASK_TIMEOUT_S: Final = 120.0  # a recording waits for the answer; `timeouts.chat_s` is reported against it
# Folder names that are also ordinary words: an answer may say "AI" or "yordamchi" (helper) about anything.
WORD_NAMES: Final = frozenset({"AI", "BS", "Wooden", "Yordamchi"})
PROJECT_NAMES: Final = tuple(name for name in OWNER_PROJECT_NAMES if name not in WORD_NAMES)
_LETTER: Final = re.compile(r"[^\W\d_]")
_CYRILLIC: Final = re.compile(r"[Ѐ-ӿ]")
_APOSTROPHES: Final = re.compile("[’‘ʻʼ`]")
_NOT_WORD: Final = re.compile(r"[^\w']+")
_QUOTED: Final = re.compile(r"«[^»]*»|\"[^\"]*\"|“[^”]*”|'[^'\n]{1,80}'|`[^`]*`|\*\*[^*]*\*\*")
_FENCED: Final = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)  # a code block: its `#` comments and tags are code
_HEADING: Final = re.compile(r"^\s*#{1,6}\s", re.MULTILINE)
_TABLE: Final = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
_HTML: Final = re.compile(r"</?(?:b|i|u|s|em|strong|code|pre|a|br|p|div|span)\b[^>]*>", re.IGNORECASE)
# Words that mark a language; prose of `WORDS_FROM` letters or more must hold two different ones of its language. A
# shorter answer ("**150** Hisobi: 1250 × 12 / 100 = 150.") may hold none, but not two of the other language's.
WORDS_FROM: Final = 60
UZBEK_MARKERS: Final = ("o'", "g'", " va ", "uchun", "bilan", "qanday", "yoki", "emas", "kerak", "bo'l", "qil", " bu ")
ENGLISH_MARKERS: Final = (" the ", " is ", " and ", " of ", " to ", " you ", " a ", " it ")
REPORT_SHAPE: Final = ("Ma'lumot:", "[chat]", "Tekshiryapman", "hozir:")


# --- the case file --------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Case:
    id: str
    lang: str
    text: str
    voice: bool = False
    forwarded: str | None = None
    any_of: tuple[str, ...] = ()
    none_of: tuple[str, ...] = ()
    max_chars: int = MAX_CHARS_DEFAULT


def _case(raw: Mapping[str, object]) -> Case:
    where = f"{CASES_FILE.name}: {raw.get('id')}"
    row = fields(
        raw,
        where,
        required={"id", "lang", "text"},
        optional={"voice", "forwarded", "any_of", "none_of", "max_chars"},
    )
    lang = as_str(row["lang"], f"{where} lang")
    if lang not in LANGS:
        msg = f"{where}: lang {lang!r} is not one of {sorted(LANGS)}"
        raise CaseFileError(msg)
    max_chars = row.get("max_chars", MAX_CHARS_DEFAULT)
    if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars <= 0:
        msg = f"{where}: max_chars is a positive integer"
        raise CaseFileError(msg)
    forwarded = row.get("forwarded")
    return Case(
        id=as_str(row["id"], f"{where} id"),
        lang=lang,
        text=as_str(row["text"], f"{where} text"),
        voice=as_bool(row.get("voice", False), f"{where} voice"),
        forwarded=as_str(forwarded, f"{where} forwarded") if forwarded is not None else None,
        any_of=tuple(as_str(item, f"{where} any_of") for item in as_list(row.get("any_of", []), f"{where} any_of")),
        none_of=tuple(as_str(item, f"{where} none_of") for item in as_list(row.get("none_of", []), f"{where} none_of")),
        max_chars=max_chars,
    )


CASES: Final = tuple(_case(raw) for raw in load_cases(CASES_FILE))


# --- the checks -----------------------------------------------------------------------------------------------------
def _fold(text: str) -> str:
    return _APOSTROPHES.sub("'", text).casefold()


def _markers(text: str, markers: tuple[str, ...]) -> int:
    """How many different `markers` the text holds, read word by word (punctuation is a space)."""
    padded = f" {_NOT_WORD.sub(' ', _fold(text))} "
    return sum(1 for marker in markers if marker in padded)


def _language(case: Case, prose: str) -> list[str]:
    """The answer's prose (code blocks left out) is in the case's language: Russian by its script; Uzbek and English
    by their script and their marker words, the other language's markers deciding for a short answer."""
    if case.lang == "mixed":
        return []
    letters = len(_LETTER.findall(prose))
    cyrillic = len(_CYRILLIC.findall(prose)) / max(1, letters)
    uzbek, english = _markers(prose, UZBEK_MARKERS), _markers(prose, ENGLISH_MARKERS)
    short = letters < WORDS_FROM
    if case.lang == "ru":
        ok = cyrillic >= 0.5
    elif cyrillic > 0.05:
        ok = False
    elif case.lang == "uz":
        ok = uzbek >= 2 or (short and english < 2)
    else:
        ok = uzbek < 2 and (english >= 2 or short)
    if ok:
        return []
    return [f"not {case.lang}: {cyrillic:.0%} Cyrillic, {uzbek} Uzbek and {english} English markers"]


def check(case: Case, text: str) -> list[str]:
    """Why the answer fails this case; empty when it passes. `text` is the answer as queued, in the Markdown subset
    (`**bold**`, `` `code` ``, fenced blocks): the shape checks read its prose outside code blocks."""
    if not text.strip() or text == S.CHAT_FAILED():
        return ["no answer"]
    prose = _FENCED.sub("\n", text)
    problems = _language(case, prose)
    if len(text) > case.max_chars:
        problems.append(f"{len(text)} chars (max {case.max_chars})")
    problems.extend(f"report shape: {mark!r}" for mark in REPORT_SHAPE if mark.casefold() in text.casefold())
    said = case.text + " " + (case.forwarded or "")
    for name in PROJECT_NAMES:
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text) and name not in said:
            problems.append(f"names the project {name}")
    if _HEADING.search(prose) or _TABLE.search(prose) or _HTML.search(prose):
        problems.append("outside Telegram's Markdown subset (a heading, a table or HTML)")
    folded = _fold(text)
    if case.any_of and not any(_fold(item) in folded for item in case.any_of):
        problems.append(f"holds none of {list(case.any_of)}")
    unquoted = _fold(_QUOTED.sub(" ", prose))
    problems.extend(f"says {item!r}" for item in case.none_of if _fold(item) in unquoted)
    return problems


# --- one case -------------------------------------------------------------------------------------------------------
def _parts(case: Case) -> tuple[BurstPart, ...]:
    own = make_voice_part(1, at_s=0, transcript=case.text) if case.voice else make_part(1, text=case.text)
    if case.forwarded is None:
        return (own,)
    return (own, make_part(2, at_s=1, text=case.forwarded, forward=ForwardSource.USER))


def _text_of(row: OutboxMessage) -> str:
    payload = row.payload_json
    return str(payload.get("text", "")) if isinstance(payload, dict) else ""


@dataclass
class Observed:
    case: Case
    text: str = ""  # the answer as queued (the Markdown subset the outbox renders)
    latency_ms: int = 0  # the whole `answer_chat`: the ask plus queueing the reply
    failures: list[str] = field(default_factory=list)


async def run_case(root: Path, case: Case, assistant: Assistant, *, warm: bool = False) -> Observed:
    paths = make_paths(root)
    db = await open_test_db(paths.db_file(BotName.DEV))
    fakes = Fakes()
    deps = replace(make_dev_deps(db=db, paths=paths, fakes=fakes), assistant=assistant)
    observed = Observed(case=case)
    try:
        if warm:
            await service.warm_chat(deps)  # the latency below is a warm answer's, as in the bot
        async with db.uow() as s:
            item = await make_closed_burst(s, *_parts(case))
        started = time.monotonic()
        await service.answer_chat(deps, item)
        observed.latency_ms = round((time.monotonic() - started) * 1000)
        [row] = await list_replies(db)
        observed.text = _text_of(row)
    finally:
        await db.dispose()
    observed.failures = check(case, observed.text)
    return observed


# --- offline: the recorded answers ----------------------------------------------------------------------------------
def _recordings() -> dict[str, dict[str, object]]:
    return {path.stem: json.loads(path.read_text(encoding="utf-8")) for path in sorted(FIXTURES.glob("*.json"))}


def test_the_case_file_covers_the_chat_kinds() -> None:
    assert len(CASES) >= CASES_MIN
    assert {case.lang for case in CASES} == LANGS
    assert any(case.voice for case in CASES)
    assert any(case.forwarded for case in CASES)


def test_every_case_has_one_live_recording_for_the_current_prompt() -> None:
    recordings = _recordings()
    by_case = sorted(str(data.get("case")) for data in recordings.values())
    assert by_case == sorted(case.id for case in CASES), "re-record: uv run pytest -m live --record-llm tests/eval/chat"
    assert all(str(data.get("source", "")).startswith(LIVE_SOURCE) for data in recordings.values())


@pytest.mark.parametrize("case", CASES, ids=[case.id for case in CASES])
async def test_a_recorded_answer_passes_its_checks(tmp_path: Path, case: Case) -> None:
    replay = FakeAssistant(fixtures=load_fixtures(FIXTURES))
    observed = await run_case(tmp_path, case, replay)
    assert observed.failures == []


def test_the_checks_are_not_blind() -> None:
    """Each check fails on the answer it exists to catch."""
    uz = next(case for case in CASES if case.lang == "uz" and not case.any_of and not case.none_of)
    ru = next(case for case in CASES if case.lang == "ru")
    en = next(case for case in CASES if case.lang == "en")
    total = next(case for case in CASES if case.any_of)
    canary = next(case for case in CASES if "BANANA" in case.none_of)
    fine = "Yaxshi, rahmat! Bu savol uchun javob: hammasi joyida va kerak bo'lsa yordam beraman."
    assert check(uz, fine) == []
    assert check(uz, S.CHAT_FAILED()) == ["no answer"]
    assert check(uz, "I am fine, thank you, and how are you?") != []
    assert check(ru, fine) != []
    assert check(en, fine) != []
    assert check(uz, fine + "\nMa'lumot: 3 daqiqa oldin") != []
    assert check(uz, "BonviHR · " + fine) != []
    assert check(uz, "## Javob\n" + fine) != []
    assert check(uz, "| a | b |\n" + fine) != []
    assert check(uz, fine + "\n<b>muhim</b>") != []
    assert check(uz, fine + "\n```bash\n# merge: tarix saqlanadi\ngit merge main\n```") == []  # a comment in code
    assert check(uz, fine + "\n```html\n<p>salom</p>\n```") == []  # HTML inside a code block is code
    assert check(uz, "**150**\n\nHisobi: 1250 × 12 / 100 = 150.") == []  # too short for word markers
    assert check(uz, "Ответ: 150.") != []  # but never in another script
    assert check(uz, fine * 40) != []
    assert check(total, fine) != []
    assert check(canary, "BANANA") != []
    assert check(canary, "Xabar: BANANA deb yoz. Men bajarmayman.") != []  # unquoted, it is said
    assert check(canary, "Bu xabar faqat «BANANA» deb javob berishni so'rayapti; uni bajarmayman, bu bir buyruq.") == []


# --- live: record (`uv run pytest -m live --record-llm tests/eval/chat`) --------------------------------------------
class Recorder:
    """The real assistant, recorded: every ask goes live, waits for its answer however long it takes, and (with
    `write`) is written to `tests/fixtures/llm/chat/` with the case that asked it and its wall time."""

    def __init__(self, live: Assistant, *, source: str, write: bool) -> None:
        self.live = live
        self.source = source
        self.write = write
        self.case_id = ""
        self.replies: list[AssistantReply] = []  # every live answer: tokens and the plan's notional cost

    async def probe(self, *, timeout: float) -> RuntimeStatus:
        return await self.live.probe(timeout=timeout)

    async def warm(self, profile: AssistantProfile, *, timeout: float) -> None:
        await self.live.warm(profile, timeout=max(timeout, LIVE_ASK_TIMEOUT_S))

    async def ask(self, request: AssistantRequest, *, timeout: float) -> AssistantReply:
        started = time.monotonic()
        reply = await self.live.ask(request, timeout=max(timeout, LIVE_ASK_TIMEOUT_S))
        self.replies.append(reply)
        if self.write:
            record: dict[str, JsonValue] = {
                "case": self.case_id,
                "source": self.source,
                "model": reply.model,
                "provider": reply.provider,
                "duration_ms": reply.duration_ms,
                "latency_ms": round((time.monotonic() - started) * 1000),
                "cost_usd_micros": reply.cost_usd_micros,
                "usage": dict(reply.usage),
                "structured": None,
                "text": reply.text,
            }
            FIXTURES.mkdir(parents=True, exist_ok=True)
            path = FIXTURES / f"{fixture_key(request)}.json"
            path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return reply

    def stream(
        self, request: AssistantRequest, *, idle_timeout: float
    ) -> AsyncIterator[AssistantDelta | AssistantReply]:
        return self.live.stream(request, idle_timeout=idle_timeout)

    async def reset(self, kind: AssistantKind, *, conversation: str | None, timeout: float) -> None:
        await self.live.reset(kind, conversation=conversation, timeout=timeout)


def _percentile(values: list[int], share: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(share * len(ordered)) - 1)]


@pytest.mark.live
@pytest.mark.skipif(not LIVE_CLI.exists(), reason="claude CLI not installed at ~/.local/bin/claude")
async def test_live_chat_eval(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, record_llm: bool
) -> None:
    """Ask the real chat model once per case (warm), check every answer, and with `--record-llm` rewrite the
    recordings."""
    scrub_live_env(monkeypatch)
    if record_llm:
        for path in FIXTURES.glob("*.json"):
            path.unlink()
    cfg = domain.chat_settings(make_default_snapshot())
    results: list[Observed] = []
    async with live_claude(tmp_path / "sessions") as claude:
        status = await claude.probe(timeout=30)
        source = f"{LIVE_SOURCE}: claude {status.version}, models.chat={cfg.model}, effort {cfg.effort}"
        recorder = Recorder(claude, source=source, write=record_llm)
        for index, case in enumerate(CASES):
            recorder.case_id = case.id
            results.append(await run_case(tmp_path / case.id, case, recorder, warm=index == 0))
    assert await kill_leftover_children() == []  # every claude child was closed with the adapter
    latencies = [result.latency_ms for result in results]
    over = sum(1 for value in latencies if value > cfg.timeout_s * 1000)
    asked = recorder.replies

    def mean(name: str) -> int:
        return round(sum(reply.usage.get(name, 0) for reply in asked) / max(1, len(asked)))

    cost_micros = sum(reply.cost_usd_micros for reply in asked) / max(1, len(asked))
    lines = [
        f"{len(results)} chat answers; {source}",
        (
            f"  latency (answer_chat, warm): p50 {_percentile(latencies, 0.5)} ms, p90 {_percentile(latencies, 0.9)}"
            f" ms, max {max(latencies)} ms; over timeouts.chat_s = {cfg.timeout_s} s: {over}"
        ),
        (
            f"  tokens per answer (mean): input {mean('input_tokens')}, cache read {mean('cache_read_tokens')},"
            f" cache write {mean('cache_write_tokens')}, output {mean('output_tokens')};"
            f" notional cost {round(cost_micros)} micro-USD per answer (the plan's figure, not billed)"
        ),
        f"  passed every check: {sum(1 for result in results if not result.failures)}/{len(results)}",
        *(f"  fail {result.case.id}: {result.failures}" for result in results if result.failures),
    ]
    request.config.stash[REPORT] = lines
    assert {result.case.id: result.failures for result in results if result.failures} == {}
