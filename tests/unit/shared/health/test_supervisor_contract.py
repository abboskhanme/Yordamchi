"""The heartbeat file the app writes is the one the supervisor reads (SPEC §2.8, NFR-REL-01; T23 ↔ T32).

The supervisor is a separate stdlib program (CONVENTIONS §12) that never imports the app (A6). Here the app imports
the supervisor's own `watchdog` (from `supervisor/src`, test-only) and checks that:
- both sides name the same file;
- `read_heartbeat` parses what `health.service` writes: the pid, and `started_at` (how starts are counted);
- the real `Watchdog` leaves a polling bot alone, restarts it once its file goes stale, and leaves a bot alone whose
  polling is paused on purpose while the app's timer keeps the file fresh.

The watchdog judges freshness by the file's mtime on its own clock, so each app write is stamped with the watchdog's
virtual time, as the supervisor's own tests do.
"""

import os
from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

from tests.factories import Fakes, make_dev_deps, make_health_state
from tests.fakes.clock import FakeClock as AppClock
from yordamchi.core.paths import BotName, Paths
from yordamchi.dev.deps import DevDeps
from yordamchi.persistence.uow import Database
from yordamchi.shared.health import service
from yordamchi.shared.health.domain import Mode
from yordamchi.shared.health.service import HealthState

SUPERVISOR_SRC = Path(__file__).resolve().parents[4] / "supervisor" / "src"
DEV_LABEL = "uz.yordamchi.dev"


@pytest.fixture
def supervisor(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The supervisor package on `sys.path` for this test only (it is not installed in the app's venv)."""
    monkeypatch.syspath_prepend(str(SUPERVISOR_SRC))
    import yordamchi_supervisor

    return yordamchi_supervisor


class Rig:
    """The supervisor's watchdog over a data dir the app writes into."""

    def __init__(self, deps: DevDeps, state: HealthState, app_clock: AppClock) -> None:
        from yordamchi_supervisor.launchd import LaunchCtl
        from yordamchi_supervisor.state import StateFile
        from yordamchi_supervisor.telegram import Alerter
        from yordamchi_supervisor.testing import UID, FakeClock, FakeJob, FakeLaunchctl, FakeOpener, TempData
        from yordamchi_supervisor.watchdog import Watchdog

        self.data = TempData()
        home, data, logs = self.data.paths.home, self.data.paths.data, self.data.paths.logs
        self.deps = replace(deps, paths=Paths.defaults(home, data=data, config=home / "config", logs=logs))
        self.state = state
        self.app_clock = app_clock
        self.launchctl = FakeLaunchctl()
        self.launchctl.jobs[DEV_LABEL] = FakeJob(pid=state.pid)
        self.opener = FakeOpener()
        self.clock = FakeClock(datetime.fromisoformat("2026-10-24T09:00:00+00:00"))
        alerter = Alerter(
            self.data.paths.env_file("dev"), self.data.log, self.data.scrubber, self.clock, opener=self.opener
        )
        self.watchdog = Watchdog(
            self.data.paths,
            LaunchCtl(self.launchctl, UID),
            alerter,
            self.data.log,
            self.data.scrubber,
            StateFile(self.data.paths.state_file),
        )

    @property
    def path(self) -> Path:
        return self.deps.paths.heartbeat(BotName.DEV)

    def stamp(self) -> None:
        moment = self.clock.now().timestamp()
        os.utime(self.path, (moment, moment))

    async def poll(self) -> None:
        """A successful `getUpdates`: the app's hook writes the file."""
        await service.record_poll(self.deps, self.state, at=self.deps.clock.now(), updates=0)
        self.stamp()

    async def timer(self) -> bool:
        wrote = await service.heartbeat_tick(self.deps, self.state)
        if wrote:
            self.stamp()
        return wrote

    def tick(self, seconds: float = 15) -> None:
        self.watchdog.tick(self.clock.advance(seconds=seconds))

    def kicks(self) -> list[list[str]]:
        return self.launchctl.kicks()


@pytest.fixture
def rig(supervisor: ModuleType, dev_db: Database, paths: Paths, fakes: Fakes) -> Iterator[Rig]:
    del supervisor  # imported for its sys.path entry
    rig = Rig(make_dev_deps(db=dev_db, paths=paths, fakes=fakes), make_health_state(fakes.clock), fakes.clock)
    yield rig
    rig.data.cleanup()


def test_both_sides_name_the_same_heartbeat_file(rig: Rig) -> None:
    assert rig.data.paths.heartbeat("dev") == rig.deps.paths.heartbeat(BotName.DEV)
    assert rig.data.paths.heartbeat("personal") == rig.deps.paths.heartbeat(BotName.PERSONAL)


async def test_the_supervisor_reads_the_pid_and_start_the_app_wrote(rig: Rig) -> None:
    from yordamchi_supervisor.clock import from_timestamp
    from yordamchi_supervisor.watchdog import read_heartbeat

    await rig.poll()
    beat = read_heartbeat(rig.path)

    assert beat is not None
    assert beat.pid == rig.state.pid
    assert beat.started_at == rig.state.started_at.isoformat()
    assert beat.written_at == from_timestamp(rig.path.stat().st_mtime)


async def test_a_polling_bot_is_left_alone_and_a_dead_poller_is_restarted(rig: Rig) -> None:
    from yordamchi_supervisor import strings as S

    await rig.poll()
    rig.watchdog.tick(rig.clock.now())
    for _ in range(40):  # 10 min of polls every 15 s
        rig.tick()
        await rig.app_clock.advance(15)
        await rig.poll()
    assert rig.kicks() == []

    for _ in range(9):  # polling died; the app's timer must not keep the file fresh
        await rig.app_clock.advance(15)
        assert not await rig.timer()
        rig.tick()
    assert len(rig.kicks()) == 1
    assert S.REASON_HEARTBEAT_STALE(seconds=135) in rig.opener.texts()[0]


async def test_a_paused_bot_kept_fresh_by_the_app_timer_is_not_restarted(rig: Rig) -> None:
    await rig.poll()
    await service.set_mode(rig.deps, rig.state, Mode.PAUSED)
    rig.stamp()
    rig.watchdog.tick(rig.clock.now())
    for _ in range(20):  # 10 min with no poll at all, the timer every 30 s
        await rig.app_clock.advance(30)
        assert await rig.timer()
        rig.tick(15)
        rig.tick(15)
    assert rig.kicks() == []
