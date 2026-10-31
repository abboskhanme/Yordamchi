"""Chat → feature for the personal bot: the typed aliases and exact phrases of SPEC §6 (deterministic, no LLM).

Natural language comes first. Everything the owner says that is not one of these goes to the assistant
(`personal/qa`, SPEC §6.1 last step): a question, a photo, a voice note, a link, "X saytining skrinshoti" (the
assistant calls its media tools for that). There is no BotFather menu and no button, ever (D8).

`command_of(text)` reads one owner message — a text, or a voice note's transcript — and returns the intent it
names, or None. The rows (SPEC §6, the "both" and "personal" rows):

- `holat?`, `/holat` → `show_health`; "sozlamalaring qanday?", `/sozlamalar` → `show_settings`;
- a settings change said outright ("sozlama: …", `/sozlamalar <request>`) or with the owner speaking to the bot
  about its voice or answers ("javoblaring qisqaroq bo'lsin", "ovozingni erkakka o'zgartir", "ovozni o'chir");
  "oldingi holatiga qaytar [key]" → `revert_setting`; "bu so'zni to'g'ri yoz: X" → `add_term`;
- "xarajat?", `/xarajat` → `show_cost`; "to'liqroq" → `more`; "yangi suhbat" → `new_conversation`;
- `/start`, `/yordam` → `help` (Telegram sends `/start` when the owner first opens the chat);
- "bugungi yangiliklar", "moliya yangiliklari", `/yangiliklar` → `news` (T75): the assistant's answer with the news
  brief, in the same line as other work;
- "integratsiyalar", `/integratsiyalar` → `integrations` (the list); "X integratsiyasini o'chir" → `remove`;
  "X integratsiyasini qaytar" → `restore`; "X'ga qayta ulan" → `reconnect` (T76). Connecting is not here: "X'ga
  ulan: …" and "mana X API kaliti …" go to the assistant, which fills in the service's details and calls its
  connect tools (`personal/gateway`);
- "ovozing yoqmadi" → `voice_samples` (UC-34): numbered samples; `sample_choice_of` then reads "ikkinchisi" / "2".

`answer_of(text)` reads "ha" / "yo'q" while one settings change waits for the owner's word (SPEC §3.2.15 step 3).
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final


class Intent(StrEnum):
    """SPEC §6 intent names of the personal bot (`ask`, the default, is not one: it is everything else)."""

    SHOW_HEALTH = "show_health"
    SHOW_SETTINGS = "show_settings"
    CHANGE_SETTING = "change_setting"
    REVERT_SETTING = "revert_setting"
    ADD_TERM = "add_term"
    SHOW_COST = "show_cost"
    MORE = "more"
    NEW_CONVERSATION = "new_conversation"
    HELP = "help"
    NEWS = "news"
    INTEGRATIONS = "integrations"
    INTEGRATION_REMOVE = "integration_remove"
    INTEGRATION_RESTORE = "integration_restore"
    INTEGRATION_RECONNECT = "integration_reconnect"
    VOICE_SAMPLES = "voice_samples"


# Every intent of this bot is built; a later one is listed here and answered `S.NOT_YET(phase)` (SPEC §7).
NOT_BUILT: Final[frozenset[Intent]] = frozenset()
LATER_PHASE: Final = "P3"
# Work on this chat's conversation: it waits behind the answer being written, so "to'liqroq" reads that answer, and
# the news and the voice samples take their turn like any other answer.
CONVERSATION: Final = frozenset({Intent.MORE, Intent.NEW_CONVERSATION, Intent.NEWS, Intent.VOICE_SAMPLES})
# The commands that change something on the owner's word, or show what only the owner may see.
OWNER_ONLY: Final = frozenset(
    {
        Intent.SHOW_SETTINGS,
        Intent.CHANGE_SETTING,
        Intent.REVERT_SETTING,
        Intent.ADD_TERM,
        Intent.INTEGRATIONS,
        Intent.INTEGRATION_REMOVE,
        Intent.INTEGRATION_RESTORE,
        Intent.INTEGRATION_RECONNECT,
    }
)


@dataclass(frozen=True, slots=True)
class Command:
    """One recognised message. `argument` is what the intent needs (a term, a setting key, a settings request), in
    the owner's own spelling."""

    intent: Intent
    argument: str = ""

    @property
    def built(self) -> bool:
        return self.intent not in NOT_BUILT

    @property
    def conversation(self) -> bool:
        return self.intent in CONVERSATION

    @property
    def needs_origin(self) -> bool:
        return self.intent in OWNER_ONLY

    @property
    def phase(self) -> str:
        """When a not-built intent arrives (SPEC §7): the rest of P3."""
        return LATER_PHASE


class Answer(StrEnum):
    YES = "yes"
    NO = "no"


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
    "/xarajat": Intent.SHOW_COST,
    "/integratsiyalar": Intent.INTEGRATIONS,
    "/yangiliklar": Intent.NEWS,
    "/start": Intent.HELP,
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
    if intent is Intent.SHOW_SETTINGS and rest:  # `/sozlamalar ovozni o'chir`: a request, said outright
        return Command(Intent.CHANGE_SETTING, argument=rest)
    return Command(intent)


# --- exact phrases --------------------------------------------------------------------------------------------------
_PHRASES: Final[dict[str, Intent]] = {
    **dict.fromkeys(("holat", "holating qanday", "holating", "status"), Intent.SHOW_HEALTH),
    **dict.fromkeys(
        ("sozlamalar", "sozlamalaring qanday", "sozlamalaring", "sozlamalarni ko'rsat", "sozlamalarni ko'rsating"),
        Intent.SHOW_SETTINGS,
    ),
    **dict.fromkeys(("xarajat", "xarajatlar", "xarajat qancha"), Intent.SHOW_COST),
    **dict.fromkeys(
        ("to'liqroq", "to'liqrog'i", "to'liqrog'ini", "to'liqroq yoz", "to'liq", "to'liq javob", "to'liqini"),
        Intent.MORE,
    ),
    **dict.fromkeys(
        ("yangi suhbat", "yangi suhbat boshla", "yangi suhbat boshlaymiz", "suhbatni yangidan boshla"),
        Intent.NEW_CONVERSATION,
    ),
    **dict.fromkeys(("integratsiyalar", "integratsiyalarni ko'rsat"), Intent.INTEGRATIONS),
    **dict.fromkeys(
        ("ovozing yoqmadi", "ovozingni yoqtirmadim", "ovozing menga yoqmadi", "boshqa ovozlarni eshittir"),
        Intent.VOICE_SAMPLES,
    ),
}
# A setting key as the registry spells it: `voice.max_words`.
_SETTING_KEY: Final = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+")
_REVERT: Final = re.compile(r"(?:sozlamani\s+)?oldingi\s+holat(?:i|ig)a\s+qaytar(?:ing)?(?:\s+(?P<key>\S+))?")
# "sozlama: …" at the start of the message, said outright; not the name of a part of some app ("sozlamalar
# sahifasini qayerdan topaman?" is a question for the assistant).
_APP_PARTS: Final = r"(?:sahifa|bo'lim|menyu|ekran|oyna|tugma|forma|panel|fayl|page|screen|menu|tab|button|file)"
_SETTINGS_REQUEST: Final = re.compile(rf"(?:bot\s+)?(?:sozlama\w*|settings?)\b(?!\s+{_APP_PARTS}).*", re.DOTALL)
# The owner speaking to the bot about its voice or its answers, anywhere in the message: "javoblaring qisqaroq
# bo'lsin", "ovozingni erkakka o'zgartir", "gaplaringni iliqroq qil", "ovozni o'chir". Never the singular "javobing"
# ("javobing noto'g'ri" is about one answer: the assistant's business).
_SELF_CUE: Final = re.compile(r"\b(?:ovozing|javoblaring|gaplaring)\w*|\bovoz\w*\s+(?:o'chir|yoq|kerak\s+emas)\w*")
_TERM: Final = re.compile(r"(?:bu\s+)?so'zni\s+to'g'ri\s+yoz(?:ing)?\s*:\s*(?P<term>.+)", re.IGNORECASE | re.DOTALL)
# News (T75): a message that is only a request for news ("bugungi yangiliklar", "moliya yangiliklari", "so'nggi
# yangiliklarni aytib ber"). "Nima yangiliklar?" is a greeting, not a request.
_NEWS: Final = re.compile(
    r"(?!nima\b)(?:(?:bugungi|so'nggi|oxirgi|eng|kunning|[a-z']+)\s+){0,2}yangilik(?:lar|lari)?(?:ni|ini)?"
    r"(?:\s+(?:ber|ayt|aytib\s+ber|ko'rsat|qanday|bormi))?"
)
# Integrations by name (T76): "Bonvi HR integratsiyasini o'chir", "... qaytar", "Bonvi HR'ga qayta ulan".
_NAME: Final = r"(?P<name>[^\s'][^']{0,62}?)"
_REMOVE: Final = re.compile(rf"{_NAME}\s+integratsiya(?:si)?(?:ni)?\s+(?:o'chir|olib\s+tashla|uz)(?:ing|ib\s+qo'y)?")
_RESTORE: Final = re.compile(rf"{_NAME}\s+integratsiya(?:si)?(?:ni)?\s+qaytar(?:ing)?")
_RECONNECT: Final = re.compile(rf"{_NAME}'?(?:ga|ka|qa)\s+qayta\s+ulan(?:ing)?")


def _phrase_of(raw: str) -> Command | None:  # noqa: PLR0911  # one return per phrase family
    folded = normalise(raw)
    if (intent := _PHRASES.get(folded)) is not None:
        return Command(intent)
    if (revert := _REVERT.fullmatch(folded)) is not None:
        key = revert.group("key") or ""
        return Command(Intent.REVERT_SETTING, argument=key) if not key or _SETTING_KEY.fullmatch(key) else None
    tidy = _tidy(raw)
    if (term := _TERM.fullmatch(tidy)) is not None:
        return Command(Intent.ADD_TERM, argument=_tidy(term.group("term")))
    if _NEWS.fullmatch(folded) is not None:
        return Command(Intent.NEWS)
    for pattern, intent in (
        (_REMOVE, Intent.INTEGRATION_REMOVE),
        (_RESTORE, Intent.INTEGRATION_RESTORE),
        (_RECONNECT, Intent.INTEGRATION_RECONNECT),
    ):
        if (named := pattern.fullmatch(folded)) is not None:
            return Command(intent, argument=_named(tidy, named.group("name")))
    if _SETTINGS_REQUEST.fullmatch(folded) is not None or _SELF_CUE.search(folded) is not None:
        return Command(Intent.CHANGE_SETTING, argument=tidy)
    return None


def _named(tidy: str, folded_name: str) -> str:
    """The integration's name in the owner's own spelling (the folded match only says where it ends)."""
    return _tidy(tidy[: len(folded_name)])


def command_of(text: str | None) -> Command | None:
    """The intent one owner message names (module docstring), or None: the assistant answers it."""
    if not text or not text.strip():
        return None
    return _alias_of(text) if text.lstrip().startswith("/") else _phrase_of(text)


# --- the owner's word on a waiting change (SPEC §3.2.3 step 5 words, apostrophe variants normalised) ---------------
_YES: Final = frozenset({"ha", "xa", "qil", "mayli", "bo'pti", "roziman", "ok", "yes", "da", "ha qil", "ha, qil"})
_NO: Final = frozenset({"yo'q", "yoq", "kerakmas", "kerak emas", "bekor", "qilma", "no", "net", "yo'q kerakmas"})


def answer_of(text: str | None) -> Answer | None:
    """ "ha" or "yo'q" said alone (a longer message is something else), else None."""
    folded = normalise(text or "")
    if folded in _YES:
        return Answer.YES
    if folded in _NO:
        return Answer.NO
    return None


# --- the owner's pick among the voice samples (UC-34: "ikkinchisi") ------------------------------------------------
_ORDINALS: Final = {"bir": 1, "ikki": 2, "uch": 3, "to'rt": 4, "besh": 5}
_PICK: Final = re.compile(
    r"(?:(?P<digit>[1-5])|(?P<word>bir|ikki|uch|to'rt|besh))(?:-?(?:inchi|nchi|chi|si|inchisi|nchisi|chisi))?"
    r"(?:\s+(?:ovoz(?:i|ni)?|yoqdi|yaxshi|bo'lsin|shu))*"
)


def sample_choice_of(text: str | None) -> int | None:
    """ "ikkinchisi", "2", "2-si", "uchinchi ovoz yoqdi" said alone, else None."""
    match = _PICK.fullmatch(normalise(text or ""))
    if match is None:
        return None
    return int(match.group("digit")) if match.group("digit") else _ORDINALS[match.group("word")]
