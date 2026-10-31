"""Test doubles for the self-updater (T57): a scripted working copy and toolchain, simulated bots under launchd, and a
Telegram that holds owner messages until a bot confirms them. Nothing in the supervisor imports this module.

The simulation keeps the properties the UC-19 acceptance criteria are about, with no real git, uv, make or launchd:
- `FakeTools` answers git, uv, make and a release's python. Each commit carries a `FIXTURE.json` that says how its
  build, its tests, its migration and its bot behave, so a "broken update" is a commit, as in production.
- `FakeBots` runs one simulated process per loaded launchd job. A process runs the code `releases/current` pointed at
  when it started (a swap alone changes nothing until a restart) and behaves as that release's fixture says:
  `ok`, `crash_at_import` (dies at once, launchd respawns it), `no_polling` (alive, never polls), `selftest_fails`,
  `ignores_probation` (polls normally during probation, which would consume owner messages).
- `FakeTelegram` holds every owner message until a normal-mode bot advances its offset in that bot's real SQLite DB
  (`poll_offsets`), and records what was processed (`processed`). A probation bot only peeks; a paused bot does
  nothing. So "0 lost messages" and "the DB equals the pre-update copy" are checked on real files.
"""

import io
import json
import os
import sqlite3
import tarfile
import unittest
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from yordamchi_supervisor.control import SelftestAnswer
from yordamchi_supervisor.launchd import LaunchCtl
from yordamchi_supervisor.paths import BOTS, Bot, SupervisorPaths, label
from yordamchi_supervisor.proc import CommandResult
from yordamchi_supervisor.releases import RELEASE_MARKER, link_target
from yordamchi_supervisor.state import StateFile
from yordamchi_supervisor.telegram import Alerter
from yordamchi_supervisor.testing import START, UID, FakeClock, FakeJob, FakeLaunchctl, FakeOpener, TempData
from yordamchi_supervisor.updater import INTERVAL, SANDBOX_EXEC, Job, Journal, Step, StepResult, Updater

FIXTURE = "FIXTURE.json"
BASE_SHA = "a" * 40
DAY = "2026.10.24"


def version_of(sha: str, day: str = DAY) -> str:
    return f"{day}-{sha[:7]}"


@dataclass
class Behaviour:
    """What a commit does when it is built, tested, migrated and run."""

    bot: str = "ok"  # ok | crash_at_import | no_polling | selftest_fails | ignores_probation
    build: str = "ok"  # ok | fail
    app_tests: int = 120
    supervisor_tests: int = 100
    failing: tuple[str, ...] = ()  # test ids that fail
    migration: str = "none"  # none | ok | fail
    trial: str = "ok"  # the supervisor's --selftest

    def to_json(self) -> str:
        return json.dumps(
            {
                "bot": self.bot,
                "build": self.build,
                "app_tests": self.app_tests,
                "supervisor_tests": self.supervisor_tests,
                "failing": list(self.failing),
                "migration": self.migration,
                "trial": self.trial,
            }
        )

    @classmethod
    def read(cls, directory: Path) -> "Behaviour":
        try:
            raw = json.loads((directory / FIXTURE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        return cls(
            bot=str(raw.get("bot", "ok")),
            build=str(raw.get("build", "ok")),
            app_tests=int(raw.get("app_tests", 120)),
            supervisor_tests=int(raw.get("supervisor_tests", 100)),
            failing=tuple(str(item) for item in raw.get("failing", [])),
            migration=str(raw.get("migration", "none")),
            trial=str(raw.get("trial", "ok")),
        )


@dataclass
class FakeCommit:
    sha: str
    parent: str | None
    files: dict[str, str]
    day: str = DAY


def tree(behaviour: Behaviour, *, kind: str = "app", extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A commit's files: the app and the supervisor, each with its fixture. Only the side of `kind` behaves as
    `behaviour`; the other keeps the base fixture, so an app update changes no supervisor file (R-32)."""
    app, supervisor = (behaviour, Behaviour()) if kind == "app" else (Behaviour(), behaviour)
    files = {
        FIXTURE: app.to_json(),
        "src/yordamchi/__init__.py": "",
        "alembic.ini": "[alembic]\n",
        "supervisor/" + FIXTURE: supervisor.to_json(),
        "supervisor/src/yordamchi_supervisor/__init__.py": "",
    }
    files.update(extra or {})
    return files


@dataclass
class FakeTools:
    """A `CommandRunner` for git (on an in-memory working copy), uv, make and a release's own python."""

    commits: dict[str, FakeCommit] = field(default_factory=dict)
    refs: dict[str, str] = field(default_factory=dict)  # "main", "selfupdate/…" → sha
    head: str = "main"
    dirty: bool = False
    origin: bool = False
    calls: list[list[str]] = field(default_factory=list)
    guarded: list[list[str]] = field(default_factory=list)  # commands run under the network guard
    pushed: list[list[str]] = field(default_factory=list)
    upstream: str | None = None  # what `git fetch origin main` brings: the GitHub `main` (None = nothing there)
    fetch_fails: bool = False
    fetches: list[dict[str, str]] = field(default_factory=list)  # the environment of every fetch

    def add(
        self,
        sha: str,
        behaviour: Behaviour,
        *,
        parent: str | None = BASE_SHA,
        extra: Mapping[str, str] | None = None,
        branch: str | None = None,
        kind: str = "app",
    ) -> FakeCommit:
        commit = FakeCommit(sha=sha, parent=parent, files=tree(behaviour, kind=kind, extra=extra))
        self.commits[sha] = commit
        if branch is not None:
            self.refs[branch] = sha
        return commit

    # --- dispatch -------------------------------------------------------------------------------------------------
    def run(
        self, argv: Sequence[str], *, timeout: float, cwd: Path | None = None, env: Mapping[str, str] | None = None
    ) -> CommandResult:
        args = list(argv)
        self.calls.append(args)
        if args and args[0] == SANDBOX_EXEC:
            args = args[3:]
            self.guarded.append(args)
        program = Path(args[0]).name
        if program == "git":
            if args[3:4] == ["fetch"]:
                self.fetches.append(dict(env or {}))
            return self._git(args[3:])
        if program == "uv":
            return self._uv(args[1:], cwd)
        if program == "make":
            return self._make(cwd)
        if args[0].endswith(".venv/bin/python"):
            return self._python(Path(args[0]).parents[2], args[1:], env or {})
        return CommandResult(returncode=127, stderr=b"not found")

    # --- git ------------------------------------------------------------------------------------------------------
    def _sha(self, ref: str) -> str | None:
        ref = ref.removesuffix("^{commit}").removeprefix("refs/heads/").removeprefix("refs/remotes/")
        if ref in self.commits:
            return ref
        return self.refs.get(ref)

    def _ancestors(self, sha: str) -> list[str]:
        chain: list[str] = []
        current: str | None = sha
        while current is not None and current in self.commits:
            chain.append(current)
            current = self.commits[current].parent
        return chain

    def _git(self, args: list[str]) -> CommandResult:  # noqa: PLR0911, PLR0912 - one branch per git command
        verb = args[0]
        if verb == "rev-parse":
            sha = self._sha(args[-1])
            return CommandResult(0, sha.encode()) if sha else CommandResult(1)
        if verb == "log":
            commit = self.commits.get(args[-1])
            return CommandResult(0, commit.day.encode()) if commit else CommandResult(128)
        if verb == "diff":
            base, head = self.commits.get(args[-2]), self.commits.get(args[-1])
            if base is None or head is None:
                return CommandResult(128)
            paths = sorted(p for p in base.files.keys() | head.files.keys() if base.files.get(p) != head.files.get(p))
            return CommandResult(0, "\n".join(paths).encode())
        if verb == "archive":
            return self._archive(args)
        if verb == "symbolic-ref":
            return CommandResult(0, self.head.encode())
        if verb == "status":
            return CommandResult(0, b" M src/yordamchi/x.py" if self.dirty else b"")
        if verb == "switch":
            self.head = args[1]
            return CommandResult(0)
        if verb == "merge-base":
            tip = self._sha(args[-1])
            return CommandResult(0 if tip is not None and self._sha(args[-2]) in self._ancestors(tip) else 1)
        if verb == "fetch":
            if self.fetch_fails or not self.origin:
                return CommandResult(128, stderr=b"fatal: unable to access the remote")
            if self.upstream is not None:
                self.refs["origin/main"] = self.upstream
            return CommandResult(0)
        if verb == "merge":
            self.refs[self.head] = args[-1]
            return CommandResult(0)
        if verb == "update-ref":
            name, new, old = args[1].removeprefix("refs/heads/"), args[2], args[3]
            if self.refs.get(name, "") != old:  # an empty old value: the ref must not exist yet (as git)
                return CommandResult(1)
            self.refs[name] = new
            return CommandResult(0)
        if verb == "branch":
            self.refs.pop(args[-1], None)
            return CommandResult(0)
        if verb == "remote":
            return CommandResult(0 if self.origin else 2, b"git@github.com:owner/Yordamchi.git" if self.origin else b"")
        if verb == "push":
            self.pushed.append(args)
            return CommandResult(0)
        return CommandResult(129, stderr=f"unknown git {verb}".encode())

    def _archive(self, args: list[str]) -> CommandResult:
        output = next(arg.removeprefix("--output=") for arg in args if arg.startswith("--output="))
        sha, _, sub = args[-1].partition(":")
        commit = self.commits.get(sha)
        if commit is None:
            return CommandResult(128, stderr=b"fatal: not a valid object name")
        prefix = f"{sub}/" if sub else ""
        with tarfile.open(output, mode="w") as tar:
            for path, text in commit.files.items():
                if not path.startswith(prefix):
                    continue
                data = text.encode()
                info = tarfile.TarInfo(path.removeprefix(prefix))
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        return CommandResult(0)

    # --- uv and make ----------------------------------------------------------------------------------------------
    def _uv(self, args: list[str], cwd: Path | None) -> CommandResult:
        if cwd is None:
            raise ValueError("uv and make run in a directory")
        behaviour = Behaviour.read(cwd)
        if args[0] == "sync":
            if behaviour.build == "fail":
                return CommandResult(1, stderr=b"error: No solution found when resolving dependencies")
            python = cwd / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("#!/bin/sh\n", encoding="utf-8")
            return CommandResult(0)
        if args[:2] == ["run", "--locked"] and "--collect-only" in args:
            return CommandResult(0, f"{behaviour.app_tests} tests collected in 1.20s\n".encode())
        return CommandResult(2, stderr=b"unknown uv command")

    def _make(self, cwd: Path | None) -> CommandResult:
        if cwd is None:
            raise ValueError("uv and make run in a directory")
        behaviour = Behaviour.read(cwd)
        if behaviour.failing:
            lines = [f"FAILED {name} - AssertionError" for name in behaviour.failing]
            return CommandResult(2, "\n".join([*lines, "make: *** [test] Error 1"]).encode())
        summary = f"{behaviour.app_tests} passed in 30.1s\nRan {behaviour.supervisor_tests} tests in 0.4s\n\nOK\n"
        return CommandResult(0, summary.encode() + b"make check: all stages green\n")

    # --- a release's own python -----------------------------------------------------------------------------------
    def _python(self, release: Path, args: list[str], env: Mapping[str, str]) -> CommandResult:
        behaviour = Behaviour.read(release)
        module = args[args.index("-m") + 1]
        if module == "alembic":
            db = next(arg.removeprefix("db=") for arg in args if arg.startswith("db="))
            return migrate(Path(db), behaviour.migration)
        if module == "unittest":
            if behaviour.failing:
                lines = [f"FAIL: {name} (tests.test_x.Case.{name})" for name in behaviour.failing]
                return CommandResult(
                    1, stderr="\n".join([*lines, "Ran 3 tests in 0.1s", "FAILED (failures=1)"]).encode()
                )
            return CommandResult(0, stderr=f"Ran {behaviour.supervisor_tests} tests in 0.4s\n\nOK\n".encode())
        if module == "yordamchi_supervisor":
            if "YORDAMCHI_DATA_DIR" not in env:
                raise AssertionError("the trial run must never see the real data dir")
            return CommandResult(
                0 if behaviour.trial == "ok" else 1, stderr=b"" if behaviour.trial == "ok" else b"ImportError: boom"
            )
        return CommandResult(2, stderr=b"unknown module")


# --- the bots' DB, and Telegram holding owner messages --------------------------------------------------------------
def make_bot_db(path: Path, *, offset: int = 0, processed: Sequence[int] = ()) -> None:
    """The slice of the P1 schema the simulation uses: the poll offset and what was processed, in WAL mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS poll_offsets (bot TEXT PRIMARY KEY, next_offset INTEGER NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS processed (update_id INTEGER PRIMARY KEY)")
        conn.execute("INSERT OR REPLACE INTO poll_offsets VALUES ('dev', ?)", (offset,))
        conn.executemany("INSERT OR IGNORE INTO processed VALUES (?)", [(n,) for n in processed])
        conn.commit()
    finally:
        conn.close()


def migrate(db: Path, how: str) -> CommandResult:
    """`alembic upgrade head` as the fixture says: nothing, a new table, or a migration that dies half way (its
    first statements are already committed: SQLite DDL is not all-or-nothing across an alembic run)."""
    if how == "none":
        return CommandResult(0)
    conn = sqlite3.connect(db)
    try:
        if how == "ok":
            conn.execute("CREATE TABLE IF NOT EXISTS self_updates (id INTEGER PRIMARY KEY, status TEXT)")
            conn.commit()
            return CommandResult(0)
        conn.execute("CREATE TABLE half_done (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO half_done VALUES (1)")
        conn.execute("UPDATE poll_offsets SET next_offset = next_offset + 1000")
        conn.commit()
    finally:
        conn.close()
    return CommandResult(1, stderr=b"sqlite3.OperationalError: duplicate column name: kind")


def dump(db: Path) -> list[str]:
    """Every schema statement and row, for 'the DB equals the pre-update copy'."""
    conn = sqlite3.connect(db)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def processed(db: Path) -> list[int]:
    conn = sqlite3.connect(db)
    try:
        return [row[0] for row in conn.execute("SELECT update_id FROM processed ORDER BY update_id")]
    finally:
        conn.close()


def offset(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        row = conn.execute("SELECT next_offset FROM poll_offsets WHERE bot = 'dev'").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


@dataclass
class FakeTelegram:
    sent: list[int] = field(default_factory=list)
    peeks: int = 0

    def send(self, count: int = 1) -> list[int]:
        start = (self.sent[-1] + 1) if self.sent else 1
        new = list(range(start, start + count))
        self.sent.extend(new)
        return new

    def consume(self, db: Path) -> int:
        """A normal-mode `getUpdates`: everything at or after the bot's offset is processed, then the offset moves."""
        conn = sqlite3.connect(db)
        try:
            row = conn.execute("SELECT next_offset FROM poll_offsets WHERE bot = 'dev'").fetchone()
            start = int(row[0]) if row else 0
            batch = [n for n in self.sent if n >= start]
            conn.executemany("INSERT OR IGNORE INTO processed VALUES (?)", [(n,) for n in batch])
            if batch:
                conn.execute("UPDATE poll_offsets SET next_offset = ? WHERE bot = 'dev'", (batch[-1] + 1,))
            conn.commit()
        finally:
            conn.close()
        return len(batch)

    def peek(self) -> None:
        """`getUpdates(offset=N, limit=1, timeout=0)` in probation: proves polling works, moves nothing."""
        self.peeks += 1


# --- bots under launchd ---------------------------------------------------------------------------------------------
@dataclass
class _Process:
    pid: int
    started_at: datetime
    version: str
    behaviour: Behaviour
    last_poll_ok_at: datetime | None = None


class FakeBots:
    """The bots as launchd runs them. Use `launchctl` as the updater's `Runner` and this object as its `SelfTester`;
    call `step(now)` once per tick to let every live process do one round of work."""

    def __init__(self, paths: SupervisorPaths, clock: FakeClock, telegram: FakeTelegram) -> None:
        self.paths = paths
        self.clock = clock
        self.telegram = telegram
        self.launchctl = FakeLaunchctl()
        self.processes: dict[str, _Process] = {}
        self.selftests: list[tuple[str, str]] = []
        self.unanswered = 0  # this many self-tests get no answer first (a socket not listening yet)

    def load(self, bot: Bot) -> None:
        self.launchctl.jobs[label(bot)] = FakeJob(pid=4242)

    def _release(self) -> Path:
        return self.paths.current_release.resolve()

    def kill(self, bot: Bot) -> None:
        """`kill -9` of a bot: launchd's KeepAlive starts a new process."""
        self.launchctl.crash(label(bot), respawn=True)

    def step(self, now: datetime) -> None:
        for bot in BOTS:
            job = self.launchctl.jobs.get(label(bot))
            if job is None or job.pid is None:
                self.processes.pop(bot, None)
                continue
            process = self.processes.get(bot)
            if process is None or process.pid != job.pid:
                release = self._release()
                process = _Process(job.pid, now, release.name, Behaviour.read(release))
                self.processes[bot] = process
            if process.behaviour.bot == "crash_at_import":
                self.launchctl.crash(label(bot), respawn=True)
                self.processes.pop(bot, None)
                continue
            self._work(bot, process, now)

    def _mode(self, process: _Process) -> str:
        flag = _read(self.paths.probation_flag)
        if flag.get("version") == process.version and process.behaviour.bot != "ignores_probation":
            return "probation"
        return "paused" if self.paths.drain_flag.exists() else "normal"

    def _work(self, bot: str, process: _Process, now: datetime) -> None:
        mode = self._mode(process)
        if mode == "paused":
            drain = _read(self.paths.drain_flag)
            marker = self.paths.run / f"{bot}.drained"
            marker.write_text(json.dumps({"id": drain.get("id"), "at": now.isoformat()}), encoding="utf-8")
        elif process.behaviour.bot != "no_polling":
            if mode == "probation":
                self.telegram.peek()
            else:
                self.telegram.consume(self.paths.data / "db" / f"{bot}.db")
            process.last_poll_ok_at = now
        beat = {
            "pid": process.pid,
            "version": process.version,
            "started_at": process.started_at.isoformat(),
            "last_poll_ok_at": None if process.last_poll_ok_at is None else process.last_poll_ok_at.isoformat(),
            "mode": mode,
            "loop_lag_ms": 1,
        }
        path = self.paths.run / f"{bot}.heartbeat.json"
        path.write_text(json.dumps(beat), encoding="utf-8")
        os.utime(path, (now.timestamp(), now.timestamp()))

    # --- the SelfTester -------------------------------------------------------------------------------------------
    def selftest(self, bot: Bot, nonce: str) -> SelftestAnswer:
        self.selftests.append((bot, nonce))
        process = self.processes.get(bot)
        if process is None or self.unanswered > 0:
            self.unanswered = max(0, self.unanswered - 1)
            return SelftestAnswer(answered=False)
        flag = _read(self.paths.probation_flag)
        if process.behaviour.bot == "selftest_fails":
            return SelftestAnswer(answered=True, ok=False, failed_step="outbox")
        return SelftestAnswer(answered=True, ok=flag.get("nonce") == nonce, failed_step="" if flag else "nonce")


def _read(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


# --- releases already installed -------------------------------------------------------------------------------------
def install_release(root: Path, link: Path, sha: str, behaviour: Behaviour, *, treeish: str | None = None) -> Path:
    """A finished release folder (marker, venv python, fixture) and `link` pointing at it, as the installer leaves."""
    release = root / version_of(sha)
    (release / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (release / ".venv" / "bin" / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    (release / FIXTURE).write_text(behaviour.to_json(), encoding="utf-8")
    marker = {"version": release.name, "treeish": treeish or sha, "python": "3.12.15", "built_by": "scripts/install.py"}
    (release / RELEASE_MARKER).write_text(json.dumps(marker), encoding="utf-8")
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(".link.tmp")
    tmp.unlink(missing_ok=True)
    tmp.symlink_to(os.path.relpath(release, link.parent))
    tmp.replace(link)
    return release


class InlineWorker:
    """A `Worker` that runs each job at once, on the caller's thread."""

    def __init__(self) -> None:
        self.keys: list[str] = []

    def run(self, key: str, job: Job) -> StepResult | None:
        self.keys.append(key)
        return job()


# --- one Mac, shared by the updater's and the upstream job's tests ----------------------------------------------------
NEW_SHA = "b" * 40
BRANCH = "selfupdate/7-ovoz-sozlamasi"
UPDATE_ID = "u000007"
APP_CHANGE = {"src/yordamchi/dev/selfupdate/domain.py": "# new\n"}
SUPERVISOR_CHANGE = {"supervisor/src/yordamchi_supervisor/updater.py": "# new\n"}
GUARD = (SANDBOX_EXEC, "-p", "(version 1)")


class World:
    """One Mac: the data dir, an installed release, the dev bot loaded and polling, a working copy, Telegram."""

    def __init__(self, case: unittest.TestCase, *, previous_tests: dict[str, int] | None = None) -> None:
        self.data = TempData()
        case.addCleanup(self.data.cleanup)
        self.paths = self.data.paths
        self.clock = FakeClock(START)
        self.opener = FakeOpener()
        self.telegram = FakeTelegram()
        self.bots = FakeBots(self.paths, self.clock, self.telegram)
        self.tools = FakeTools()
        self.tools.add(BASE_SHA, Behaviour(), parent=None, branch="main")
        self.previous = install_release(self.paths.releases, self.paths.current_release, BASE_SHA, Behaviour())
        install_release(
            self.paths.supervisor_releases,
            self.paths.supervisor_current,
            BASE_SHA,
            Behaviour(),
            treeish=f"{BASE_SHA}:supervisor",
        )
        self.db = self.paths.db_file("dev")
        self.telegram.send(5)
        make_bot_db(self.db, offset=6, processed=[1, 2, 3, 4, 5])
        self.bots.load("dev")
        self.state = StateFile(self.paths.state_file)
        if previous_tests is not None:
            self.state.save_section(
                "update", {"last_good": {"app": {"version": self.previous.name, "tests": previous_tests}}}
            )
        self.alerter = Alerter(
            self.paths.env_file("dev"),
            self.data.log,
            self.data.scrubber,
            self.clock,
            keychain=self.data.keychain,
            opener=self.opener,
        )
        self.updater = self.new_updater()
        self.bots.step(self.clock.now())

    def new_updater(self, cls: type[Updater] = Updater, *, release: str = "") -> Updater:
        """A supervisor (re)start: a fresh process reading the same state file."""
        self.state = StateFile(self.paths.state_file)
        return cls(
            self.paths,
            runner=self.tools,
            launchctl=LaunchCtl(self.bots.launchctl, UID),
            selftester=self.bots,
            alerter=self.alerter,
            log=self.data.log,
            scrubber=self.data.scrubber,
            state=self.state,
            worker=InlineWorker(),
            release=release,
            network_guard=GUARD,
        )

    # --- the dev bot's side ---------------------------------------------------------------------------------------
    def request(
        self,
        behaviour: Behaviour,
        *,
        kind: str = "app",
        change: dict[str, str] | None = None,
        sha: str = NEW_SHA,
        health_timeout_s: int = 120,
    ) -> None:
        self.tools.add(sha, behaviour, extra=change if change is not None else APP_CHANGE, branch=BRANCH, kind=kind)
        self.tools.head = BRANCH
        directory = self.paths.update_dir(UPDATE_ID)
        directory.mkdir(parents=True, exist_ok=True)
        body = {
            "id": UPDATE_ID,
            "kind": kind,
            "commit": sha,
            "branch": BRANCH,
            "health_timeout_s": health_timeout_s,
            "drain_max_min": 30,
        }
        (directory / "request.json").write_text(json.dumps(body), encoding="utf-8")

    # --- time -----------------------------------------------------------------------------------------------------
    def tick(self, updater: Updater | None = None) -> None:
        self.bots.step(self.clock.now())
        (updater or self.updater).tick(self.clock.now())
        self.clock.advance(seconds=INTERVAL.total_seconds())

    def run(self, *, ticks: int = 200, updater: Updater | None = None, during: int = 0) -> dict[str, object]:
        """Tick until the status is final; `during` owner messages arrive on every tick meanwhile."""
        for _ in range(ticks):
            if during:
                self.telegram.send(during)
            self.tick(updater)
            status = self.status()
            if status.get("final"):
                for _ in range(3):  # the bots work off what Telegram held
                    self.tick(updater)
                return status
        raise AssertionError(f"no final status after {ticks} ticks: {self.status()}")

    def run_until(self, step: Step, *, ticks: int = 100, updater: Updater | None = None) -> Journal:
        for _ in range(ticks):
            self.tick(updater)
            journal = Journal.from_json(StateFile(self.paths.state_file).section("update").get("journal"))
            if journal is not None and journal.step == step.value:
                return journal
        raise AssertionError(f"never reached {step}")

    # --- reading ----------------------------------------------------------------------------------------------
    def status(self) -> dict[str, object]:
        path = self.paths.update_dir(UPDATE_ID) / "status.json"
        if not path.exists():
            return {}
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError(path)
        return raw

    def current(self) -> str | None:
        return link_target(self.paths.current_release)

    def beat(self) -> dict[str, object]:
        raw = json.loads(self.paths.heartbeat("dev").read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise TypeError(self.paths.heartbeat("dev"))
        return raw

    def kickstarts(self) -> int:
        return len(self.bots.launchctl.kicks())
