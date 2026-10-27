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

# --- a banner on the Mac while Telegram is out of reach (`notify.py`, owner request 2026-10-27) ---
MAC_TITLE = Msg("Yordamchi")
BOTS_OFFLINE = Msg("{bots} {minutes} daqiqadan beri Telegram'ga ulana olmayapti. Mac'da internet va DNS'ni tekshiring.")

# --- backups (SPEC §9.2) ---
BACKUP_FAILED = Msg(
    "{db} bazasining {date} zaxira nusxasi olinmadi yoki tekshiruvdan o'tmadi ({problem}). "
    "Oldingi nusxalar o'chirilmadi; bir soatdan so'ng qayta urinaman."
)
BACKUP_DISK_LOW = Msg("Zaxira nusxa olinmadi: diskda {free_mb} MB bo'sh joy bor, kamida {need_mb} MB kerak.")

# --- self-update (SPEC §3.2.12, UC-19; T57) ---
# The supervisor speaks only when the bots may not be able to: a rollback and a manual repair. Every other outcome is
# posted by the dev bot from `updates/<id>/status.json`.
ROLLED_BACK = Msg(
    "Yordamchi yangilanishi {version} ishlamadi: {reason}. Avtomatik ravishda oldingi versiyaga ({previous}) "
    "qaytdim, botlar yana ishlayapti.{db} Yangilanish paytida kelgan xabarlaringiz yo'qolmadi — hozir ishlanadi."
)
ROLLED_BACK_DB = Msg(" Ma'lumotlar bazasi yangilanishdan oldingi nusxadan tiklandi.")
MANUAL_REPAIR = Msg(
    "Yordamchi yangilanishi {version} ishlamadi ({reason}) va oldingi versiya {previous} ham sog'lom ishga tushmadi. "
    "Mac'da qo'lda tiklash kerak: 1) `launchctl print gui/$(id -u)/uz.yordamchi.dev` holatini ko'ring; "
    "2) jurnal: ~/Library/Logs/Yordamchi/dev.jsonl va supervisor.jsonl; "
    "3) `ls -l ~/Library/Application\\ Support/Yordamchi/releases` — `current` qaysi versiyaga "
    "qarayotganini tekshiring; "
    "4) docs/DEPLOY.md dagi qaytarish bo'limiga amal qiling. Bazaning yangilanishdan oldingi nusxasi: "
    "backups/pre-update/{update_id}/."
)
UPDATE_JOURNAL_LOST = Msg(
    "Yangilanish {update_id} o'rtasida to'xtab qolgan, lekin supervisor qaysi qadamda qolganini bilmaydi "
    "(state.json yo'qolgan). Hech narsani o'zgartirmadim; Mac'da `releases/current` va botlar holatini tekshiring."
)
SUPERVISOR_SWITCHED_BACK = Msg(
    "Supervisor yangilanishi {version} ishga tushmadi; oldingi supervisor ({previous}) qaytarildi va ishlayapti."
)
UPDATE_REASONS: Final = {
    "migration_failed": Msg("ma'lumotlar bazasi migratsiyasi xato bilan tugadi"),
    "no_heartbeat": Msg("yangi versiya ishga tushmadi (heartbeat kelmadi)"),
    "polling_not_started": Msg("yangi versiya Telegram'dan xabar olishni boshlamadi"),
    "selftest_failed": Msg("yangi versiya o'z-o'zini tekshirishdan o'tmadi"),
    "crash_loop": Msg("yangi versiya qayta-qayta yiqildi"),
    "supervisor_restarted": Msg("almashtirish o'rtasida supervisor qayta ishga tushdi"),
    "switch_failed": Msg("yangi versiyaga o'tib bo'lmadi"),
}
REASON_OTHER = Msg("kutilmagan xato ({code})")

# --- the supervisor itself ---
SUPERVISOR_ERROR = Msg("Supervisor ichida xato ({job}). Tafsilotlar: ~/Library/Logs/Yordamchi/supervisor.jsonl.")


def bot_name(bot: str) -> str:
    return BOT_NAMES.get(bot, bot)


def bot_names(bots: list[str]) -> str:
    """The names joined for a sentence: "Dev bot", "Dev bot va Shaxsiy bot", "A, B va C"."""
    names = [bot_name(bot) for bot in bots]
    return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} va {names[-1]}"


def update_reason(code: str) -> str:
    entry = UPDATE_REASONS.get(code)
    return entry() if entry is not None else REASON_OTHER(code=code)
