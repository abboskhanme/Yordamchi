"""Every behaviour value and its default — the one defaults file (SPEC §4.3; CONVENTIONS §5; UC-09).

No behaviour literal lives anywhere else: code reads `snap[SETTING]` from `await settings.snapshot(deps)` once per
turn or job. Every §4.3 key is declared here, including the inline ones of SPEC §2–§5 and the later-phase keys, so
`test_registry` covers them all from day one.

Block file (TASKS R2): a feature adds or edits settings only inside its own `# --- <feature> ---` block.
"""

from typing import Final

from yordamchi.shared.settings.registry import (
    BOTH,
    DEV,
    FROM_BOOTSTRAP,
    PERSONAL,
    BotScope,
    Duration,
    Group,
    HostList,
    MaxLen,
    OneOf,
    PerBot,
    Range,
    Regex,
    Registry,
    Setting,
    SettingValue,
    SubsetOf,
    TimeOfDay,
    adds_items,
    always,
    disables,
    increases,
    removes_items,
    to_values,
)

# Claude model aliases (STACK §2.1). Fable/best "can bill to usage credits": choosing one is a spending decision.
CLAUDE_MODELS: Final = ("haiku", "sonnet", "opus", "fable", "best")
CREDIT_BILLED_MODELS: Final = frozenset({"fable", "best"})
EFFORTS: Final = ("low", "medium", "high", "xhigh", "max")
MODEL_ID: Final = Regex(r"[a-z0-9][a-z0-9._/-]{1,80}", description="model id, masalan gemini-3.8-flash")
LANGUAGE_TAG: Final = Regex(r"[a-z]{2,3}(?:-[A-Z][A-Za-z]{1,3})?", description="til kodi, masalan uz-UZ")
_ID_LIMIT: Final = 2**53


def _claude_model(key: str, default: str, *, bots: BotScope, doc: str) -> Setting[str]:
    return Setting(
        key,
        str,
        default,
        check=OneOf(*CLAUDE_MODELS),
        group=Group.MODELS,
        bots=bots,
        weakens=to_values(CREDIT_BILLED_MODELS),
        doc=doc,
    )


def _effort(key: str, default: str, *, bots: BotScope, doc: str) -> Setting[str]:
    return Setting(key, str, default, check=OneOf(*EFFORTS), group=Group.MODELS, bots=bots, doc=doc)


# --- core ---
# Cross-cutting values with no single owning feature.
UI_TIMEZONE = Setting(
    "ui.timezone",
    str,
    "Asia/Tashkent",
    check=Regex(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}", description="IANA zona, masalan Asia/Tashkent"),
    group=Group.SCHEDULES,
    bots=BOTH,
    doc="Zone of every time shown to the owner (HH:MM).",
)
LANG_REPLY = Setting(
    "lang.reply",
    str,
    "uz",
    check=OneOf("uz", "ru", "en"),
    group=Group.LANGUAGE,
    bots=BOTH,
    doc="Default reply language (Uzbek Latin); the personal bot mirrors the owner's language.",
)
SCHEDULE_QUIET_HOURS = Setting(
    "schedule.quiet_hours",
    str,
    "",
    check=Duration(allow_empty=True),
    group=Group.SCHEDULES,
    bots=BOTH,
    doc="Daily span sent with disable_notification, e.g. 23:00-07:00; empty = off.",
)
SCHEDULE_RETENTION_AT = Setting(
    "schedule.retention_at",
    str,
    "04:30",
    check=TimeOfDay(),
    group=Group.SCHEDULES,
    bots=BOTH,
    doc="Daily retention run time (SPEC §4.2); never deletes a row in a non-terminal state.",
)

# --- inbox ---
UI_SEEN_REACTION = Setting(
    "ui.seen_reaction",
    str,
    "\U0001f440",
    check=MaxLen(8),
    group=Group.PERSONA,
    bots=BOTH,
    doc="Reaction put on each owner message as the 'seen' ack (SPEC §3.2.1).",
)

# --- audit ---
# --- vault ---
# --- cost ---
CAPS_GOOGLE_MONTHLY_USD = Setting(
    "caps.google_monthly_usd",
    int,
    40,
    check=Range(0, 10_000),
    group=Group.CAPS,
    bots=BOTH,
    weakens=increases,
    doc="Monthly cap on paid Google (Gemini) calls, whole USD (NFR-COST-02).",
)
CAPS_OPENROUTER_MONTHLY_USD = Setting(
    "caps.openrouter_monthly_usd",
    int,
    10,
    check=Range(0, 10_000),
    group=Group.CAPS,
    bots=BOTH,
    weakens=increases,
    doc="Monthly cap on paid OpenRouter calls, whole USD.",
)
CAPS_OPENAI_MONTHLY_USD = Setting(
    "caps.openai_monthly_usd",
    int,
    10,
    check=Range(0, 10_000),
    group=Group.CAPS,
    bots=BOTH,
    weakens=increases,
    doc="Monthly cap on paid OpenAI calls, whole USD.",
)
CAPS_WARN_PCT = Setting(
    "caps.warn_pct",
    int,
    80,
    check=Range(10, 100),
    group=Group.CAPS,
    bots=BOTH,
    weakens=increases,
    doc="Share of a monthly cap at which the owner is warned once.",
)
SCHEDULE_COST_SUMMARY_AT = Setting(
    "schedule.cost_summary_at",
    str,
    "",
    check=TimeOfDay(allow_empty=True),
    group=Group.SCHEDULES,
    bots=DEV,
    doc="Daily cost summary time; empty = off (the owner asks 'xarajat?').",
)

# --- access ---
ACCESS_OWNER_ID = Setting(
    "access.owner_id",
    int,
    FROM_BOOTSTRAP,
    check=Range(1, _ID_LIMIT),
    group=Group.ACCESS,
    bots=BOTH,
    weakens=always,
    doc="The only Telegram user either bot serves; starts as OWNER_ID from the env file.",
)
ACCESS_DEV_GROUP_ID = Setting(
    "access.dev_group_id",
    int,
    FROM_BOOTSTRAP,
    check=Range(-_ID_LIMIT, -1),
    group=Group.ACCESS,
    bots=DEV,
    weakens=always,
    doc="The only chat the dev bot accepts; starts as DEV_GROUP_ID from the env file.",
)
ACCESS_PERSONAL_GROUP_ID = Setting(
    "access.personal_group_id",
    int,
    FROM_BOOTSTRAP,
    check=Range(-_ID_LIMIT, 0),
    group=Group.ACCESS,
    bots=PERSONAL,
    weakens=always,
    doc="The optional private group of the personal bot; 0 = none. Starts as PERSONAL_GROUP_ID.",
)


def bootstrap_values(
    *, owner_id: int, dev_group_id: int | None = None, personal_group_id: int | None = None
) -> dict[str, SettingValue]:
    """The `FROM_BOOTSTRAP` defaults of one process, from its env file (`core.config`)."""
    values: dict[str, SettingValue] = {ACCESS_OWNER_ID.key: owner_id}
    if dev_group_id is not None:
        values[ACCESS_DEV_GROUP_ID.key] = dev_group_id
    values[ACCESS_PERSONAL_GROUP_ID.key] = personal_group_id or 0
    return values


# --- outbox ---
OUTBOX_MAX_PARTS = Setting(
    "outbox.max_parts",
    int,
    3,
    check=Range(1, 10),
    group=Group.LIMITS,
    bots=BOTH,
    doc="More parts than this → a one-line summary plus a .md file (NFR-CAP-03).",
)
OUTBOX_PROGRESS_EDIT_S = Setting(
    "outbox.progress_edit_s",
    float,
    5.0,
    check=Range(5, 60),
    group=Group.LIMITS,
    bots=DEV,
    doc="At most one edit of a progress message per this many seconds (UC-11).",
)

# --- burst ---
BURST_AFTER_COMPLETE_TEXT_S = Setting(
    "burst.after_complete_text_s",
    float,
    1.2,
    check=Range(0.5, 10),
    group=Group.BURST,
    bots=BOTH,
    doc="Quiet window after a text that reads as complete (≥ 3 words, ends in ? . !).",
)
BURST_AFTER_TEXT_S = Setting(
    "burst.after_text_s",
    float,
    2.0,
    check=Range(0.5, 15),
    group=Group.BURST,
    bots=BOTH,
    doc="Quiet window after any other text.",
)
BURST_AFTER_MEDIA_S = Setting(
    "burst.after_media_s",
    float,
    10.0,
    check=Range(2, 60),
    group=Group.BURST,
    bots=BOTH,
    doc="Quiet window after a bare photo, file or link (UC-02).",
)
BURST_ALBUM_GAP_S = Setting(
    "burst.album_gap_s",
    float,
    1.5,
    check=Range(0.5, 10),
    group=Group.BURST,
    bots=BOTH,
    doc="An album stays open until this long after its last item.",
)
BURST_AFTER_VOICE_S = Setting(
    "burst.after_voice_s",
    float,
    2.0,
    check=Range(0.5, 15),
    group=Group.BURST,
    bots=BOTH,
    doc="Quiet window after a voice note's transcript arrives.",
)
BURST_HOLD_MAX_S = Setting(
    "burst.hold_max_s",
    float,
    120.0,
    check=Range(10, 600),
    group=Group.BURST,
    bots=BOTH,
    doc="Longest 'kut' hold while a burst is assembling.",
)
BURST_MAX_TOTAL_S = Setting(
    "burst.max_total_s",
    float,
    30.0,
    check=Range(5, 120),
    group=Group.BURST,
    bots=BOTH,
    doc="Hard cap on a burst from its first part ('kut' is exempt).",
)
BURST_MERGE_MIN_CONFIDENCE = Setting(
    "burst.merge_min_confidence",
    float,
    0.85,
    check=Range(0.5, 1.0),
    group=Group.BURST,
    bots=DEV,
    doc="Routing confidence needed to steer a follow-up into the project's running turn.",
)

# --- projects ---
PROJECTS_ROOT = Setting(
    "projects.root",
    str,
    "~/Documents/Projects",
    check=MaxLen(400),
    group=Group.ROUTING,
    bots=DEV,
    doc="Folder whose top-level directories are the project registry.",
)
PROJECTS_ALIASES = Setting(
    "projects.aliases",
    dict[str, str],
    {},
    check=MaxLen(1000),
    group=Group.ROUTING,
    bots=DEV,
    doc="Extra names for projects: alias → project folder name.",
)
PROJECTS_DESCRIPTIONS = Setting(
    "projects.descriptions",
    dict[str, str],
    {},
    check=MaxLen(1000),
    group=Group.ROUTING,
    bots=DEV,
    doc="Owner-written one-line project descriptions (override README/CLAUDE.md/SPEC).",
)
PROJECTS_ARCHIVED = Setting(
    "projects.archived",
    list[str],
    [],
    check=MaxLen(1000),
    group=Group.ROUTING,
    bots=DEV,
    doc="Projects excluded from routing unless named exactly.",
)
PROJECTS_UNARCHIVED = Setting(
    "projects.unarchived",
    list[str],
    [],
    check=MaxLen(1000),
    group=Group.ROUTING,
    bots=DEV,
    doc="Projects routed normally although their name looks archived (*Archive*, *-wt-*).",
)
EXECUTOR_DEFAULT = Setting(
    "executor.default",
    str,
    "claude",
    check=OneOf("claude", "codex"),
    group=Group.ROUTING,
    bots=DEV,
    doc="Executor for projects without their own choice (Codex is P4).",
)
EXECUTOR_PER_PROJECT = Setting(
    "executor.per_project",
    dict[str, str],
    {},
    check=MaxLen(1000),
    group=Group.ROUTING,
    bots=DEV,
    doc="Executor per project: folder name → claude | codex.",
)

# --- gate ---
GATE_CLASSES: Final = (
    "deploy",
    "server_write",
    "deploy_push",
    "force_push",
    "data_loss_db",
    "docker_destroy",
    "fs_destroy",
    "repo_delete",
    "repo_public",
    "spend",
    "third_party_send",
    "architecture",
)
READONLY_REMOTE_COMMANDS: Final = (
    "uptime",
    "df -h",
    "free -m",
    "uname -a",
    "hostname",
    "whoami",
    "ls",
    "ps aux",
    "top -bn1",
    "docker ps",
    "docker compose ps",
    "docker stats --no-stream",
    "docker logs --tail N <c>",
    "systemctl status <u>",
    "systemctl is-active <u>",
    "journalctl -u <u> -n N --no-pager",
    "tail -n N <path>",
    "cat /var/log/<path>",
)
GATE_ASK_CLASSES = Setting(
    "gate.ask_classes",
    list[str],
    list(GATE_CLASSES),
    check=SubsetOf(GATE_CLASSES),
    group=Group.DECISIONS,
    bots=DEV,
    weakens=removes_items,
    doc="UC-13 command classes that ask the owner before running (SPEC §5.7).",
)
GATE_SECRET_SCAN_ENABLED = Setting(
    "gate.secret_scan_enabled",
    bool,
    True,
    group=Group.DECISIONS,
    bots=DEV,
    weakens=disables,
    doc="The pre-push secret scan blocks .env files, keys and vault values (UC-13).",
)
GATE_APPROVAL_TTL_MIN = Setting(
    "gate.approval_ttl_min",
    int,
    120,
    check=Range(5, 1440),
    group=Group.DECISIONS,
    bots=DEV,
    weakens=increases,
    doc="How long an approved decision stays usable (single use).",
)
GATE_HOOK_TIMEOUT_S = Setting(
    "gate.hook_timeout_s",
    float,
    15.0,
    check=Range(2, 60),
    group=Group.DECISIONS,
    bots=DEV,
    doc="PreToolUse hook timeout; a timed-out guard denies.",
)
GATE_READONLY_REMOTE_COMMANDS = Setting(
    "gate.readonly_remote_commands",
    list[str],
    list(READONLY_REMOTE_COMMANDS),
    check=MaxLen(200),
    group=Group.DECISIONS,
    bots=DEV,
    weakens=adds_items,
    doc="Remote commands that run without asking (N = number, <c>/<u>/<path> = name); pipes only into filters.",
)
GATE_JUMP_ONLY_HOSTS = Setting(
    "gate.jump_only_hosts",
    list[str],
    ["server"],
    check=HostList(),
    group=Group.DECISIONS,
    bots=DEV,
    weakens=removes_items,
    doc="Hosts where any command asks (the Windows host with production 1C).",
)
GATE_GIT_HOSTS = Setting(
    "gate.git_hosts",
    list[str],
    ["github.com"],
    check=HostList(),
    group=Group.DECISIONS,
    bots=DEV,
    weakens=adds_items,
    doc="Hosts treated as git remotes, not servers, by the ssh rules.",
)
GATE_HTTP_WRITE_ALLOW_HOSTS = Setting(
    "gate.http_write_allow_hosts",
    list[str],
    [],
    check=HostList(),
    group=Group.DECISIONS,
    bots=DEV,
    weakens=adds_items,
    doc="Hosts that may receive mutating HTTP without asking.",
)
GATE_TEMP_ROOTS = Setting(
    "gate.temp_roots",
    list[str],
    ["/tmp", "/private/tmp", "/private/var/folders"],  # noqa: S108  # data: dirs where deleting never asks
    check=Regex(r"/[^\x00]*", description="absolut yo'l, masalan /tmp"),
    group=Group.DECISIONS,
    bots=DEV,
    weakens=adds_items,
    doc="Directories outside a project where deleting never asks.",
)
GATE_SHIM_GRANT_WINDOW_S = Setting(
    "gate.shim_grant_window_s",
    int,
    120,
    check=Range(10, 600),
    group=Group.DECISIONS,
    bots=DEV,
    weakens=increases,
    doc="After an approved command ran, how long its own ssh/scp/rsync calls pass the shims (SPEC §5.7 Layer 2).",
)

# --- voice ---
VOICE_ENABLED = Setting(
    "voice.enabled",
    bool,
    PerBot(dev=False, personal=True),
    group=Group.VOICE,
    bots=BOTH,
    doc="Voice summary after the text reply (dev: only on request).",
)
VOICE_NAME = Setting(
    "voice.name",
    str,
    "Kore",
    check=MaxLen(40),
    group=Group.VOICE,
    bots=BOTH,
    doc="TTS voice name.",
)
VOICE_STYLE = Setting(
    "voice.style",
    str,
    "tabiiy o'zbek talaffuzi, iliq, aniq",
    check=MaxLen(300),
    group=Group.VOICE,
    bots=BOTH,
    doc="TTS style and accent instruction.",
)
VOICE_GENDER = Setting(
    "voice.gender",
    str,
    "female",
    check=OneOf("female", "male"),
    group=Group.VOICE,
    bots=BOTH,
    doc="Voice gender used when picking voices.",
)
VOICE_LANGUAGE_CODE = Setting(
    "voice.language_code",
    str,
    "uz-UZ",
    check=LANGUAGE_TAG,
    group=Group.VOICE,
    bots=BOTH,
    doc="TTS language.",
)
VOICE_MAX_WORDS = Setting(
    "voice.max_words",
    int,
    70,
    check=Range(10, 300),
    group=Group.VOICE,
    bots=BOTH,
    doc="Longest voice script.",
)
VOICE_MAX_S = Setting(
    "voice.max_s",
    int,
    35,
    check=Range(5, 120),
    group=Group.VOICE,
    bots=BOTH,
    doc="Longest voice note.",
)
SPEECH_STT_PROVIDER = Setting(
    "speech.stt_provider",
    str,
    "auto",
    check=OneOf("auto", "google", "openrouter"),
    group=Group.SPEECH,
    bots=BOTH,
    doc="auto = Google, then OpenRouter, then none (SPEC §3.2.11).",
)
SPEECH_TTS_PROVIDER = Setting(
    "speech.tts_provider",
    str,
    "auto",
    check=OneOf("auto", "google", "openrouter", "edge", "off"),
    group=Group.SPEECH,
    bots=BOTH,
    doc="auto = Google, then OpenRouter, then Edge, then text only.",
)
STT_GOOGLE_MODEL = Setting(
    "stt.google_model",
    str,
    "gemini-3.5-transcribe",
    check=MODEL_ID,
    group=Group.SPEECH,
    bots=BOTH,
    doc="Gemini transcription model.",
)
STT_OPENROUTER_MODEL = Setting(
    "stt.openrouter_model",
    str,
    "google/gemini-3.8-flash",
    check=MODEL_ID,
    group=Group.SPEECH,
    bots=BOTH,
    doc="OpenRouter chat model used for STT with hints.",
)
TTS_GOOGLE_MODEL = Setting(
    "tts.google_model",
    str,
    "gemini-3.8-flash-tts",
    check=MODEL_ID,
    group=Group.SPEECH,
    bots=BOTH,
    doc="Gemini TTS model.",
)
TTS_OPENROUTER_MODEL = Setting(
    "tts.openrouter_model",
    str,
    "google/gemini-3.8-flash-tts",
    check=MODEL_ID,
    group=Group.SPEECH,
    bots=BOTH,
    doc="OpenRouter TTS model.",
)
TTS_EDGE_VOICE = Setting(
    "tts.edge_voice",
    str,
    "uz-UZ-MadinaNeural",
    check=MaxLen(80),
    group=Group.SPEECH,
    bots=BOTH,
    doc="Edge TTS voice (free fallback, P3).",
)
STT_MODE = Setting(
    "stt.mode",
    str,
    "smart",
    check=Regex(r"[a-z_]{1,32}", description="rejim nomi, masalan smart"),
    group=Group.SPEECH,
    bots=BOTH,
    doc="Gemini transcription_config mode.",
)
STT_SPLIT_OVER_S = Setting(
    "stt.split_over_s",
    int,
    20,
    check=Range(5, 120),
    group=Group.SPEECH,
    bots=BOTH,
    doc="Voice notes longer than this are split at silences and transcribed in parallel.",
)
STT_LANGUAGES = Setting(
    "stt.languages",
    list[str],
    ["uz-UZ", "ru-RU", "en-US"],
    check=LANGUAGE_TAG,
    group=Group.LANGUAGE,
    bots=BOTH,
    doc="Language hints for every transcription.",
)
STT_EXTRA_TERMS = Setting(
    "stt.extra_terms",
    list[str],
    [],
    check=MaxLen(1000),
    group=Group.LANGUAGE,
    bots=BOTH,
    doc="Terms added by chat ('bu so'zni to'g'ri yoz: …'), spelled exactly in transcripts.",
)
TIMEOUTS_STT_S = Setting(
    "timeouts.stt_s",
    float,
    8.0,
    check=Range(2, 60),
    group=Group.TIMEOUTS,
    bots=BOTH,
    doc="One transcription call.",
)
TIMEOUTS_TTS_S = Setting(
    "timeouts.tts_s",
    float,
    15.0,
    check=Range(2, 120),
    group=Group.TIMEOUTS,
    bots=BOTH,
    doc="One synthesis call.",
)

# --- routing ---
ROUTING_MIN_CONFIDENCE = Setting(
    "routing.min_confidence",
    float,
    0.75,
    check=Range(0.5, 1.0),
    group=Group.ROUTING,
    bots=DEV,
    doc="LLM route confidence needed to route without asking (UC-10).",
)
ROUTING_MIN_MARGIN = Setting(
    "routing.min_margin",
    float,
    0.2,
    check=Range(0.0, 1.0),
    group=Group.ROUTING,
    bots=DEV,
    doc="Lead over the second candidate needed to route without asking.",
)
ROUTING_CONTEXT_WINDOW_MIN = Setting(
    "routing.context_window_min",
    int,
    15,
    check=Range(1, 240),
    group=Group.ROUTING,
    bots=DEV,
    doc="Recent project activity counted as context for a follow-up.",
)
MODELS_ROUTING = _claude_model("models.routing", "haiku", bots=DEV, doc="Model of the warm routing assistant.")
TIMEOUTS_ROUTING_S = Setting(
    "timeouts.routing_s",
    float,
    2.5,
    check=Range(0.5, 15),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="LLM routing call; on timeout the context project or a question.",
)

# --- settings ---
MODELS_SETTINGS = _claude_model(
    "models.settings", "sonnet", bots=BOTH, doc="Model that interprets a settings change said in chat."
)

# --- turns ---
MODELS_CODING = _claude_model("models.coding", "opus", bots=DEV, doc="Model of project coding sessions.")
MODELS_CODING_EFFORT = _effort("models.coding_effort", "xhigh", bots=DEV, doc="Effort of coding sessions.")
PERSONA_DEV_TONE = Setting(
    "persona.dev_tone",
    str,
    "qisqa, aniq, texnik",
    check=MaxLen(300),
    group=Group.PERSONA,
    bots=DEV,
    doc="Tone of the dev bot's own messages.",
)
PROMPT_DEV_EXECUTOR_EXTRA = Setting(
    "prompt.dev_executor_extra",
    str,
    "",
    check=MaxLen(4000),
    group=Group.PROMPTS,
    bots=DEV,
    doc="Owner instructions appended to every coding session (slot in prompts/dev/).",
)
TURNS_MAX_CONCURRENT = Setting(
    "turns.max_concurrent",
    int,
    15,
    check=Range(1, 30),
    group=Group.CAPS,
    bots=DEV,
    weakens=increases,
    doc="Coding turns working at once; the rest queue (NFR-CAP-01).",
)
TURNS_MAX_WALL_MIN = Setting(
    "turns.max_wall_min",
    int,
    60,
    check=Range(5, 600),
    group=Group.CAPS,
    bots=DEV,
    weakens=increases,
    doc="Runaway guard: wall time per turn before it pauses and asks (NFR-COST-03).",
)
TURNS_MAX_STEPS = Setting(
    "turns.max_steps",
    int,
    200,
    check=Range(10, 5000),
    group=Group.CAPS,
    bots=DEV,
    weakens=increases,
    doc="Runaway guard: tool calls per turn.",
)
TURNS_MAX_SAME_FAILURE = Setting(
    "turns.max_same_failure",
    int,
    5,
    check=Range(2, 50),
    group=Group.CAPS,
    bots=DEV,
    weakens=increases,
    doc="Runaway guard: repeats of the same failing call.",
)
TURNS_RESULT_GRACE_S = Setting(
    "turns.result_grace_s",
    float,
    1.5,
    check=Range(0.5, 10),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="Silence after a result before the turn counts as complete (SPEC §5.1).",
)
SESSIONS_IDLE_CLOSE_MIN = Setting(
    "sessions.idle_close_min",
    int,
    10,
    check=Range(1, 240),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="An idle live session closes after this long.",
)
TIMEOUTS_SESSION_OPEN_S = Setting(
    "timeouts.session_open_s",
    float,
    60.0,
    check=Range(10, 300),
    group=Group.TIMEOUTS,
    bots=BOTH,
    doc="Opening a Claude session.",
)
GUARD_MIN_FREE_DISK_GB = Setting(
    "guard.min_free_disk_gb",
    int,
    10,
    check=Range(1, 500),
    group=Group.CAPS,
    bots=DEV,
    doc="Below this free disk the owner is warned once per 6 h (NFR-CAP-05).",
)
GUARD_BLOCK_DISK_GB = Setting(
    "guard.block_disk_gb",
    int,
    3,
    check=Range(1, 100),
    group=Group.CAPS,
    bots=DEV,
    doc="Below this free disk new coding turns queue.",
)
PARK_GRACE_S = Setting(
    "park.grace_s",
    int,
    15,
    check=Range(1, 120),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="Time a parking turn gets to stop at a safe point (P2).",
)

# --- questions ---
QUESTIONS_HOLD_PROCESS_MIN = Setting(
    "questions.hold_process_min",
    int,
    30,
    check=Range(1, 240),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="How long an agent question holds its process before the answer moves to a message.",
)

# --- digest ---
DIGEST_STALE_AFTER_MIN = Setting(
    "digest.stale_after_min",
    int,
    30,
    check=Range(1, 1440),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="A digest component older than this triggers a fresh check.",
)
SCHEDULE_DIGEST_ACTIVE_POLL_S = Setting(
    "schedule.digest_active_poll_s",
    int,
    120,
    check=Range(30, 3600),
    group=Group.SCHEDULES,
    bots=DEV,
    doc="Git poll for projects active within 7 days.",
)
SCHEDULE_DIGEST_IDLE_POLL_MIN = Setting(
    "schedule.digest_idle_poll_min",
    int,
    60,
    check=Range(5, 1440),
    group=Group.SCHEDULES,
    bots=DEV,
    doc="Git poll for the other projects.",
)
MODELS_REPORT = _claude_model("models.report", "sonnet", bots=DEV, doc="Model of the warm report assistant.")
MODELS_REPORT_EFFORT = _effort("models.report_effort", "low", bots=DEV, doc="Effort of the report assistant.")
TIMEOUTS_REPORT_S = Setting(
    "timeouts.report_s",
    float,
    3.5,
    check=Range(1, 15),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="Free-form report answer; on timeout the template answers.",
)

# --- servers ---
SERVERS_HOSTS = Setting(
    "servers.hosts",
    list[str],
    [],
    check=HostList(),
    group=Group.SERVERS,
    bots=DEV,
    doc="~/.ssh/config aliases checked read-only every schedule.server_check_min.",
)
SCHEDULE_SERVER_CHECK_MIN = Setting(
    "schedule.server_check_min",
    int,
    30,
    check=Range(5, 1440),
    group=Group.SCHEDULES,
    bots=DEV,
    doc="Interval of the read-only server checks.",
)
MODELS_INVESTIGATION = _claude_model(
    "models.investigation", "opus", bots=DEV, doc="Model of read-only investigation sessions."
)

# --- health ---
SCHEDULE_HEARTBEAT_EDIT_MIN = Setting(
    "schedule.heartbeat_edit_min",
    int,
    5,
    check=Range(1, 60),
    group=Group.SCHEDULES,
    bots=DEV,
    doc="Edit interval of the pinned 'Onlayn: HH:MM' message (UC-06: never older than 10 min).",
)
MEDIA_RETENTION_DAYS = Setting(
    "media.retention_days",
    int,
    7,
    check=Range(1, 90),
    group=Group.LIMITS,
    bots=BOTH,
    doc="Media cache files are purged after this many days.",
)

# --- integrations (P2) ---
TIMEOUTS_INTEGRATION_VALIDATE_S = Setting(
    "timeouts.integration_validate_s",
    float,
    25.0,
    check=Range(5, 60),
    group=Group.TIMEOUTS,
    bots=BOTH,
    doc="Validating a new integration (UC-08: answer within 30 s).",
)
INTEGRATIONS_OAUTH_REDIRECT_PORT = Setting(
    "integrations.oauth_redirect_port",
    int,
    33418,
    check=Range(1024, 65535),
    group=Group.LIMITS,
    bots=BOTH,
    doc="Port of the OAuth redirect URI the owner pastes back.",
)
INTEGRATIONS_OAUTH_PASTE_TIMEOUT_MIN = Setting(
    "integrations.oauth_paste_timeout_min",
    int,
    15,
    check=Range(1, 60),
    group=Group.TIMEOUTS,
    bots=BOTH,
    doc="How long an OAuth flow waits for the pasted callback URL.",
)
INTEGRATIONS_OAUTH_CLIENT_METADATA_URL = Setting(
    "integrations.oauth_client_metadata_url",
    str,
    "",
    check=Regex(r"(?:https://\S{1,500})?", description="https:// manzil yoki bo'sh"),
    group=Group.LIMITS,
    bots=BOTH,
    doc="Static HTTPS Client ID Metadata Document, for servers that require one.",
)

# --- newproject (P2) ---
NEWPROJECT_DRAFT_SILENCE_S = Setting(
    "newproject.draft_silence_s",
    int,
    120,
    check=Range(10, 1800),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="Silence that closes a new-project description.",
)
NEWPROJECT_PLAN_MAX_MIN = Setting(
    "newproject.plan_max_min",
    int,
    14,
    check=Range(1, 120),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="Wall limit of the planning turn (UC-14: plan within 15 min).",
)

# --- handoff (P2) ---
HANDOFF_SCAN_S = Setting(
    "handoff.scan_s",
    float,
    3.0,
    check=Range(1, 60),
    group=Group.SCHEDULES,
    bots=DEV,
    doc="Interval of the Mac-attach scan (UC-16: detect within 10 s).",
)

# --- selfupdate (P2) ---
SELFUPDATE_DRAIN_MAX_MIN = Setting(
    "selfupdate.drain_max_min",
    int,
    30,
    check=Range(1, 240),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="How long running coding turns may finish before an update asks.",
)
SELFUPDATE_HEALTH_TIMEOUT_S = Setting(
    "selfupdate.health_timeout_s",
    int,
    120,
    check=Range(30, 600),
    group=Group.TIMEOUTS,
    bots=DEV,
    doc="Probation time for a new release to prove healthy.",
)

# --- qa (P3) ---
PERSONA_PERSONAL = Setting(
    "persona.personal",
    str,
    "Short, precise and professional, a little warm. Takes its own evidence-based position, cites scholars and "
    "sources, and notes where they disagree.",
    check=MaxLen(2000),
    group=Group.PERSONA,
    bots=PERSONAL,
    doc="Persona of the personal assistant (prompt slot).",
)
PERSONA_PERSONAL_VERBOSITY = Setting(
    "persona.personal_verbosity",
    str,
    "short",
    check=OneOf("short", "medium", "long"),
    group=Group.PERSONA,
    bots=PERSONAL,
    doc="Default answer length of the personal assistant.",
)
PROMPT_PERSONAL_EXTRA = Setting(
    "prompt.personal_extra",
    str,
    "",
    check=MaxLen(4000),
    group=Group.PROMPTS,
    bots=PERSONAL,
    doc="Owner instructions appended to the personal assistant (slot in prompts/personal/).",
)
MODELS_QA = _claude_model("models.qa", "sonnet", bots=PERSONAL, doc="Model of the personal assistant.")
MODELS_QA_EFFORT = _effort("models.qa_effort", "medium", bots=PERSONAL, doc="Effort of the personal assistant.")
MODELS_FALLBACK_TEXT = Setting(
    "models.fallback_text",
    str,
    "gemini-3.8-flash",
    check=MODEL_ID,
    group=Group.MODELS,
    bots=PERSONAL,
    doc="'zaxira model' used while Claude is unavailable.",
)
QA_DRAFT_INTERVAL_S = Setting(
    "qa.draft_interval_s",
    float,
    1.0,
    check=Range(0.5, 10),
    group=Group.LIMITS,
    bots=PERSONAL,
    doc="At most one sendMessageDraft per this many seconds while streaming.",
)
QA_SESSION_IDLE_ROTATE_H = Setting(
    "qa.session_idle_rotate_h",
    int,
    6,
    check=Range(1, 72),
    group=Group.TIMEOUTS,
    bots=PERSONAL,
    doc="Idle time after which the assistant starts a new conversation (seeded with a summary).",
)
TEXT_MAX_CHARS = Setting(
    "text.max_chars",
    int,
    3500,
    check=Range(500, 4096),
    group=Group.LIMITS,
    bots=PERSONAL,
    doc="Longest default answer text; longer answers are compressed ('to'liqroq' gives the full one).",
)
TEXT_MAX_LINES = Setting(
    "text.max_lines",
    int,
    40,
    check=Range(5, 200),
    group=Group.LIMITS,
    bots=PERSONAL,
    doc="Most lines in a default answer.",
)

# --- media (P3) ---
SPEECH_VIDEO_PROVIDER = Setting(
    "speech.video_provider",
    str,
    "auto",
    check=OneOf("auto", "google", "openrouter", "off"),
    group=Group.SPEECH,
    bots=PERSONAL,
    doc="auto = Google, then OpenRouter, then none (pages still work through WebFetch).",
)

# --- wiring (bots) ---


# Every Setting bound above, by key (Registry rejects a duplicate key).
REGISTRY: Final = Registry.from_namespace(globals())
