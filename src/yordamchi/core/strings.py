"""Every bot-facing Uzbek (Latin) text — the single catalog (CONVENTIONS §3, §10; OvozYoz `strings.py` pattern).

Import it as `S`: `from yordamchi.core import strings as S`, then `S.HEALTH_ONLINE(time="14:05")`. An entry is a `Msg`;
calling it with exactly its placeholders returns `BotText`. A message about a project starts with the project name.
Technical terms stay in English. `ERRORS` maps every `ErrorCode` to its `ERR_<CODE>` entry.

Block file (TASKS R2): each P1 feature adds entries only inside its own `# --- <feature> ---` block.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from yordamchi.core.errors import ErrorCode
from yordamchi.core.text import BotText, TextParam, placeholders, render


class Msg:
    """One catalog entry. `Msg(` appears only in this module (A5)."""

    __slots__ = ("params", "template")

    def __init__(self, template: str) -> None:
        self.template = template
        self.params = placeholders(template)

    def __call__(self, **params: TextParam) -> BotText:
        return render(self.template, params, self.params)

    def __repr__(self) -> str:
        return f"Msg(params={sorted(self.params)})"


# --- core ---
STARTUP_REFUSED = Msg(
    "Ishga tushmadim: Claude uchun begona kirish manbai topildi ({what}). Uni olib tashlang — men faqat Mac'dagi "
    "claude.ai login bilan ishlayman."
)
STARTUP_SCHEMA_MISMATCH = Msg(
    "Ishga tushmadim: ma'lumotlar bazasi sxemasi bu versiyaga mos emas. Migratsiyani installer yoki supervisor "
    "bajaradi."
)
ERR_INTERNAL = Msg("Ichki xato yuz berdi (#{incident}). Jurnalga yozdim; qayta urinib ko'ring.")
ERR_CONFIG_INVALID = Msg("Sozlash faylida xato: {key} ({problem}).")
ERR_PROVIDER_UNAVAILABLE = Msg("{provider} hozir javob bermayapti. Birozdan so'ng qayta urinib ko'ring.")
ERR_RATE_LIMITED = Msg("{provider}: so'rovlar limiti tugadi (429). Birozdan so'ng qayta urinib ko'ring.")
ERR_QUOTA_EXHAUSTED = Msg("{provider}: kvota yoki balans tugadi. Tarif yoki balansni tekshiring.")
ERR_AUTH_FAILED = Msg("{provider}: kalit yoki login rad etildi. Kalitni tekshiring yoki yangisini yuboring.")
ERR_CALL_TIMED_OUT = Msg("{operation} vaqtida tugamadi. Qayta urinib ko'ring.")
ERR_NOT_INSTALLED = Msg("{binary} topilmadi. O'rnating yoki PATH'ni tekshiring.")

# --- inbox ---
# Markers that replace a key inside the stored text (SPEC §3.2.1 step 3); later stages and the LLM see only these.
SECRET_MARKER = Msg("[kalit: {name} {masked}]")
SECRET_REFUSED_MARKER = Msg("[rad etildi]")
SECRET_NOT_STORED_MARKER = Msg("[kalit saqlanmadi]")
# The masked reply after a message holding a key was deleted (UC-04).
SECRETS_STORED = Msg("Kalit shifrlab saqlandi: {keys}.")
CLAUDE_TOKEN_REFUSED = Msg(
    "Claude login tokenini qabul qilmayman — akkauntlarni ClaudeManager boshqaradi. Token chatda ko'rindi: "
    "uni bekor qilib, yangisini oling."
)
SECRET_NOT_DELETED = Msg(
    "Xabaringizni o'chira olmadim (o'chirish huquqi yo'q yoki xabar 48 soatdan eski). Kalit chatda qoldi — "
    "kalitni almashtiring."
)

# --- audit ---
# --- vault ---
ERR_VAULT_KEY_MISSING = Msg("Kalitlar ombori ochilmadi: master.key topilmadi. Installer'ni qayta ishga tushiring.")
ERR_VAULT_KEY_INSECURE = Msg(
    "Kalitlar ombori ochilmadi: master.key boshqalarga ham ochiq (ruxsat {mode}). "
    "`chmod 600 ~/.config/yordamchi/master.key` qiling."
)
ERR_VAULT_DECRYPT_FAILED = Msg("«{name}» kalitini ochib bo'lmadi (ombor o'zgargan). Kalitni qayta yuboring.")
ERR_VAULT_SECRET_NOT_FOUND = Msg("«{name}» degan kalit omborda yo'q.")

# --- cost ---
# --- access ---
# --- outbox ---
# --- burst ---
# --- projects ---
ERR_GIT_FAILED = Msg("git buyrug'i bajarilmadi ({operation}). Loyiha papkasi va uning git holatini tekshiring.")
ERR_PROJECT_NOT_FOUND = Msg("«{name}» degan loyiha topilmadi. Ro'yxatini ko'rish uchun: loyihalar.")
ERR_PROJECT_AMBIGUOUS = Msg("«{name}» bir nechta loyihaga mos keladi: {candidates}. Qaysi biri?")
ERR_PROJECT_ALIAS_TAKEN = Msg("{project}: «{alias}» nomi allaqachon shu loyihaga tegishli.")
ERR_PROJECT_ALIAS_INVALID = Msg("«{alias}» qisqa nom bo'la olmaydi: 2–{limit} belgi bo'lsin, bitta qatorda.")
ERR_PROJECT_DESCRIPTION_INVALID = Msg("Tavsif 1–{limit} belgi bo'lsin, bitta qatorda.")
# UC-18: the replies to alias_project / archive_project / describe_project.
PROJECT_ALIAS_ADDED = Msg("{project}: endi «{alias}» deb ham taniyman.")
PROJECT_ARCHIVED = Msg("{project}: arxivga oldim — endi faqat aniq nomi bilan murojaat qilsangiz ishlayman.")
PROJECT_UNARCHIVED = Msg("{project}: arxivdan chiqardim.")
PROJECT_DESCRIBED = Msg("{project}: tavsifni yangiladim.")
# "loyihalar" (UC-18, P1 basic): a summary line, one line per active project, then the archived and vanished ones.
PROJECTS_SUMMARY = Msg("Loyihalar: {active} ta faol, {archived} ta arxivda.")
PROJECTS_NONE = Msg("{root} ichida loyiha topilmadi.")
PROJECTS_ITEM = Msg("• {project}")
PROJECTS_ITEM_DESCRIPTION = Msg(" — {description}")
PROJECTS_ITEM_ALIASES = Msg(" (yana: {aliases})")
PROJECTS_ITEM_WORKTREE = Msg(" (worktree)")
PROJECTS_ARCHIVED_LIST = Msg("Arxivda: {projects}")
PROJECTS_MISSING_LIST = Msg("Papkasi topilmadi: {projects}")

# --- gate ---
# --- voice ---
# --- routing ---
# --- settings ---
ERR_SETTING_UNKNOWN = Msg("«{key}» degan sozlama yo'q.")
ERR_SETTING_INVALID = Msg("«{key}» uchun bu qiymat to'g'ri emas. Mumkin: {options}.")
ERR_SETTING_PERSONAL_ONLY = Msg("«{key}» shaxsiy botning sozlamasi — buni shaxsiy botga ayting.")
ERR_SETTING_DEV_ONLY = Msg("«{key}» dev botning sozlamasi — buni dev botga ayting.")

# --- turns ---
DELIVERY_UNCERTAIN = Msg("{project}: oxirgi topshiriq bajarilmay qoldi — qayta yuboraymi?")

# --- questions ---
# --- digest ---
# --- servers ---
# --- health ---
HEALTH_ONLINE = Msg("Onlayn: {time}")
HEALTH_RESTARTED = Msg("Qayta ishga tushdim ({reason}). Uzilib qolgan ishlar: {interrupted} — davom ettiraymi?")
HEALTH_RESTARTED_IDLE = Msg("Qayta ishga tushdim ({reason}). Uzilib qolgan ish yo'q.")
WAKE_GAP = Msg("Mac uxlagan edi {from_}–{to}, {n} ta xabar qayta ishlanmoqda")

# --- wiring (bots) ---


# --- error catalog: every ErrorCode → its ERR_<CODE> entry above (test_error_catalog checks completeness) ---
ERRORS: Final[Mapping[ErrorCode, Msg]] = MappingProxyType(
    {code: entry for code in ErrorCode if isinstance(entry := globals().get(f"ERR_{code.name}"), Msg)}
)
