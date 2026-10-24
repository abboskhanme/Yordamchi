"""Chat → feature for the dev bot: the typed aliases and exact phrases of SPEC §6 (deterministic, no LLM).

Natural language comes first; the aliases (`/holat`, `/hisobot BonviHR`, …) are shortcuts for the same intents.
There is no BotFather menu (`setMyCommands` is never called) and no button, ever (D8).

`command_of(text)` reads one owner message — a text, or a voice note's transcript — and returns the intent it
names, or None: then it is a project message, and routing decides (SPEC §6.1, last step). The pre-parse order
lives in `bots/dev/router.py`; this table only says what each phrase means:

- `EARLY` commands are answered before the burst is offered to open questions and decisions. They can never be an
  answer ("holat?", "/sozlamalar"), and an agent question that is the only open item binds any text at all
  (`dev/questions` rule 5), so asking first would deliver "holat?" to the agent as its answer.
- the rest are matched after binding (SPEC §6.1 steps 6–7), so "davom ettir" can still answer a question.

A phrase that needs a project (`X'ni to'xtat`) carries the name as written; the router resolves it against the
registry and lets the message through to routing when it names no project ("serverni to'xtat" is an instruction).
Later-phase intents are recognised here and answered with `S.NOT_YET`, never sent to a project as work.

A settings request is recognised by its clear cues only (SPEC §6 `change_setting`, UC-09): a message that starts with
"sozlama…" / "settings" (not "sozlamalar sahifasi…": a page of some app), `/sozlamalar <request>`, or the owner speaking
to the bot about itself anywhere in it ("javoblaringni qisqaroq qil", "ovozingni ayolga o'zgartir", "o'zingni …").
Such a cue is `implicit`: when the message also names a project it is work for that project, and the router lets it
through. A request with no cue ("rasmdan keyin 15 soniya kut") still goes to routing, which has no settings intent
yet (docs/PENDING_WIRING.md, From T34b): say it with "sozlama: …".
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Intent(StrEnum):
    """SPEC §6 intent names (the routed ones — instruct, report, investigate — live in `dev/routing`)."""

    SHOW_HEALTH = "show_health"
    GREETING = "greeting"  # "salom", "qandaysan?": small talk, answered as such, never with a report
    THANKS = "thanks"
    HELP = "help"  # "nima qila olasan?", `/yordam`
    SHOW_SETTINGS = "show_settings"
    CHANGE_SETTING = "change_setting"  # read by the settings assistant: a change, a revert or a question
    REVERT_SETTING = "revert_setting"
    ADD_TERM = "add_term"
    LIST_PROJECTS = "list_projects"
    ALIAS_PROJECT = "alias_project"
    ARCHIVE_PROJECT = "archive_project"
    UNARCHIVE_PROJECT = "unarchive_project"
    DESCRIBE_PROJECT = "describe_project"
    REPORT = "report"  # `/hisobot [X]`: the same fast path as "X qanday?"
    STOP_PROJECT = "stop_turn"  # "X'ni to'xtat", `/stop`
    CONTINUE_TURN = "continue_turn"
    RETRY_DELIVERY = "retry_delivery"
    NEW_SESSION = "new_session"
    WATCH_SERVER = "add_server_host"  # "bonvi-srv serverini kuzat"
    UNWATCH_SERVER = "remove_server_host"  # "bonvi-srv'ni kuzatma"
    # Later phases (SPEC §7: P1 answers `S.NOT_YET(phase)`).
    SHOW_COST = "show_cost"
    INTEGRATIONS = "integrations"
    NEW_PROJECT = "new_project"
    MAC_HANDOFF = "mac_handoff"
    PARK_PROJECT = "park_project"
    RESUME_PROJECT = "resume_project"
    SELF_UPDATE = "self_update"


LATER_PHASE: Final = "P2"
# Answered before open items are offered the message (module docstring).
EARLY: Final = frozenset(
    {
        Intent.SHOW_HEALTH,
        Intent.SHOW_SETTINGS,
        Intent.LIST_PROJECTS,
        Intent.SHOW_COST,
        Intent.GREETING,  # "rahmat" or "salom" is never the answer to an open question
        Intent.THANKS,
        Intent.HELP,
    }
)
# `Command.argument` of a greeting: which opening the reply takes.
GREETING_HOW: Final = "how"  # "qandaysan?", also "salom, qandaysan?"
GREETING_SALAM: Final = "salam"  # "assalomu alaykum"
GREETING_HELLO: Final = "hello"
# Not wired in the dev bot yet: each is answered with `S.NOT_YET`. `self_update` waits for P2's drain and probation
# wiring (docs/PENDING_WIRING.md, T56/T57).
NOT_BUILT: Final = frozenset(
    {
        Intent.INTEGRATIONS,
        Intent.NEW_PROJECT,
        Intent.MAC_HANDOFF,
        Intent.PARK_PROJECT,
        Intent.RESUME_PROJECT,
        Intent.SELF_UPDATE,
    }
)


@dataclass(frozen=True, slots=True)
class Command:
    """One recognised message. `project` is a project name as written (unresolved); `argument` the rest the intent
    needs (a term, an alias, a description, a setting key, a settings request), with the owner's spelling kept.
    `implicit`: a settings request read from a cue word rather than said outright; one that names a project is work
    for it (the router checks the registry)."""

    intent: Intent
    project: str | None = None
    argument: str = ""
    implicit: bool = False

    @property
    def early(self) -> bool:
        return self.intent in EARLY

    @property
    def built(self) -> bool:
        return self.intent not in NOT_BUILT

    @property
    def phase(self) -> str:
        """When a not-built intent arrives (SPEC §7): every one left is P2."""
        return LATER_PHASE


# --- normalising ----------------------------------------------------------------------------------------------------
_APOSTROPHES: Final = str.maketrans(dict.fromkeys("ʻʼ’‘`\u00b4", "'"))  # \u00b4: acute accent
_EDGE: Final = " .,!?;:…"


def normalise(text: str) -> str:
    """Lowercase, one apostrophe, single spaces, no punctuation at either end (as `burst.domain.normalise`)."""
    return " ".join(text.lower().translate(_APOSTROPHES).split()).strip(_EDGE)


def _tidy(text: str) -> str:
    """The owner's own spelling of a value: one apostrophe, single spaces, no quotes or edge punctuation."""
    return " ".join(text.translate(_APOSTROPHES).split()).strip(_EDGE + "'\"«»“”")


# --- typed aliases (SPEC §6 "Alias" column) -------------------------------------------------------------------------
_ALIASES: Final[dict[str, Intent]] = {
    "/holat": Intent.SHOW_HEALTH,
    "/sozlamalar": Intent.SHOW_SETTINGS,
    "/loyihalar": Intent.LIST_PROJECTS,
    "/hisobot": Intent.REPORT,
    "/stop": Intent.STOP_PROJECT,
    "/xarajat": Intent.SHOW_COST,
    "/integratsiyalar": Intent.INTEGRATIONS,
    "/sessiya": Intent.MAC_HANDOFF,
    "/yordam": Intent.HELP,
    "/help": Intent.HELP,
}
_ALIAS: Final = re.compile(r"(/[a-z]+)(?:@\w+)?(?:\s+(.*))?", re.DOTALL)


def _alias_of(raw: str) -> Command | None:
    match = _ALIAS.fullmatch(raw.strip())
    if match is None:
        return None
    intent = _ALIASES.get(match.group(1).lower())
    if intent is None:
        return None
    rest = _tidy(match.group(2) or "")
    if intent is Intent.SHOW_SETTINGS and rest:  # `/sozlamalar rasmdan keyin 15 soniya kut`: a request, said outright
        return Command(Intent.CHANGE_SETTING, argument=rest)
    return Command(intent, project=rest or None)


# --- exact phrases --------------------------------------------------------------------------------------------------
_PHRASES: Final[dict[str, Intent]] = {
    **dict.fromkeys(("holat", "holating qanday", "holating", "status"), Intent.SHOW_HEALTH),
    **dict.fromkeys(
        ("sozlamalar", "sozlamalaring qanday", "sozlamalaring", "sozlamalarni ko'rsat", "sozlamalarni ko'rsating"),
        Intent.SHOW_SETTINGS,
    ),
    **dict.fromkeys(("loyihalar", "loyihalar ro'yxati", "loyihalarni ko'rsat", "projects"), Intent.LIST_PROJECTS),
    **dict.fromkeys(("xarajat", "xarajatlar", "xarajat qancha"), Intent.SHOW_COST),
    **dict.fromkeys(("integratsiyalar", "integratsiyalarni ko'rsat"), Intent.INTEGRATIONS),
    **dict.fromkeys(("davom ettir", "davom et", "davom ettiring"), Intent.CONTINUE_TURN),
    **dict.fromkeys(("qayta yubor", "qayta yuboring"), Intent.RETRY_DELIVERY),
    **dict.fromkeys(("yangi loyiha", "tamom", "boshla"), Intent.NEW_PROJECT),
}
# Small talk (owner, 2026-10-24: "qandaysan?" got a report). Only a whole message of such words: one that says
# anything more ("salom, BonviHR'da login xatosini tuzat") is work and goes on to routing.
_HELLO: Final = (
    r"(?:assal[ao]mu?\s+alaykum|va\s*alaykum\s+assal[ao]m|assal[ao]m|salom\w*|xayrli\s+(?:tong|kun|kech)|hello|hi|hey"
    r"|good\s+(?:morning|evening)|привет|здравствуй(?:те)?|салам|доброе\s+утро|добрый\s+(?:день|вечер))"
)
_HOW: Final = (
    r"(?:qand[ae]y(?:san(?:iz)?|siz)|qal[ae]y(?:san(?:iz)?|siz)?|qanaqasan|yaxshi\s*mi(?:san(?:iz)?|siz)"
    r"|zo'r\s*mi(?:san|siz)|ishlar\s+(?:qalay|yaxshimi)|nima\s+gap(?:lar)?"
    r"|how\s+are\s+you|как\s+дела|как\s+ты)"
)
_SMALL_TALK: Final = re.compile(rf"(?:(?:{_HELLO}|{_HOW})[\s,.!?]*)+")
_HOW_WORD: Final = re.compile(_HOW)
_THANKS: Final = re.compile(
    r"(?:katta\s+)?(?:ra[hx]mat(?:ingiz|lar)?|tashakkur|thanks?(?:\s+you)?|спасибо|spasibo)[\s,.!?)🙏👍🙂😊]*"
)
_HELP: Final = re.compile(
    r"(?:sen\s+|siz\s+)?(?:nima(?:lar)?\s+(?:ish(?:lar)?\s+)?qil\s*(?:a\s*)?ola(?:san(?:iz)?|siz)"
    r"|qanday\s+yordam\s+bera\s*ola(?:san(?:iz)?|siz)|imkoniyatlaring(?:\s+(?:qanday|nima(?:lar)?))?|yordam|help"
    r"|(?:nima(?:lar)?\s+)?qo'l(?:ing|ingiz)dan\s+(?:nima(?:lar)?\s+)?keladi|nima(?:lar)?ga\s+qodirsan(?:iz)?)"
)
# A setting key as the registry spells it: `outbox.progress_edit_s`.
_SETTING_KEY: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
_REVERT: Final = re.compile(r"(?:sozlamani\s+)?oldingi\s+holat(?:i|ig)a\s+qaytar(?:ing)?(?:\s+(?P<key>\S+))?")
# A message that starts with the word "sozlama…" ("sozlama: javoblar qisqaroq", "sozlamani o'zgartir …"). Only at the
# start, and not as the name of a part of some app ("sozlamalar sahifasiga dark mode qo'sh"): "BonviHR'ning sozlamalar
# sahifasini tuzat" is work for a project.
_APP_PARTS: Final = r"(?:sahifa|bo'lim|menyu|ekran|oyna|tugma|forma|panel|fayl|page|screen|menu|tab|button|file)"
_SETTINGS_REQUEST: Final = re.compile(rf"(?:bot\s+)?(?:sozlama\w*|settings?)\b(?!\s+{_APP_PARTS}).*", re.DOTALL)
# The owner speaking to the bot about itself, anywhere in the message: "javoblaringni qisqaroq qil", "ovozingni
# ayolga o'zgartir", "gaplaringni iliqroq qil", "sozlamalaringda …", "o'zingni …" (never "o'zing tekshir": do it
# yourself is an instruction).
_SELF_CUE: Final = re.compile(r"\b(?:ovozing|javoblaring|javobing|gaplaring|sozlamalaring|o'zingni)\w*")
_TERM: Final = re.compile(r"(?:bu\s+)?so'zni\s+to'g'ri\s+yoz(?:ing)?\s*:\s*(?P<term>.+)", re.IGNORECASE | re.DOTALL)
_NAME: Final = r"(?P<name>[^\s'][^']*?|[^\s']+)"
_OBJECT: Final = _NAME + r"'?ni"  # "BonviHR'ni", "BonviHRni"
_ALIAS_PHRASE: Final = re.compile(
    _OBJECT + r"\s+['\"«“]?(?P<alias>[^'\"»”]+?)['\"»”]?\s+deb\s+(?:ham\s+)?tani(?:ng)?", re.IGNORECASE
)
_ARCHIVE: Final = re.compile(_OBJECT + r"\s+arxivla(?:ng)?", re.IGNORECASE)
_UNARCHIVE: Final = re.compile(_OBJECT + r"\s+arxivdan\s+chiqar(?:ing)?", re.IGNORECASE)
_DESCRIBE: Final = re.compile(_NAME + r"\s+tavsifi\s*:\s*(?P<text>.+)", re.IGNORECASE | re.DOTALL)
_STOP: Final = re.compile(_OBJECT + r"\s+(?:to'xtat|toxtat|to'xtating)", re.IGNORECASE)
_CONTINUE: Final = re.compile(_OBJECT + r"\s+davom\s+ettir(?:ing)?", re.IGNORECASE)
_RETRY: Final = re.compile(_NAME + r"'?(?:ga|ka|qa)\s+qayta\s+yubor(?:ing)?", re.IGNORECASE)
_NEW_SESSION: Final = re.compile(_NAME + r"\s+yangi\s+sessiya(?:si)?", re.IGNORECASE)
# A `~/.ssh/config` alias: one word ("bonvi-srv"), never a project name with spaces.
_HOST: Final = r"(?P<host>[A-Za-z0-9][A-Za-z0-9._-]*)"
_WATCH: Final = re.compile(_HOST + r"\s+server(?:in)?i\s+kuzat(?:ib\s+bor)?(?:ing)?", re.IGNORECASE)
_UNWATCH: Final = re.compile(_HOST + r"(?:\s+server(?:in)?i|'?ni)\s+kuzatma(?:ng)?", re.IGNORECASE)
# Later phases.
_RESUME: Final = re.compile(_OBJECT + r"\s+davom\s+ettiramiz", re.IGNORECASE)
_PARK: Final = re.compile(_OBJECT + r"\s+yopib\s+tur(?:ing)?", re.IGNORECASE)
_HANDOFF: Final = re.compile(_NAME + r"\s+sessiyasi", re.IGNORECASE)
_CONNECT: Final = re.compile(_NAME + r"'?(?:ga|ka|qa)\s+ulan\s*:.+", re.IGNORECASE | re.DOTALL)
_NEW_PROJECT: Final = re.compile(r"yangi\s+loyiha\s*:.+", re.IGNORECASE | re.DOTALL)
_SELF_UPDATE: Final = re.compile(r"o'zingga\s+.+\s+qo'sh", re.IGNORECASE | re.DOTALL)

# Order matters where phrases share words: "X'ni davom ettiramiz" (resume, P2) before "X'ni davom ettir".
_NAMED: Final[tuple[tuple[re.Pattern[str], Intent], ...]] = (
    (_RESUME, Intent.RESUME_PROJECT),
    (_PARK, Intent.PARK_PROJECT),
    (_UNARCHIVE, Intent.UNARCHIVE_PROJECT),
    (_ARCHIVE, Intent.ARCHIVE_PROJECT),
    (_STOP, Intent.STOP_PROJECT),
    (_CONTINUE, Intent.CONTINUE_TURN),
    (_RETRY, Intent.RETRY_DELIVERY),
    (_NEW_SESSION, Intent.NEW_SESSION),
    (_HANDOFF, Intent.MAC_HANDOFF),
)


def _small_talk_of(folded: str) -> Command | None:
    if _SMALL_TALK.fullmatch(folded) is not None:
        greeting = GREETING_SALAM if re.search(r"assal[ao]m", folded) else GREETING_HELLO
        opening = GREETING_HOW if _HOW_WORD.search(folded) else greeting
        return Command(Intent.GREETING, argument=opening)
    if _THANKS.fullmatch(folded) is not None:
        return Command(Intent.THANKS)
    if _HELP.fullmatch(folded) is not None:
        return Command(Intent.HELP)
    return None


def _phrase_of(raw: str) -> Command | None:
    folded = normalise(raw)
    if (intent := _PHRASES.get(folded)) is not None:
        return Command(intent)
    if (talk := _small_talk_of(folded)) is not None:
        return talk
    if (revert := _REVERT.fullmatch(folded)) is not None:
        key = revert.group("key") or ""
        return Command(Intent.REVERT_SETTING, argument=key) if not key or _SETTING_KEY.fullmatch(key) else None
    if (command := _argument_phrase_of(_tidy(raw))) is not None:
        return command
    if _SELF_UPDATE.fullmatch(folded) is not None:
        return Command(Intent.SELF_UPDATE)
    return _settings_request_of(raw, folded)


def _settings_request_of(raw: str, folded: str) -> Command | None:
    """ "sozlama: …" said outright, or a cue word anywhere (`implicit`: the router checks it names no project)."""
    if _SETTINGS_REQUEST.fullmatch(folded) is not None:
        return Command(Intent.CHANGE_SETTING, argument=_tidy(raw))
    if _SELF_CUE.search(folded) is not None:
        return Command(Intent.CHANGE_SETTING, argument=_tidy(raw), implicit=True)
    return None


def _argument_phrase_of(tidy: str) -> Command | None:  # noqa: PLR0911  # one return per phrase family
    """The phrases that carry a name, a term or a host in the owner's own spelling."""
    if (term := _TERM.fullmatch(tidy)) is not None:
        return Command(Intent.ADD_TERM, argument=_tidy(term.group("term")))
    if (alias := _ALIAS_PHRASE.fullmatch(tidy)) is not None:
        return Command(Intent.ALIAS_PROJECT, project=_tidy(alias.group("name")), argument=_tidy(alias.group("alias")))
    if (describe := _DESCRIBE.fullmatch(tidy)) is not None:
        return Command(Intent.DESCRIBE_PROJECT, project=_tidy(describe.group("name")), argument=describe.group("text"))
    if (watch := _WATCH.fullmatch(tidy)) is not None:
        return Command(Intent.WATCH_SERVER, argument=watch.group("host"))
    if (unwatch := _UNWATCH.fullmatch(tidy)) is not None:
        return Command(Intent.UNWATCH_SERVER, argument=unwatch.group("host"))
    for pattern, intent in _NAMED:
        if (named := pattern.fullmatch(tidy)) is not None:
            return Command(intent, project=_tidy(named.group("name")))
    if _CONNECT.fullmatch(tidy) is not None:
        return Command(Intent.INTEGRATIONS)
    if _NEW_PROJECT.fullmatch(tidy) is not None:
        return Command(Intent.NEW_PROJECT)
    return None


def command_of(text: str | None) -> Command | None:
    """The intent one owner message names (module docstring), or None for a project message."""
    if not text or not text.strip():
        return None
    return _alias_of(text) if text.lstrip().startswith("/") else _phrase_of(text)
