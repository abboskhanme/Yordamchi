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
ERR_SECRET_IN_PLAINTEXT = Msg(
    "Ishga tushmadim: sozlash faylida {key} ochiq holda yozilgan ({problem}). Maxfiy qiymatlar faqat macOS "
    "Keychain'da saqlanadi. Mac'da `python scripts/install.py --env-only --rotate-tokens` ni ishga tushiring: yangi "
    "token Keychain'ga yoziladi, faylda faqat havola qoladi. Eski token faylda turgani uchun BotFather'da almashtiring."
)
ERR_KEYCHAIN_ITEM_MISSING = Msg(
    "macOS Keychain'da «{key}» yozuvi topilmadi ({problem}). Mac'da "
    "`python scripts/install.py --env-only --rotate-tokens` ni ishga tushiring."
)
ERR_KEYCHAIN_UNAVAILABLE = Msg(
    "macOS Keychain'dan «{key}» o'qilmadi ({problem}): Keychain qulflangan yoki javob bermadi. Mac'ga login qiling, "
    "keyin `python -m yordamchi.check` ni ishga tushiring."
)
ERR_KEYCHAIN_ITEM_FORBIDDEN = Msg(
    "«{key}» bu botniki emas ({problem}): har bot faqat o'z Keychain yozuvlarini o'qiydi. Sozlash faylidagi "
    "havolani tuzating."
)

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
ERR_VAULT_KEY_MISSING = Msg(
    "Kalitlar ombori ochilmadi: ombor kaliti macOS Keychain'dan o'qilmadi (Keychain qulflangan yoki kalit yo'q). "
    "Mac'ga login qiling va `python -m yordamchi.check` ni ishga tushiring: u nima yetishmayotganini aytadi."
)
ERR_VAULT_KEY_INSECURE = Msg(
    "Kalitlar ombori ochilmadi: eski master.key fayli boshqalarga ham ochiq (ruxsat {mode}). "
    "`chmod 600 ~/.config/yordamchi/master.key` qiling: keyin u Keychain'ga ko'chiriladi va fayl o'chiriladi."
)
ERR_VAULT_DECRYPT_FAILED = Msg("«{name}» kalitini ochib bo'lmadi (ombor o'zgargan). Kalitni qayta yuboring.")
ERR_VAULT_SECRET_NOT_FOUND = Msg("«{name}» degan kalit omborda yo'q.")

# --- cost ---
# "xarajat?" (UC-07): each amount is "bugun / 7 kun / shu oy". Paid spend is money; the Claude plan's figure is an
# API-equivalent estimate within the subscription and is always labelled so.
COST_HEAD = Msg("Xarajat · bugun / 7 kun / shu oy")
COST_PAID = Msg("Pullik API: {today} / {week} / {month}")
COST_PROVIDER = Msg("• {provider}: {today} / {week} / {month} · limit {cap}, {pct}% ishlatildi")
COST_PROVIDER_PAUSED = Msg(
    "• {provider}: {today} / {week} / {month} · limit {cap} tugadi — pullik chaqiruvlar to'xtatilgan"
)
COST_PROVIDER_OFF = Msg("• {provider}: {today} / {week} / {month} · limit $0 — pullik chaqiruvlar o'chirilgan")
COST_USES = Msg("Nimaga: {uses}")
COST_USE_ITEM = Msg("{use} {today} / {week} / {month}")
COST_PLAN = Msg("Claude obunasi (API narxida taxmin; obuna doirasida, pul to'lanmaydi): {today} / {week} / {month}")
COST_TURNS = Msg("Ishlar: {today} / {week} / {month} · vaqt: {time}")
COST_CLAUDE_LIMITS = Msg("Claude limiti (ClaudeManager): 5 soat {five} · 7 kun {seven}")
COST_PROJECTS = Msg("Eng faol loyihalar (shu oy; Claude — taxmin): {projects}")
COST_PROJECT = Msg("{project} — {turns} ish, {estimate}")
COST_PROJECT_WITH_PAID = Msg("{project} — {turns} ish, {estimate} + pullik {paid}")
COST_BOTS = Msg("Botlar (shu oy): {bots}")
COST_BOT_ITEM = Msg("{bot} — pullik {paid}, Claude taxmin {estimate}")
COST_BOT_DEV = Msg("dev")
COST_BOT_PERSONAL = Msg("shaxsiy")
COST_PROVIDER_GOOGLE = Msg("Google Gemini")
COST_PROVIDER_OPENROUTER = Msg("OpenRouter")
COST_PROVIDER_OPENAI = Msg("OpenAI")
COST_USE_STT = Msg("STT")
COST_USE_TTS = Msg("TTS")
COST_USE_ROUTING = Msg("routing")
COST_USE_REPORT = Msg("hisobot")
COST_USE_VIDEO = Msg("video")
COST_USE_QA = Msg("savol-javob")
COST_NAME = Msg("{name}")  # a provider or use without its own label: its ledger name
COST_MINUTES = Msg("{minutes} daq")
COST_HOURS = Msg("{hours} soat {minutes} daq")
COST_PERCENT = Msg("{value}%")
COST_UNKNOWN = Msg("noma'lum")
# NFR-COST-02: once per provider, month and cap each. The pause names what still works and how to raise the cap.
COST_CAP_WARNING = Msg(
    "{provider}: oylik limitning {pct} foizi ishlatildi ({spent} / {cap}). {cap} ga yetganda pullik chaqiruvlar "
    "to'xtaydi."
)
COST_CAP_REACHED = Msg(
    "{provider}: oylik limit ({cap}) tugadi — {spent} sarflandi. Pullik chaqiruvlar to'xtatildi: ovozni boshqa "
    "provayder taniydi (u ham bo'lmasa, aytaman), ovozli javoblar Edge orqali yoki faqat matnda bo'ladi. Limitni "
    "oshirish uchun ayting, masalan: «{provider} limitini {suggested} dollar qil»."
)
# `schedule.cost_summary_at` (off by default): one line a day.
COST_SUMMARY = Msg(
    "Xarajat, so'nggi 24 soat: pullik API {paid} (shu oy {month}) · Claude {estimate} — obuna doirasida, pul "
    "to'lanmaydi · {turns} ta ish"
)
# Why a voice note skipped a provider (the voice failure line names it: "Google Gemini — oylik xarajat …").
COST_STT_PROBLEM_CAP = Msg("oylik xarajat limiti tugagan")
ERR_COST_CAP_REACHED = Msg(
    "{provider}: oylik xarajat limiti ({cap}) tugagan — pullik chaqiruv qilinmadi. Limitni oshirish uchun ayting."
)

# --- access ---
# --- outbox ---
# The caption of a `.md` file that replaces a text longer than `outbox.max_parts` messages (NFR-CAP-03). `summary`
# is the text's first line, so a project message still starts with the project name.
OUTBOX_TEXT_IN_FILE = Msg("{summary} — to'liq matn ilovadagi faylda.")

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
# UC-13 / SPEC §3.2.3 step 4: ONE question per decision, naming the project, what runs, where, and the consequence.
# Keyed by `GateClass` value; "unclear" is an ask with no class (a command the gate could not read in advance).
def _decision_question(what: str) -> Msg:
    return Msg(
        "{project} · Muhim qaror #{decision_id}: " + what + " — «{command}» → {where}. Oqibati: {consequence}. "
        "Bajaraymi? (ha / yo'q / boshqa ko'rsatma)"
    )


DECISION_QUESTIONS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "deploy": _decision_question("deploy qilmoqchiman"),
        "server_write": _decision_question("serverda o'zgarish qilmoqchiman"),
        "deploy_push": _decision_question("push qilmoqchiman, u deploy'ni ishga tushiradi"),
        "force_push": _decision_question("asosiy branch'ga force push qilmoqchiman"),
        "data_loss_db": _decision_question("bazadagi ma'lumotlarni o'chiradigan buyruq"),
        "docker_destroy": _decision_question("Docker volume'larini o'chirmoqchiman"),
        "fs_destroy": _decision_question("qaytarib bo'lmaydigan o'chirish"),
        "repo_delete": _decision_question("GitHub repo'ni o'chirmoqchiman"),
        "repo_public": _decision_question("repo'ni ochiq (public) qilmoqchiman"),
        "spend": _decision_question("pul sarflanadigan amal"),
        "third_party_send": _decision_question("tashqariga yubormoqchiman"),
        "architecture": _decision_question("arxitektura bo'yicha qaror kerak"),
        "unclear": _decision_question("buyruqni oldindan tekshira olmadim"),
    }
)
# The consequence of each gate `Reason` (SPEC §5.7), in the question and in audit views. The forbidden reasons are
# never asked about; their line says why the agent was stopped.
GATE_CONSEQUENCES: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "deploy_cli": Msg("ishlab turgan (prod) muhit yangilanadi"),
        "deploy_target": Msg("deploy/release skripti ishga tushadi, prod yangilanishi mumkin"),
        "remote_command": Msg("serverdagi holat o'zgaradi"),
        "jump_only_host": Msg("bu server faqat o'tish uchun (production 1C) — unda hech narsa qilinmasligi kerak"),
        "remote_docker": Msg("serverdagi Docker konteynerlari o'zgaradi"),
        "remote_copy": Msg("serverdagi fayllar almashtiriladi"),
        "push_to_server": Msg("kod to'g'ridan-to'g'ri serverga push qilinadi va u yerda yangilanadi"),
        "deploy_branch": Msg("CI shu push'dan keyin deploy qiladi"),
        "force_default_branch": Msg("asosiy branch tarixi qayta yoziladi, boshqa commit'lar yo'qolishi mumkin"),
        "sql_destructive": Msg("jadval yoki ma'lumotlar qaytarib bo'lmas o'chadi"),
        "drop_database": Msg("butun baza o'chadi"),
        "migration_destructive": Msg("migratsiya ma'lumotlarni o'chiradi yoki orqaga qaytaradi"),
        "migration_remote_db": Msg("migratsiya bu Mac'dagi emas, boshqa bazaga qo'llanadi"),
        "docker_volumes": Msg("volume'lardagi ma'lumotlar (baza ham) o'chadi"),
        "delete_project_root": Msg("loyiha papkasi butunlay o'chadi"),
        "delete_outside_project": Msg("loyihadan tashqaridagi fayllar o'chadi"),
        "delete_untracked": Msg("git'da yo'q (commit qilinmagan) fayllar o'chadi, ularni qaytarib bo'lmaydi"),
        "delete_git_dir": Msg("git tarixi (.git) o'chadi"),
        "delete_unknown": Msg("nima o'chishini oldindan bilib bo'lmaydi"),
        "discard_changes": Msg("siz Mac'da qilgan, hali commit qilinmagan o'zgarishlar yo'qoladi"),
        "repo_delete": Msg("repo va uning butun tarixi GitHub'dan o'chadi"),
        "repo_public": Msg("kod hammaga ochiq bo'ladi"),
        "cloud_spend": Msg("bulutda pullik resurs yaratiladi yoki kattalashtiriladi"),
        "package_publish": Msg("paket ochiq registrga chiqariladi"),
        "mail": Msg("xat boshqa odamga yuboriladi"),
        "http_write": Msg("ma'lumot tashqi xizmatga yuboriladi"),
        "mcp_write": Msg("integratsiya orqali tashqi tizimda o'zgarish qilinadi"),
        "opaque_command": Msg("qaysi dastur ishlashi faqat ishga tushganda ma'lum bo'ladi"),
        "unparseable": Msg("buyruqni tahlil qilib bo'lmadi, unda xavfli so'zlar bor"),
        "recipe_unknown": Msg("skript matnini oldindan o'qib bo'lmadi"),
        "forbidden_path": Msg("Yordamchi yoki Claude'ning maxfiy fayllariga tegish taqiqlangan"),
        "keychain": Msg("Keychain'dan parol o'qish taqiqlangan"),
        "bot_control": Msg("Yordamchi botlarini to'xtatish yoki boshqarish taqiqlangan"),
        "yordamchi_socket": Msg("Yordamchi boshqaruv soketiga murojaat taqiqlangan"),
        "push_no_verify": Msg("push'dagi maxfiy kalit tekshiruvini o'chirib bo'lmaydi"),
        "hooks_bypass": Msg("git hook'larini chetlab o'tish taqiqlangan"),
        "credential_env": Msg("Claude kirish o'zgaruvchilarini o'rnatish taqiqlangan"),
        "self_update_main": Msg("o'z-o'zini yangilashda main'ga push yoki merge qilinmaydi — buni supervisor qiladi"),
        "read_only_policy": Msg("tekshiruv rejimida faqat o'qish mumkin, hech narsa o'zgartirilmaydi"),
        "plan_only_policy": Msg("rejalashtirish bosqichida faqat docs/, README.md va .gitignore yoziladi"),
    }
)
DECISION_ITEM = Msg("#{decision_id} ({project})")
WHICH_DECISION = Msg(
    "Qaysi qarorga javob berdingiz: {decisions}? O'sha savol xabariga javob qiling yoki raqamini yozing "
    "(masalan: #17 ha)."
)
DECISION_APPROVED = Msg("{project} · #{decision_id} tasdiqlandi — aynan shu buyruq bir marta bajariladi.")
DECISION_DECLINED = Msg("{project} · #{decision_id} bekor qilindi, bajarilmaydi.")
DECISION_REDIRECTED = Msg("{project} · #{decision_id} bajarilmaydi; ko'rsatmangizni agentga yetkazaman.")
DECISION_ALREADY_APPROVED = Msg("{project} · #{decision_id} allaqachon tasdiqlangan.")
DECISION_CLOSED = Msg("{project} · #{decision_id} yopilgan, unga endi javob kerak emas.")
DECISION_EXPIRED = Msg("{project} · #{decision_id} muddati o'tib ketgan — yangidan so'rayman.")
PUSH_BLOCKED_SECRET = Msg(
    "{project}: push to'xtatildi — {file} faylining {line}-qatorida maxfiy kalit bor ({rule}). "
    "Uni commit'dan olib tashlang va kalitni almashtiring."
)
ERR_DECISION_NOT_FOUND = Msg("#{decision_id} raqamli qaror topilmadi.")

# --- voice ---
# A voice note that was not transcribed stays in its burst as this marker (SPEC §3.2 step 5; UC-05 F).
VOICE_NOT_UNDERSTOOD_MARKER = Msg("[ovoz tushunilmadi: {reason}]")
# UC-05 F: the owner is told about every note that failed; {reason} is built from the STT_REASON_* entries.
STT_FAILED = Msg("Ovozli xabarni matnga o'gira olmadim: {reason}. Matn bilan yozing yoki qayta yuboring.")
STT_FAILED_NEEDS_KEY = Msg(
    "Ovozli xabarni matnga o'gira olmadim: {reason}. Google (AIza…) yoki OpenRouter (sk-or-v1-…) kalitini chatga "
    "yuboring — o'zim sozlayman."
)
STT_REASON_NO_KEY = Msg("ovoz uchun kalit yo'q")
STT_REASON_NO_AUDIO = Msg("audio fayl yo'q")
STT_REASON_DOWNLOAD = Msg("faylni Telegram'dan yuklab bo'lmadi")
STT_REASON_NO_SPEECH = Msg("nutq eshitilmadi")
STT_REASON_PROVIDER = Msg("{provider} — {problem}")
STT_PROBLEM_QUOTA = Msg("kvota yoki balans tugagan")
STT_PROBLEM_AUTH = Msg("kalit rad etildi")
STT_PROBLEM_RATE_LIMITED = Msg("so'rovlar limitiga urildi")
STT_PROBLEM_TIMEOUT = Msg("javob vaqtida kelmadi")
STT_PROBLEM_UNAVAILABLE = Msg("javob bermayapti")
# SPEC §3.2.11: after a new speech key, one line says what is active now ("Ovoz: OpenRouter (Gemini) ulandi.").
SPEECH_GOOGLE = Msg("Google Gemini")
SPEECH_OPENROUTER = Msg("OpenRouter (Gemini)")
SPEECH_WITH_FALLBACK = Msg("{provider}, zaxira — {fallback}")
SPEECH_CONNECTED = Msg("Ovoz: {providers} ulandi.")
SPEECH_ACTIVE = Msg("Ovoz hozir: {providers}.")
SPEECH_NONE = Msg("Ovoz: ishlaydigan kalit yo'q — ovozli xabarlarni matnga o'gira olmayman.")
SPEECH_KEY_REJECTED = Msg("{provider} kaliti rad etildi — uni olib tashladim.")
SPEECH_KEY_NO_MODEL = Msg("{provider} kaliti to'g'ri, lekin {model} modeli unga ochiq emas.")
SPEECH_KEY_OUT_OF_QUOTA = Msg(
    "{provider} kaliti to'g'ri, lekin kvota yoki balans tugagan — to'ldirilmaguncha ishlamaydi."
)
SPEECH_KEY_RATE_LIMITED = Msg("{provider} kaliti to'g'ri, lekin hozir so'rovlar limitida — birozdan so'ng ishlaydi.")
SPEECH_KEY_UNCHECKED = Msg("{provider} kalitini hozir tekshirib bo'lmadi ({problem}) — baribir ishlataman.")
SPEECH_FREE_TIER = Msg("{provider}: bepul tarif — limit tez tugashi mumkin.")
# "bu so'zni to'g'ri yoz: …" (UC-05): the term applies from the next voice note.
STT_TERM_ADDED = Msg("Endi «{term}» so'zini aynan shunday yozaman.")
STT_TERM_KNOWN = Msg("«{term}» ro'yxatda bor — shunday yozaman.")
ERR_STT_TERM_INVALID = Msg("So'z 1–{limit} belgi bo'lsin, bitta qatorda.")

# --- routing ---
# The first reply names the target project (UC-10 AC, SPEC §3.2.1 step 7); a voice note is echoed with it (UC-05).
ROUTED = Msg("→ {project}")
ROUTED_MERGED = Msg("→ {project} (ishlayotgan topshiriqqa qo'shdim)")
VOICE_ECHO = Msg("Ovoz: «{text}» → {project}")
VOICE_ECHO_MERGED = Msg("Ovoz: «{text}» → {project} (ishlayotgan topshiriqqa qo'shdim)")
# The one clarification (UC-10 F): it quotes the material (UC-02 AC) and names the candidates in the order "2" means.
ROUTING_WHICH_PROJECT_ONE = Msg("{material} — {first} uchunmi? «ha» deb yoki kerakli loyiha nomini yozing.")
ROUTING_WHICH_PROJECT_TWO = Msg("{material} — {first}'gami yoki {second}'gami?")
ROUTING_WHICH_PROJECT_THREE = Msg("{material} — {first}'gami, {second}'gami yoki {third}'gami?")
ROUTING_WHICH_PROJECT_ANY = Msg("{material} — qaysi loyiha uchun? Loyiha nomini yozing.")
ROUTING_MATERIAL_QUOTE = Msg("«{text}»")
ROUTING_MATERIAL_PHOTO = Msg("Rasm")
ROUTING_MATERIAL_VIDEO = Msg("Video")
ROUTING_MATERIAL_FILE = Msg("Fayl")
ROUTING_MATERIAL_AUDIO = Msg("Audio")
ROUTING_MATERIAL_VOICE = Msg("Ovozli xabar")
ROUTING_MATERIAL_LINK = Msg("Havola")
ROUTING_MATERIAL_MESSAGE = Msg("Xabar")
ROUTING_CHOICE_OUT_OF_RANGE = Msg("1 dan {count} gacha raqam yoki loyiha nomini yozing: {options}.")
ROUTING_CLARIFY_CANCELLED = Msg("Xo'p, u xabarni hech qaysi loyihaga yubormadim.")
ROUTING_CLARIFY_EXPIRED = Msg(
    "«{candidates}» degan savolimga javob bo'lmadi — o'sha xabarni hech qaysi loyihaga yubormadim. Kerak bo'lsa, "
    "loyiha nomi bilan qayta yozing."
)
# "bu X uchun edi" (UC-10 F): routing moves the work; turns stops the wrong turn, lists its files, reverts on "ha".
ROUTING_WRONG_TARGET_MOVED = Msg("→ {to_project}: topshiriqni {from_project}'dan olib, {to_project}'ga qayta yubordim.")
ROUTING_WRONG_TARGET_FILES = Msg(
    "{project}: shu topshiriq o'zgartirgan fayllar ({count}): {files}. Ularni avvalgi holatiga qaytaraymi? (ha / yo'q)"
)
ROUTING_WRONG_TARGET_CLEAN = Msg("{project}: hech qaysi fayl o'zgarmagan — qaytaradigan narsa yo'q.")
ROUTING_WRONG_TARGET_REVERTED = Msg("{project}: {count} ta faylni avvalgi holatiga qaytardim.")
ROUTING_WRONG_TARGET_KEPT = Msg("{project}: o'zgarishlarni o'z holicha qoldirdim.")
ROUTING_WRONG_TARGET_SAME = Msg("{project}: bu topshiriq allaqachon shu loyihada.")
ROUTING_WRONG_TARGET_NONE = Msg(
    "{project}: so'nggi {hours} soatda boshqa loyihaga yuborilgan topshiriq topilmadi. Kerak bo'lsa, topshiriqni "
    "{project} nomi bilan qayta yozing."
)

# --- settings ---
ERR_SETTING_UNKNOWN = Msg("«{key}» degan sozlama yo'q.")
ERR_SETTING_INVALID = Msg("«{key}» uchun bu qiymat to'g'ri emas. Mumkin: {options}. Hech narsa o'zgarmadi.")
ERR_SETTING_PERSONAL_ONLY = Msg("«{key}» shaxsiy botning sozlamasi — buni shaxsiy botga ayting.")
ERR_SETTING_DEV_ONLY = Msg("«{key}» dev botning sozlamasi — buni dev botga ayting.")
# UC-09: one line restates every change; it applies from the next message, with no restart.
SETTING_CHANGE_LINE = Msg("{key}: {old} → {new}")
SETTING_CHANGED = Msg("Sozlama o'zgardi: {change}. Keyingi xabardan kuchga kiradi.")
SETTING_REVERTED = Msg("Oldingi holatiga qaytardim: {change}. Keyingi xabardan kuchga kiradi.")
SETTINGS_UNCHANGED = Msg("O'zgartiradigan narsa yo'q — hammasi allaqachon shunday.")
SETTINGS_NOTHING_TO_REVERT = Msg("Qaytaradigan o'zgarish yo'q.")
SETTING_NOTHING_TO_REVERT = Msg("«{key}» hali o'zgartirilmagan — qaytaradigan narsa yo'q.")
# UC-09 F, SPEC §3.2.15 step 3: a change that weakens a safeguard is ONE important decision; "ha" applies it.
SETTINGS_SUBJECT = Msg("Sozlamalar")
SETTING_WEAKEN_QUESTION = Msg(
    "Sozlamalar · Muhim qaror #{decision_id}: {change}. Bu himoyani susaytiradi. Qo'llaymi? (ha / yo'q)"
)
SETTING_CHANGE_DECLINED = Msg("Sozlamalar · #{decision_id} bekor qilindi, o'zgarmadi: {change}.")
# The chat interpretation (SPEC §3.2.15 step 1): an ambiguous request gets one clarifying question.
SETTINGS_CLARIFY = Msg("Aniqlashtiring, qaysi birini nazarda tutdingiz: {options}? Aniqroq qilib ayting.")
SETTINGS_CLARIFY_OPTION = Msg("{number}) {change}")
SETTINGS_UNCLEAR = Msg(
    "Qaysi sozlamani o'zgartirishni tushunmadim. Aniqroq ayting, masalan: «rasmdan keyin 15 soniya kut». "
    "Hammasini ko'rish: «sozlamalaring qanday?»."
)
SETTINGS_NEEDS_CODE = Msg(
    "Buni sozlama bilan qilib bo'lmaydi — kod o'zgarishi kerak. «Yordamchi'da … o'zgartir» deb ayting, "
    "o'z-o'zini yangilash (self-update) orqali qilaman."
)
# Values as the owner reads them.
SETTING_ON = Msg("yoqilgan")
SETTING_OFF = Msg("o'chirilgan")
SETTING_EMPTY = Msg("bo'sh")
# "sozlamalaring qanday?" (UC-09): a grouped summary, then the full list as a `.md` file (tests/golden/settings/).
SETTINGS_BOTS: Final[Mapping[str, Msg]] = MappingProxyType({"dev": Msg("dev"), "personal": Msg("shaxsiy")})
SETTINGS_GROUPS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "access": Msg("Kirish (egasi va chatlar)"),
        "persona": Msg("Persona va ohang"),
        "prompts": Msg("Promptlar"),
        "voice": Msg("Ovoz"),
        "speech": Msg("Nutq xizmatlari (STT/TTS)"),
        "language": Msg("Til"),
        "models": Msg("Modellar"),
        "routing": Msg("Routing va loyihalar"),
        "decisions": Msg("Muhim qarorlar (gate)"),
        "burst": Msg("Xabar oynalari (burst)"),
        "timeouts": Msg("Timeout'lar"),
        "schedules": Msg("Jadvallar"),
        "caps": Msg("Limitlar (xarajat, parallel ish)"),
        "limits": Msg("Cheklovlar (matn, fayl, xotira)"),
        "servers": Msg("Serverlar"),
    }
)
SETTINGS_LIST_HEAD = Msg("Sozlamalar · {bot} bot: {total} ta, {groups} guruh; standartdan farqli — {changed} ta.")
SETTINGS_LIST_GROUP = Msg("• {group} — {count} ta: {items}")
SETTINGS_LIST_DEFAULTS = Msg("hammasi standart")
SETTINGS_LIST_ITEM = Msg("{key} = {value}")
SETTINGS_LIST_FOOT = Msg(
    "To'liq ro'yxat — faylda. O'zgartirish uchun shunchaki ayting, masalan: «rasmdan keyin 15 soniya kut»; "
    "bekor qilish: «oldingi holatiga qaytar»."
)
SETTINGS_FILE_HEAD = Msg("Sozlamalar · {bot} bot — to'liq ro'yxat ({total} ta)")
SETTINGS_FILE_GROUP = Msg("## {group}")
SETTINGS_FILE_ITEM = Msg("• {key} = {value} — standart{options}")
SETTINGS_FILE_ITEM_CHANGED = Msg("• {key} = {value} — o'zgartirilgan, standart: {default}{options}")
SETTINGS_FILE_OPTIONS = Msg(" · mumkin: {options}")

# --- turns ---
DELIVERY_UNCERTAIN = Msg("{project}: oxirgi topshiriq bajarilmay qoldi — qayta yuboraymi?")
DELIVERY_RESENT = Msg("{project}: topshiriqni qayta yubordim.")
DELIVERY_NOTHING_TO_RETRY = Msg("{project}: qayta yuboradigan topshiriq yo'q.")
ERR_TURN_NOT_FOUND = Msg("#{turn_id} raqamli topshiriq topilmadi.")
# The one live progress line per project (SPEC §6.2, UC-11): edited at most every `outbox.progress_edit_s`.
TURN_PROGRESS = Msg("{project} · ishlayapti {minutes} daq · {steps} qadam")
TURN_PROGRESS_STEP = Msg("• {step}")
TURN_PROGRESS_FINISHED = Msg("{project} · tugadi · {minutes} daq · {steps} qadam")
TURN_PROGRESS_STOPPED = Msg("{project} · to'xtatildi · {minutes} daq · {steps} qadam")
TURN_PROGRESS_PAUSED = Msg("{project} · to'xtab turibdi · {minutes} daq · {steps} qadam")
TURN_PROGRESS_WAITING = Msg("{project} · javobingizni kutyapti · {minutes} daq · {steps} qadam")
# The final result, a new message (SPEC §6.2 golden format).
TURN_RESULT_NATIJA = Msg("{project} · Natija: {natija}")
TURN_RESULT_NO_NATIJA = Msg("(natija yozilmadi)")
TURN_RESULT_FILES = Msg("Fayllar: {count} ta ({names})")
TURN_RESULT_NO_FILES = Msg("Fayllar: o'zgarmadi")
TURN_RESULT_TESTS = Msg("Testlar: {status}")
TURN_RESULT_TESTS_SUMMARY = Msg("Testlar: {status} ({summary})")
TESTS_WORDS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "passed": Msg("o'tdi"),
        "failed": Msg("yiqildi"),
        "not_run": Msg("ishga tushirilmadi"),
        "unknown": Msg("noma'lum"),
    }
)
TURN_RESULT_COMMIT_PUSHED = Msg("Commit: {sha} · push: bajarildi ({upstream})")
TURN_RESULT_COMMIT_UNPUSHED = Msg("Commit: {sha} · push: qilinmadi")
TURN_RESULT_NO_COMMIT = Msg("Commit: yo'q")
TURN_RESULT_PUSH_MISSED = Msg("Commit: commit/push qilinmadi")
TURN_RESULT_GIT_UNKNOWN = Msg("Commit: git holatini o'qib bo'lmadi")
TURN_RESULT_TIME = Msg("Vaqt: {minutes} daq")
# A turn the executor started on its own after background work (no instruction owns it).
TURN_LATE_NOTE = Msg("{project} · qo'shimcha: {text}")
# The scheduler (SPEC §5.6, NFR-CAP-01): a queued turn names its place and why it waits.
QUEUED = Msg("{project}: navbatga qo'ydim ({position}-o'rin) — {reason}.")
QUEUE_REASONS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "cap": Msg("bir vaqtda ishlaydigan topshiriqlar soni to'lgan"),
        "pressure": Msg("Mac xotirasi band"),
        "disk": Msg("diskda joy kam"),
        "mac_attached": Msg("loyiha Mac'da ochiq"),
        "update_drain": Msg("Yordamchi yangilanishni kutyapti"),
        "busy": Msg("oldingi topshiriq tugashini kutyapti"),
    }
)
QUEUE_REASON_PRESSURE_CRITICAL = Msg("Mac xotirasi juda band — biror loyihani yopib turishni o'ylab ko'ring")
# "to'xta", "hammasini to'xtat" (UC-03): sent right after the guard closes, before the interrupt completes.
STOPPED = Msg("{project}: to'xtatdim.")
STOP_NOTHING = Msg("{project}: hozir ishlayotgan topshiriq yo'q.")
STOP_ALL_NOTHING = Msg("Hozir ishlayotgan topshiriq yo'q.")
# The runaway guard (NFR-COST-03): the turn pauses and asks.
TURN_RUNAWAY = Msg("{project}: {why} — to'xtatib turibman. Davom ettiraymi?")
RUNAWAY_STEPS = Msg("{steps} qadamdan oshdi")
RUNAWAY_WALL = Msg("{minutes} daqiqadan oshdi")
RUNAWAY_SAME_FAILURE = Msg("bir xil xato {count} marta takrorlandi")
TURN_CONTINUED = Msg("{project}: davom ettiryapman.")
TURN_NOTHING_TO_CONTINUE = Msg("{project}: davom ettiradigan ish yo'q.")
# Usage limit (UC-11 F, SPEC §5.6): one notice, then the turn goes on by itself.
USAGE_LIMIT = Msg("{project}: Claude limiti tugadi — {time} da (yoki akkaunt almashganda) o'zim davom ettiraman.")
USAGE_LIMIT_UNKNOWN = Msg("{project}: Claude limiti tugadi — limit tiklanganda o'zim davom ettiraman.")
# The session or its process failed.
TURN_PROCESS_DIED = Msg("{project}: Claude jarayoni kutilmaganda to'xtadi. Davom ettiraymi?")
TURN_SHUTDOWN = Msg("{project}: Yordamchi qayta ishga tushayotgani uchun ishni to'xtatdim. Davom ettiraymi?")
TURN_START_FAILED = Msg("{project}: sessiyani ochib bo'lmadi. Davom ettiraymi?")
TURN_AUTH_FAILED = Msg("{project}: Claude login rad etildi — Mac'da `claude auth status` ni tekshiring.")
TURN_FAILED = Msg("{project}: ish xato bilan tugadi ({reason}).")
TURN_CREDENTIALS_REFUSED = Msg(
    "{project}: sessiyani ochmadim — loyiha sozlamalarida begona Claude kirish manbai bor ({what}). Uni olib tashlang."
)
SESSION_RENEWED = Msg("{project}: eski sessiya o'chgan (30 kun) — eslatma bilan yangi sessiya boshladim")
# Wrong target (UC-10 F): the routing block holds the offer and the outcome; this is the failed revert.
WRONG_TARGET_REVERT_FAILED = Msg(
    "{project}: o'zgarishlarni qaytarib bo'lmadi ({operation}). Fayllar o'z holicha qoldi."
)

# --- questions ---
# An agent's question (UC-12, SPEC §3.2.2 step 3): the project first, the options numbered "1." with their
# descriptions. No buttons: the owner answers with a number, an option, his own words or a voice note.
AGENT_QUESTION = Msg("{project} · Savol: {question}")
AGENT_QUESTIONS = Msg("{project} · {count} ta savol:")
AGENT_QUESTION_ITEM = Msg("{number}) {question}")
AGENT_QUESTION_ITEM_HEADED = Msg("{number}) {header}: {question}")
AGENT_QUESTION_OPTION = Msg("{number}. {label}")
AGENT_QUESTION_OPTION_DESCRIBED = Msg("{number}. {label} — {description}")
AGENT_QUESTION_MULTI = Msg("Bir nechtasini tanlash mumkin (masalan: 1, 3).")
AGENT_QUESTION_NO_TEXT = Msg("(savol matni kelmadi)")
AGENT_QUESTION_HINT = Msg("Javob: raqam, variant nomi yoki o'z so'zingiz — matn yoki ovoz.")
AGENT_QUESTIONS_HINT = Msg("Javob: har bir savolga alohida qatorda — raqam, variant nomi yoki o'z so'zingiz.")
# The answer went to the agent; a voice answer is echoed (UC-05).
QUESTION_ANSWERED = Msg("→ {project}: javob «{answer}»")
QUESTION_ANSWERED_VOICE = Msg("Ovoz: «{text}» → {project}: javob «{answer}»")
QUESTION_ANSWERED_AS_MESSAGE = Msg("(agent javobni kutib turmagan edi — xabar qilib yubordim)")
QUESTION_OUT_OF_RANGE = Msg("{project}: 1 dan {count} gacha raqam yozing yoki javobni so'z bilan yozing.")
QUESTION_NOT_OPEN = Msg("{project}: bu savol endi ochiq emas — javobni hech qayerga yubormadim.")
# A number or an option that fits questions of several projects (UC-12 F): one short question naming them; a name,
# "birinchisi" or a number answers it (routing's clarification reads the reply).
QUESTION_WHICH_TWO = Msg("«{answer}» — {first} savoligami yoki {second} savoligami?")
QUESTION_WHICH_MANY = Msg("«{answer}» — qaysi loyiha savoliga: {projects}? Loyiha nomini yozing.")

# --- digest ---
# The fast report path (UC-15, SPEC §3.2.4): answered from the project digest, never from a session. Every report ends
# with one freshness line; a stale digest is refreshed first, and the line says so.
FRESHNESS = Msg("Ma'lumot: {age} oldin")
FRESHNESS_JUST_NOW = Msg("Ma'lumot: hozirgina")
FRESHNESS_REFRESHED = Msg("Ma'lumot: eskirgan edi ({age} oldin) — hozir yangiladim")
FRESHNESS_COLLECTED = Msg("Ma'lumot: hozir yig'dim")
FRESHNESS_REFRESH_FAILED = Msg("Ma'lumot: {age} oldin — yangilab bo'lmadi")
FRESHNESS_NONE = Msg("Ma'lumot: hali yig'ilmagan — git'ni o'qib bo'lmadi")
AGE_MINUTES = Msg("{count} daqiqa")
AGE_HOURS = Msg("{count} soat")
AGE_DAYS = Msg("{count} kun")
# A question that needs a fresh look: sent within 2 s, then a read-only investigation turn answers (≤ 3 min).
CHECKING = Msg("{project} · Tekshiryapman (~{minutes} daqiqa)")
# One project (`report_project`): the live turn first, then what the digest holds.
REPORT_TURN = Msg("{project} · hozir: «{title}», {state}, {minutes} daq")
REPORT_TURN_MORE = Msg("Yana: «{title}», {state}, {minutes} daq")
REPORT_IDLE = Msg("{project} · hozir ish yo'q")
REPORT_TURN_STATES: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "starting": Msg("boshlanmoqda"),
        "running": Msg("ishlayapti"),
        "stopping": Msg("to'xtatilmoqda"),
        "waiting_question": Msg("javobingizni kutyapti"),
        "waiting_decision": Msg("qaroringizni kutyapti"),
        "waiting_limit": Msg("Claude limitini kutyapti"),
        "paused_runaway": Msg("to'xtab turibdi"),
    }
)
REPORT_QUEUED = Msg("Navbatda: {count} ta topshiriq")
REPORT_LAST_RESULT = Msg("Oxirgi natija ({time}): {natija} · Testlar: {tests}")
REPORT_LAST_ENDED = Msg("Oxirgi topshiriq ({time}) {state}: «{title}»")
REPORT_ENDED_STATES: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "failed": Msg("xato bilan tugadi"),
        "interrupted": Msg("to'xtatilgan"),
    }
)
REPORT_NO_RESULT = Msg("Hali natija yo'q")
REPORT_GIT = Msg("Git: {branch} · {sync} · {tree}")
REPORT_GIT_UNKNOWN = Msg("Git: o'qib bo'lmadi")
REPORT_GIT_EMPTY = Msg("Git: {branch} · hali commit yo'q")
REPORT_GIT_DETACHED = Msg("detached HEAD")
REPORT_GIT_EVEN = Msg("{upstream} bilan bir xil")
REPORT_GIT_AHEAD = Msg("{count} commit push qilinmagan")
REPORT_GIT_BEHIND = Msg("{count} commit orqada")
REPORT_GIT_DIVERGED = Msg("{ahead} commit oldinda, {behind} commit orqada")
REPORT_GIT_NO_UPSTREAM = Msg("upstream yo'q")
REPORT_GIT_CLEAN = Msg("toza")
REPORT_GIT_DIRTY = Msg("{count} fayl commit qilinmagan")
REPORT_TODAY = Msg("Bugun: {count} commit — {subjects}")
REPORT_TODAY_CAPPED = Msg("Bugun: {count}+ commit — {subjects}")
REPORT_LAST_COMMIT = Msg("Bugun commit yo'q. Oxirgisi ({time}): «{subject}»")
REPORT_NO_COMMITS = Msg("So'nggi 7 kunda commit yo'q")
REPORT_FILES = Msg("O'zgargan fayllar: {count} ta ({names})")
REPORT_PENDING = Msg("Javobingizni kutyapti: {questions} ta savol, {decisions} ta qaror")
# Every project at once (`report_all`): "bugun qaysi loyihalarda nima o'zgardi?".
REPORT_ALL_HEADER = Msg("Bugun o'zgargan loyihalar: {count} ta")
REPORT_ALL_NONE = Msg("Bugun hech qaysi loyihada o'zgarish bo'lmadi")
REPORT_ALL_LINE = Msg("• {project}: {summary}")
REPORT_ALL_COMMITS = Msg("{count} commit (oxirgisi: «{subject}»)")
REPORT_ALL_COMMITS_CAPPED = Msg("{count}+ commit (oxirgisi: «{subject}»)")
REPORT_ALL_RESULT = Msg("natija: {natija}")
REPORT_ALL_RUNNING = Msg("Hozir ishlayapti: {items}")
REPORT_ALL_RUNNING_ITEM = Msg("{project} («{title}», {minutes} daq)")
REPORT_ALL_QUIET = Msg("Qolgan {count} ta loyihada bugun o'zgarish yo'q")
REPORT_ALL_PENDING = Msg("Javobingizni kutyapti: {projects}")
# Server checks (T31 fills them): the latest read-only check per host.
REPORT_SERVERS = Msg("Serverlar: {hosts}")
REPORT_SERVER_OK = Msg("{host} ✓ ({age} oldin)")
REPORT_SERVER_FAILED = Msg("{host} ✗ {summary} ({age} oldin)")
# --- servers ---
# UC-15, SPEC §3.2.4: a status answer is one line per host (each starts with the host), then the freshness line.
SERVERS_NONE = Msg("Kuzatiladigan server yo'q. Qo'shish uchun ayting, masalan: «bonvi-srv serverini kuzat».")
SERVERS_CHECKING = Msg("Tekshiryapman (~{minutes} daqiqa): {hosts}.")
SERVERS_HOST_OK = Msg("{host}: hammasi joyida · {details}")
SERVERS_HOST_PROBLEMS = Msg("{host}: muammo — {issues} · {details}")
SERVERS_HOST_UNREACHABLE = Msg("{host}: ulanib bo'lmadi — {reason}")
SERVERS_HOST_UNCHECKED = Msg("{host}: hali tekshirilmagan")
SERVERS_HOST_JUMP_ONLY = Msg("{host}: faqat o'tish (jump) uchun — unda buyruq ishlatmayman, tekshirmayman")
SERVERS_HOST_REFUSED = Msg("{host}: gate hech bir tekshiruv buyrug'iga ruxsat bermadi (gate.readonly_remote_commands)")
SERVERS_LOAD = Msg("yuklama {load}")
SERVERS_DISK = Msg("disk {pct}% ({mount})")
SERVERS_MEMORY = Msg("xotira {pct}%")
SERVERS_CONTAINERS = Msg("{count} ta konteyner")
SERVERS_NO_DETAILS = Msg("ma'lumot olinmadi")
SERVERS_FRESHNESS = Msg("Ma'lumot: {minutes} daqiqa oldin")
SERVERS_FRESH_NOW = Msg("Ma'lumot: hozirgina")
# The problems a check flags; each is alerted once (SPEC §3.2.4).
SERVERS_ISSUE_UNREACHABLE = Msg("server javob bermayapti")
SERVERS_ISSUE_DISK = Msg("disk {mount} {pct}% to'lgan")
SERVERS_ISSUE_MEMORY = Msg("xotira {pct}% band")
SERVERS_ISSUE_UNIT = Msg("{unit} xizmati ishdan chiqqan (failed)")
SERVERS_ISSUE_DOCKER_DOWN = Msg("Docker javob bermayapti (docker ps xato bilan tugadi)")
SERVERS_ISSUE_CONTAINER_DOWN = Msg("«{name}» konteyneri to'xtagan")
SERVERS_ISSUE_CONTAINER_RESTARTING = Msg("«{name}» konteyneri qayta-qayta ishga tushyapti")
SERVERS_ISSUE_CONTAINER_UNHEALTHY = Msg("«{name}» konteyneri sog'lom emas (unhealthy)")
SERVERS_ISSUE_LOG = Msg("{path} log'ida {count} ta xato qatori")
SERVERS_ALERT = Msg("{host} serverida muammo: {issues}.")
# Why a host did not answer, keyed by `ErrorCode` value (adapters/ssh/translate.py maps ssh failures to these).
SERVERS_REASONS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "provider_unavailable": Msg("javob bermayapti (tarmoqni tekshiring)"),
        "call_timed_out": Msg("vaqtida javob bermadi"),
        "auth_failed": Msg("kalit yoki login rad etildi"),
        "not_installed": Msg("Mac'da ssh topilmadi"),
    }
)
SERVERS_REASON_UNKNOWN = Msg("noma'lum xato")
# The owner names the servers by chat (`servers.hosts`, `~/.ssh/config` aliases).
SERVERS_HOST_ADDED = Msg(
    "{host}: kuzatuvga oldim — har {minutes} daqiqada faqat o'qiydigan buyruqlar bilan tekshiraman."
)
SERVERS_HOST_KNOWN = Msg("{host} allaqachon kuzatilyapti.")
SERVERS_HOST_REMOVED = Msg("{host}: kuzatuvdan chiqardim.")
SERVERS_NONE_LISTED = Msg("hech biri")
ERR_SERVER_JUMP_ONLY = Msg(
    "{host} faqat o'tish (jump) uchun belgilangan — unda hech qanday buyruq ishlatmayman, kuzatmayman ham."
)
ERR_SERVER_NOT_WATCHED = Msg("{host} kuzatiladigan serverlar ro'yxatida yo'q. Ro'yxatda: {hosts}.")

# --- health ---
HEALTH_ONLINE = Msg("Onlayn: {time}")
HEALTH_RESTARTED = Msg("Qayta ishga tushdim ({reason}). Uzilib qolgan ishlar: {interrupted} — davom ettiraymi?")
HEALTH_RESTARTED_IDLE = Msg("Qayta ishga tushdim ({reason}). Uzilib qolgan ish yo'q.")
# The `reason` of a restart notice: the previous run ended without writing its clean-exit marker (SPEC §2.5 step 8).
HEALTH_REASON_UNCLEAN = Msg("oldingi ish kutilmaganda uzildi")
HEALTH_REASON_UNCLEAN_SINCE = Msg("oldingi ish kutilmaganda uzildi, oxirgi marta {time} da ishlagan edim")
WAKE_GAP = Msg("Mac uxlagan edi {from_}–{to}, {n} ta xabar qayta ishlanmoqda")
# T32b: told once polling is back after ≥ 5 min of network failures; the watchdog stays quiet while offline.
NETWORK_GAP = Msg("Internet uzilgan edi {from_}–{to}")
# NFR-REL-08: told only when the smoke turn after a `claude` update fails; the bot keeps running. Keyed by the step.
SMOKE_FAILED = Msg(
    "Claude CLI {version} ga yangilandi, lekin sinov ishi o'tmadi: {step}. Men ishlashda davom etyapman; loyihalardagi "
    "ishlar to'xtab qolsa, Mac'da `claude --version` va `claude auth status` ni tekshiring."
)
SMOKE_STEPS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "timeout": Msg("5 daqiqada tugamadi"),
        "commit": Msg("fayl commit qilinmadi"),
        "file": Msg("fayl to'g'ri yozilmadi"),
        "repo": Msg("sinov papkasini tayyorlab bo'lmadi"),
        "auth_failed": Msg("Claude login rad etildi"),
        "start": Msg("sessiya ochilmadi"),
        "queued": Msg("navbatdan chiqmadi (Mac band)"),
        "waiting_limit": Msg("Claude limiti tugagan"),
    }
)
SMOKE_STEP_OTHER = Msg("ish xato bilan tugadi ({code})")
# NFR-CAP-05, SPEC §9.2: at most once per 6 h. Below `guard.block_disk_gb` the turn scheduler queues coding turns.
DISK_LOW = Msg("Diskda {free} GB joy qoldi ({limit} GB dan kam). Keraksiz fayllarni o'chirib, joy bo'shating.")
DISK_BLOCKED = Msg(
    "Diskda atigi {free} GB qoldi ({limit} GB dan kam) — yangi kodlash ishlari navbatda turadi. Joy bo'shating."
)
# "holat?" (UC-06, SPEC §6.2): one line per part, built from local state only (golden: tests/golden/holat).
HOLAT_HEAD = Msg("Holat · {bot} bot")
HOLAT_UPTIME = Msg("Ish vaqti: {uptime} · Yordamchi {version} · SDK {sdk}")
HOLAT_MINUTES = Msg("{minutes} daq")
HOLAT_HOURS = Msg("{hours} soat {minutes} daq")
HOLAT_DAYS = Msg("{days} kun {hours} soat")
HOLAT_CLAUDE_OK = Msg("Claude CLI {version}: login — claude.ai")
HOLAT_CLAUDE_WRONG_AUTH = Msg(
    "Claude CLI {version}: login usuli «{method}», claude.ai bo'lishi kerak — Mac'da `claude auth status` ni tekshiring"
)
HOLAT_CLAUDE_LOGGED_OUT = Msg("Claude CLI {version}: login qilinmagan — Mac'da `claude` ni ochib, kiring")
HOLAT_CLAUDE_MISSING = Msg("Claude CLI: topilmadi")
HOLAT_CLAUDE_UNCHECKED = Msg("Claude CLI: hali tekshirilmadi")
HOLAT_CLAUDE_FAILED = Msg("Claude CLI: tekshirib bo'lmadi ({code})")
HOLAT_MANAGER = Msg("ClaudeManager: {email} · 5 soat: {five} · 7 kun: {seven}")
HOLAT_PERCENT = Msg("{value}%")
HOLAT_MANAGER_NO_ACTIVE = Msg("ClaudeManager: faol akkaunt yo'q")
HOLAT_MANAGER_UNREAD = Msg("ClaudeManager: holatini o'qib bo'lmadi")
HOLAT_SPEECH_UNREAD = Msg("Ovoz: kalitlar holatini o'qib bo'lmadi")
HOLAT_RUNNING = Msg("Hozir ishlayapti: {projects}")
HOLAT_RUNNING_NONE = Msg("Hozir ishlayapti: hech narsa")
HOLAT_QUEUED = Msg("Navbatda: {items}")
HOLAT_QUEUED_ITEM = Msg("{project} ({position}-o'rin)")
HOLAT_PENDING = Msg("Javob kutayotgan qarorlar: {decisions} · savollar: {questions}")
HOLAT_WORK_UNREAD = Msg("Ishlar: o'qib bo'lmadi")
HOLAT_MAC = Msg("Mac: xotira bosimi — {pressure} · diskda {free} bo'sh")
HOLAT_GB = Msg("{gb} GB")
HOLAT_PRESSURE: Final[Mapping[int, Msg]] = MappingProxyType(
    {1: Msg("normal"), 2: Msg("o'rtacha"), 4: Msg("yuqori")}  # kern.memorystatus_vm_pressure_level
)
HOLAT_UNKNOWN = Msg("noma'lum")
HOLAT_DEGRADED = Msg("Cheklangan imkoniyatlar: {missing} topilmadi")
HOLAT_ERRORS = Msg("Oxirgi xatolar: {errors}")
HOLAT_ERROR_ITEM = Msg("{code} ({time})")
HOLAT_NO_ERRORS = Msg("Oxirgi xatolar: yo'q")

# --- wiring (bots) ---
# SPEC §7: a later phase's intent is recognised and answered, never routed to a project as an instruction.
NOT_YET = Msg("Bu imkoniyat hali yo'q — {phase} bosqichida qo'shiladi.")
# SPEC §3.2.5 step 2: "to'xta" with several turns working and no project named or replied to.
WHICH_RUNNING = Msg("Bir nechta ish ketyapti: {projects}. Qaysi birini to'xtatay? Loyiha nomini yozing.")
# "davom ettir", "qayta yubor" with no project named, replied to, or left over from the restart.
WHICH_PROJECT_NAME = Msg("Qaysi loyiha? Loyiha nomini yozing yoki uning xabariga javob qiling.")

# --- web ---
# adapters/web failures (UC-32 F). `site` is the link's host name.
ERR_WEB_UNREACHABLE = Msg("{site} ochilmadi: sayt topilmadi yoki ulanishni rad etdi. Manzilni tekshirib ko'ring.")
ERR_WEB_NOT_FOUND = Msg("{site}: bu sahifa yoki fayl topilmadi (404). Havola eskirgan bo'lishi mumkin.")
ERR_WEB_ACCESS_DENIED = Msg(
    "{site} kirishga ruxsat bermadi (403): sayt botlarni to'sayotgan yoki login talab qilayotgan bo'lishi mumkin."
)
ERR_WEB_LOGIN_REQUIRED = Msg(
    "{site}: bu kontent faqat login qilganlar uchun. Men loginsiz, toza brauzer bilan ishlayman, shuning uchun uni "
    "ocha olmayman."
)
ERR_WEB_PAYWALLED = Msg("{site}: bu kontent pullik (obuna yoki to'lov kerak), uni yuklab bera olmayman.")
ERR_WEB_GEO_BLOCKED = Msg("{site}: bu kontent bizning mintaqada ochilmaydi (geo-cheklov).")
ERR_WEB_BOT_CHECK = Msg("{site} so'rovimni bot deb to'xtatdi. Birozdan keyin qayta so'rab ko'ring.")
ERR_WEB_CONTENT_UNAVAILABLE = Msg("{site}: bu kontent o'chirilgan yoki yopilgan, endi mavjud emas.")
ERR_WEB_NO_MEDIA = Msg("{site}: bu sahifada yuklab olinadigan rasm yoki fayl topmadim.")
ERR_WEB_NOT_A_PAGE = Msg(
    "{site}: bu havola sahifa emas, fayl ekan. Skrinshot o'rniga faylning o'zini yuborishim mumkin."
)
ERR_WEB_CONTENT_TOO_LARGE = Msg("Fayl {limit_mb} MB dan katta, Telegram orqali yubora olmayman.")
ERR_VIDEO_NOT_FOUND = Msg("{site}: bu havolada yuklab olinadigan video topmadim.")
ERR_VIDEO_IS_LIVE = Msg("{site}: bu jonli efir. Efir tugamaguncha uni yuklab bo'lmaydi.")
ERR_VIDEO_TOO_LONG = Msg(
    "Video juda uzun ({minutes} daqiqa): Telegram'ga sig'adigan hajmga hatto past sifatda ham tushmaydi."
)
ERR_VIDEO_ENCODE_FAILED = Msg(
    "Videoni kichraytirib bo'lmadi (ffmpeg xatosi). Qayta urinib ko'ring yoki qisqaroq qismini so'rang."
)

# --- artefacts ---
ERR_WEB_URL_INVALID = Msg("Bu havolani ocha olmayman: faqat http:// yoki https:// bilan boshlanadigan manzillar.")
ERR_VIDEO_CLIP_INVALID = Msg(
    "Qism noto'g'ri: boshlanishi tugashidan oldin bo'lsin va {max_minutes} daqiqadan oshmasin (masalan 1:30–4:00)."
)
ERR_VIDEO_TOO_LARGE = Msg("Bu video {limit_mb} MB ga hatto past sifatda ham sig'maydi.")
# UC-32: captions of what is sent. `size` is "12.3 MB", `duration` "4:05".
ARTEFACT_SCREENSHOT = Msg("{site} — skrinshot.")
ARTEFACT_SCREENSHOT_FULL = Msg("{site} — to'liq sahifa.")
ARTEFACT_FILE = Msg("{site} — {name} ({size}).")
ARTEFACT_VIDEO = Msg("{site} — video ({duration}, {size}).")
ARTEFACT_VIDEO_REENCODED = Msg(
    "{site} — video ({duration}, {size}). {limit_mb} MB ga sig'ishi uchun {height}p sifatga tushirdim."
)
# UC-32 F: what can be done instead. `minutes` is the longest clip that still fits.
ARTEFACT_VIDEO_OFFER = Msg(
    "Buning o'rniga: {minutes} daqiqagacha bo'lgan qismini yuborishim (qaysi joyidan — ayting), qisqacha mazmunini "
    "aytib berishim yoki havolaning o'zini qoldirishim mumkin."
)
ARTEFACT_VIDEO_OFFER_NO_ACCESS = Msg(
    "Buning o'rniga havolaning o'zini qoldiraman; videoni o'zingiz menga yuborsangiz, mazmunini aytib beraman."
)
ARTEFACT_DIRECT_LINK = Msg("Videoning o'z sahifasi: {url}")

# --- voice_tts ---
# SPEC §3.2.11 step 6: the text always goes; a voice that moved to the next provider or did not come says so in one
# line, at most once per reason per hour.
SPEECH_EDGE = Msg("Edge TTS")
TTS_FELL_BACK = Msg("Ovoz: {provider} ishlamadi ({problem}) — {fallback} orqali yubordim.")
VOICE_SKIPPED = Msg("Ovozli javob yo'q: {reason}. Javob faqat matnda.")
TTS_REASON_NO_PROVIDER = Msg("ovoz uchun ishlaydigan provayder yo'q")
TTS_REASON_NO_FFMPEG = Msg("ffmpeg o'rnatilmagan")
TTS_REASON_ENCODE = Msg("ovozni Telegram formatiga o'girib bo'lmadi")
TTS_REASON_SUMMARY = Msg("ovoz uchun qisqa matnni tayyorlab bo'lmadi")

# --- integrations ---
# UC-08, SPEC §3.2.10. Integrations belong to Yordamchi (its registry and vault), never to a Claude account.
INTEGRATION_ADDED = Msg("«{name}» ulandi ({bots}). Vositalar: {tools}.")
INTEGRATION_TOOL = Msg("{tool}")
INTEGRATION_TOOL_READ_ONLY = Msg("{tool} (faqat o'qish)")
INTEGRATION_NO_TOOLS = Msg("vosita yo'q")
INTEGRATION_RECONNECTED = Msg("«{name}» qayta ulandi. Vositalar: {tools}.")
API_KEY_ADDED = Msg("«{name}» API kaliti saqlandi ({bots}): {url}. Botlar uni faqat o'qish uchun ishlatadi.")
INTEGRATION_LIST_HEAD = Msg("Integratsiyalar:")
INTEGRATION_LIST_ITEM = Msg("• {name} — {kind}, {bots}: {status} (tekshirildi: {checked})")
INTEGRATION_LIST_EMPTY = Msg("Hozircha integratsiya yo'q. «X'ga ulan: <manzil>» deb yozing.")
INTEGRATION_NEVER_CHECKED = Msg("hali yo'q")
INTEGRATION_KINDS: Final[Mapping[str, Msg]] = MappingProxyType({"mcp_http": Msg("MCP"), "api_http": Msg("API kalit")})
INTEGRATION_STATUSES: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "ok": Msg("ishlayapti"),
        "auth_failed": Msg("kalit rad etilgan"),
        "unreachable": Msg("javob bermayapti"),
        "needs_reauth": Msg("qayta kirish kerak"),
        "disabled": Msg("o'chirilgan"),
    }
)
INTEGRATION_BOTS: Final[Mapping[str, Msg]] = MappingProxyType(
    {"dev": Msg("dev bot"), "personal": Msg("shaxsiy bot"), "both": Msg("ikkala bot")}
)
INTEGRATION_REMOVED = Msg(
    "«{name}» o'chirildi: keyingi sessiyalarda bo'lmaydi. 30 kun ichida «{name}'ni qaytar» desangiz, tiklayman."
)
INTEGRATION_RESTORED = Msg("«{name}» qaytarildi: keyingi sessiyalarda yana bor.")
INTEGRATION_RENAMED = Msg("«{old}» endi «{name}» deb ataladi.")
INTEGRATION_ASSIGNED = Msg("«{name}» endi: {bots}.")
INTEGRATION_CHECKED = Msg("«{name}»: {status}. Vositalar: {tools}.")
# The OAuth paste-back flow from the phone (R-43): a localhost redirect cannot open on the phone, so he pastes it.
OAUTH_LINK = Msg(
    "«{name}» uchun kirish kerak. Havolani oching, ruxsat bering:\n{url}\n\n"
    "So'ng ochilmay qolgan sahifa manzilini (http://localhost:{port}/…) shu yerga yuboring. "
    "{minutes} daqiqa kutaman."
)
OAUTH_STILL_WORKING = Msg("«{name}»: kod qabul qilindi, tokenni olyapman — natijani alohida yozaman.")
OAUTH_REAUTH = Msg(
    "«{name}»ga kirish muddati tugadi va yangilab bo'lmadi. Qayta ulash uchun «{name}'ga qayta ulan» deb yozing — "
    "kirish havolasini yuboraman."
)
# `IntegrationFailed(name, reason, fix)`: the reason and the fix are picked by a snake_case code.
INTEGRATION_REASONS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "unreachable": Msg("server javob bermayapti"),
        "timed_out": Msg("server vaqtida javob bermadi"),
        "auth_failed": Msg("kalit yoki token rad etildi"),
        "rate_limited": Msg("serverning so'rovlar limiti tugadi (429)"),
        "oauth_failed": Msg("OAuth kirishi yakunlanmadi"),
        "oauth_client_metadata_needed": Msg("server Client ID Metadata Document talab qiladi"),
        "oauth_reauth_required": Msg("kirish muddati tugagan"),
        "oauth_paste_timed_out": Msg("sahifa manzili vaqtida kelmadi"),
        "protocol": Msg("server MCP tilida javob bermadi"),
        "secret_unreadable": Msg("saqlangan kalitni ochib bo'lmadi"),
        "internal": Msg("ichki xato"),
    }
)
INTEGRATION_FIXES: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "unreachable": Msg("Manzilni tekshiring yoki server ishga tushgach qayta yozing."),
        "timed_out": Msg("Birozdan so'ng qayta urinib ko'ring."),
        "auth_failed": Msg("Yangi kalit yuboring: «X'ga ulan: <manzil> <kalit>»."),
        "rate_limited": Msg("Bir necha daqiqadan so'ng qayta urinib ko'ring."),
        "oauth_failed": Msg("«X'ga qayta ulan» deb yozing — yangi havola yuboraman."),
        "oauth_client_metadata_needed": Msg(
            "integrations.oauth_client_metadata_url sozlamasiga statik HTTPS JSON manzilini kiriting."
        ),
        "oauth_reauth_required": Msg("«X'ga qayta ulan» deb yozing — yangi havola yuboraman."),
        "oauth_paste_timed_out": Msg("«X'ga qayta ulan» deb yozing — yangi havola yuboraman."),
        "protocol": Msg("Bu MCP server manzili ekanini tekshiring (odatda …/mcp bilan tugaydi)."),
        "secret_unreadable": Msg("Kalitni qayta yuboring."),
        "internal": Msg("Qayta urinib ko'ring; takrorlansa jurnalga qarayman."),
    }
)
ERR_INTEGRATION_FAILED = Msg("«{name}» ulanmadi: {reason}. {fix}")
ERR_INTEGRATION_NOT_FOUND = Msg(
    "«{name}» degan integratsiya yo'q. «integratsiyalar» deb yozsangiz, ro'yxatini beraman."
)
ERR_INTEGRATION_EXISTS = Msg("«{name}» allaqachon ulangan va ishlayapti.")
ERR_INTEGRATION_NAME_INVALID = Msg(
    "«{name}» nomidan integratsiya nomi chiqmadi: lotin harfi bilan boshlansin, 2–31 belgi bo'lsin."
)
ERR_INTEGRATION_URL_INVALID = Msg(
    "Manzil https:// bilan boshlanishi kerak (http faqat localhost uchun) va ichida login/parol bo'lmasin."
)
ERR_INTEGRATION_ASSIGN_REFUSED = Msg("Shaxsiy bot integratsiyani faqat o'zi uchun qo'sha oladi.")
ERR_OAUTH_REQUIRED = Msg("{provider} OAuth kirishini talab qiladi.")
ERR_OAUTH_REAUTH_REQUIRED = Msg("{provider}: kirish muddati tugagan. Qayta ulaning.")
ERR_OAUTH_CLIENT_METADATA_NEEDED = Msg(
    "{provider} Client ID Metadata Document talab qiladi: integrations.oauth_client_metadata_url sozlamasini kiriting."
)
ERR_OAUTH_PASTE_TIMED_OUT = Msg(
    "«{name}»: {minutes} daqiqada sahifa manzili kelmadi. «{name}'ga qayta ulan» deb yozing — yangi havola yuboraman."
)
ERR_OAUTH_NOT_PENDING = Msg("Hozir hech qaysi integratsiya kirish manzilini kutmayapti. Avval «X'ga ulan» deb yozing.")
ERR_OAUTH_CALLBACK_INVALID = Msg(
    "Yuborilgan manzilda kirish kodi yo'q. Ruxsat bergach ochilgan sahifa manzilini to'liq yuboring."
)

# --- selfupdate ---
# UC-19, SPEC §3.2.12: "O'zingga X qo'sh". Every message starts with the project name (Yordamchi itself).
SELFUPDATE_STARTED = Msg(
    "{project}: o'zimni yangilashni boshladim ({branch}). Ishni alohida branch'da qilaman; testlar o'tmasa hech "
    "narsa almashtirilmaydi. Natijasini aytaman."
)
SELFUPDATE_REQUESTED = Msg(
    "{project}: o'zgarishlar tayyor ({files} ta fayl). Endi supervisor yangi versiyani yig'adi, to'liq testdan "
    "o'tkazadi va almashtiradi — shu paytda kelgan xabarlaringiz yo'qolmaydi, keyin ishlanadi."
)
SELFUPDATE_CORE_QUESTION = Msg(
    "{project}: bu yangilanish himoya qatlamiga tegadi (#{decision_id}). Nima o'zgaradi: {summary}. "
    "Tegilgan himoya qismlari: {files}. Qo'llaymi? «ha» yoki «yo'q» deb javob bering."
)
SELFUPDATE_CORE_DECLINED = Msg("{project}: yangilanishni qo'llamayman (#{decision_id}). Branch saqlanib qoladi.")
SELFUPDATE_SPLIT_NEEDED = Msg(
    "{project}: bu o'zgarish ham supervisor'ga, ham botlarga tegadi. Xavfsizlik uchun ularni bitta yangilanishda "
    "qo'llamayman — ikkita alohida so'rov qilib bering. Branch {branch} saqlanib qoldi."
)
SELFUPDATE_NO_CHANGES = Msg("{project}: yangilanish ishi hech qanday commit qoldirmadi — qo'llaydigan narsa yo'q.")
SELFUPDATE_TURN_FAILED = Msg(
    "{project}: yangilanish ishi oxiriga yetmadi ({reason}). Hech narsa almashtirilmadi; branch {branch} saqlanib "
    "qoldi. Davom ettirish uchun «Yordamchi'da main'ga qayt» deb, keyin so'rovni qaytadan ayting."
)
SELFUPDATE_DONE = Msg(
    "{project}: yangilandim — versiya {version}. O'zgardi: {changes}. Testlar: {tests}. Holat: botlar ishga "
    "tushdi, xabar olish va o'z-o'zini tekshirish o'tdi."
)
SELFUPDATE_DONE_NOT_MERGED = Msg(
    "{project}: yangilandim — versiya {version} ishlayapti (testlar: {tests}), lekin branch {branch} main'ga "
    "qo'shilmadi: {reason}."
)
SELFUPDATE_REFUSED = Msg("{project}: yangilanish qo'llanmadi — {reason}. Hozirgi versiya o'zgarmadi.")
SELFUPDATE_FAILED = Msg("{project}: yangilanish to'xtadi — {reason}. Botlar avvalgi versiyada ishlayapti.")
SELFUPDATE_ROLLED_BACK = Msg(
    "{project}: yangi versiya {version} ishlamadi ({reason}), avtomatik ravishda {previous} ga qaytdim. "
    "Xabarlaringiz yo'qolmadi."
)
SELFUPDATE_SUPERVISOR_DONE = Msg("{project}: supervisor yangilandi — versiya {version}. Testlar: {tests}.")
SELFUPDATE_SUPERVISOR_SWITCHED_BACK = Msg(
    "{project}: yangi supervisor {version} {seconds} soniyada ishga tushmadi — oldingisiga ({previous}) qaytardim."
)
SELFUPDATE_TESTS = Msg("{count} ta (oldin {previous})")
SELFUPDATE_TESTS_FIRST = Msg("{count} ta")
SELFUPDATE_DETAIL = Msg("{reason}: {detail}")
SELFUPDATE_REASONS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "tests_failed": Msg("testlar o'tmadi"),
        "tests_dropped": Msg("testlar soni kamaydi"),
        "tests_uncounted": Msg("testlar sonini aniqlab bo'lmadi"),
        "build_failed": Msg("yangi versiyani yig'ib bo'lmadi"),
        "trial_failed": Msg("yangi supervisor sinov ishga tushirishdan o'tmadi"),
        "split_needed": Msg("o'zgarish supervisor'ga ham, botlarga ham tegadi"),
        "kind_mismatch": Msg("o'zgarish turi so'rovga mos emas"),
        "commit_missing": Msg("commit ishchi nusxada topilmadi"),
        "no_changes": Msg("o'zgarish yo'q"),
        "already_current": Msg("bu versiya allaqachon ishlayapti"),
        "core_declined": Msg("himoya qatlamidagi o'zgarishga ruxsat berilmadi"),
        "drain_timeout": Msg("ishlayotgan vazifalar vaqtida tugamadi"),
        "snapshot_failed": Msg("bazaning zaxira nusxasini olib bo'lmadi"),
        "bots_not_loaded": Msg("botlar ishga tushirilmagan"),
        "migration_failed": Msg("ma'lumotlar bazasi migratsiyasi xato bilan tugadi"),
        "no_heartbeat": Msg("yangi versiya ishga tushmadi"),
        "polling_not_started": Msg("yangi versiya Telegram'dan xabar olishni boshlamadi"),
        "selftest_failed": Msg("yangi versiya o'z-o'zini tekshirishdan o'tmadi"),
        "crash_loop": Msg("yangi versiya qayta-qayta yiqildi"),
        "probation_ignored": Msg("yangi versiya sinov rejimini bilmaydi"),
        "supervisor_restarted": Msg("almashtirish o'rtasida supervisor qayta ishga tushdi"),
        "supervisor_not_started": Msg("yangi supervisor ishga tushmadi"),
        "merge_not_ff": Msg("main shu orada o'zgargan"),
        "merge_failed": Msg("main'ga qo'shib bo'lmadi"),
        "branch_lost": Msg("ish boshqa branch'da qoldi"),
        "git_failed": Msg("git buyrug'i bajarilmadi"),
        "bad_request": Msg("so'rov fayli buzilgan"),
        "turn_failed": Msg("ish xato bilan tugadi"),
        "turn_interrupted": Msg("ish to'xtatildi"),
        "turn_cancelled": Msg("ish bekor qilindi"),
        "turn_missing": Msg("ish topilmadi"),
        "journal_lost": Msg("supervisor qaysi qadamda qolganini bilmaydi"),
    }
)
SELFUPDATE_REASON_OTHER = Msg("kutilmagan holat ({code})")
ERR_SELFUPDATE_BUSY = Msg("{project}: oldingi yangilanish hali tugamagan ({update}). Tugagach yana ayting.")
ERR_SELFUPDATE_DIRTY = Msg(
    "{project}: ishchi nusxada {count} ta saqlanmagan o'zgarish bor — ular yangilanishga aralashmasligi uchun "
    "boshlamadim."
)
ERR_SELFUPDATE_NOT_ON_MAIN = Msg(
    "{project}: ishchi nusxa «{branch}» branch'da, main'da emas. Yangilanish main'dan boshlanadi — avval "
    "«Yordamchi'da main'ga qayt» deb ayting."
)

# --- github ---
ERR_GITHUB_FAILED = Msg("GitHub amali bajarilmadi ({operation}). Mac'da `gh auth status` ni tekshiring.")

# --- newproject ---
# UC-14: a new project from a description, in as many messages as the owner likes. No buttons: "tamom", "ha",
# "o'zgartir: …" and "yo'q" are typed (or said).
NEWPROJECT_COLLECTING = Msg(
    "Yangi loyiha: tavsifini yozib boring — bir nechta xabar yoki ovoz bo'lsa ham bo'ladi. Tugagach «tamom» deng "
    "(yoki {minutes} daqiqa jim tursangiz, o'zim boshlayman)."
)
NEWPROJECT_EMPTY = Msg("Yangi loyiha: tavsif hali bo'sh — nima qurmoqchi ekanligingizni yozing.")
NEWPROJECT_EMPTY_CANCELLED = Msg("Yangi loyiha: tavsif kelmadi — bekor qildim.")
NEWPROJECT_CANCELLED = Msg("Yangi loyiha bekor qilindi.")
NEWPROJECT_PROJECT_CANCELLED = Msg("{project}: yangi loyiha bekor qilindi — papka va repo o'z holicha qoldi.")
NEWPROJECT_PLANNING = Msg("{project}: papkasi ochildi ({path}). Rejani yozyapman — ~{minutes} daqiqada yuboraman.")
# ASSUMPTIONS 2026-10-23: a taken name continues with the first free <Name>2, and the message says so.
NEWPROJECT_RENAMED = Msg("«{wanted}» nomi band edi — {project} deb ochdim.")
NEWPROJECT_REPO = Msg("GitHub: {url} (private)")
NEWPROJECT_REPO_FAILED = Msg("GitHub repo ochilmadi: {why} Reja shu yerda yoziladi; keyingi bosqichda yana urinaman.")
NEWPROJECT_GITHUB_LOGGED_OUT = Msg("Mac'da GitHub'ga kirilmagan — `gh auth login` qiling.")
# SPEC §3.2.8 step 5: the summary, the docs as files, and one question.
NEWPROJECT_PLAN_READY = Msg("{project} · Reja tayyor: {summary}")
NEWPROJECT_PLAN_DOCS = Msg("Hujjatlar: {names}")
NEWPROJECT_PLAN_NO_DOCS = Msg("Hujjatlar: docs/ papkasida topilmadi.")
NEWPROJECT_PLAN_ASK = Msg("Rejani tasdiqlaysizmi? (ha / o'zgartir: … / yo'q)")
NEWPROJECT_DOC_CAPTION = Msg("{project} · {name}")
NEWPROJECT_BUILDING = Msg("{project}: reja tasdiqlandi — noldan quryapman; commit va push o'zim qilaman.")
NEWPROJECT_REVISING = Msg("{project}: rejani o'zgartiryapman.")
NEWPROJECT_REVISE_WHAT = Msg("{project}: rejada nimani o'zgartiray? «o'zgartir: …» deb yozing.")
NEWPROJECT_DONE = Msg("{project}: yangi loyiha qurildi.")
NEWPROJECT_WHICH = Msg("Qaysi yangi loyiha haqida: {projects}? Loyiha nomini yozing.")
NEWPROJECT_FAILED = Msg("Yangi loyihani ochib bo'lmadi: {why}")
ERR_NEWPROJECT_FOLDER_FAILED = Msg(
    "«{name}» papkasini ochib bo'lmadi — ~/Documents/Projects'ni tekshiring. Mavjud papkalarga tegmadim."
)


# --- media ---
# UC-31: the immediate ack for a video, a YouTube link or a page (≤ 2 s; the analysis follows within 60 s).
LOOKING = Msg("Ko'ryapman…")
# SPEC §3.2.14: an inbound file over Telegram's 20 MB bot download cap (UC-31 F).
FILE_TOO_LARGE_IN = Msg(
    "Fayl {limit_mb} MB dan katta — Telegram botlarga undan kattasini bermaydi. Havolasini yoki qisqaroq qismini "
    "yuboring."
)
ERR_MEDIA_TOO_LARGE = FILE_TOO_LARGE_IN
ERR_MEDIA_TOO_LONG = Msg(
    "Video {max_minutes} daqiqadan uzun. Qaysi qismini ko'ray — vaqtini ayting yoki kesib yuboring."
)
ERR_MEDIA_VIDEO_OFF = Msg("Video tahlili o'chirilgan. Yoqish uchun: «video tahlilini yoq».")
ERR_MEDIA_NO_VIDEO_KEY = Msg(
    "Videoni ko'rish uchun kalit yo'q. Google (AIza…) yoki OpenRouter (sk-or-v1-…) kalitini chatga yuboring — o'zim "
    "sozlayman."
)
ERR_MEDIA_NO_ANSWER = Msg(
    "Videoni ko'rdim, lekin tushunarli xulosa chiqmadi. Qayta urinib ko'ring yoki savolni aniqroq bering."
)
ERR_MEDIA_PAGE_NO_TEXT = Msg(
    "{site}: sahifada o'qiladigan matn topmadim — u JavaScript yoki login bilan ochiladigan bo'lishi mumkin. "
    "Skrinshotini olib beraymi?"
)
ERR_MEDIA_PAGE_NOT_READABLE = Msg(
    "{site}: bu havola sahifa emas, fayl ekan (masalan PDF yoki rasm). Faylning o'zini yuborishim mumkin."
)
# The video reader failed with every provider tried; {reason} joins STT_REASON_PROVIDER parts ("Google Gemini —
# kvota yoki balans tugagan").
MEDIA_VIDEO_FAILED = Msg("Videoni ko'ra olmadim: {reason}.")
MEDIA_DOWNLOAD_FAILED = Msg("Faylni Telegram'dan yuklab bo'lmadi. Qayta yuboring.")

# --- bridge ---
# UC-36: "BonviHR qanday?" in the personal chat, read from the dev bot's digest through the read-only bridge. The
# lines reuse the dev report's `REPORT_*` entries; every answer ends with one `FRESHNESS*` line.
BRIDGE_PROJECT = Msg("{project} · {description}")
BRIDGE_PROJECT_BARE = Msg("{project}")
BRIDGE_QUEUED_PARKED = Msg("Navbatda: {queued} ta · to'xtatib qo'yilgan: {parked} ta")
BRIDGE_NO_DIGEST = Msg("{project}: dev bot bu loyiha haqida hali ma'lumot yig'magan.")
ERR_BRIDGE_UNAVAILABLE = Msg(
    "Loyihalar holatini hozir o'qib bo'lmadi: dev botning ma'lumotlari ochilmadi. Dev botning o'zidan so'rang."
)
ERR_BRIDGE_NOT_READ_ONLY = Msg(
    "Loyihalar holatini o'qimadim: dev bot ma'lumotlariga ulanish faqat o'qish rejimida emas (xavfsizlik qoidasi). "
    "O'rnatishni tekshirish kerak."
)

# --- qa ---
# UC-30: the personal assistant. Its answers are model output (`redact.scrubbed`); these are the frames around them.
# Q4: while Claude is unavailable a plain question is answered by the fallback model, labelled first.
FALLBACK_LABEL = Msg("(zaxira model)")
QA_FALLBACK_HEADER = Msg("(zaxira model) Claude hozir ishlamayapti — {reason}. Javob zaxira modeldan:")
QA_FALLBACK_REASONS: Final[Mapping[str, Msg]] = MappingProxyType(
    {
        "rate_limited": Msg("limit tugagan"),
        "quota_exhausted": Msg("limit tugagan"),
        "auth_failed": Msg("login rad etildi"),
        "not_installed": Msg("Claude CLI topilmadi"),
        "call_timed_out": Msg("javob vaqtida kelmadi"),
    }
)
QA_FALLBACK_REASON_OTHER = Msg("javob bermayapti")
QA_NEW_CONVERSATION = Msg("Yangi suhbat boshladim — oldingi mavzularni unutdim.")
# After a shortened answer (SPEC §3.2.13 step 4): the full one is stored and sent on "to'liqroq".
QA_MORE_HINT = Msg("To'liq javob uchun: «to'liqroq»")
QA_OPERATION = Msg("Javob")  # the `operation` of ERR_CALL_TIMED_OUT when an answer took too long
QA_EMPTY_QUESTION = Msg("Savolni tushunmadim — matn yoki ovoz bilan qayta yozing.")
QA_EMPTY_ANSWER = Msg("Javob bo'sh qaytdi. Savolni boshqacharoq qilib qayta so'rang.")
# NFR-SEC-08: the personal session reported tools or MCP servers outside its allowlist; it is refused, never used.
PERSONAL_TOOLS_MISMATCH = Msg(
    "Shaxsiy yordamchi xavfsizlik tekshiruvidan o'tmadi: Claude sessiyasi ruxsat etilmagan vositalar bilan ochildi, "
    "shuning uchun uni ishlatmadim. Claude CLI yangilangan bo'lishi mumkin — Mac'da `claude --version` ni tekshiring."
)
ERR_PERSONAL_TOOLS_MISMATCH = PERSONAL_TOOLS_MISMATCH
ERR_QA_NOTHING_MORE = Msg("Oxirgi javob allaqachon to'liq edi — qo'shimcha matn yo'q.")


# --- error catalog: every ErrorCode → its ERR_<CODE> entry above (test_error_catalog checks completeness) ---
ERRORS: Final[Mapping[ErrorCode, Msg]] = MappingProxyType(
    {code: entry for code in ErrorCode if isinstance(entry := globals().get(f"ERR_{code.name}"), Msg)}
)
