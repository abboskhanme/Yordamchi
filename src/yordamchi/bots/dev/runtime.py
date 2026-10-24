"""The running dev bot: its in-memory state, its long-lived loops, the recovery pass and the shutdown (SPEC §2.5
steps 6–10, §2.6, §2.7, §2.8).

`bots/dev/app.py` builds the adapters and `DevDeps` (the only place adapters are constructed, C4) and hands them
here; tests build the same `DevApp` on fakes. Nothing here imports a vendor SDK.

- **Start** (`start`): `turns.recover_turns` → `questions.recover_questions` → `health.report_start` (it reads the
  previous heartbeat file, so before the first heartbeat write), the vault's masking set and hash file, the gate's
  unasked questions, then the loops, each under `core.aio.forever` (a failing body is logged and restarted with
  backoff; siblings never share a TaskGroup).
- **Loops**: poller · burst timers · router (`bots/dev/pipeline.py`) · turn scheduler · outbox sender · digest
  refresher · server checks · cost watch (the 80 %/100 % cap notices and the optional daily summary, T51) ·
  heartbeat writer · "Onlayn" pin · self-checks (+ disk guard, CLI smoke) · project scan · maintenance
  (clarification expiry, unasked decisions, assistant re-warm, vault republish) · daily retention · control socket.
  Every one-off job inside them runs under `common.guarded()` (`bots/dev/jobs.py`).
- **Stop** (`stop`, SIGTERM): stop polling and the loops (no new turns), `turns.shutdown_turns` (guards deny-all,
  interrupt, `interrupted`), flush outbox priority 0–1 for ≤ 5 s, close the adapters, write `run/dev.clean`.
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
from yordamchi.bots.dev import jobs, telegram
from yordamchi.bots.dev.control_socket import ControlSocket
from yordamchi.bots.dev.pipeline import Pipeline
from yordamchi.core import aio, timefmt
from yordamchi.core.log import get_log
from yordamchi.dev.deps import DevDeps
from yordamchi.dev.digest import service as digest
from yordamchi.dev.digest.service import DigestState
from yordamchi.dev.gate import service as gate
from yordamchi.dev.projects import domain as projects_domain
from yordamchi.dev.projects import service as projects
from yordamchi.dev.questions import service as questions
from yordamchi.dev.routing import service as routing
from yordamchi.dev.servers import service as servers
from yordamchi.dev.turns import service as turns
from yordamchi.dev.turns.service import Recovered, TurnsState
from yordamchi.shared.access.domain import OwnerOrigin, require_owner_origin
from yordamchi.shared.audit import service as audit
from yordamchi.shared.burst import service as burst
from yordamchi.shared.burst.domain import BurstEvents
from yordamchi.shared.cost import service as cost
from yordamchi.shared.health import service as health
from yordamchi.shared.health.domain import Mode
from yordamchi.shared.health.service import HealthState
from yordamchi.shared.inbox import service as inbox
from yordamchi.shared.inbox.domain import Accepted
from yordamchi.shared.outbox import service as outbox
from yordamchi.shared.outbox.domain import Kind
from yordamchi.shared.settings import defaults
from yordamchi.shared.settings import service as settings
from yordamchi.shared.vault import service as vault
from yordamchi.shared.voice import service as voice

log = get_log(__name__)

# Mechanism, not owner behaviour (SPEC §2.6, §2.8; the SPEC fixes these and has no §4.3 key for them).
FLUSH_MAX_PRIORITY: Final = 1
FLUSH_WITHIN_S: Final = 5.0
MAINTENANCE_EVERY_S: Final = 60.0  # clarifications expire ~ every minute (T20)
REPUBLISH_EVERY: Final = 10  # maintenance passes between vault republishes
DEGRADED_VAULT: Final = "master.key"

type Closer = Callable[[], Awaitable[None]]


@dataclass(slots=True)
class Intake:
    """What the Telegram handlers (`bots/dev/telegram.py`) tell the running bot after the inbox took an update."""

    deps: DevDeps
    health: HealthState
    wake: asyncio.Event

    def accepted(self, origin: OwnerOrigin, accepted: Accepted) -> None:
        origin = require_owner_origin(origin, self.deps.bot)
        self.wake.set()  # the burst loop attaches it now
        if accepted.secrets:  # a speech key among them is checked and reported (SPEC §3.2.11), off the handler
            job = voice.configure_speech_keys(self.deps, origin, accepted.secrets)
            aio.spawn("speech_keys", jobs.run(self.deps, self.health, job, job="speech_keys", chat_id=origin.chat_id))


@dataclass(slots=True)
class DevApp:
    """One dev bot process. Build it with `build_app`."""

    deps: DevDeps
    bot: Bot | None  # None in tests that feed updates to `dispatcher` directly (no poller)
    version: str
    turns: TurnsState
    digest: DigestState
    health: HealthState
    pipeline: Pipeline
    dispatcher: Dispatcher
    intake: Intake
    socket: ControlSocket
    closers: Sequence[Closer] = ()
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    warmed_version: int | None = None

    # --- start (SPEC §2.5 steps 6–10) --------------------------------------------------------------------------
    async def recover(self) -> Recovered:
        """SPEC §2.7: the turn rows, then the answers they were waiting for, then the restart notice."""
        recovered = await turns.recover_turns(self.deps)
        await questions.recover_questions(self.deps, self.turns)
        await health.report_start(self.deps, self.health, interrupted=recovered.interrupted)
        self.pipeline.restarted = recovered.interrupted
        return recovered

    async def start(self, *, polling: bool = True) -> None:
        """Recover, prepare the vault and the gate, then start every loop. `polling=False` (tests) leaves the poller
        out: updates come through `dispatcher.feed_update` instead."""
        await self.recover()
        await self._publish_secrets()
        await jobs.run(self.deps, self.health, gate.post_unasked_decisions(self.deps), job="post_unasked_decisions")
        for name, body in self._loops(polling=polling).items():
            self._spawn(name, body)

    async def start_probation(self) -> None:
        """SPEC §2.5 step 6, §3.2.12: a self-update's health check. Only the heartbeat and the control socket run; no
        owner message is consumed and nothing is recovered until the supervisor clears the flag (P2 adds the peek
        poller and the self-test)."""
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
            "turns": partial(turns.run_scheduler, deps, self.turns),
            "outbox": partial(outbox.run_sender, deps),
            "digest": partial(digest.run_digest_poll, deps, self.digest),
            "heartbeat": partial(health.run_heartbeat, deps, self.health),
            "online": partial(health.run_online, deps),
            "health_checks": partial(
                health.run_checks, deps, self.health, smoke=partial(turns.run_smoke_turn, deps, self.turns)
            ),
            "projects": self._scan_projects,
            "maintenance": self._maintain,
            "retention": self._retain,
            "servers": partial(servers.run_server_checks, deps),
            "cost": partial(cost.run_cost_watch, deps),
            "control_socket": self.socket.serve,
        }
        if polling and self.bot is not None:
            loops = {"poller": partial(self._poll, self.bot), **loops}
        return loops

    async def _poll(self, bot: Bot) -> None:
        await common.run_polling(self.deps, self.dispatcher, bot, heartbeat=self._on_poll)

    async def _on_poll(self, tick: PollTick) -> None:
        """The poller's heartbeat hook (SPEC §2.8): the file after every successful `getUpdates`."""
        await health.record_poll(self.deps, self.health, at=tick.at, updates=tick.updates, error=tick.error)

    def burst_events(self, events: BurstEvents) -> None:
        """The burst loop's `notify` (it must not block): closed bursts wake the router; each new voice part starts
        its STT job, which records the transcript and wakes the burst loop again."""
        if events.closed:
            self.pipeline.poke()
        if events.transcribe:
            named = (partial(projects.list_projects, self.deps),)
            job = voice.transcribe_parts(
                self.deps, events.transcribe, wake=self.intake.wake, notify=self.burst_events, named=named
            )
            aio.spawn("stt", jobs.run(self.deps, self.health, job, job="stt"))

    # --- periodic jobs ------------------------------------------------------------------------------------------
    async def _scan_projects(self) -> None:
        """The registry from `projects.root`, at start and every `SCAN_INTERVAL_S` (T17)."""
        while True:
            await jobs.run(self.deps, self.health, projects.sync_projects(self.deps), job="sync_projects")
            await self.deps.clock.sleep(projects_domain.SCAN_INTERVAL_S)

    async def _maintain(self) -> None:
        """Every minute: expired routing questions are told, unasked decisions are asked, the warm assistants follow a
        settings change; every ten minutes the vault's masking set and hash file are rebuilt."""
        passes = 0
        while True:
            await jobs.run(self.deps, self.health, self._expire_clarifications(), job="expire_clarifications")
            await jobs.run(self.deps, self.health, gate.post_unasked_decisions(self.deps), job="post_unasked_decisions")
            await jobs.run(self.deps, self.health, self.warm(), job="warm_assistants")
            passes += 1
            if passes % REPUBLISH_EVERY == 0:
                await self._publish_secrets()
            await self.deps.clock.sleep(MAINTENANCE_EVERY_S)

    async def _expire_clarifications(self) -> None:
        snap = await settings.snapshot(self.deps)
        for expired in await routing.expire_clarifications(self.deps):
            await outbox.enqueue_reply(
                self.deps,
                chat_id=snap[defaults.ACCESS_DEV_GROUP_ID],
                text=expired.reply,
                kind=Kind.NOTICE,
                ref=f"clarification-expired-{expired.clarification_id}",
                max_parts=snap[defaults.OUTBOX_MAX_PARTS],
            )

    async def warm(self) -> None:
        """Warm the assistant kinds `assistant.warm_clients` keeps (routing, report; settings is cold by default), at
        start and again after any settings change (a new snapshot version: models, prompts, the counts)."""
        snap = await settings.snapshot(self.deps)
        if snap.version == self.warmed_version:
            return
        self.warmed_version = snap.version
        counts = snap[defaults.ASSISTANT_WARM_CLIENTS]
        if counts.get("routing", "0") != "0":
            await routing.warm_routing(self.deps)
        if counts.get("report", "0") != "0":
            await digest.warm_report(self.deps)

    async def _publish_secrets(self) -> None:
        """The masking set and `run/secret-hashes.json` (without the file every push is blocked). A missing or
        insecure master key is a degraded capability shown in `holat?`, never a crash."""
        try:
            await vault.publish_secrets(self.deps)
        except vault.KEY_UNAVAILABLE as error:
            log.warning("vault_degraded", error_code=error.code.value)
            if DEGRADED_VAULT not in self.health.degraded:
                self.health.degraded = (*self.health.degraded, DEGRADED_VAULT)
        else:
            self.health.degraded = tuple(item for item in self.health.degraded if item != DEGRADED_VAULT)

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
            "routing": partial(routing.apply_retention, deps),
            "gate": partial(gate.apply_retention, deps),
            "turns": partial(turns.apply_retention, deps),
            "questions": partial(questions.apply_retention, deps),
            "health": partial(health.apply_retention, deps),
            "servers": partial(servers.apply_retention, deps),
            "media": partial(health.purge_media, deps),
        }
        for name, retention in retentions.items():
            await jobs.run(deps, self.health, retention(), job=f"retention_{name}")

    # --- stop (SPEC §2.6) ---------------------------------------------------------------------------------------
    async def stop(self) -> None:
        """SIGTERM: no new update, no new turn; the working turns are interrupted and asked about; what the owner
        must still see goes out within 5 s; then the sessions close and the clean marker is written."""
        poller = self.tasks.pop("poller", None)
        await _cancel([poller] if poller is not None else [])
        await _cancel(list(self.tasks.values()))
        self.tasks.clear()
        deps = self.deps
        await jobs.run(deps, self.health, turns.shutdown_turns(deps, self.turns), job="shutdown_turns")
        flush = outbox.flush_queue(deps, max_priority=FLUSH_MAX_PRIORITY, within_s=FLUSH_WITHIN_S)
        await jobs.run(deps, self.health, flush, job="outbox_flush")
        for close in self.closers:
            await jobs.run(deps, self.health, close(), job="close_adapter")
        await jobs.run(deps, self.health, health.mark_clean_exit(deps, self.health), job="clean_exit")
        await asyncio.to_thread(self.socket.remove)
        log.info("dev_bot_stopped", version=self.version)


async def _cancel(tasks: Sequence[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def status_of(version: str, health_state: HealthState, turns_state: TurnsState) -> dict[str, object]:
    """The control socket's `status` (SPEC §3.1.1). `drained` is P2's self-update drain; P1 never drains."""
    live = sum(1 for session in turns_state.live.values() if session.turn_id is not None)
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
    deps: DevDeps,
    *,
    bot: Bot | None,
    version: str,
    sdk_version: str = "",
    degraded: Sequence[str] = (),
    closers: Sequence[Closer] = (),
) -> DevApp:
    """The dev bot's state and loops over built deps. Wiring only: nothing runs until `start`."""
    turns_state = TurnsState()
    turns_state.questions = questions.open_question
    # `server_checks` stays unset: `dev/servers` answers `server_status` itself (T31), and the digest's reader of
    # its `latest_statuses` is a T30 follow-up (docs/PENDING_WIRING.md, From T31).
    digest_state = DigestState(turns=turns_state)
    turns_state.turn_ended = partial(digest.refresh_after_turn, state=digest_state)
    health_state = health.new_state(deps, version=version, sdk_version=sdk_version, degraded=degraded)
    pipeline = Pipeline(deps=deps, turns=turns_state, digest=digest_state, health=health_state)
    intake = Intake(deps=deps, health=health_state, wake=asyncio.Event())
    dispatcher = common.build_dispatcher(deps, telegram.build_router())
    dispatcher["deps"] = deps
    dispatcher["intake"] = intake
    status = partial(status_of, version, health_state, turns_state)
    return DevApp(
        deps=deps,
        bot=bot,
        version=version,
        turns=turns_state,
        digest=digest_state,
        health=health_state,
        pipeline=pipeline,
        dispatcher=dispatcher,
        intake=intake,
        socket=ControlSocket(deps, turns_state, health_state, path=deps.paths.socket(deps.bot), status=status),
        closers=closers,
    )
