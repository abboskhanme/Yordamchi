"""The running personal bot: its in-memory state, its long-lived loops, the start pass and the shutdown (SPEC §2.5
steps 6–10, §2.6, §2.7, §2.8), in the shape of `bots/dev/runtime.py` (C7 keeps the two bots apart).

`bots/personal/app.py` builds the adapters and `PersonalDeps` (the only place adapters are constructed, C4) and
hands them here; tests build the same `PersonalApp` on fakes. Nothing here imports a vendor SDK, and there is no
executor, git, GitHub or SSH handle to reach (UC-36, C7).

- **Start** (`start`): `health.report_start` (it reads the previous heartbeat file, so before the first heartbeat
  write), the vault's masking set, the bridge's read-only check (dev.db is `mode=ro` + `query_only`, or the bridge
  stays off), then the loops, each under `core.aio.forever`. The start-up tool check (`verify_tools`, NFR-SEC-08) is
  `app.py`'s, before `start`: a refused tool list never starts the bot.
- **Loops**: poller · burst timers · router and the answer worker (`bots/personal/pipeline.py`) · outbox sender ·
  heartbeat writer · self-checks · cost watch (the caps are one budget with the dev bot) · maintenance (the warm
  conversation, the vault republish) · daily retention · control socket. Every one-off job inside them runs under
  `common.guarded()` (`bots/personal/jobs.py`).
- **Recovery** (SPEC §2.7): received updates re-attach and open bursts re-arm in the burst loop, closed bursts are
  answered by the router's first pass, the outbox resends what was in flight. There are no turns to interrupt.
- **Stop** (`stop`, SIGTERM): stop polling and the loops, stop the answer being written (its burst stays `closed`
  for the next start), flush outbox priority 0–1 for ≤ 5 s, close the adapters, write `run/personal.clean`.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from functools import partial
from typing import Final
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher

from yordamchi.bots import common
from yordamchi.bots.common import PollTick
from yordamchi.bots.personal import jobs, telegram
from yordamchi.bots.personal.control_socket import ControlSocket
from yordamchi.bots.personal.pipeline import Pipeline
from yordamchi.core import aio, timefmt
from yordamchi.core.errors import BridgeNotReadOnly, BridgeUnavailable
from yordamchi.core.log import get_log
from yordamchi.personal.deps import PersonalDeps
from yordamchi.personal.qa import service as qa
from yordamchi.shared.access import service as access
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.audit import service as audit
from yordamchi.shared.bridge import service as bridge
from yordamchi.shared.bridge.domain import ProjectName
from yordamchi.shared.burst import service as burst
from yordamchi.shared.burst.domain import BurstEvents
from yordamchi.shared.cost import service as cost
from yordamchi.shared.health import service as health
from yordamchi.shared.health.domain import Mode
from yordamchi.shared.health.service import HealthState
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox.domain import Accepted
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.vault import service as vault
from yordamchi.shared.voice import service as voice

log = get_log(__name__)

# Mechanism, not owner behaviour (SPEC §2.6, §2.8; the SPEC fixes these and has no §4.3 key for them).
FLUSH_MAX_PRIORITY: Final = 1
FLUSH_WITHIN_S: Final = 5.0
MAINTENANCE_EVERY_S: Final = 60.0
REPUBLISH_EVERY: Final = 10  # maintenance passes between vault republishes
PROBE_REF: Final = "qa-probe"  # what the start-up check's tools would queue under (it asks them to call none)
WARM_REF: Final = "qa-warm"  # the warm session only needs the tools' shape; each answer brings its own handlers
DEGRADED_VAULT: Final = "master.key"
DEGRADED_BRIDGE: Final = "dev.db"  # no read-only view of the dev bot's data: project status is off

type Closer = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class Intake:
    """What the Telegram handlers (`bots/personal/telegram.py`) tell the running bot after the inbox took an update."""

    deps: PersonalDeps
    health: HealthState
    wake: asyncio.Event

    def accepted(self, origin: OwnerOrigin, accepted: Accepted) -> None:
        origin = require_owner_origin(origin, self.deps.bot)
        self.wake.set()  # the burst loop attaches it now
        if accepted.secrets:  # a speech key among them is checked and reported (SPEC §3.2.11), off the handler
            job = voice.configure_speech_keys(self.deps, origin, accepted.secrets)
            aio.spawn("speech_keys", jobs.run(self.deps, self.health, job, job="speech_keys", chat_id=origin.chat_id))


@dataclass(slots=True)
class PersonalApp:
    """One personal bot process. Build it with `build_app`."""

    deps: PersonalDeps
    bot: Bot | None  # None in tests that feed updates to `dispatcher` directly (no poller)
    version: str
    health: HealthState
    pipeline: Pipeline
    dispatcher: Dispatcher
    intake: Intake
    socket: ControlSocket
    closers: Sequence[Closer] = ()
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    warmed_version: int | None = None

    # --- start (SPEC §2.5 steps 6–10) --------------------------------------------------------------------------
    async def verify_tools(self) -> bool:
        """NFR-SEC-08: the personal session, opened with exactly the tools an answer gets, reports the allowlist.
        Raises `PersonalToolsMismatch` when it does not; False when Claude could not be reached to check."""
        chat_id = await access.resolve_home_chat(self.deps)
        tools = await self.pipeline.media_tools(chat_id, ref=PROBE_REF)
        return await qa.verify_tools(self.deps, toolsets=(tools,))

    async def start(self, *, polling: bool = True) -> None:
        """The start pass, then every loop. `polling=False` (tests) leaves the poller out: updates come through
        `dispatcher.feed_update` instead."""
        await health.report_start(self.deps, self.health)
        await self._publish_secrets()
        await self._check_bridge()
        for name, body in self._loops(polling=polling).items():
            self._spawn(name, body)

    async def start_probation(self) -> None:
        """SPEC §2.5 step 6, §3.2.12: a self-update's health check. Only the heartbeat and the control socket run; no
        owner message is consumed until the supervisor clears the flag (P2 adds the peek poller and the self-test)."""
        await health.set_mode(self.deps, self.health, Mode.PROBATION)
        self._spawn("heartbeat", partial(health.run_heartbeat, self.deps, self.health))
        self._spawn("control_socket", self.socket.serve)

    def _spawn(self, name: str, body: aio.Body) -> None:
        if name not in self.tasks:
            self.tasks[name] = aio.spawn(name, aio.forever(name, body))

    def _loops(self, *, polling: bool) -> dict[str, aio.Body]:
        deps = self.deps
        loops: dict[str, aio.Body] = {
            "bursts": partial(burst.run_bursts, deps, wake=self.intake.wake, notify=self.burst_events),
            "router": self.pipeline.run,
            "answers": self.pipeline.run_work,
            "outbox": partial(outbox.run_sender, deps),
            "heartbeat": partial(health.run_heartbeat, deps, self.health),
            "health_checks": partial(health.run_checks, deps, self.health),
            "cost": partial(cost.run_cost_watch, deps),
            "maintenance": self._maintain,
            "retention": self._retain,
            "control_socket": self.socket.serve,
        }
        if polling and self.bot is not None:
            loops = {"poller": partial(self._poll, self.bot), **loops}
        return loops

    async def _poll(self, bot: Bot) -> None:
        await common.run_polling(self.deps, self.dispatcher, bot, heartbeat=self._on_poll)

    async def _on_poll(self, tick: PollTick) -> None:
        """The poller's heartbeat hook (SPEC §2.8): the file after every `getUpdates` attempt."""
        await health.record_poll(self.deps, self.health, at=tick.at, updates=tick.updates, error=tick.error)

    def burst_events(self, events: BurstEvents) -> None:
        """The burst loop's `notify` (it must not block): closed bursts wake the router; each new voice part starts
        its STT job, with the dev bot's project names in the vocabulary ("BonviHR qanday?" said aloud)."""
        if events.closed:
            self.pipeline.poke()
        if events.transcribe:
            job = voice.transcribe_parts(
                self.deps,
                events.transcribe,
                wake=self.intake.wake,
                notify=self.burst_events,
                named=(self._project_names,),
            )
            aio.spawn("stt", jobs.run(self.deps, self.health, job, job="stt"))

    async def _project_names(self) -> tuple[ProjectName, ...]:
        snap = await settings.snapshot(self.deps)
        return await bridge.project_names(self.deps, timeout=snap[defaults.TIMEOUTS_BRIDGE_S])

    async def _check_bridge(self) -> None:
        """UC-36: the bridge reads dev.db only through a `mode=ro`, `query_only` handle. Without one, project status
        is a degraded capability shown in `holat?` (the bridge refuses every read by itself)."""
        if self.deps.peer_db is None:
            self._degrade(DEGRADED_BRIDGE, on=True)
            return
        snap = await settings.snapshot(self.deps)
        try:
            await bridge.check_read_only(self.deps, timeout=snap[defaults.TIMEOUTS_BRIDGE_S])
        except (BridgeUnavailable, BridgeNotReadOnly) as error:
            log.warning("bridge_degraded", error_code=error.code.value)
            self._degrade(DEGRADED_BRIDGE, on=True)
        else:
            self._degrade(DEGRADED_BRIDGE, on=False)

    def _degrade(self, item: str, *, on: bool) -> None:
        others = tuple(entry for entry in self.health.degraded if entry != item)
        self.health.degraded = (*others, item) if on else others

    # --- periodic jobs ------------------------------------------------------------------------------------------
    async def _maintain(self) -> None:
        """Every minute: the owner's conversation is kept warm after a settings change (the first question of the
        day is then as quick as the rest); every ten minutes the vault's masking set is rebuilt."""
        passes = 0
        while True:
            await jobs.run(self.deps, self.health, self.warm(), job="warm_conversation")
            passes += 1
            if passes % REPUBLISH_EVERY == 0:
                await self._publish_secrets()
            await self.deps.clock.sleep(MAINTENANCE_EVERY_S)

    async def warm(self) -> None:
        """Warm the home chat's conversation at start and again after any settings change (models, prompts)."""
        snap = await settings.snapshot(self.deps)
        if snap.version == self.warmed_version:
            return
        self.warmed_version = snap.version
        chat_id = await access.resolve_home_chat(self.deps)
        tools = await self.pipeline.media_tools(chat_id, ref=WARM_REF)
        await qa.warm_conversation(self.deps, chat_id, toolsets=(tools,))

    async def _publish_secrets(self) -> None:
        """The masking set (the personal bot writes no hash file). A missing master key is a degraded capability
        shown in `holat?`, never a crash."""
        try:
            await vault.publish_secrets(self.deps)
        except vault.KEY_UNAVAILABLE as error:
            log.warning("vault_degraded", error_code=error.code.value)
            self._degrade(DEGRADED_VAULT, on=True)
        else:
            self._degrade(DEGRADED_VAULT, on=False)

    async def _retain(self) -> None:
        """Daily at `schedule.retention_at` (owner's zone): every feature's retention and the media purge, each on its
        own (one failing never skips the others)."""
        while True:
            snap = await settings.snapshot(self.deps)
            wait = seconds_until(
                self.deps.clock.now(), snap[defaults.SCHEDULE_RETENTION_AT], snap[defaults.UI_TIMEZONE]
            )
            await self.deps.clock.sleep(wait)
            await self.retain()

    async def retain(self) -> None:
        deps = self.deps
        retentions: dict[str, Callable[[], Awaitable[int]]] = {
            "inbox": partial(inbox.apply_retention, deps),
            "audit": partial(audit.apply_retention, deps),
            "vault": partial(vault.apply_retention, deps),
            "cost": partial(cost.apply_retention, deps),
            "burst": partial(burst.apply_retention, deps),
            "outbox": partial(outbox.apply_retention, deps),
            "voice": partial(voice.apply_retention, deps),
            "health": partial(health.apply_retention, deps),
            "qa": partial(qa.apply_retention, deps),
            "media": partial(health.purge_media, deps),
        }
        for name, retention in retentions.items():
            await jobs.run(deps, self.health, retention(), job=f"retention_{name}")

    # --- stop (SPEC §2.6) ---------------------------------------------------------------------------------------
    async def stop(self) -> None:
        """SIGTERM: no new update, no new answer; the answer being written stops and is answered after the restart;
        what the owner must still see goes out within 5 s; then the adapters close and the clean marker is written."""
        poller = self.tasks.pop("poller", None)
        await _cancel([poller] if poller is not None else [])
        await _cancel(list(self.tasks.values()))
        self.tasks.clear()
        deps = self.deps
        await self.pipeline.stop()
        flush = outbox.flush_queue(deps, max_priority=FLUSH_MAX_PRIORITY, within_s=FLUSH_WITHIN_S)
        await jobs.run(deps, self.health, flush, job="outbox_flush")
        for close in self.closers:
            await jobs.run(deps, self.health, close(), job="close_adapter")
        await jobs.run(deps, self.health, health.mark_clean_exit(deps, self.health), job="clean_exit")
        await asyncio.to_thread(self.socket.remove)
        log.info("personal_bot_stopped", version=self.version)


async def _cancel(tasks: Sequence[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def status_of(version: str, health_state: HealthState, pipeline: Pipeline) -> dict[str, object]:
    """The control socket's `status` (SPEC §3.1.1): `live_turns` counts the answer being written (0 or 1)."""
    current = pipeline.current
    live = 1 if current is not None and not current.done() else 0
    return {"version": version, "mode": health_state.mode.value, "live_turns": live, "drained": False}


def seconds_until(now: datetime, at: str, timezone: str) -> float:
    """Seconds from `now` to the next `HH:MM` in `timezone` (tomorrow's if today's has passed)."""
    hours, minutes = (int(part) for part in at.split(":"))
    local_now = timefmt.local(now, timezone)
    target = datetime.combine(local_now.date(), time(hours, minutes), tzinfo=ZoneInfo(timezone))
    if target <= local_now:
        target += timedelta(days=1)
    return (target - local_now).total_seconds()


def build_app(
    deps: PersonalDeps,
    *,
    bot: Bot | None,
    version: str,
    sdk_version: str = "",
    degraded: Sequence[str] = (),
    closers: Sequence[Closer] = (),
) -> PersonalApp:
    """The personal bot's state and loops over built deps. Wiring only: nothing runs until `start`."""
    health_state = health.new_state(deps, version=version, sdk_version=sdk_version, degraded=degraded)
    pipeline = Pipeline(deps=deps, health=health_state)
    intake = Intake(deps=deps, health=health_state, wake=asyncio.Event())
    dispatcher = common.build_dispatcher(deps, telegram.build_router())
    dispatcher["deps"] = deps
    dispatcher["intake"] = intake
    status = partial(status_of, version, health_state, pipeline)
    return PersonalApp(
        deps=deps,
        bot=bot,
        version=version,
        health=health_state,
        pipeline=pipeline,
        dispatcher=dispatcher,
        intake=intake,
        socket=ControlSocket(deps.bot, path=deps.paths.socket(deps.bot), status=status),
        closers=closers,
    )
