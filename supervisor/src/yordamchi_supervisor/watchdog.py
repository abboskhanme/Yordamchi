"""The watchdog (SPEC §2.8, NFR-REL-01), one `tick(now)` every 15 s for each installed bot label.

Per bot:
- the job is not loaded in `gui/<uid>` → not installed (the personal bot before P3) or stopped on purpose → no action;
- an update is in progress (`updates.scan`) → no action and no alert, and the restart history is reset so the
  update's own restarts never count as a crash loop;
- no pid → `launchctl kickstart -k` and `S.BOT_RESTARTED`;
- the heartbeat file is older than 120 s → the same. Its age is measured from the latest of: the file's mtime, when
  this pid was first seen, the last kickstart, and the last wake from sleep. A process that just started (or a Mac
  that just woke) therefore gets 120 s to write its first heartbeat before it is restarted again;
- ≥ 3 starts within 10 min → crash loop: `S.CRASH_LOOP` with the last line of `run/<bot>.lasterror`, at most
  once per 30 min. That alert replaces the individual restart alerts while it is active.

Starts are counted two ways, and the larger count wins: new `started_at` values in the heartbeat (SPEC §2.8), and
the `runs` counter of `launchctl print`, which also counts a process that died before its first heartbeat (for
example a refused start, exit 78).
"""

import json
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from yordamchi_supervisor import strings as S
from yordamchi_supervisor.clock import from_timestamp
from yordamchi_supervisor.launchd import JobStatus, LaunchCtl
from yordamchi_supervisor.log import JsonLog, Scrubber
from yordamchi_supervisor.paths import BOTS, Bot, SupervisorPaths, label
from yordamchi_supervisor.state import StateFile, parse_instant
from yordamchi_supervisor.telegram import Alerter
from yordamchi_supervisor.updates import scan

INTERVAL: Final = timedelta(seconds=15)
HEARTBEAT_STALE: Final = timedelta(seconds=120)
KICK_COOLDOWN: Final = timedelta(seconds=30)  # never two kickstarts of one job closer than this
CRASH_WINDOW: Final = timedelta(minutes=10)
CRASH_COUNT: Final = 3
CRASH_ALERT_EVERY: Final = timedelta(minutes=30)
WAKE_GAP: Final = timedelta(seconds=60)  # a tick gap this long means the Mac slept (ticks are 15 s apart)
MAX_ERROR_LINE: Final = 500
STATE_SECTION: Final = "watchdog"


@dataclass(frozen=True, slots=True)
class Heartbeat:
    written_at: datetime
    pid: int | None
    started_at: str | None


def read_heartbeat(path: Path) -> Heartbeat | None:
    try:
        written_at = from_timestamp(path.stat().st_mtime)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        # Written atomically by the bot, so this is damage, not a race. The mtime alone still dates it.
        try:
            return Heartbeat(written_at=from_timestamp(path.stat().st_mtime), pid=None, started_at=None)
        except OSError:
            return None
    pid = raw.get("pid") if isinstance(raw, dict) else None
    started = raw.get("started_at") if isinstance(raw, dict) else None
    return Heartbeat(
        written_at=written_at,
        pid=pid if isinstance(pid, int) else None,
        started_at=started if isinstance(started, str) else None,
    )


def last_error_line(path: Path, scrubber: Scrubber) -> str:
    """The last non-empty line of `run/<bot>.lasterror` (the bot scrubs it; this scrubs again), or a placeholder."""
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        lines = []
    tail = next((line for line in reversed(lines) if line), "")
    return scrubber(tail[:MAX_ERROR_LINE]) if tail else S.NO_LAST_ERROR()


@dataclass(slots=True)
class _BotWatch:
    installed: bool | None = None
    suppressed_by: str | None = None
    pid: int | None = None
    pid_seen_at: datetime | None = None
    last_kick_at: datetime | None = None
    runs: int | None = None
    started_seen: deque[str] = field(default_factory=lambda: deque(maxlen=50))
    started_primed: bool = False
    start_events: deque[datetime] = field(default_factory=deque)
    run_events: deque[datetime] = field(default_factory=deque)
    last_loop_alert_at: datetime | None = None

    def forget_history(self) -> None:
        self.runs = None
        self.started_seen.clear()
        self.started_primed = False
        self.start_events.clear()
        self.run_events.clear()


class Watchdog:
    def __init__(
        self,
        paths: SupervisorPaths,
        launchctl: LaunchCtl,
        alerter: Alerter,
        log: JsonLog,
        scrubber: Scrubber,
        state: StateFile,
        bots: tuple[Bot, ...] = BOTS,
    ) -> None:
        self._paths = paths
        self._launchctl = launchctl
        self._alerter = alerter
        self._log = log
        self._scrub = scrubber
        self._state = state
        self._bots = bots
        self._watch: dict[Bot, _BotWatch] = {bot: _BotWatch() for bot in bots}
        self._last_tick: datetime | None = None
        self._woke_at: datetime | None = None
        self._stale_logged: set[str] = set()
        saved = state.section(STATE_SECTION)
        for bot in bots:
            entry = saved.get(bot)
            stamp = entry.get("last_loop_alert_at") if isinstance(entry, dict) else None
            self._watch[bot].last_loop_alert_at = parse_instant(stamp)

    def tick(self, now: datetime) -> None:
        if self._last_tick is not None and now - self._last_tick > WAKE_GAP:
            self._woke_at = now
            self._log.info("wake_detected", gap_s=int((now - self._last_tick).total_seconds()))
        self._last_tick = now
        updates = scan(self._paths.updates, now)
        for stale in set(updates.stale) - self._stale_logged:
            self._log.warning("update_stale_ignored", update_id=stale)
        self._stale_logged = set(updates.stale)
        for bot in self._bots:
            self._check(bot, now, updates.active, updates.status)

    # --- one bot -------------------------------------------------------------------------------------------------
    def _check(self, bot: Bot, now: datetime, update_id: str | None, update_status: str | None) -> None:
        watch = self._watch[bot]
        status = self._launchctl.status(label(bot))
        if status is None:
            self._log.warning("launchctl_no_answer", bot=bot)
            return
        if not status.loaded:
            if watch.installed is not False:
                self._log.info("bot_not_loaded", bot=bot)
            watch.installed = False
            watch.forget_history()
            return
        if watch.installed is not True:
            self._log.info("bot_watched", bot=bot, pid=status.pid)
            watch.installed = True

        if update_id is not None:
            if watch.suppressed_by != update_id:
                self._log.info("watchdog_suppressed", bot=bot, update_id=update_id, status=update_status)
            watch.suppressed_by = update_id
            watch.forget_history()
            return
        if watch.suppressed_by is not None:
            self._log.info("watchdog_resumed", bot=bot, update_id=watch.suppressed_by)
            watch.suppressed_by = None
            watch.pid_seen_at = now  # the update restarted the bot: a fresh grace period

        heartbeat = read_heartbeat(self._paths.heartbeat(bot))
        self._observe_starts(watch, status, heartbeat, now)

        reason = self._problem(watch, status, heartbeat, now)
        kicked: bool | None = None
        if reason is not None:
            kicked = self._launchctl.kickstart(label(bot))
            watch.last_kick_at = now
            self._log.warning("bot_kickstarted" if kicked else "bot_kickstart_failed", bot=bot, reason=reason[0])

        starts = self._starts_in_window(watch, now)
        loop_alert_active = watch.last_loop_alert_at is not None and now - watch.last_loop_alert_at < CRASH_ALERT_EVERY
        if starts >= CRASH_COUNT and not loop_alert_active:
            self._alert_crash_loop(bot, watch, starts, now)
        elif reason is not None and not loop_alert_active:
            text = (
                S.BOT_RESTARTED(bot=S.bot_name(bot), reason=reason[1])
                if kicked
                else S.BOT_RESTART_FAILED(bot=S.bot_name(bot), reason=reason[1], label=label(bot))
            )
            self._alerter.send("bot_restarted" if kicked else "bot_restart_failed", text)

    def _observe_starts(self, watch: _BotWatch, status: JobStatus, heartbeat: Heartbeat | None, now: datetime) -> None:
        if status.pid != watch.pid:
            watch.pid = status.pid
            watch.pid_seen_at = now
        if status.runs is not None:
            if watch.runs is not None and status.runs > watch.runs:
                watch.run_events.extend([now] * min(status.runs - watch.runs, 10))
            watch.runs = status.runs
        started = heartbeat.started_at if heartbeat else None
        if started is not None and started not in watch.started_seen:
            if watch.started_primed:
                watch.start_events.append(now)
            watch.started_seen.append(started)
        watch.started_primed = True

    def _problem(
        self, watch: _BotWatch, status: JobStatus, heartbeat: Heartbeat | None, now: datetime
    ) -> tuple[str, str] | None:
        """(log code, owner-facing reason) when the bot needs a kickstart now."""
        if watch.last_kick_at is not None and now - watch.last_kick_at < KICK_COOLDOWN:
            return None
        if status.pid is None:
            return "process_missing", S.REASON_PROCESS_MISSING()
        marks = [heartbeat.written_at if heartbeat else None, watch.pid_seen_at, watch.last_kick_at, self._woke_at]
        reference = max((mark for mark in marks if mark is not None), default=now)
        age = now - reference
        if age > HEARTBEAT_STALE:
            return "heartbeat_stale", S.REASON_HEARTBEAT_STALE(seconds=int(age.total_seconds()))
        return None

    @staticmethod
    def _starts_in_window(watch: _BotWatch, now: datetime) -> int:
        for events in (watch.start_events, watch.run_events):
            while events and now - events[0] > CRASH_WINDOW:
                events.popleft()
        return max(len(watch.start_events), len(watch.run_events))

    def _alert_crash_loop(self, bot: Bot, watch: _BotWatch, starts: int, now: datetime) -> None:
        watch.last_loop_alert_at = now
        saved = self._state.section(STATE_SECTION)
        saved[bot] = {"last_loop_alert_at": now.isoformat()}
        self._state.save_section(STATE_SECTION, saved)
        self._log.error("crash_loop", bot=bot, starts=starts)
        text = S.CRASH_LOOP(
            bot=S.bot_name(bot),
            count=starts,
            minutes=int(CRASH_WINDOW.total_seconds() // 60),
            last_error=last_error_line(self._paths.last_error(bot), self._scrub),
        )
        self._alerter.send("crash_loop", text)
