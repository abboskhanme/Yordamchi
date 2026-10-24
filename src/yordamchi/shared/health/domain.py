"""Health rules, pure (SPEC §2.5 step 8, §2.8, §5.6 smoke test, §6.2 `holat?`, §9.2 disk; UC-06, NFR-REL-01/04/08,
NFR-CAP-05).

This module decides, from values only:
- what `run/<bot>.heartbeat.json` holds (`heartbeat_json`), in the shape the supervisor's watchdog reads, and when
  the 30 s timer may refresh it (`timer_writes`): while polling is paused on purpose, and while polls fail on the
  network, but never for a poll loop that stopped trying, so a poller that died silently leaves stale attempts for
  the watchdog's 120 s limit (NFR-REL-01, T32b);
- whether the Mac slept (`gap_since`: ≥ 5 min of wall clock between two ticks) and what the owner reads after the
  first `getUpdates` batch that follows (NFR-REL-04); likewise a network outage of ≥ 5 min (`network_gap`);
- the restart notice after an unclean exit (SPEC §2.5 step 8);
- the pinned "Onlayn: HH:MM" message (`plan_online`): send and pin it once, edit it in place, and send and pin a new
  one when Telegram refused an edit because the message is gone (SPEC §2.8);
- when a `claude --version` change calls for the smoke turn (`cli_change`, NFR-REL-08) and what a failure says;
- the disk guard (`disk_level`, `disk_warning_due`: once per 6 h, SPEC §9.2) and which `media/<yyyymmdd>/` folders
  the purge removes;
- the `holat?` text (`holat_text`) from a `HealthView` the service reads from local state only.

Every input is an argument, `now` and setting values included; nothing here does I/O (C6, A1).
"""

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Final

from yordamchi.core import strings as S
from yordamchi.core import timefmt
from yordamchi.core.json_value import JsonObject, JsonValue
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText, join
from yordamchi.ports.agent import RuntimeStatus
from yordamchi.ports.mac_host import ClaudeManagerState
from yordamchi.shared.outbox import domain as outbox
from yordamchi.shared.outbox.domain import Delivery, Kind, Status
from yordamchi.shared.voice import domain as voice

# --- fixed values of the SPEC (mechanism, not owner behaviour; §4.3 has no key for them) ---------------------------
HEARTBEAT_EVERY_S: Final = 30.0  # §2.8: the file is rewritten at least this often while paused
# Normal mode: before the first successful `getUpdates` the timer shows the new process (its `started_at`) for this
# long; after that only a successful poll refreshes the file (idle polls return every 25 s), so a dead poller goes
# stale after exactly the watchdog's 120 s.
FIRST_POLL_GRACE: Final = timedelta(seconds=60)
# A live poll loop attempts `getUpdates` at least this often: a long poll takes ≤ 35 s, and after a failure it waits
# ≤ 30 s, then probes for ≤ 10 s (`bots.common.PollConfig`). While polls fail on the network the timer keeps the file
# fresh only this long after the last attempt, so a loop that died offline still leaves a stale mtime for a reader
# that judges by it alone (a supervisor from before T32b).
LIVE_ATTEMPT_WINDOW: Final = timedelta(seconds=60)
WAKE_GAP_MIN: Final = timedelta(minutes=5)  # §2.8, NFR-REL-04: a gap this long between ticks means the Mac slept
# T32b: an outage this long is told once polling is back; a shorter one only delayed messages by a few minutes.
NETWORK_GAP_MIN: Final = timedelta(minutes=5)
CHECK_EVERY_S: Final = 600.0  # §5.6 `claude --version` (at start, then every 10 min); §9.2 disk guard (10 min)
DISK_WARN_EVERY: Final = timedelta(hours=6)  # §9.2: `S.DISK_LOW` at most this often
EVENT_KEEP: Final = timedelta(days=90)  # §4.2 `health_events`: 90 d
PROBE_TIMEOUT_S: Final = 20.0  # `claude --version` + `claude auth status`, off the polling path
HOST_TIMEOUT_S: Final = 5.0  # one MacHost reading outside `holat?`
HOLAT_PART_TIMEOUT_S: Final = 1.5  # each part of `holat?`, read concurrently: the answer stays within 3 s (UC-06)
# The smoke runner bounds its own turn (§5.3: wall 5 min, plus opening the session); this backstop only stops a
# runner that never returns from holding the checks loop forever.
SMOKE_BACKSTOP_S: Final = 600.0
ERRORS_KEPT: Final = 3  # §6.2: `holat?` shows the last 3 error codes
GIB: Final = 1024**3
LAST_ALIVE_KEY: Final = "last_poll_ok_at"
VERSION_MAX: Final = 64  # a CLI version as stored and shown; `claude --version` prints e.g. `2.1.310`

# `kv_state` keys (§4.2: pinned message id, last CLI version, …), one row per bot each.
KV_ONLINE: Final = "online_message"
KV_CLI_VERSION: Final = "cli_version"
KV_DISK_WARNED: Final = "disk_warned_at"

ONLINE_REF: Final = "online"  # the first "Onlayn" message is `heartbeat:online:0`, its pin `heartbeat:online:pin`
ONLINE_COALESCE: Final = "heartbeat-online"  # one pending edit at a time: a newer edit supersedes an older one
_MEDIA_DAY: Final = re.compile(r"\d{8}")


class Mode(StrEnum):
    """`mode` in the heartbeat file (SPEC §2.8): `paused` while polling is stopped on purpose, `probation` during a
    self-update's health check (§3.2.12)."""

    NORMAL = "normal"
    PAUSED = "paused"
    PROBATION = "probation"


class PollErrorKind(StrEnum):
    """`last_poll_error_kind` in the heartbeat file: how the latest `getUpdates` attempt went (T32b).

    `network`: only the connection failed (DNS, connect, a timeout, or a 5xx or garbage from Telegram or a proxy).
    The bot is offline, not dead, and the watchdog waits for the network instead of restarting it. `other`: any other
    failure (a revoked token, a 409 conflict, a 429), judged as before T32b. The polling runner classifies them
    (`bots.common.poll_error_kind`).
    """

    NONE = "none"
    NETWORK = "network"
    OTHER = "other"


class EventKind(StrEnum):
    """`health_events.kind` (SPEC §4.2)."""

    STARTED = "started"
    CLEAN_EXIT = "clean_exit"
    UNCLEAN_RESTART = "unclean_restart"
    WAKE_GAP = "wake_gap"
    NETWORK_GAP = "network_gap"
    SMOKE_OK = "smoke_ok"
    SMOKE_FAILED = "smoke_failed"
    CLI_VERSION = "cli_version"
    PRESSURE = "pressure"
    DISK_LOW = "disk_low"


# --- the heartbeat file (SPEC §2.8) --------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Heartbeat:
    """One `run/<bot>.heartbeat.json`. The watchdog reads `pid` and `started_at` (a new value is a new start). In
    normal mode it judges a polling bot by `last_poll_attempt_at` and `last_poll_error_kind` (T32b): fresh attempts
    that fail on the network are offline, not dead. Otherwise, as before, by the file's mtime. Probation reads
    `version`, `mode` and `last_poll_ok_at`. The attempt fields are empty before the first `getUpdates` attempt."""

    pid: int
    version: str
    started_at: datetime
    last_poll_ok_at: datetime | None
    mode: Mode
    loop_lag_ms: int
    last_poll_attempt_at: datetime | None = None
    last_poll_error_kind: PollErrorKind = PollErrorKind.NONE
    consecutive_poll_failures: int = 0


def heartbeat_json(beat: Heartbeat) -> JsonObject:
    """The file's content: instants as ISO 8601 with their UTC offset; `last_poll_ok_at` is null before the first
    successful `getUpdates`, `last_poll_attempt_at` before the first attempt."""
    return {
        "pid": beat.pid,
        "version": beat.version,
        "started_at": _iso(beat.started_at),
        LAST_ALIVE_KEY: _iso(beat.last_poll_ok_at),
        "mode": beat.mode.value,
        "loop_lag_ms": max(0, beat.loop_lag_ms),
        "last_poll_attempt_at": _iso(beat.last_poll_attempt_at),
        "last_poll_error_kind": beat.last_poll_error_kind.value,
        "consecutive_poll_failures": max(0, beat.consecutive_poll_failures),
    }


def _iso(moment: datetime | None) -> str | None:
    return None if moment is None else timefmt.require_aware(moment).isoformat()


def last_alive(raw: JsonValue) -> datetime | None:
    """When the previous run last polled, read from the heartbeat file it left; None when unreadable."""
    stamp = raw.get(LAST_ALIVE_KEY) if isinstance(raw, dict) else None
    if not isinstance(stamp, str):
        return None
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def timer_writes(
    mode: Mode,
    *,
    last_poll_ok_at: datetime | None,
    started_at: datetime,
    now: datetime,
    last_poll_attempt_at: datetime | None = None,
    last_poll_error_kind: PollErrorKind = PollErrorKind.NONE,
) -> bool:
    """Whether the 30 s timer rewrites the file now. Paused or in probation: always (SPEC §2.8: polling is stopped on
    purpose). Normal: in the first minute before the first successful poll, and while polls fail on the network
    within `LIVE_ATTEMPT_WINDOW` of the last attempt (T32b: at least every 30 s while offline). Otherwise only a
    `getUpdates` attempt refreshes it, so the watchdog restarts a bot whose polling died."""
    if mode is not Mode.NORMAL:
        return True
    if (
        last_poll_error_kind is PollErrorKind.NETWORK
        and last_poll_attempt_at is not None
        and now - last_poll_attempt_at <= LIVE_ATTEMPT_WINDOW
    ):
        return True
    return last_poll_ok_at is None and now - started_at <= FIRST_POLL_GRACE


# --- sleep and wake (SPEC §2.8, NFR-REL-04) ------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Gap:
    """The Mac slept from `from_` (the last tick before) to `to` (the first tick after); for an outage, from the
    first failed poll to the first successful one."""

    from_: datetime
    to: datetime


def gap_since(previous: datetime | None, now: datetime) -> Gap | None:
    """A gap of ≥ 5 min of wall clock between two heartbeat ticks, else None. The first tick has nothing before it:
    a start after sleep is a restart, and the restart notice covers it."""
    if previous is None or now - previous < WAKE_GAP_MIN:
        return None
    return Gap(from_=previous, to=now)


def merge_gaps(pending: Gap | None, new: Gap | None) -> Gap | None:
    """Two sleeps before the first successful poll are one notice: from the first sleep to the last wake."""
    if pending is None or new is None:
        return pending or new
    return Gap(from_=min(pending.from_, new.from_), to=max(pending.to, new.to))


def wake_text(gap: Gap, *, updates: int, timezone: str) -> BotText:
    """`S.WAKE_GAP`, posted after the first `getUpdates` batch after the gap; `updates` is that batch's size."""
    return S.WAKE_GAP(from_=timefmt.hhmm(gap.from_, timezone), to=timefmt.hhmm(gap.to, timezone), n=max(0, updates))


def wake_ref(gap: Gap) -> str:
    return f"wake-{int(gap.to.timestamp())}"


def network_gap(offline_since: datetime | None, back_at: datetime) -> Gap | None:
    """The outage to tell after the first successful poll at `back_at` (T32b): from the first network failure of
    the streak, when it lasted ≥ 5 min; else None. A sleep inside it is told separately (`S.WAKE_GAP`)."""
    if offline_since is None or back_at - offline_since < NETWORK_GAP_MIN:
        return None
    return Gap(from_=offline_since, to=back_at)


def network_gap_text(gap: Gap, *, timezone: str) -> BotText:
    return S.NETWORK_GAP(from_=timefmt.hhmm(gap.from_, timezone), to=timefmt.hhmm(gap.to, timezone))


def network_gap_ref(gap: Gap) -> str:
    return f"network-{int(gap.to.timestamp())}"


# --- restart (SPEC §2.5 step 8) ------------------------------------------------------------------------------------
def restart_text(interrupted: Sequence[str], *, last_alive_at: datetime | None, timezone: str) -> BotText:
    """The "Qayta ishga tushdim (…)" notice, with the projects whose turns the restart cut off, or that none were."""
    reason = (
        S.HEALTH_REASON_UNCLEAN()
        if last_alive_at is None
        else S.HEALTH_REASON_UNCLEAN_SINCE(time=timefmt.hhmm(last_alive_at, timezone))
    )
    projects = list(dict.fromkeys(interrupted))
    if not projects:
        return S.HEALTH_RESTARTED_IDLE(reason=reason)
    return S.HEALTH_RESTARTED(reason=reason, interrupted=", ".join(projects))


def restart_ref(started_at: datetime) -> str:
    return f"restart-{int(started_at.timestamp())}"


# --- the pinned "Onlayn: HH:MM" message (SPEC §2.8, UC-06) ---------------------------------------------------------
@dataclass(frozen=True, slots=True)
class OnlineMessage:
    """The `kv_state` row of the pinned message. Generation 1 is `heartbeat:online:0`; each re-send after the
    message was deleted gets the next generation (`online-2`, …), since a dedupe key is never reused."""

    generation: int = 1
    message_id: int | None = None
    edits: int = 0

    @property
    def ref(self) -> str:
        return ONLINE_REF if self.generation <= 1 else f"{ONLINE_REF}-{self.generation}"

    @property
    def send_key(self) -> str:
        return outbox.dedupe_key(Kind.HEARTBEAT, self.ref, 0)

    @property
    def edit_part(self) -> str:
        """The part of the latest edit's dedupe key (`heartbeat:<ref>:edit<n>`)."""
        return f"edit{self.edits}"

    @property
    def last_edit_key(self) -> str | None:
        return outbox.dedupe_key(Kind.HEARTBEAT, self.ref, self.edit_part) if self.edits else None

    def to_json(self) -> JsonObject:
        return {"generation": self.generation, "message_id": self.message_id, "edits": self.edits}

    @classmethod
    def from_json(cls, raw: JsonValue) -> "OnlineMessage | None":
        """The stored row, or None when it is not one (then a new message is sent)."""
        if not isinstance(raw, dict):
            return None
        generation, edits, message_id = _int(raw.get("generation")), _int(raw.get("edits")), raw.get("message_id")
        if generation is None or edits is None or (message_id is not None and _int(message_id) is None):
            return None
        return cls(generation=generation, message_id=_int(message_id), edits=edits)


def _int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class OnlineStep(StrEnum):
    SEND = "send"  # post a new message and pin it
    EDIT = "edit"  # edit the pinned message in place
    WAIT = "wait"  # the first post is still on its way: nothing to edit yet


@dataclass(frozen=True, slots=True)
class OnlinePlan:
    step: OnlineStep
    message: OnlineMessage  # the state to store after this step


def edit_refused(delivery: Delivery) -> bool:
    """Telegram refused the edit itself (the message to edit is gone): the outbox gives up on such a call at its
    first attempt, or finds no message to edit. An edit that failed only after its retries (a network drop) is not
    refused; the next edit tries again, so an outage never posts a second "Onlayn"."""
    if delivery.status is not Status.FAILED:
        return False
    return delivery.last_error_code == outbox.Code.TARGET_MISSING.value or (
        delivery.attempts <= 1 and delivery.last_error_code != outbox.Code.DELIVERY_UNKNOWN.value
    )


def plan_online(current: OnlineMessage | None, *, sent: Delivery | None, last_edit: Delivery | None) -> OnlinePlan:
    """The next step for the pinned message. `sent` is the outbox state of `current.send_key`, `last_edit` of
    `current.last_edit_key` (None when there is no such row, e.g. after the 30-day outbox retention)."""
    if current is None:
        return OnlinePlan(OnlineStep.SEND, OnlineMessage())
    fresh = OnlineMessage(generation=current.generation + 1)
    if current.message_id is None:
        if sent is None or sent.status is Status.FAILED:
            return OnlinePlan(OnlineStep.SEND, fresh)
        if sent.message_id is None:
            return OnlinePlan(OnlineStep.WAIT, current)
        current = replace(current, message_id=sent.message_id)
    elif last_edit is not None and edit_refused(last_edit):
        return OnlinePlan(OnlineStep.SEND, fresh)
    return OnlinePlan(OnlineStep.EDIT, replace(current, edits=current.edits + 1))


def online_text(now: datetime, *, timezone: str) -> BotText:
    return S.HEALTH_ONLINE(time=timefmt.hhmm(now, timezone))


# --- the CLI smoke test (SPEC §5.6, NFR-REL-08) --------------------------------------------------------------------
class CliChange(StrEnum):
    BASELINE = "baseline"  # no version recorded yet: this one is the reference, no smoke turn
    SAME = "same"
    CHANGED = "changed"  # the CLI auto-updated: one smoke turn


def cli_change(previous: str | None, current: str) -> CliChange:
    if previous is None:
        return CliChange.BASELINE
    return CliChange.SAME if previous == current else CliChange.CHANGED


@dataclass(frozen=True, slots=True)
class SmokeOutcome:
    """What the smoke turn showed. `step` names what failed (`timeout`, `commit`, `file`, an error code, …)."""

    ok: bool
    step: str = ""


def smoke_failed_text(version: str, step: str) -> BotText:
    entry = S.SMOKE_STEPS.get(step)
    reason = entry() if entry is not None else S.SMOKE_STEP_OTHER(code=step or "unknown")
    return S.SMOKE_FAILED(version=version, step=reason)


def smoke_ref(version: str) -> str:
    """One failure notice per version: a restart during the same version's smoke test is not told twice."""
    return f"smoke-{version[:VERSION_MAX]}"


@dataclass(frozen=True, slots=True)
class ClaudeCheck:
    """The last `claude --version` + `claude auth status`, cached for `holat?` (it never runs on the polling
    path). `error_code` is set when the probe itself failed."""

    at: datetime
    installed: bool = False
    version: str | None = None
    logged_in: bool = False
    auth_method: str | None = None
    error_code: str | None = None


EXPECTED_AUTH_METHOD: Final = "claude.ai"  # §2.5 step 7: the subscription login, never an API key


def claude_check(status: RuntimeStatus, *, at: datetime) -> ClaudeCheck:
    return ClaudeCheck(
        at=at,
        installed=status.installed,
        version=status.version,
        logged_in=status.logged_in,
        auth_method=status.auth_method,
    )


# --- disk guard and media purge (SPEC §9.2, NFR-CAP-05) ------------------------------------------------------------
class DiskLevel(StrEnum):
    OK = "ok"
    LOW = "low"  # under `guard.min_free_disk_gb`: warn
    BLOCKED = "blocked"  # under `guard.block_disk_gb`: new coding turns queue (the turn scheduler reads it too)


def disk_level(free_bytes: int, *, warn_gb: int, block_gb: int) -> DiskLevel:
    if free_bytes < block_gb * GIB:
        return DiskLevel.BLOCKED
    if free_bytes < warn_gb * GIB:
        return DiskLevel.LOW
    return DiskLevel.OK


_SEVERITY: Final[Mapping[DiskLevel, int]] = {DiskLevel.OK: 0, DiskLevel.LOW: 1, DiskLevel.BLOCKED: 2}


@dataclass(frozen=True, slots=True)
class DiskWarning:
    """The `kv_state` row of the last disk warning: when, and how bad it was."""

    at: datetime
    level: DiskLevel

    def to_json(self) -> JsonObject:
        return {"at": timefmt.require_aware(self.at).isoformat(), "level": self.level.value}

    @classmethod
    def from_json(cls, raw: JsonValue) -> "DiskWarning | None":
        if not isinstance(raw, dict):
            return None
        at, level = raw.get("at"), raw.get("level")
        if not isinstance(at, str) or level not in {item.value for item in DiskLevel}:
            return None
        try:
            moment = datetime.fromisoformat(at)
        except ValueError:
            return None
        return cls(at=moment, level=DiskLevel(str(level))) if moment.tzinfo is not None else None


def disk_warning_due(level: DiskLevel, *, last: DiskWarning | None, now: datetime) -> bool:
    """Warn at most once per 6 h (SPEC §9.2), but at once when it got worse (low → blocked)."""
    if level is DiskLevel.OK:
        return False
    if last is None or _SEVERITY[level] > _SEVERITY[last.level]:
        return True
    return now - last.at >= DISK_WARN_EVERY


def gigabytes(free_bytes: int) -> int:
    """Whole GB, rounded down: "2 GB qoldi" never overstates the space."""
    return max(0, free_bytes // GIB)


def disk_text(level: DiskLevel, free_bytes: int, *, warn_gb: int, block_gb: int) -> BotText:
    if level is DiskLevel.BLOCKED:
        return S.DISK_BLOCKED(free=gigabytes(free_bytes), limit=block_gb)
    return S.DISK_LOW(free=gigabytes(free_bytes), limit=warn_gb)


def expired_media_dirs(names: Iterable[str], *, today: date, keep_days: int) -> tuple[str, ...]:
    """The `media/<yyyymmdd>/` folders older than `keep_days` (`media.retention_days`), oldest first. Any other
    name is never touched."""
    cutoff = today - timedelta(days=keep_days)
    found: list[tuple[date, str]] = []
    for name in names:
        if not _MEDIA_DAY.fullmatch(name):
            continue
        try:
            day = date(int(name[:4]), int(name[4:6]), int(name[6:]))
        except ValueError:
            continue
        if day < cutoff:
            found.append((day, name))
    return tuple(name for _, name in sorted(found))


def event_cutoff(now: datetime) -> datetime:
    """A `health_events` row is deleted once `at` is strictly older than this (SPEC §4.2: 90 d)."""
    return now - EVENT_KEEP


# --- `holat?` (UC-06, SPEC §6.2) -----------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class QueuedWork:
    project_id: str
    position: int  # 1 = next to start


@dataclass(frozen=True, slots=True)
class WorkStatus:
    """The dev bot's work as `holat?` shows it. `shared` cannot import `dev` (C1), so the dev bot injects a reader
    (`turns.service.work_status`) that returns this."""

    running: tuple[str, ...] = ()
    queued: tuple[QueuedWork, ...] = ()
    decisions: int = 0
    questions: int = 0


@dataclass(frozen=True, slots=True)
class ErrorNote:
    code: str
    at: datetime


class Unread(StrEnum):
    """A part of `holat?` that could not be read within its time budget."""

    UNREAD = "unread"


UNREAD: Final = Unread.UNREAD


@dataclass(frozen=True, slots=True)
class HealthView:
    """Everything `holat?` shows, read from local state only. `work` is None on a bot without turns (personal)."""

    bot: BotName
    now: datetime
    timezone: str
    started_at: datetime
    version: str
    sdk_version: str
    claude: ClaudeCheck | None
    manager: ClaudeManagerState | Unread | None
    speech_providers: tuple[str, ...] | Unread  # in STT order; () = no usable speech key
    work: WorkStatus | Unread | None
    pressure_level: int | Unread
    free_disk_bytes: int | Unread
    degraded: tuple[str, ...] = ()
    errors: tuple[ErrorNote, ...] = field(default_factory=tuple)


def duration_text(seconds: float) -> BotText:
    """`12 daq`, `3 soat 5 daq`, `2 kun 4 soat`."""
    minutes = max(0, int(seconds // 60))
    if minutes < 60:
        return S.HOLAT_MINUTES(minutes=minutes)
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return S.HOLAT_HOURS(hours=hours, minutes=minutes)
    days, hours = divmod(hours, 24)
    return S.HOLAT_DAYS(days=days, hours=hours)


def _claude_line(check: ClaudeCheck | None) -> BotText:
    if check is None:
        return S.HOLAT_CLAUDE_UNCHECKED()
    if check.error_code is not None:
        return S.HOLAT_CLAUDE_FAILED(code=check.error_code)
    if not check.installed:
        return S.HOLAT_CLAUDE_MISSING()
    version = check.version or S.HOLAT_UNKNOWN()
    if not check.logged_in:
        return S.HOLAT_CLAUDE_LOGGED_OUT(version=version)
    if check.auth_method != EXPECTED_AUTH_METHOD:
        return S.HOLAT_CLAUDE_WRONG_AUTH(version=version, method=check.auth_method or S.HOLAT_UNKNOWN())
    return S.HOLAT_CLAUDE_OK(version=version)


def _percent(value: float | None) -> BotText:
    return S.HOLAT_UNKNOWN() if value is None else S.HOLAT_PERCENT(value=round(value))


def _manager_line(manager: ClaudeManagerState | Unread | None) -> BotText:
    if not isinstance(manager, ClaudeManagerState):
        return S.HOLAT_MANAGER_UNREAD()
    active = next((account for account in manager.accounts if account.is_active), None)
    if active is None:
        return S.HOLAT_MANAGER_NO_ACTIVE()
    return S.HOLAT_MANAGER(
        email=active.email, five=_percent(active.five_hour_pct), seven=_percent(active.seven_day_pct)
    )


def _speech_line(providers: tuple[str, ...] | Unread) -> BotText:
    """The same line the bot says after a new speech key, with no key outcome: what STT uses now, by name only."""
    if isinstance(providers, Unread):
        return S.HOLAT_SPEECH_UNREAD()
    return voice.speech_status((), providers)


def _work_lines(work: WorkStatus | Unread) -> list[BotText]:
    if not isinstance(work, WorkStatus):
        return [S.HOLAT_WORK_UNREAD()]
    lines = [S.HOLAT_RUNNING(projects=", ".join(work.running)) if work.running else S.HOLAT_RUNNING_NONE()]
    if work.queued:
        items = ", ".join(S.HOLAT_QUEUED_ITEM(project=item.project_id, position=item.position) for item in work.queued)
        lines.append(S.HOLAT_QUEUED(items=items))
    lines.append(S.HOLAT_PENDING(decisions=work.decisions, questions=work.questions))
    return lines


def _mac_line(pressure: int | Unread, free: int | Unread) -> BotText:
    level = S.HOLAT_PRESSURE.get(pressure) if isinstance(pressure, int) else None
    disk = S.HOLAT_GB(gb=gigabytes(free)) if isinstance(free, int) else S.HOLAT_UNKNOWN()
    return S.HOLAT_MAC(pressure=level() if level is not None else S.HOLAT_UNKNOWN(), free=disk)


def _errors_line(errors: Sequence[ErrorNote], timezone: str) -> BotText:
    if not errors:
        return S.HOLAT_NO_ERRORS()
    items = ", ".join(
        S.HOLAT_ERROR_ITEM(code=note.code, time=timefmt.hhmm(note.at, timezone)) for note in reversed(errors)
    )
    return S.HOLAT_ERRORS(errors=items)


BOT_NAMES: Final[Mapping[BotName, S.Msg]] = {
    BotName.DEV: S.SETTINGS_BOTS["dev"],
    BotName.PERSONAL: S.SETTINGS_BOTS["personal"],
}


def holat_text(view: HealthView) -> BotText:
    """The `holat?` answer (SPEC §6.2): uptime and versions, Claude login and ClaudeManager, speech, the work (dev),
    the Mac, degraded capabilities, the last errors (newest first)."""
    lines = [
        S.HOLAT_HEAD(bot=BOT_NAMES[view.bot]()),
        S.HOLAT_UPTIME(
            uptime=duration_text((view.now - view.started_at).total_seconds()),
            version=view.version,
            sdk=view.sdk_version or S.HOLAT_UNKNOWN(),
        ),
        _claude_line(view.claude),
        _manager_line(view.manager),
        _speech_line(view.speech_providers),
    ]
    if view.work is not None:
        lines.extend(_work_lines(view.work))
    lines.append(_mac_line(view.pressure_level, view.free_disk_bytes))
    if view.degraded:
        lines.append(S.HOLAT_DEGRADED(missing=", ".join(dict.fromkeys(view.degraded))))
    lines.append(_errors_line(view.errors, view.timezone))
    return join(lines)


# --- self-update flags (SPEC §3.2.12 steps 2, 5–7; T57's supervisor writes them, both bots read them) ---------------
PEEK_LIMIT: Final = 1  # probation `getUpdates(offset=N, limit=1, timeout=0)`: proves polling, consumes nothing
PEEK_TIMEOUT_S: Final = 0


@dataclass(frozen=True, slots=True)
class ProbationFlag:
    """`run/flags/probation.json` naming this release: start in probation (heartbeat, socket and a peek poller only,
    nothing real written) until the supervisor deletes the flag."""

    update_id: str
    version: str
    nonce: str


def probation_for(raw: JsonValue, version: str) -> ProbationFlag | None:
    """The flag, when it names `version`; a flag for another release (the old one restarting) is not ours."""
    if not isinstance(raw, dict):
        return None
    update_id, named, nonce = raw.get("id"), raw.get("version"), raw.get("nonce")
    if not (isinstance(named, str) and named == version and isinstance(nonce, str) and nonce):
        return None
    return ProbationFlag(update_id=update_id if isinstance(update_id, str) else "", version=named, nonce=nonce)


@dataclass(frozen=True, slots=True)
class DrainFlag:
    """`run/flags/drain.json`: stop starting turns, let coding turns finish (≤ `drain_max_min`, then ask), pause
    polling and write `run/<bot>.drained` with this id."""

    update_id: str
    since: datetime | None
    drain_max_min: int


def drain_flag(raw: JsonValue) -> DrainFlag | None:
    if not isinstance(raw, dict):
        return None
    update_id = raw.get("id")
    if not isinstance(update_id, str) or not update_id:
        return None
    since = raw.get("since")
    try:
        started = datetime.fromisoformat(since) if isinstance(since, str) else None
    except ValueError:
        started = None
    limit = raw.get("drain_max_min")
    return DrainFlag(
        update_id=update_id,
        since=started if started is not None and started.tzinfo is not None else None,
        drain_max_min=limit if isinstance(limit, int) and not isinstance(limit, bool) else 30,
    )


def drained_json(flag: DrainFlag, *, at: datetime) -> JsonObject:
    return {"id": flag.update_id, "at": timefmt.require_aware(at).isoformat()}
