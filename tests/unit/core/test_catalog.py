"""`core/strings.py` as a whole (T35; CONVENTIONS §3, §9, §10; NFR-LANG-01): the catalog is the bot's only UI.

- every entry is read by `src/` (dead ones are reported as a warning, each with its reason in `DEAD_ALLOWED`);
- every keyed family has exactly one entry per value of the enum that keys it;
- no English leaks into owner text (a heuristic: English words and non-Uzbek spelling, minus technical terms);
- a message about one project starts with its name;
- nothing the bot sends unsplit (an edit, a direct send) can outgrow one Telegram message.

The error catalog itself (`ErrorCode` ↔ `S.ERRORS`) is `test_errors_and_strings.py`.
"""

import ast
import re
import warnings
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import Final

import pytest

from tests.factories import Fakes, make_bash_call, make_gate_scope, projects_root_of
from yordamchi.core import errors
from yordamchi.core import strings as S
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.digest import domain as digest
from yordamchi.dev.gate import service as gate
from yordamchi.dev.turns import domain as turns
from yordamchi.dev.turns.domain import ProgressKind, SuiteStatus
from yordamchi.shared.integrations.domain import IntegrationKind, IntegrationStatus
from yordamchi.shared.outbox.domain import ELLIPSIS, TEXT_LIMIT, fit_one, tg_len

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "src" / "yordamchi"
CATALOG_FILE = SRC / "core" / "strings.py"
ERRORS_FILE = SRC / "core" / "errors.py"


def catalog() -> dict[str, S.Msg]:
    """Every catalog entry: each module-level `Msg`, and each value of a keyed family as `FAMILY[key]`."""
    found: dict[str, S.Msg] = {}
    for name, value in vars(S).items():
        if isinstance(value, S.Msg):
            found[name] = value
        elif isinstance(value, Mapping) and name.isupper() and name != "ERRORS":  # ERRORS holds the ERR_ entries again
            found.update({f"{name}[{key!r}]": entry for key, entry in value.items() if isinstance(entry, S.Msg)})
    return found


# --- every entry is used (dead entries are reported, not deleted) ---------------------------------------------------
# Name → why nothing in src/ reads it. The test fails on a dead entry missing here and on a line that is no longer
# true, and lists the ones here as a warning on every run.
DEAD_ALLOWED: Final[Mapping[str, str]] = {
    "FALLBACK_LABEL": (
        "only tests read it: QA_FALLBACK_HEADER spells the same «(zaxira model)» label inline (T72, personal/qa)"
    ),
}
# Module-level `Msg` names that are not UPPER_SNAKE catalog names (CONVENTIONS §9), with why they exist.
ODD_NAMES: Final[Mapping[str, str]] = {
    "entry": (
        "the walrus target of the ERRORS comprehension leaks into the module (it is the last ERR_ entry again); "
        "drop it with the next strings.py change that is not wording-only"
    ),
}

type Sources = tuple[tuple[Path, ast.Module], ...]


@cache
def _src_trees() -> Sources:
    return tuple((path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(SRC.rglob("*.py")))


def catalog_reads(trees: Sources) -> set[str]:
    """The catalog names code reads (`S.NAME`, `strings.NAME`, `from yordamchi.core.strings import NAME`); a name in
    a comment or a docstring does not count."""
    read: set[str] = set()
    for path, tree in trees:
        if path == CATALOG_FILE:
            continue
        aliases = {"S"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "yordamchi.core.strings":
                read.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module == "yordamchi.core":
                aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "strings")
        read.update(
            node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in aliases
        )
    return read


def _is_code(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "ErrorCode"


def raised_codes(trees: Sources, errors_tree: ast.Module) -> set[str]:
    """`ErrorCode` names whose `S.ERRORS` message can reach the owner: an error class (in `errors_tree`) carrying the
    code is used in `trees`, or a call there is built with `ErrorCode.X` as its first argument. A code used only as a
    value (a voice attempt's `code`, a mapping key) raises nothing."""
    codes_of = {
        statement.name: {sub.attr for sub in ast.walk(statement) if isinstance(sub, ast.Attribute) and _is_code(sub)}
        for statement in errors_tree.body
        if isinstance(statement, ast.ClassDef)
    }
    used: set[str] = set()
    raised: set[str] = set()
    for path, tree in trees:
        if path in {ERRORS_FILE, CATALOG_FILE}:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
            first = node.args[0] if isinstance(node, ast.Call) and node.args else None
            if isinstance(first, ast.Attribute) and _is_code(first):
                raised.add(first.attr)
    for name, codes in codes_of.items():
        if name in used:
            raised |= codes
    return raised


def dead_entries() -> set[str]:
    """Module-level entries and keyed families nothing in src/ reads. An `ERR_` entry is read through `S.ERRORS`
    whenever its code is raised, and an entry bound to two names (`ERR_MEDIA_TOO_LARGE = FILE_TOO_LARGE_IN`) is read
    when either name is."""
    trees = _src_trees()
    read = catalog_reads(trees)
    raised = raised_codes(trees, ast.parse(ERRORS_FILE.read_text(encoding="utf-8")))
    bound = {
        name: value
        for name, value in vars(S).items()
        if isinstance(value, S.Msg) or (isinstance(value, Mapping) and name.isupper() and name != "ERRORS")
    }
    live = {
        id(value)
        for name, value in bound.items()
        if name in read or (name.startswith("ERR_") and name.removeprefix("ERR_") in raised)
    }
    return {name for name, value in bound.items() if id(value) not in live}


@pytest.mark.filterwarnings("default:catalog entries nothing reads:UserWarning")
def test_every_catalog_entry_is_read_by_src_or_allowlisted_with_a_reason() -> None:
    dead = dead_entries()
    unexplained = sorted(dead - DEAD_ALLOWED.keys())
    assert unexplained == [], f"catalog entries nothing in src/ reads: {unexplained}; use them or add a reason"
    stale = sorted(DEAD_ALLOWED.keys() - dead)
    assert stale == [], f"DEAD_ALLOWED lines that are no longer true (now read, or gone): {stale}"
    assert all(reason.strip() for reason in DEAD_ALLOWED.values())
    if dead:
        listed = "; ".join(f"{name} — {DEAD_ALLOWED[name]}" for name in sorted(dead))
        warnings.warn(f"catalog entries nothing reads (allowlisted): {listed}", UserWarning, stacklevel=1)


def test_catalog_names_are_upper_snake() -> None:
    odd = {
        name
        for name, value in vars(S).items()
        if isinstance(value, S.Msg) and not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
    }
    assert odd == set(ODD_NAMES), "name catalog entries <FEATURE>_<WHAT>, or explain the name in ODD_NAMES"


def test_the_dead_entry_check_counts_code_reads_and_raised_codes_only() -> None:
    module = Path("x.py")
    errors_tree = ast.parse(
        "class Raised(DomainError):\n    def __init__(self):\n        super().__init__(ErrorCode.RAISED)\n"
        "class Unused(DomainError):\n    def __init__(self):\n        super().__init__(ErrorCode.UNUSED)\n"
    )
    trees: Sources = (
        (
            module,
            ast.parse(
                '"""S.IN_A_DOCSTRING is no read."""\n'
                "from yordamchi.core import strings as T\n"
                "from yordamchi.core.strings import IMPORTED\n"
                "# S.IN_A_COMMENT\n"
                "text = T.READ_BY_ALIAS() + S.READ()\n"
                "LABELS = {ErrorCode.ONLY_A_KEY: 1}\n"
                "raise Raised()\n"
                "DomainError(ErrorCode.BUILT_DIRECTLY)\n"
            ),
        ),
    )
    assert catalog_reads(trees) == {"READ_BY_ALIAS", "READ", "IMPORTED"}
    assert raised_codes(trees, errors_tree) == {"RAISED", "BUILT_DIRECTLY"}
    assert {"HOLAT_HEAD", "DECISION_QUESTIONS", "ERR_INTERNAL", "NOT_YET"} <= catalog_reads(_src_trees())
    assert "ERR_PROJECT_NOT_FOUND" not in catalog_reads(_src_trees())  # reached through S.ERRORS only …
    assert "ERR_PROJECT_NOT_FOUND" not in dead_entries()  # … because `ProjectNotFound` is raised


# --- keyed families are complete both ways ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("family", "keys"),
    [
        ("REPORT_TURN_STATES", {status.value for status in turns.ACTIVE}),
        ("REPORT_ENDED_STATES", {status.value for status in digest.RESULT_STATUSES} - {turns.TurnStatus.DONE.value}),
        ("TESTS_WORDS", {status.value for status in SuiteStatus}),
        ("INTEGRATION_KINDS", {kind.value for kind in IntegrationKind}),
        ("INTEGRATION_STATUSES", {status.value for status in IntegrationStatus}),
        ("SETTINGS_BOTS", {bot.value for bot in BotName}),
    ],
)
def test_a_keyed_family_has_one_entry_per_value_of_its_enum(family: str, keys: set[str]) -> None:
    """A missing key shows the owner a fallback word (or raises); an extra one is a dead entry."""
    assert set(getattr(S, family)) == keys


def test_server_reasons_are_keyed_by_error_codes() -> None:
    assert set(S.SERVERS_REASONS) <= {code.value for code in errors.ErrorCode}


# --- no English in owner text (NFR-LANG-01) -------------------------------------------------------------------------
# English words a message could slip in (most others trip NON_UZBEK below). Not here: the loans Uzbek writes as is
# (bot, server, limit, login, token, test, video, agent, variant, normal, internet) and English spellings of Uzbek
# words (men, ham, bor). Words of one letter are never checked.
_ENGLISH_TEXT: Final = """
    about add added after again all already also an and answer any approve approved are at available back be been
    before busy but by can cancel cancelled cannot change changed check checked click close closed connect connected
    could create created day days decision delete deleted denied did disabled do does done down downloaded each enter
    error errors expired fail failed file files finished first for found free from full get go group has have help here
    hour hours how if in information interrupted into invalid is it its just key keys last later less list loading
    message messages minute minutes missing more must my name new next no none not now of off ok old on once only open
    or other our out over page paused pending please previous project projects queued question questions ready
    received remove removed reply request restarted result results retry run running save saved second seconds see
    send sent session set settings should some sorry start started status steps still stop stopped success successful
    task tasks than thanks that the then there these this time to today too try turn turns unavailable unknown until
    up update updated usage use used user value very wait waiting warning was we were what when where which while who
    why will with without working would yes yet you your
"""
ENGLISH_WORDS: Final = frozenset(_ENGLISH_TEXT.split())
# Spelling Uzbek (Latin) does not use: w, a c outside "ch", th, ph, ck, ee, oo.
NON_UZBEK: Final = re.compile(r"w|c(?!h)|th|ph|ck|ee|oo")
# Technical terms, product and vendor names, commands and model names the catalog may use anywhere (lowercase).
TECH_TERMS: Final = frozenset(
    {
        "api",
        "botfather",  # Telegram's bot that issues tokens
        "ci",
        "claude",
        "claudemanager",
        "cli",
        "commit",
        "docker",
        "github",
        "google",
        "hook",  # git hooks
        "javascript",
        "localhost",
        "mac",
        "macos",
        "mcp",
        "oauth",
        "openai",
        "path",
        "self-update",
        "worktree",
    }
)
# A term allowed in one entry only, where it names something by its own (English) name.
ENTRY_TERMS: Final[Mapping[str, frozenset[str]]] = {
    "SERVERS_ISSUE_UNIT": frozenset({"failed"}),  # systemd's own state word, in parentheses after the Uzbek
    "SERVERS_ISSUE_CONTAINER_UNHEALTHY": frozenset({"unhealthy"}),  # Docker's health state, in parentheses
    "GATE_CONSEQUENCES['jump_only_host']": frozenset({"production"}),  # "production 1C": the host's own name
    "DECISION_QUESTIONS['force_push']": frozenset({"force"}),  # git's "force push"
    "DECISION_QUESTIONS['repo_public']": frozenset({"public"}),  # GitHub's visibility value, in parentheses
    # "Client ID Metadata Document": the OAuth draft's own name for what the server asks for
    "INTEGRATION_REASONS['oauth_client_metadata_needed']": frozenset({"client", "document"}),
    "ERR_OAUTH_CLIENT_METADATA_NEEDED": frozenset({"client", "document"}),
}
_PLACEHOLDER: Final = re.compile(r"\{[a-z_]+\}")
_CODE_SPAN: Final = re.compile(r"`[^`]*`")  # a command to type, quoted as code
_URL: Final = re.compile(r"\S*://\S*")
_PATH: Final = re.compile(r"(?<!\S)~?/\S*")  # "~/Documents/Projects'ni", "/var/log"
_TOKEN: Final = re.compile(r"[A-Za-z][A-Za-z0-9'._/-]*")
_IDENTIFIER: Final = re.compile(r"_|\w\.\w|\d|/$")  # a setting key, a file, a host, a key prefix, a folder


def _words(template: str) -> list[str]:
    """The lowercase words of a template that are prose: placeholders, code spans, URLs and identifiers removed; a
    hyphen or slash pair split ("commit/push"); a technical base before an Uzbek suffix checked too ("Mac'da")."""
    text = _PATH.sub(" ", _URL.sub(" ", _CODE_SPAN.sub(" ", _PLACEHOLDER.sub(" ", template))))
    words: list[str] = []
    for raw in _TOKEN.findall(text):
        token = raw.rstrip(".-'").lower()
        if _IDENTIFIER.search(token) or token in TECH_TERMS:
            continue
        for part in re.split(r"[-/]", token):
            base = part.split("'", 1)[0]
            if "'" in part and len(base) >= 3 and base[-1] not in "og":  # not oʻ/gʻ: an Uzbek suffix after a loan
                words.append(base)
            elif len(part) >= 2:
                words.append(part)
    return words


def english_in(name: str, template: str) -> list[str]:
    allowed = TECH_TERMS | ENTRY_TERMS.get(name, frozenset())
    return [
        word for word in _words(template) if word not in allowed and (word in ENGLISH_WORDS or NON_UZBEK.search(word))
    ]


def test_no_english_leaks_into_owner_text() -> None:
    leaks = {name: found for name, entry in catalog().items() if (found := english_in(name, entry.template))}
    assert leaks == {}


def test_the_english_heuristic_catches_a_leak_and_spares_technical_terms() -> None:
    assert english_in("X", "Please try again later.") == ["please", "try", "again", "later"]
    assert english_in("X", "Faylni tekshiring (check the key).") == ["check", "the", "key"]
    assert english_in("X", "{project}: commit'dan oldin `claude auth status` ni tekshiring, gate.ask_classes") == []
    assert english_in("X", "Mac'da Docker volume'larini o'chirmoqchiman") == []
    assert english_in("X", "So'rovlar limiti tugadi; o'z-o'zini yangilash (self-update)") == []
    assert english_in("X", "~/Documents/Projects'ni tekshiring; OpenRouter (sk-or-v1-…) kaliti") == []
    assert english_in("SERVERS_ISSUE_UNIT", "{unit} xizmati ishdan chiqqan (failed)") == []
    assert english_in("X", "{unit} xizmati ishdan chiqqan (failed)") == ["failed"]


def test_entry_terms_name_real_entries_and_each_term_is_needed() -> None:
    entries = catalog()
    for name, terms in ENTRY_TERMS.items():
        assert name in entries, name
        flagged_without_them = set(english_in("", entries[name].template))
        assert terms <= flagged_without_them, f"{name}: {sorted(terms - flagged_without_them)} need no allowance"


# --- a project message starts with the project's name (CONVENTIONS §10) ---------------------------------------------
PROJECT_PARAMS: Final = frozenset({"project", "to_project"})
# UC-10 AC: routing names its target as "→ BonviHR"; that arrow form counts as the project first.
ARROW: Final = "→ "
# A title in bold ("**BonviHR · Natija:** …") is read as "BonviHR · Natija: …": the project is still first.
BOLD: Final = "**"
# Entries with a project that legitimately do not open with it.
PROJECT_LATER: Final[Mapping[str, str]] = {
    "PROJECTS_ITEM": "one line of the «loyihalar» list, under its summary line",
    "DECISION_ITEM": "one item of WHICH_DECISION, a question about several projects",
    "REPORT_ALL_LINE": "one line of the cross-project report, bulleted under its header",
    "NEWPROJECT_RENAMED": "the second line of NEWPROJECT_PLANNING, which opens with the project",
    "VOICE_ECHO": "UC-05: the transcript is echoed first, then «→ project» names the target",
    "VOICE_ECHO_MERGED": "UC-05: the transcript is echoed first, then «→ project» names the target",
    "QUESTION_ANSWERED_VOICE": "UC-05: the transcript is echoed first, then «→ project» names the target",
}


def _opens_with_project(entry: S.Msg) -> bool:
    return any(
        entry.template.startswith((f"{{{param}}}", f"{ARROW}{{{param}}}", f"{BOLD}{{{param}}}"))
        for param in PROJECT_PARAMS
    )


def test_every_project_message_starts_with_the_project_name() -> None:
    entries = catalog()
    late = sorted(
        name
        for name, entry in entries.items()
        if entry.params & PROJECT_PARAMS and not _opens_with_project(entry) and name not in PROJECT_LATER
    )
    assert late == [], "start these with {project} (CONVENTIONS §10) or explain them in PROJECT_LATER"
    for name in PROJECT_LATER:
        assert name in entries, name
        assert entries[name].params & PROJECT_PARAMS, name
        assert not _opens_with_project(entries[name]), f"{name} opens with its project now: drop its PROJECT_LATER line"


# --- nothing outgrows one Telegram message unsplit (NFR-CAP-03; CONVENTIONS §10) -----------------------------------
FIELD_MAX: Final = 255  # the widest bounded value a catalog placeholder gets (a name, a code, a summary ≤ 200)


def test_no_entry_outgrows_one_message_with_column_sized_values() -> None:
    """A notice sent straight to the Messenger (gate questions, `guarded()` error notices, the startup notice) is
    never split: each entry, every value at its column's width, must fit one message."""
    too_long = {
        name: length
        for name, entry in catalog().items()
        if (length := tg_len(entry(**dict.fromkeys(entry.params, "x" * FIELD_MAX)))) > TEXT_LIMIT
    }
    assert too_long == {}


def test_the_progress_line_is_never_cut_by_the_edit() -> None:
    """The progress message is edited in place, and an edit cannot be split: `outbox.fit_one` would cut it. Its
    widest form (a 64-character project name, every step at `turn_progress.summary`'s 200) must not need that."""
    project = "P" * 64  # projects.id is String(64)
    steps = [turns.progress_line(ProgressKind.TOOL, "Bash", "x " * 3000) for _ in range(turns.PROGRESS_LINES + 2)]
    text = turns.progress_text(project, seconds=10**7, steps=10**6, latest=steps)
    assert len(text.splitlines()) == 1 + turns.PROGRESS_LINES
    assert all(len(line) <= turns.SUMMARY_MAX + 2 for line in text.splitlines()[1:])  # "• " + the summary
    assert fit_one(text) == text
    assert tg_len(text) <= TEXT_LIMIT


async def test_a_decision_question_on_a_huge_command_fits_one_message(
    dev_deps: DevDeps, fakes: Fakes, paths: Paths
) -> None:
    """The gate posts its question straight to the Messenger: the command is shown cut, the question fits."""
    command = "ssh prod 'docker compose up -d " + "x" * 6000 + "'"
    verdict = await gate.check_call(dev_deps, make_gate_scope(projects_root_of(paths)), make_bash_call(command))
    assert not verdict.allow
    [question] = fakes.messenger.sent()
    text = str(question.payload["text"])
    assert tg_len(text) <= TEXT_LIMIT
    shown = text.split("«", 1)[1].split("»", 1)[0]
    assert len(shown) == gate.COMMAND_SHOWN_CHARS
    assert shown.endswith(ELLIPSIS)
    assert command.startswith(shown.removesuffix(ELLIPSIS))
