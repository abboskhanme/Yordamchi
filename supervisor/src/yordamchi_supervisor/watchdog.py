"""The watchdog (SPEC §2.8, NFR-REL-01), one `tick(now)` every 15 s for each installed bot label.

Per bot:
- the job is not loaded in `gui/<uid>` → not installed (the personal bot before P3) or stopped on purpose → no action;
- an update is in progress (`updates.scan`) → no action and no alert, and the restart history is reset so the
  update's own restarts never count as a crash loop;
- no pid → `launchctl kickstart -k` and `S.BOT_RESTARTED`, at once;
- no sign of life for 120 s → the same. The age is measured from the latest of: the bot's last sign of life
  (`Heartbeat.alive_at`), when this pid was first seen, the last kickstart, and the last wake from sleep. A process
  that just started (or a Mac that just woke) therefore gets 120 s to show one before it is restarted again;
- ≥ 3 starts within 10 min → crash loop: `S.CRASH_LOOP` with the last line of `run/<bot>.lasterror`, at most
  once per 30 min. That alert replaces the individual restart alerts while it is active.

The last sign of life (T32b). A bot in normal mode writes, after every `getUpdates` attempt, when it tried
(`last_poll_attempt_at`) and how it failed (`last_poll_error_kind`: none, network or other):
- attempts that work, or fail on the network, count: a loop that keeps trying lives. With no attempt for 120 s the
  loop is stuck or dead and is restarted (`poll_loop_stuck`);
- while the attempts are fresh and fail on the network, the bot is **offline, not dead**: no kickstart and no alert,
  only `bot_offline` and then `bot_back_online` in the log. The bot itself tells the owner once it is back, and renews
  its HTTP session every 5 min meanwhile (`bots/common.py`). Telegram cannot carry the news of its own outage, so
  after 10 min offline the owner gets one banner on the Mac per outage (`notify.py`, owner request 2026-10-27), one
  for all the bots offline by then;
- any other failure (a revoked token, a 409, a 429) is judged as before T32b: no successful poll for 120 s, counted
  from no earlier than when this watchdog first saw that failure (`polls_failing`);
- a file from before T32b, a paused bot (its 30 s timer keeps the file fresh while it does not poll) and a process
  that has not polled yet are judged by the file's mtime, as before (`heartbeat_stale`). A mixed-version update
  therefore keeps the rules each side knows.

Starts are counted two ways, and the larger count wins: new `started_at` values in the heartbeat (SPEC §2.8), and
the `runs` counter of `launchctl print`, which also counts a process that died before its first heartbeat (for
example a refused start, exit 78). Starts made while the bot is offline are not counted: an outage must never end in
a crash-loop alert.
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
from yordamchi_supervisor.notify import Notifier
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
OFFLINE_NOTIFY_AFTER: Final = timedelta(minutes=10)
WAKE_GAP: Final = timedelta(seconds=60)  # a tick gap this long means the Mac slept (ticks are 15 s apart)
MAX_ERROR_LINE: Final = 500
STATE_SECTION: Final = "watchdog"
# The app's vocabulary (`shared/health/domain.py`: `Mode`, `PollErrorKind`), copied because A6 forbids importing it.
MODE_NORMAL: Final = "normal"
KIND_NONE: Final = "none"
KIND_NETWORK: Final = "network"
# Attempts of these kinds show a live loop: they worked, or the network failed them (offline, not dead).
LIVE_KINDS: Final = frozenset({KIND_NONE, KIND_NETWORK})


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """What the watchdog reads from `run/<bot>.heartbeat.json` (SPEC §2.8). `last_poll_error_kind` is None in a file
    from before T32b, which is then judged by its mtime alone, as before."""

    written_at: datetime  # the file's mtime
    pid: int | None
    started_at: str | None
    mode: str | None = None
    last_poll_ok_at: datetime | None = None
    last_poll_attempt_at: datetime | None = None
    last_poll_error_kind: str | None = None
    consecutive_poll_failures: int | None = None

    @property
    def tracks_attempts(self) -> bool:
        """A T32b file of a bot in normal mode: its poll attempts, not the mtime, show whether the loop lives. A paused
        bot (`paused`, `probation`) polls nothing; its 30 s timer keeps the mtime fresh instead."""
        return self.mode == MODE_NORMAL and self.last_poll_error_kind is not None

    @property
    def not_polled_yet(self) -> bool:
        return self.tracks_attempts and self.last_poll_attempt_at is None

    def _attempt(self) -> datetime | None:
        # Never later than the mtime: the bot writes the file after the attempt.
        return None if self.last_poll_attempt_at is None else min(self.last_poll_attempt_at, self.written_at)

    def alive_at(self) -> tuple[datetime | None, str]:
        """The last sign of life the 120 s limit counts from, with the log code of the restart it may cause (module
        docstring). None: there is none in the file (polls that never worked)."""
        attempt = self._attempt()
        if not self.tracks_attempts or attempt is None:
            return self.written_at, "heartbeat_stale"
        if self.last_poll_error_kind in LIVE_KINDS:
            return attempt, "poll_loop_stuck"
        ok = None if self.last_poll_ok_at is None else min(self.last_poll_ok_at, self.written_at)
        return ok, "polls_failing"

    def offline(self, now: datetime) -> bool:
        """The loop keeps trying, and its attempts fail on the network."""
        attempt = self._attempt()
        return (
            self.tracks_attempts
            and self.last_poll_error_kind == KIND_NETWORK
            and attempt is not None
            and now - attempt <= HEARTBEAT_STALE
        )

    @property
    def failing(self) -> bool:
        """The latest attempt failed for a reason other than the network (a revoked token, a 409, a 429)."""
        return (
            self.tracks_attempts
            and self.last_poll_attempt_at is not None
            and self.last_poll_error_kind not in LIVE_KINDS
        )


def read_heartbeat(path: Path) -> Heartbeat | None:
    """The file, or None when there is none. A damaged file or field reads as missing, which falls back to the mtime
    rule: a hand edit or a half-understood newer format can only make the watchdog stricter, never blind."""
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
    body: dict[str, object] = raw if isinstance(raw, dict) else {}
    pid, failures = body.get("pid"), body.get("consecutive_poll_failures")
    return Heartbeat(
        written_at=written_at,
        pid=pid if isinstance(pid, int) else None,
        started_at=_text(body.get("started_at")),
        mode=_text(body.get("mode")),
        last_poll_ok_at=parse_instant(body.get("last_poll_ok_at")),
        last_poll_attempt_at=parse_instant(body.get("last_poll_attempt_at")),
        last_poll_error_kind=_text(body.get("last_poll_error_kind")),
        consecutive_poll_failures=failures if isinstance(failures, int) and not isinstance(failures, bool) else None,
    )


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


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
    offline_since: datetime | None = None  # first check that saw the bot offline (T32b)
    offline_notified: bool = False  # this outage already had its banner on the Mac
    failing_since: datetime | None = None  # first check that saw its polls fail for another reason (T32b)

    def forget_history(self) -> None:
        """An update or a bootout: what came before says nothing about the process that comes next."""
        self.runs = None
        self.started_seen.clear()
        self.started_primed = False
        self.start_events.clear()
        self.run_events.clear()
        self.offline_since = None
        self.offline_notified = False
        self.failing_since = None


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
        *,
        notifier: Notifier | None = None,
    ) -> None:
        self._paths = paths
        self._launchctl = launchctl
        self._alerter = alerter
        self._log = log
        self._scrub = scrubber
        self._state = state
        self._bots = bots
        self._notifier = notifier
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
        self._notify_offline(now)

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
        failing = heartbeat is not None and heartbeat.failing
        watch.failing_since = (watch.failing_since or now) if failing else None

        reason = self._problem(watch, status, heartbeat, now)
        kicked: bool | None = None
        if reason is not None:
            kicked = self._launchctl.kickstart(label(bot))
            watch.last_kick_at = now
            self._log.warning("bot_kickstarted" if kicked else "bot_kickstart_failed", bot=bot, reason=reason[0])
        self._track_offline(bot, watch, heartbeat, None if reason is None else reason[0], now)

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
        """Records new starts. Those made while the bot was offline are seen but not counted toward a crash loop: the
        network failed, not the bot (T32b)."""
        counts = watch.offline_since is None
        if status.pid != watch.pid:
            watch.pid = status.pid
            watch.pid_seen_at = now
        if status.runs is not None:
            if watch.runs is not None and status.runs > watch.runs and counts:
                watch.run_events.extend([now] * min(status.runs - watch.runs, 10))
            watch.runs = status.runs
        started = heartbeat.started_at if heartbeat else None
        if started is not None and started not in watch.started_seen:
            if watch.started_primed and counts:
                watch.start_events.append(now)
            watch.started_seen.append(started)
        watch.started_primed = True

    def _track_offline(
        self, bot: Bot, watch: _BotWatch, heartbeat: Heartbeat | None, kick_reason: str | None, now: datetime
    ) -> None:
        """`bot_offline` and `bot_back_online` on each change (T32b). A process that has not polled yet changes
        nothing: a bot that restarted during an outage is still offline, not back."""
        if kick_reason is None and heartbeat is not None and heartbeat.not_polled_yet:
            return
        offline = kick_reason is None and heartbeat is not None and heartbeat.offline(now)
        since = watch.offline_since
        if offline and since is None:
            watch.offline_since = now
            failures = None if heartbeat is None else heartbeat.consecutive_poll_failures
            self._log.info("bot_offline", bot=bot, failures=failures)
        elif not offline and since is not None:
            watch.offline_since = None
            watch.offline_notified = False
            offline_s = int((now - since).total_seconds())
            kind = None if heartbeat is None else heartbeat.last_poll_error_kind
            if kick_reason is None and kind == KIND_NONE:
                self._log.info("bot_back_online", bot=bot, offline_s=offline_s)
            else:  # stuck, gone, or now failing for another reason: what follows is logged on its own
                self._log.warning("bot_offline_ended", bot=bot, offline_s=offline_s, cause=kick_reason or kind)

    def _problem(
        self, watch: _BotWatch, status: JobStatus, heartbeat: Heartbeat | None, now: datetime
    ) -> tuple[str, str] | None:
        """(log code, owner-facing reason) when the bot needs a kickstart now."""
        if watch.last_kick_at is not None and now - watch.last_kick_at < KICK_COOLDOWN:
            return None
        if status.pid is None:
            return "process_missing", S.REASON_PROCESS_MISSING()
        alive, code = heartbeat.alive_at() if heartbeat else (None, "heartbeat_stale")
        # `failing_since` is set only while polls fail for another reason: such a streak gets its own 120 s, so a
        # 409 right after a long outage does not count the outage against the bot.
        marks = [alive, watch.pid_seen_at, watch.last_kick_at, self._woke_at, watch.failing_since]
        reference = max((mark for mark in marks if mark is not None), default=now)
        age = now - reference
        if age > HEARTBEAT_STALE:
            return code, S.REASON_HEARTBEAT_STALE(seconds=int(age.total_seconds()))
        return None

    def _notify_offline(self, now: datetime) -> None:
        """Once a bot has been offline for 10 min, one banner names it and every other bot offline by then that has
        not had one in this outage. A failed banner is not retried: the log has it."""
        if self._notifier is None:
            return
        waiting = {bot: since for bot in self._bots if (since := self._offline_unnotified(bot)) is not None}
        if not waiting or now - min(waiting.values()) < OFFLINE_NOTIFY_AFTER:
            return
        for bot in waiting:
            self._watch[bot].offline_notified = True
        minutes = int((now - min(waiting.values())).total_seconds() // 60)
        text = S.BOTS_OFFLINE(bots=S.bot_names(list(waiting)), minutes=minutes)
        self._notifier.notify("bots_offline", title=S.MAC_TITLE(), text=text)

    def _offline_unnotified(self, bot: Bot) -> datetime | None:
        watch = self._watch[bot]
        return None if watch.offline_notified else watch.offline_since

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
