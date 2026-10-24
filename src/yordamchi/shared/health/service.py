"""Health (SPEC §2.5 steps 7–8, §2.6, §2.8, §5.6, §6.2, §9.2; UC-06; NFR-REL-01/02/04/08, NFR-CAP-05).

- **Heartbeat file.** `record_poll` is the polling runner's `heartbeat` hook: after every `getUpdates` attempt,
  success or failure, it rewrites `run/<bot>.heartbeat.json` atomically (temp file, fsync, rename) with
  `last_poll_attempt_at`, `last_poll_error_kind` and `consecutive_poll_failures`; an idle long poll returns every
  25 s. `run_heartbeat` is the 30 s timer for the time polling is paused on purpose (`set_mode`), for the start
  before the first poll, and while polls fail on the network (T32b). Otherwise only a poll refreshes the file, so
  the supervisor's watchdog still restarts a bot whose polling died (no attempt for > 120 s, NFR-REL-01), and
  leaves alone one that is only offline.
- **Sleep and wake, network outages.** Both write paths are heartbeat ticks. A wall-clock gap of ≥ 5 min between two
  ticks means the Mac slept; after the first successful `getUpdates` batch that follows, `S.WAKE_GAP` names the gap
  and the batch size (NFR-REL-04). Polls that failed on the network for ≥ 5 min are told the same way, once, with
  `S.NETWORK_GAP` (T32b).
- **Restarts.** `report_start` reads and removes `run/<bot>.clean`: without it the previous run ended uncleanly,
  and `S.HEALTH_RESTARTED` lists the turns it cut off (from `turns.recover_turns`) within seconds of the start.
  `mark_clean_exit` writes the marker as the last step of a SIGTERM shutdown (SPEC §2.6).
- **"Onlayn: HH:MM".** `refresh_online` (dev bot) sends and pins it once, then edits it in place every
  `schedule.heartbeat_edit_min`; its message id lives in `kv_state`. A refused edit means the message is gone: a new
  one is sent and pinned.
- **Self-checks.** `run_checks` (every 10 min) caches `claude --version` + `claude auth status` for `holat?`, never on
  the polling path. A changed CLI version runs one smoke turn through the dev bot's `SmokeRunner`; `smoke_ok` /
  `smoke_failed` goes to `health_events`, and only a failure is told to the owner (NFR-REL-08). On the dev bot it also
  runs the disk guard: `S.DISK_LOW` at most once per 6 h (the turn scheduler queues coding turns by itself under
  `guard.block_disk_gb`).
- **`holat?`.** `show_health` reads local state only, each part bounded and read at once, so it answers in ≤ 3 s.
- **Daily.** `purge_media` (`media/<yyyymmdd>/` after `media.retention_days`) and `apply_retention` (90 d events),
  called by the bot's retention scheduler.

`shared` cannot import `dev` (C1), so the dev bot passes its readers in: `work=functools.partial(
turns.service.work_status, deps)` for `holat?` and `smoke=functools.partial(turns.service.run_smoke_turn, deps,
turns_state)` for the checks. Wiring order at start (T34): `new_state` → `turns.recover_turns` → `report_start` (it
reads the previous heartbeat file, so before the first write) → the loops.

Ports are awaited only outside units of work (A3). Logs carry ids, codes, counts and durations, never text.
"""

import asyncio
import os
import shutil
import tempfile
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from yordamchi.core import json_value, timefmt
from yordamchi.core.errors import ExternalError
from yordamchi.core.json_value import JsonValue
from yordamchi.core.log import get_log
from yordamchi.core.paths import BotName
from yordamchi.core.text import BotText
from yordamchi.persistence import bulk
from yordamchi.shared.access import service as access
from yordamchi.shared.deps import SharedDeps
from yordamchi.shared.health import domain, store
from yordamchi.shared.health.domain import (
    UNREAD,
    ClaudeCheck,
    DiskLevel,
    DiskWarning,
    ErrorNote,
    EventKind,
    Gap,
    HealthView,
    Heartbeat,
    Mode,
    OnlineMessage,
    OnlineStep,
    PollErrorKind,
    SmokeOutcome,
    Unread,
    WorkStatus,
)
from yordamchi.shared.health.models import KvState
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.settings.domain import Snapshot
from yordamchi.shared.vault import service as vault
from yordamchi.shared.voice import domain as voice

log = get_log(__name__)

# The dev bot's readers (C1: `shared` never imports `dev`).
type WorkProbe = Callable[[], Awaitable[WorkStatus]]
type SmokeRunner = Callable[[str], Awaitable[SmokeOutcome]]


# --- in-memory state ------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class HealthState:
    """This process's health, in memory: what the heartbeat file says and what `holat?` shows. Built once by
    `new_state` in `bots/<bot>/app.py`."""

    version: str
    started_at: datetime
    pid: int
    sdk_version: str = ""
    degraded: tuple[str, ...] = ()  # SPEC §2.5 step 4: ffmpeg, gh, node not found
    mode: Mode = Mode.NORMAL
    last_poll_ok_at: datetime | None = None
    last_poll_attempt_at: datetime | None = None
    last_poll_error_kind: PollErrorKind = PollErrorKind.NONE
    consecutive_poll_failures: int = 0
    offline_since: datetime | None = None  # the first network failure since the last successful poll
    last_tick_at: datetime | None = None
    pending_gap: Gap | None = None  # the Mac slept; told after the next successful poll
    pending_network_gap: Gap | None = None  # the network was down ≥ 5 min; told right after `pending_gap`
    loop_lag_ms: int = 0
    claude: ClaudeCheck | None = None
    errors: deque[ErrorNote] = field(default_factory=lambda: deque(maxlen=domain.ERRORS_KEPT))


def new_state(deps: SharedDeps, *, version: str, sdk_version: str = "", degraded: Sequence[str] = ()) -> HealthState:
    """The state of a process starting now. `version` is the release (`YYYY.MM.DD-<sha>`), `degraded` the optional
    binaries the start found missing."""
    return HealthState(
        version=version,
        started_at=deps.clock.now(),
        pid=os.getpid(),
        sdk_version=sdk_version,
        degraded=tuple(dict.fromkeys(degraded)),
    )


def note_error(deps: SharedDeps, state: HealthState, code: str) -> None:
    """Remember an error code for `holat?` (the last 3, SPEC §6.2). A code only, never a message or a traceback."""
    state.errors.append(ErrorNote(code=code, at=deps.clock.now()))


# --- files in run/ --------------------------------------------------------------------------------------------------
def _write_private_file(path: Path, data: bytes) -> None:
    """Atomic write: a 0600 temp file in the same 0700 directory, fsync, rename. A reader never sees half a file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")  # mode 0600
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> JsonValue:
    try:
        return json_value.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _take_previous_run(marker: Path, heartbeat: Path) -> tuple[bool, datetime | None]:
    """Whether the previous run left its clean-exit marker (removed now, so a crash of this run is caught next
    time), and when it last polled, from the heartbeat file it left."""
    clean = marker.exists()
    marker.unlink(missing_ok=True)
    return clean, domain.last_alive(_read_json(heartbeat))


# --- the heartbeat (SPEC §2.8) --------------------------------------------------------------------------------------
async def _loop_lag_ms() -> int:
    """How long the event loop took to come back to us: its scheduling delay right now."""
    started = time.monotonic()
    await asyncio.sleep(0)
    return round((time.monotonic() - started) * 1000)


async def write_heartbeat(deps: SharedDeps, state: HealthState) -> None:
    """Rewrite `run/<bot>.heartbeat.json` from `state`, atomically."""
    state.loop_lag_ms = await _loop_lag_ms()
    beat = Heartbeat(
        pid=state.pid,
        version=state.version,
        started_at=state.started_at,
        last_poll_ok_at=state.last_poll_ok_at,
        mode=state.mode,
        loop_lag_ms=state.loop_lag_ms,
        last_poll_attempt_at=state.last_poll_attempt_at,
        last_poll_error_kind=state.last_poll_error_kind,
        consecutive_poll_failures=state.consecutive_poll_failures,
    )
    data = json_value.dumps(domain.heartbeat_json(beat)).encode()
    await asyncio.to_thread(_write_private_file, deps.paths.heartbeat(deps.bot), data)


def _tick(state: HealthState, now: datetime) -> None:
    state.pending_gap = domain.merge_gaps(state.pending_gap, domain.gap_since(state.last_tick_at, now))
    state.last_tick_at = now


async def record_poll(
    deps: SharedDeps, state: HealthState, *, at: datetime, updates: int, error: PollErrorKind
) -> None:
    """The polling runner's `heartbeat` hook (`PollTick(at, updates, error)`), after every `getUpdates` attempt: a
    success (`error` NONE) before its batch is fed, a failure before the backoff wait. `error` has no default, so a
    caller cannot record a failed attempt as a successful one by leaving it out.

    The file comes first. After a success, `S.WAKE_GAP` with this batch's size if the Mac slept, then `S.NETWORK_GAP`
    if polls had failed on the network for ≥ 5 min. A failure here ends the runner, so `forever` restarts it and the
    watchdog sees the attempts go stale."""
    _tick(state, at)
    state.last_poll_attempt_at = at
    state.last_poll_error_kind = error
    if error is not PollErrorKind.NONE:
        state.consecutive_poll_failures += 1
        if error is PollErrorKind.NETWORK and state.offline_since is None:
            state.offline_since = at
        await write_heartbeat(deps, state)
        return
    state.last_poll_ok_at = at
    state.consecutive_poll_failures = 0
    state.pending_network_gap = domain.merge_gaps(
        state.pending_network_gap, domain.network_gap(state.offline_since, at)
    )
    state.offline_since = None
    await write_heartbeat(deps, state)
    if state.pending_gap is not None:
        await _post_wake_gap(deps, state.pending_gap, updates=updates)
        state.pending_gap = None
    if state.pending_network_gap is not None:
        await _post_network_gap(deps, state.pending_network_gap)
        state.pending_network_gap = None


async def heartbeat_tick(deps: SharedDeps, state: HealthState) -> bool:
    """One tick of the 30 s timer. Returns whether it rewrote the file (`domain.timer_writes`)."""
    now = deps.clock.now()
    _tick(state, now)
    writes = domain.timer_writes(
        state.mode,
        last_poll_ok_at=state.last_poll_ok_at,
        started_at=state.started_at,
        now=now,
        last_poll_attempt_at=state.last_poll_attempt_at,
        last_poll_error_kind=state.last_poll_error_kind,
    )
    if not writes:
        return False
    await write_heartbeat(deps, state)
    return True


async def run_heartbeat(deps: SharedDeps, state: HealthState) -> None:
    """The long-lived timer: `aio.forever("heartbeat", lambda: health.run_heartbeat(deps, state))`. Its first tick
    writes the file at once, so a starting bot shows up before its first poll."""
    while True:
        await heartbeat_tick(deps, state)
        await deps.clock.sleep(domain.HEARTBEAT_EVERY_S)


async def set_mode(deps: SharedDeps, state: HealthState, mode: Mode) -> None:
    """Polling paused on purpose, or a self-update's probation (P2): the file says so at once, and the timer keeps
    it fresh while no poll does."""
    state.mode = mode
    log.info("health_mode_set", bot=deps.bot.value, mode=mode.value)
    await write_heartbeat(deps, state)


async def _post_wake_gap(deps: SharedDeps, gap: Gap, *, updates: int) -> None:
    snap = await settings.snapshot(deps)
    chat = await access.resolve_home_chat(deps)
    now = deps.clock.now()
    text = domain.wake_text(gap, updates=updates, timezone=snap[defaults.UI_TIMEZONE])
    slept_s = round((gap.to - gap.from_).total_seconds())
    async with deps.db.uow() as s:
        await store.add_health_event(
            s, bot=deps.bot.value, at=now, kind=EventKind.WAKE_GAP.value, data={"slept_s": slept_s, "updates": updates}
        )
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat,
            text=text,
            kind=Kind.SYSTEM,
            ref=domain.wake_ref(gap),
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
    log.info("wake_gap_told", bot=deps.bot.value, slept_s=slept_s, updates=updates)


async def _post_network_gap(deps: SharedDeps, gap: Gap) -> None:
    snap = await settings.snapshot(deps)
    chat = await access.resolve_home_chat(deps)
    now = deps.clock.now()
    offline_s = round((gap.to - gap.from_).total_seconds())
    async with deps.db.uow() as s:
        await store.add_health_event(
            s, bot=deps.bot.value, at=now, kind=EventKind.NETWORK_GAP.value, data={"offline_s": offline_s}
        )
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat,
            text=domain.network_gap_text(gap, timezone=snap[defaults.UI_TIMEZONE]),
            kind=Kind.SYSTEM,
            ref=domain.network_gap_ref(gap),
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
    log.info("network_gap_told", bot=deps.bot.value, offline_s=offline_s)


# --- start and clean exit (SPEC §2.5 step 8, §2.6) ------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Started:
    """What `report_start` found. `notice` is the restart notice it queued (None after a clean exit)."""

    clean: bool
    notice: BotText | None = None


async def report_start(deps: SharedDeps, state: HealthState, *, interrupted: Sequence[str] = ()) -> Started:
    """The start's health pass. Without the previous run's clean-exit marker, `S.HEALTH_RESTARTED` (with the
    projects in `interrupted`, from `turns.recover_turns`) is queued for the home chat, so the owner reads it within
    seconds (UC-06 AC: ≤ 60 s after `kill -9`). The marker is removed first: a crash from here on is reported by the
    next start. Call it before the first heartbeat write, which replaces the file it reads the last poll time from."""
    paths = deps.paths
    clean, alive = await asyncio.to_thread(_take_previous_run, paths.clean_marker(deps.bot), paths.heartbeat(deps.bot))
    snap = await settings.snapshot(deps)
    chat = await access.resolve_home_chat(deps)
    now = deps.clock.now()
    notice = (
        None if clean else domain.restart_text(interrupted, last_alive_at=alive, timezone=snap[defaults.UI_TIMEZONE])
    )
    async with deps.db.uow() as s:
        await store.add_health_event(
            s,
            bot=deps.bot.value,
            at=now,
            kind=EventKind.STARTED.value,
            data={"version": state.version, "pid": state.pid, "clean": clean},
        )
        if notice is not None:
            await store.add_health_event(
                s,
                bot=deps.bot.value,
                at=now,
                kind=EventKind.UNCLEAN_RESTART.value,
                data={"interrupted": len(set(interrupted)), "down_s": _seconds_since(alive, now)},
            )
            await outbox.enqueue_text(
                s,
                bot=deps.bot,
                chat_id=chat,
                text=notice,
                kind=Kind.SYSTEM,
                ref=domain.restart_ref(state.started_at),
                at=now,
                max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            )
    log.info("health_started", bot=deps.bot.value, version=state.version, clean=clean, interrupted=len(interrupted))
    return Started(clean=clean, notice=notice)


def _seconds_since(moment: datetime | None, now: datetime) -> int | None:
    return None if moment is None else max(0, round((now - moment).total_seconds()))


async def mark_clean_exit(deps: SharedDeps, state: HealthState) -> None:
    """The last step of a SIGTERM shutdown (SPEC §2.6), after the outbox flush: `run/<bot>.clean`, so the next start
    knows nothing was cut off. The marker is written before the event: it is what the next start reads."""
    now = deps.clock.now()
    marker = json_value.dumps({"at": now.isoformat(), "pid": state.pid, "version": state.version}).encode()
    await asyncio.to_thread(_write_private_file, deps.paths.clean_marker(deps.bot), marker)
    uptime_s = round((now - state.started_at).total_seconds())
    async with deps.db.uow() as s:
        await store.add_health_event(
            s, bot=deps.bot.value, at=now, kind=EventKind.CLEAN_EXIT.value, data={"uptime_s": uptime_s}
        )
    log.info("health_clean_exit", bot=deps.bot.value, uptime_s=uptime_s)


# --- the pinned "Onlayn: HH:MM" (SPEC §2.8, UC-06) ------------------------------------------------------------------
def _online_of(row: KvState | None) -> OnlineMessage | None:
    return None if row is None else OnlineMessage.from_json(row.value_json)


async def refresh_online(deps: SharedDeps) -> OnlineStep:
    """One step of the dev bot's pinned message: send and pin it, or edit it to now, or wait for its first post.
    The plan and its outbox rows are one unit of work with the stored state, so a crash never sends it twice."""
    snap = await settings.snapshot(deps)
    chat = await access.resolve_home_chat(deps)
    async with deps.db.read() as s:
        current = _online_of(await store.get_kv(s, bot=deps.bot.value, key=domain.KV_ONLINE))
    sent = None if current is None else await outbox.get_delivery(deps, current.send_key)
    last_key = None if current is None else current.last_edit_key
    last_edit = None if last_key is None else await outbox.get_delivery(deps, last_key)
    plan = domain.plan_online(current, sent=sent, last_edit=last_edit)
    message = plan.message
    now = deps.clock.now()
    text = domain.online_text(now, timezone=snap[defaults.UI_TIMEZONE])
    async with deps.db.uow() as s:
        if plan.step is OnlineStep.SEND:
            await outbox.enqueue_text(
                s,
                bot=deps.bot,
                chat_id=chat,
                text=text,
                kind=Kind.HEARTBEAT,
                ref=message.ref,
                at=now,
                max_parts=snap[defaults.OUTBOX_MAX_PARTS],
                disable_notification=True,
            )
            await outbox.enqueue_pin(s, bot=deps.bot, chat_id=chat, ref=message.ref, at=now, target=message.send_key)
        elif plan.step is OnlineStep.EDIT:
            await outbox.enqueue_edit(
                s,
                bot=deps.bot,
                chat_id=chat,
                text=text,
                kind=Kind.HEARTBEAT,
                ref=message.ref,
                part=message.edit_part,
                at=now,
                message_id=message.message_id,
                coalesce_key=domain.ONLINE_COALESCE,
            )
        if message != current:
            await store.set_kv(s, bot=deps.bot.value, key=domain.KV_ONLINE, value=message.to_json(), at=now)
    if plan.step is not OnlineStep.EDIT:
        log.info("online_message_planned", step=plan.step.value, generation=message.generation)
    return plan.step


async def run_online(deps: SharedDeps) -> None:
    """The dev bot's loop: `aio.forever("online", lambda: health.run_online(deps))`, every
    `schedule.heartbeat_edit_min` (UC-06 AC: the pinned time is never older than 10 min while the Mac is up)."""
    while True:
        await refresh_online(deps)
        snap = await settings.snapshot(deps)
        await deps.clock.sleep(snap[defaults.SCHEDULE_HEARTBEAT_EDIT_MIN] * 60.0)


# --- self-checks and the CLI smoke test (SPEC §2.5 step 7, §5.6; NFR-REL-08) ----------------------------------------
def _stored_version(row: KvState | None) -> str | None:
    raw = None if row is None else row.value_json
    version = raw.get("version") if isinstance(raw, dict) else None
    return version if isinstance(version, str) and version else None


async def _run_smoke(smoke: SmokeRunner, version: str) -> SmokeOutcome:
    try:
        async with asyncio.timeout(domain.SMOKE_BACKSTOP_S):
            return await smoke(version)
    except TimeoutError:
        return SmokeOutcome(ok=False, step="timeout")
    except ExternalError as error:
        return SmokeOutcome(ok=False, step=error.code.value)


async def check_claude(deps: SharedDeps, state: HealthState, *, smoke: SmokeRunner | None = None) -> ClaudeCheck:
    """`claude --version` + `claude auth status` (through the Assistant's probe), cached in `state` for `holat?`.
    A version other than the one in `kv_state` is an auto-update: the dev bot runs one smoke turn (`smoke`), records
    `smoke_ok` or `smoke_failed`, and tells the owner only about a failure. The first version ever seen is the
    reference, with no smoke turn. The new version is stored after the smoke turn, so a crash during it repeats the
    test after the restart."""
    now = deps.clock.now()
    try:
        status = await deps.assistant.probe(timeout=domain.PROBE_TIMEOUT_S)
    except ExternalError as error:
        state.claude = ClaudeCheck(at=now, error_code=error.code.value)
        log.warning("claude_probe_failed", bot=deps.bot.value, error_code=error.code.value)
        return state.claude
    check = domain.claude_check(status, at=now)
    state.claude = check
    log.info(
        "claude_checked",
        bot=deps.bot.value,
        version=check.version,
        logged_in=check.logged_in,
        auth_ok=check.auth_method == domain.EXPECTED_AUTH_METHOD,
    )
    if check.version is None:
        return check
    version = check.version[: domain.VERSION_MAX]
    async with deps.db.read() as s:
        previous = _stored_version(await store.get_kv(s, bot=deps.bot.value, key=domain.KV_CLI_VERSION))
    change = domain.cli_change(previous, version)
    if change is domain.CliChange.SAME:
        return check
    outcome = await _run_smoke(smoke, version) if change is domain.CliChange.CHANGED and smoke is not None else None
    await _record_cli(deps, previous=previous, version=version, outcome=outcome)
    return check


async def _record_cli(deps: SharedDeps, *, previous: str | None, version: str, outcome: SmokeOutcome | None) -> None:
    snap = await settings.snapshot(deps)
    chat = await access.resolve_home_chat(deps)
    now = deps.clock.now()
    bot = deps.bot.value
    async with deps.db.uow() as s:
        await store.add_health_event(
            s, bot=bot, at=now, kind=EventKind.CLI_VERSION.value, data={"from": previous, "to": version}
        )
        if outcome is not None:
            kind = EventKind.SMOKE_OK if outcome.ok else EventKind.SMOKE_FAILED
            await store.add_health_event(
                s, bot=bot, at=now, kind=kind.value, data={"version": version, "step": outcome.step}
            )
            if not outcome.ok:
                await outbox.enqueue_text(
                    s,
                    bot=deps.bot,
                    chat_id=chat,
                    text=domain.smoke_failed_text(version, outcome.step),
                    kind=Kind.SYSTEM,
                    ref=domain.smoke_ref(version),
                    at=now,
                    max_parts=snap[defaults.OUTBOX_MAX_PARTS],
                )
        await store.set_kv(s, bot=bot, key=domain.KV_CLI_VERSION, value={"version": version}, at=now)
    log.info(
        "cli_version_changed",
        bot=bot,
        previous=previous,
        version=version,
        smoke=None if outcome is None else ("ok" if outcome.ok else outcome.step),
    )


# --- disk guard and media purge (SPEC §9.2, NFR-CAP-05) -------------------------------------------------------------
def _disk_warning(row: KvState | None) -> DiskWarning | None:
    return None if row is None else DiskWarning.from_json(row.value_json)


async def guard_disk(deps: SharedDeps) -> DiskLevel | None:
    """The dev bot's disk guard: under `guard.min_free_disk_gb` the owner is warned (`S.DISK_LOW`), at most once per
    6 h unless it got worse; under `guard.block_disk_gb` the warning says new coding turns queue (the scheduler does
    that itself). None when the free space could not be read."""
    snap = await settings.snapshot(deps)
    warn_gb, block_gb = snap[defaults.GUARD_MIN_FREE_DISK_GB], snap[defaults.GUARD_BLOCK_DISK_GB]
    try:
        free = await deps.mac_host.free_disk_bytes(deps.paths.data, timeout=domain.HOST_TIMEOUT_S)
    except (ExternalError, OSError) as error:
        log.warning("disk_unreadable", bot=deps.bot.value, error_type=type(error).__name__)
        return None
    level = domain.disk_level(free, warn_gb=warn_gb, block_gb=block_gb)
    if level is DiskLevel.OK:
        return level
    now = deps.clock.now()
    async with deps.db.read() as s:
        last = _disk_warning(await store.get_kv(s, bot=deps.bot.value, key=domain.KV_DISK_WARNED))
    if not domain.disk_warning_due(level, last=last, now=now):
        return level
    chat = await access.resolve_home_chat(deps)
    free_gb = domain.gigabytes(free)
    async with deps.db.uow() as s:
        await store.add_health_event(
            s,
            bot=deps.bot.value,
            at=now,
            kind=EventKind.DISK_LOW.value,
            data={"free_gb": free_gb, "level": level.value},
        )
        await outbox.enqueue_text(
            s,
            bot=deps.bot,
            chat_id=chat,
            text=domain.disk_text(level, free, warn_gb=warn_gb, block_gb=block_gb),
            kind=Kind.SYSTEM,
            ref=f"disk-{int(now.timestamp())}",
            at=now,
            max_parts=snap[defaults.OUTBOX_MAX_PARTS],
        )
        await store.set_kv(
            s, bot=deps.bot.value, key=domain.KV_DISK_WARNED, value=DiskWarning(at=now, level=level).to_json(), at=now
        )
    log.warning("disk_low", bot=deps.bot.value, free_gb=free_gb, level=level.value)
    return level


async def run_checks(deps: SharedDeps, state: HealthState, *, smoke: SmokeRunner | None = None) -> None:
    """The self-check loop, every 10 min from the start (SPEC §5.6, §9.2):
    `aio.forever("health_checks", lambda: health.run_checks(deps, state, smoke=…))`. The dev bot also runs the disk
    guard; the personal bot passes no `smoke`."""
    while True:
        await check_claude(deps, state, smoke=smoke)
        if deps.bot is BotName.DEV:
            await guard_disk(deps)
        await deps.clock.sleep(domain.CHECK_EVERY_S)


def _purge_media_dirs(root: Path, today: date, keep_days: int) -> int:
    try:
        names = [entry.name for entry in os.scandir(root) if entry.is_dir(follow_symlinks=False)]
    except FileNotFoundError:
        return 0
    removed = 0
    for name in domain.expired_media_dirs(names, today=today, keep_days=keep_days):
        try:
            shutil.rmtree(root / name)
        except OSError as error:
            log.warning("media_purge_failed", folder=name, error_type=type(error).__name__)
            continue
        removed += 1
    return removed


async def purge_media(deps: SharedDeps) -> int:
    """Daily: remove `media/<yyyymmdd>/` folders older than `media.retention_days` (dates in `ui.timezone`). Only
    real folders named by a date are touched. Returns how many went."""
    snap = await settings.snapshot(deps)
    today = timefmt.local(deps.clock.now(), snap[defaults.UI_TIMEZONE]).date()
    removed = await asyncio.to_thread(_purge_media_dirs, deps.paths.media, today, snap[defaults.MEDIA_RETENTION_DAYS])
    log.info("media_purged", bot=deps.bot.value, folders=removed)
    return removed


# --- `holat?` (UC-06, SPEC §6.2) ------------------------------------------------------------------------------------
async def _part[T](name: str, read: Awaitable[T]) -> T | Unread:
    """One part of `holat?`, bounded: what it could not read in time shows as unknown, never as a delay."""
    try:
        async with asyncio.timeout(domain.HOLAT_PART_TIMEOUT_S):
            return await read
    except (TimeoutError, ExternalError, OSError, ValueError) as error:
        log.warning("holat_part_unread", part=name, error_type=type(error).__name__)
        return UNREAD


async def _speech_providers(deps: SharedDeps, snap: Snapshot) -> tuple[str, ...]:
    """What voice STT would use now (voice's own rule): providers with a key in the vault, by name only."""
    keys = voice.current_keys(await vault.list_secrets(deps))
    return voice.pick_stt(snap[defaults.SPEECH_STT_PROVIDER], [name for name in keys if name in deps.transcribers])


async def show_health(deps: SharedDeps, state: HealthState, *, work: WorkProbe | None = None) -> BotText:
    """The `holat?` answer, from local state only: the cached Claude check, ClaudeManager's state file, the vault's
    key names, this bot's work (`work`, dev only), memory pressure and free disk. Each part is read at once and
    bounded by `domain.HOLAT_PART_TIMEOUT_S`, so the answer takes ≤ 3 s even when a part hangs."""
    started = time.monotonic()
    snap = await settings.snapshot(deps)
    timeout = domain.HOLAT_PART_TIMEOUT_S
    async with asyncio.TaskGroup() as group:
        manager = group.create_task(_part("manager", deps.mac_host.claude_manager_state(timeout=timeout)))
        pressure = group.create_task(_part("pressure", deps.mac_host.memory_pressure_level(timeout=timeout)))
        disk = group.create_task(_part("disk", deps.mac_host.free_disk_bytes(deps.paths.data, timeout=timeout)))
        speech = group.create_task(_part("speech", _speech_providers(deps, snap)))
        doing = None if work is None else group.create_task(_part("work", work()))
    view = HealthView(
        bot=deps.bot,
        now=deps.clock.now(),
        timezone=snap[defaults.UI_TIMEZONE],
        started_at=state.started_at,
        version=state.version,
        sdk_version=state.sdk_version,
        claude=state.claude,
        manager=manager.result(),
        speech_providers=speech.result(),
        work=None if doing is None else doing.result(),
        pressure_level=pressure.result(),
        free_disk_bytes=disk.result(),
        degraded=state.degraded,
        errors=tuple(state.errors),
    )
    log.info("health_shown", bot=deps.bot.value, duration_ms=round((time.monotonic() - started) * 1000))
    return domain.holat_text(view)


# --- retention (SPEC §4.2) ------------------------------------------------------------------------------------------
async def apply_retention(deps: SharedDeps) -> int:
    """`health_events` go after 90 days, in batches; `kv_state` is kept. Returns how many rows went."""
    cutoff = domain.event_cutoff(deps.clock.now())
    deleted = await bulk.run_in_batches(
        deps.db, lambda s, limit: store.delete_health_events_before(s, cutoff, limit=limit)
    )
    log.info("health_retention_applied", events_deleted=deleted)
    return deleted


# --- self-update: drain and probation (SPEC §3.2.12; the supervisor side is T57) -----------------------------------
type Peek = Callable[[int], Awaitable[int]]
"""`getUpdates(offset=<offset>, limit=1, timeout=0)` without confirming anything; returns how many are held. The
polling runner in `bots/` builds it (aiogram lives there, C5)."""


async def read_probation(deps: SharedDeps, version: str) -> domain.ProbationFlag | None:
    """SPEC §2.5 step 6: the probation flag, when it names this release."""
    raw = await asyncio.to_thread(_read_json, deps.paths.probation_flag)
    return domain.probation_for(raw, version)


async def read_drain(deps: SharedDeps) -> domain.DrainFlag | None:
    return domain.drain_flag(await asyncio.to_thread(_read_json, deps.paths.drain_flag))


async def mark_drained(deps: SharedDeps, flag: domain.DrainFlag) -> None:
    """Polling is paused and nothing is left running: the supervisor may snapshot the DB now."""
    data = json_value.dumps(domain.drained_json(flag, at=deps.clock.now())).encode()
    await asyncio.to_thread(_write_private_file, deps.paths.drained_marker(deps.bot), data)
    log.info("drained", bot=deps.bot.value, update_id=flag.update_id)


async def probation_peek(deps: SharedDeps, peek: Peek) -> int:
    """One probation poll: it reads the stored offset and peeks from there, and never advances it, so Telegram keeps
    every owner message for whichever release ends up running (a rollback restores the DB, and nothing is lost)."""
    offset = await inbox.get_poll_offset(deps)
    held = await peek(offset)
    log.info("probation_peek", bot=deps.bot.value, offset=offset, held=held)
    return held
