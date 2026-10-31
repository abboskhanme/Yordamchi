"""`scripts/windows/run.py`, the Windows runner of the personal bot (owner request 2026-10-31), on this Mac: a fake
git/uv/alembic `CommandRunner`, a fake bot child and a hand-driven clock.

- `decide()`: update only on `main`, clean, and strictly behind `origin/main`; a held commit waits for GitHub.
- Timing: the first check a minute after start, then every 3 days; a failed fetch retries after 1 h.
- Crashes: 2 s, 4 s, 8 s … capped, reset after a stable run; a refused start waits the maximum.
- An update: stop the bot, ff-merge, `uv sync --frozen`, DB backup, alembic, start again; a failure rolls back the
  commit, the packages and the DB, and holds the commit. A change to the runner itself ends it with `RESTART_EXIT`.
- Stop: CTRL_BREAK_EVENT (SIGTERM off Windows), then a kill after the grace.
"""

import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from scripts.windows import run

from yordamchi.core import config

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
HEAD = "a" * 40
TARGET = "b" * 40
REPO = Path("C:/repo")
PYTHON = Path("C:/repo/.venv/Scripts/python.exe")


# --- fakes ---------------------------------------------------------------------------------------------------------
@dataclass
class FakeCommands:
    """Answers by argv prefix (after the tool): the longest matching prefix wins, default success with no output."""

    answers: dict[tuple[str, ...], run.Completed] = field(default_factory=dict)
    calls: list[tuple[str, ...]] = field(default_factory=list)
    envs: list[Mapping[str, str] | None] = field(default_factory=list)
    hooks: dict[tuple[str, ...], Callable[[], None]] = field(default_factory=dict)

    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout: float, env: Mapping[str, str] | None = None
    ) -> run.Completed:
        assert cwd == REPO
        assert timeout > 0
        call = tuple(argv)
        self.calls.append(call)
        self.envs.append(env)
        matches = [prefix for prefix in self.answers if call[: len(prefix)] == prefix]
        for prefix, hook in self.hooks.items():
            if call[: len(prefix)] == prefix:
                hook()
        return self.answers[max(matches, key=len)] if matches else run.Completed(0)

    def answer(self, *prefix: str, code: int = 0, out: str = "") -> None:
        self.answers[prefix] = run.Completed(code, stdout=out)

    def git_calls(self) -> list[tuple[str, ...]]:
        return [call[1:] for call in self.calls if call[0] == "git"]


def behind_repo(commands: FakeCommands) -> FakeCommands:
    """A clean `main` checkout at HEAD whose origin/main is TARGET, a fast-forward away."""
    commands.answer("git", "remote", "get-url", "origin", out="https://github.com/abboskhanme/Yordamchi.git")
    commands.answer("git", "rev-parse", "--verify", "--quiet", f"{run.UPSTREAM_REF}^{{commit}}", out=TARGET)
    commands.answer("git", "rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}", out=HEAD)
    commands.answer("git", "rev-parse", "--verify", "--quiet", "HEAD^{commit}", out=HEAD)
    commands.answer("git", "symbolic-ref", "--quiet", "--short", "HEAD", out="main")
    commands.answer("git", "status", "--porcelain", "--untracked-files=no", out="")
    commands.answer("git", "diff", "--name-only", out="src/yordamchi/core/config.py\n")
    return commands


class FakeLog(run.RunLog):
    def __init__(self) -> None:
        super().__init__(None, None, lambda: NOW)
        self.events: list[tuple[str, dict[str, object]]] = []

    def _write(self, level: str, event: str, fields: Mapping[str, object]) -> None:
        del level
        self.events.append((event, dict(fields)))

    def names(self) -> list[str]:
        return [event for event, _ in self.events]


@dataclass
class FakeChild:
    pid: int = 4242
    code: int | None = None
    obeys: bool = True
    signals: list[int] = field(default_factory=list)
    killed: bool = False

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        if self.code is None:
            raise run.TimeoutExpired("bot", timeout or 0)
        return self.code

    def send_signal(self, sig: int) -> None:
        self.signals.append(sig)
        if self.obeys:
            self.code = 0

    def kill(self) -> None:
        self.killed = True
        self.code = -9


@pytest.fixture
def dirs(tmp_path: Path) -> run.Dirs:
    return run.Dirs(data=tmp_path / "data", logs=tmp_path / "data" / "logs")


def make_updater(dirs: run.Dirs, commands: FakeCommands, log: FakeLog | None = None) -> run.Updater:
    return run.Updater(repo=REPO, dirs=dirs, runner=commands, log=log or FakeLog(), python=PYTHON, uv="uv", git="git")


# --- decide --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("branch", "dirty", "head", "ancestor", "held", "update", "reason"),
    [
        ("main", False, HEAD, True, None, True, "behind"),
        (None, False, HEAD, True, None, False, "not_on_main"),
        ("feature", False, HEAD, True, None, False, "not_on_main"),
        ("main", True, HEAD, True, None, False, "dirty"),
        ("main", False, TARGET, True, None, False, "up_to_date"),
        ("main", False, HEAD, False, None, False, "local_ahead"),
        ("main", False, HEAD, True, TARGET, False, "held"),
        ("main", False, HEAD, True, "c" * 40, True, "behind"),
    ],
)
def test_decide(
    branch: str | None, dirty: bool, head: str, ancestor: bool, held: str | None, update: bool, reason: str
) -> None:
    decision = run.decide(branch=branch, dirty=dirty, head=head, target=TARGET, head_is_ancestor=ancestor, held=held)
    assert decision == run.Decision(update=update, reason=reason)


# --- timing --------------------------------------------------------------------------------------------------------
def test_the_first_check_waits_a_minute_after_start() -> None:
    assert not run.check_due({}, now=NOW + timedelta(seconds=59), started=NOW)
    assert run.check_due({}, now=NOW + timedelta(minutes=1), started=NOW)


def test_a_check_is_due_only_once_next_at_has_passed() -> None:
    started = NOW - timedelta(days=1)
    state = {"next_at": (NOW + timedelta(hours=2)).isoformat()}
    assert not run.check_due(state, now=NOW, started=started)
    assert run.check_due(state, now=NOW + timedelta(hours=2), started=started)


def test_a_clock_set_backwards_does_not_push_the_check_past_one_period() -> None:
    state = {"next_at": (NOW + run.CHECK_EVERY + timedelta(days=30)).isoformat()}
    assert run.check_due(state, now=NOW, started=NOW - timedelta(hours=1))


def test_a_garbled_next_at_counts_as_due() -> None:
    assert run.check_due({"next_at": "soon"}, now=NOW, started=NOW - timedelta(hours=1))


def test_backoff_doubles_and_caps() -> None:
    assert [run.backoff_delay(n) for n in range(1, 6)] == [2.0, 4.0, 8.0, 16.0, 32.0]
    assert run.backoff_delay(50) == run.BACKOFF_MAX_S


def test_a_stable_run_resets_the_backoff_and_a_refusal_waits_the_maximum() -> None:
    backoff = run.Backoff()
    assert [backoff.after_exit(ran_s=1, code=1) for _ in range(3)] == [2.0, 4.0, 8.0]
    assert backoff.after_exit(ran_s=run.STABLE_AFTER_S, code=1) == 2.0
    assert backoff.after_exit(ran_s=1, code=run.REFUSED_EXIT) == run.BACKOFF_MAX_S


# --- check ---------------------------------------------------------------------------------------------------------
def test_a_behind_checkout_is_updated_and_the_next_check_is_in_three_days(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())

    check = make_updater(dirs, commands).check(NOW)

    assert check == run.Check(run.Decision(update=True, reason="behind"), head=HEAD, target=TARGET)
    fetch = next(call for call in commands.git_calls() if call[0] == "fetch")
    assert fetch == ("fetch", "--quiet", "--no-tags", "origin", f"+refs/heads/main:{run.UPSTREAM_REF}")
    assert all(env is not None and env["GIT_TERMINAL_PROMPT"] == "0" for env in commands.envs)
    assert ("merge-base", "--is-ancestor", HEAD, TARGET) in commands.git_calls()
    state = run.read_state(dirs.run / run.STATE_FILE)
    assert state["next_at"] == (NOW + run.CHECK_EVERY).isoformat()
    assert state["failures"] == 0


def test_a_failed_fetch_retries_after_an_hour(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    commands.answer("git", "fetch", code=128)
    log = FakeLog()

    check = make_updater(dirs, commands, log).check(NOW)

    assert check.decision == run.Decision(update=False, reason="fetch_failed")
    state = run.read_state(dirs.run / run.STATE_FILE)
    assert state["next_at"] == (NOW + run.RETRY_AFTER).isoformat()
    assert state["failures"] == 1
    assert "update_fetch_failed" in log.names()


def test_without_an_origin_remote_nothing_is_fetched(dirs: run.Dirs) -> None:
    commands = FakeCommands()
    commands.answer("git", "remote", "get-url", "origin", code=2)

    check = make_updater(dirs, commands).check(NOW)

    assert check.decision.reason == "no_origin"
    assert [call[0] for call in commands.git_calls()] == ["remote"]
    assert run.read_state(dirs.run / run.STATE_FILE)["next_at"] == (NOW + run.CHECK_EVERY).isoformat()


def test_uncommitted_work_is_never_touched(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    commands.answer("git", "status", "--porcelain", "--untracked-files=no", out=" M src/yordamchi/core/config.py\n")

    check = make_updater(dirs, commands).check(NOW)

    assert check.decision.reason == "dirty"
    assert not any(call[0] in {"merge", "reset", "checkout"} for call in commands.git_calls())


def test_local_commits_ahead_of_github_win(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    commands.answer("git", "merge-base", "--is-ancestor", code=1)

    assert make_updater(dirs, commands).check(NOW).decision.reason == "local_ahead"


def test_a_clone_without_local_main_gets_one_created_only(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    commands.answer("git", "rev-parse", "--verify", "--quiet", "refs/heads/main^{commit}", code=1)
    commands.answer("git", "symbolic-ref", "--quiet", "--short", "HEAD", code=1)  # detached

    check = make_updater(dirs, commands).check(NOW)

    assert ("update-ref", "refs/heads/main", TARGET, "") in commands.git_calls()
    assert check.decision.reason == "not_on_main"


# --- apply ---------------------------------------------------------------------------------------------------------
def _check() -> run.Check:
    return run.Check(run.Decision(update=True, reason="behind"), head=HEAD, target=TARGET)


def _make_db(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS marker (value TEXT)")
        db.execute("DELETE FROM marker")
        db.execute("INSERT INTO marker VALUES (?)", (value,))
        db.commit()
    finally:
        db.close()


def _db_value(path: Path) -> str:
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT value FROM marker").fetchone()
    finally:
        db.close()
    return str(row[0])


def test_an_update_merges_syncs_backs_up_and_migrates(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    _make_db(dirs.db, "before")

    applied = make_updater(dirs, commands).apply(_check(), NOW)

    assert applied == run.Applied(ok=True, restart_runner=False)
    tools = [call[:4] if call[0] == "git" else call[:3] for call in commands.calls]
    assert tools == [
        ("git", "merge", "--ff-only", "--quiet"),
        ("uv", "sync", "--frozen"),
        (str(PYTHON), "-I", "-m"),
        ("git", "diff", "--name-only", HEAD),
    ]
    assert commands.calls[0][-1] == TARGET
    alembic = commands.calls[2]
    assert alembic[3:] == ("alembic", "-c", str(REPO / "alembic.ini"), "-x", f"db={dirs.db}", "upgrade", "head")
    assert _db_value(dirs.backups / f"personal-{HEAD[:12]}.db") == "before"
    state = run.read_state(dirs.run / run.STATE_FILE)
    assert state["last_update"] == {"from": HEAD, "to": TARGET, "at": NOW.isoformat()}


def test_an_update_that_changes_the_runner_restarts_it(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    commands.answer("git", "diff", "--name-only", out="scripts/windows/run.py\nREADME.md\n")

    assert make_updater(dirs, commands).apply(_check(), NOW) == run.Applied(ok=True, restart_runner=True)


def test_a_failed_sync_rolls_back_and_holds_the_commit(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    commands.answer("uv", "sync", code=2)
    log = FakeLog()

    applied = make_updater(dirs, commands, log).apply(_check(), NOW)

    assert applied == run.Applied(ok=False)
    assert ("reset", "--keep", "--quiet", HEAD) in commands.git_calls()
    assert run.read_state(dirs.run / run.STATE_FILE)["held"] == TARGET
    assert "update_rolled_back" in log.names()
    held = run.decide(branch="main", dirty=False, head=HEAD, target=TARGET, head_is_ancestor=True, held=TARGET)
    assert held.reason == "held"


def test_a_failed_migration_restores_the_db_and_the_old_commit(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    commands.answer(str(PYTHON), code=1)
    _make_db(dirs.db, "before")
    commands.hooks[(str(PYTHON),)] = lambda: _make_db(dirs.db, "half-migrated")

    applied = make_updater(dirs, commands).apply(_check(), NOW)

    assert applied == run.Applied(ok=False)
    assert _db_value(dirs.db) == "before"
    assert ("reset", "--keep", "--quiet", HEAD) in commands.git_calls()
    assert sum(1 for call in commands.calls if call[:2] == ("uv", "sync")) == 2  # forward, then back


def test_a_refused_fast_forward_changes_nothing(dirs: run.Dirs) -> None:
    commands = behind_repo(FakeCommands())
    commands.answer("git", "merge", code=128)

    applied = make_updater(dirs, commands).apply(_check(), NOW)

    assert applied == run.Applied(ok=False)
    assert [call[0] for call in commands.calls] == ["git"]
    assert run.read_state(dirs.run / run.STATE_FILE)["held"] == TARGET


# --- the loop ------------------------------------------------------------------------------------------------------
@dataclass
class Clock:
    wall: datetime = NOW
    mono: float = 1000.0

    def now(self) -> datetime:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def advance(self, seconds: float) -> None:
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds


def make_runner(dirs: run.Dirs, commands: FakeCommands, clock: Clock) -> tuple[run.Runner, list[FakeChild]]:
    children: list[FakeChild] = []

    def spawn() -> FakeChild:
        children.append(FakeChild(pid=100 + len(children)))
        return children[-1]

    runner = run.Runner(
        spawn=spawn,
        updater=make_updater(dirs, commands),
        log=FakeLog(),
        now=clock.now,
        monotonic=clock.monotonic,
        platform="win32",
        grace_s=0.01,
    )
    return runner, children


def test_a_crashed_bot_restarts_after_the_backoff(dirs: run.Dirs) -> None:
    clock = Clock()
    runner, children = make_runner(dirs, FakeCommands(), clock)

    assert runner.tick() is None
    assert len(children) == 1
    children[0].code = 1
    clock.advance(5)
    runner.tick()
    assert len(children) == 1  # waits 2 s
    clock.advance(2)
    runner.tick()
    assert len(children) == 2


def test_a_due_check_with_an_update_stops_and_restarts_the_bot(dirs: run.Dirs) -> None:
    clock = Clock()
    commands = behind_repo(FakeCommands())
    runner, children = make_runner(dirs, commands, clock)
    runner.tick()
    clock.advance(30)
    runner.tick()
    assert not any(call[:2] == ("git", "fetch") for call in commands.calls)  # not before the first minute

    clock.advance(31)
    assert runner.tick() is None

    assert children[0].signals == [run.stop_signal("win32")]
    assert len(children) == 2
    assert runner.child is children[1]
    assert any(call[:2] == ("git", "merge") for call in commands.calls)


def test_a_runner_update_ends_the_runner_with_the_restart_code(dirs: run.Dirs) -> None:
    clock = Clock()
    commands = behind_repo(FakeCommands())
    commands.answer("git", "diff", "--name-only", out="scripts/windows/run.py\n")
    runner, children = make_runner(dirs, commands, clock)
    runner.tick()
    clock.advance(61)

    assert runner.tick() == run.RESTART_EXIT
    assert len(children) == 1
    assert runner.child is None


def test_ctrl_c_stops_the_bot_and_ends_the_loop(dirs: run.Dirs) -> None:
    runner, children = make_runner(dirs, FakeCommands(), Clock())

    def interrupt(seconds: float) -> None:
        del seconds
        raise KeyboardInterrupt

    assert runner.loop(sleep=interrupt) == 0
    assert children[0].signals == [run.stop_signal("win32")]
    assert runner.child is None


def test_a_bot_that_ignores_the_stop_is_killed_after_the_grace() -> None:
    child = FakeChild(obeys=False)
    log = FakeLog()

    assert run.stop_child(child, grace_s=0.01, log=log, platform="win32") == -9
    assert child.killed
    assert "bot_kill" in log.names()


def test_the_stop_signal_is_ctrl_break_on_windows_and_sigterm_elsewhere() -> None:
    import signal

    assert run.stop_signal("win32") == int(getattr(signal, "CTRL_BREAK_EVENT", 1))
    assert run.stop_signal("darwin") == int(signal.SIGTERM)


@pytest.mark.parametrize(("platform", "key"), [("win32", "creationflags"), ("darwin", "start_new_session")])
def test_the_bot_runs_in_its_own_process_group(monkeypatch: pytest.MonkeyPatch, platform: str, key: str) -> None:
    seen: dict[str, object] = {}

    def popen(argv: list[str], **kwargs: object) -> FakeChild:
        seen["argv"] = argv
        seen.update(kwargs)
        return FakeChild()

    monkeypatch.setattr("scripts.windows.run.subprocess.Popen", popen)

    run.spawn_bot(PYTHON, REPO, platform=platform)

    assert seen["argv"] == [str(PYTHON), "-m", "yordamchi.bots.personal.app"]
    assert seen["cwd"] == REPO
    assert seen[key] == (0x200 if key == "creationflags" else True)


# --- where DATA is -------------------------------------------------------------------------------------------------
def test_dirs_match_the_bots_resolution_with_and_without_the_venv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    environ = {"LOCALAPPDATA": str(tmp_path / "Local"), "APPDATA": str(tmp_path / "Roaming")}
    paths = config.load_paths(environ, home=tmp_path, platform="win32")

    with_venv = run.resolve_dirs(environ, home=tmp_path, platform="win32")
    monkeypatch.setitem(sys.modules, "yordamchi.core.config", None)  # `import` now raises ImportError
    without = run.resolve_dirs(environ, home=tmp_path, platform="win32")

    assert with_venv == without == run.Dirs(data=paths.data, logs=paths.logs)
    overridden = {**environ, "YORDAMCHI_DATA_DIR": str(tmp_path / "elsewhere")}
    assert run.resolve_dirs(overridden, home=tmp_path).data == tmp_path / "elsewhere"


def test_the_log_writes_json_lines_and_rotates(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "runner.log"
    log = run.RunLog(log_file, None, lambda: NOW)
    log.info("bot_started", pid=1)
    assert '"event": "bot_started"' in log_file.read_text(encoding="utf-8")
    log_file.write_bytes(b"x" * (run.LOG_MAX_BYTES + 1))
    log.info("bot_stopped", pid=1)
    assert log_file.with_name("runner.log.1").exists()
    assert log_file.read_text(encoding="utf-8").count("\n") == 1


def test_main_refuses_outside_windows(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    assert run.main([]) == 2
    assert "launchd" in capsys.readouterr().err
