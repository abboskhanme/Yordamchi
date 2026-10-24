"""Every Uzbek (Latin) text the supervisor sends to the owner: the catalog SPEC calls `S_SUP`.

CONVENTIONS §12 makes this the one exception to the single catalog in `core/strings.py`, because A6 forbids importing
the app. Import it as `S`, like the app's catalog (`from yordamchi_supervisor import strings as S`; ruff's naming rule
allows only that alias), and call an entry with exactly its placeholders: `S.BOT_RESTARTED(bot=..., reason=...)`.
Texts are plain: they are sent without `parse_mode` and without `reply_markup`. Technical terms stay in English.
"""

import string
from typing import Final

type Param = str | int


class Msg:
    """One catalog entry. A call with a missing or an unknown placeholder raises, so a typo cannot reach the owner."""

    __slots__ = ("params", "template")

    def __init__(self, template: str) -> None:
        self.template = template
        self.params = frozenset(name for _, name, _, _ in string.Formatter().parse(template) if name)

    def __call__(self, **params: Param) -> str:
        if set(params) != self.params:
            raise KeyError(f"expected {sorted(self.params)}, got {sorted(params)}")
        return self.template.format(**params)

    def __repr__(self) -> str:
        return f"Msg(params={sorted(self.params)})"


BOT_NAMES: Final = {"dev": "Dev bot", "personal": "Shaxsiy bot"}

# Every alert carries the time it happened, because a queued alert may arrive after a network drop.
ALERT = Msg("[Supervisor {time}] {text}")

# --- watchdog (SPEC §2.8, NFR-REL-01) ---
REASON_PROCESS_MISSING = Msg("jarayon ishlamayotgan edi")
REASON_HEARTBEAT_STALE = Msg("{seconds} soniyadan beri heartbeat yangilanmadi")
BOT_RESTARTED = Msg("{bot}ni qayta ishga tushirdim: {reason}.")
BOT_RESTART_FAILED = Msg(
    "{bot}ni qayta ishga tushira olmadim: {reason}. launchctl buyrug'i bajarilmadi; Mac'da "
    "`launchctl print gui/$(id -u)/{label}` ni tekshiring."
)
CRASH_LOOP = Msg(
    "{bot} {minutes} daqiqada {count} marta qayta ishga tushdi (crash loop). Oxirgi xato: {last_error}. "
    "Keyingi eslatma kamida 30 daqiqadan so'ng."
)
NO_LAST_ERROR = Msg("yozilmagan")

# --- backups (SPEC §9.2) ---
BACKUP_FAILED = Msg(
    "{db} bazasining {date} zaxira nusxasi olinmadi yoki tekshiruvdan o'tmadi ({problem}). "
    "Oldingi nusxalar o'chirilmadi; bir soatdan so'ng qayta urinaman."
)
BACKUP_DISK_LOW = Msg("Zaxira nusxa olinmadi: diskda {free_mb} MB bo'sh joy bor, kamida {need_mb} MB kerak.")

# --- the supervisor itself ---
SUPERVISOR_ERROR = Msg("Supervisor ichida xato ({job}). Tafsilotlar: ~/Library/Logs/Yordamchi/supervisor.jsonl.")


def bot_name(bot: str) -> str:
    return BOT_NAMES.get(bot, bot)
