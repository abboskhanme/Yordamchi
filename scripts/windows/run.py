"""`scripts\\windows\\run.cmd` (= `uv run python scripts\\windows\\run.py`): keeps the personal bot running in this
terminal on Windows and updates it from GitHub (owner request 2026-10-31: other people run only the personal bot,
from a terminal, and it follows `origin/main` by itself). The Windows counterpart of launchd plus the supervisor's
upstream job (SPEC §3.2.12 "Upstream updates"), for a working copy the bot runs from directly.

- **The bot**: `python -m yordamchi.bots.personal.app` with this venv's interpreter, cwd = the checkout, in its own
  process group (`CREATE_NEW_PROCESS_GROUP`), so the terminal's Ctrl+C reaches only this runner, which then stops
  the bot with `CTRL_BREAK_EVENT` and kills it after `STOP_GRACE_S`.
- **Crashes**: restarted after 2 s, 4 s, 8 s … at most `BACKOFF_MAX_S`; a run of `STABLE_AFTER_S` resets the
  backoff. A refused start (exit 78: `DATA\\run\\personal.lasterror` names why) waits the maximum straight away.
- **Updates**: the first check `FIRST_CHECK_AFTER` after start, then every `CHECK_EVERY` (3 days; state in
  `DATA\\run\\updater.json`); a failed fetch is retried after `RETRY_AFTER`. `git fetch origin main` never prompts
  (`GIT_TERMINAL_PROMPT=0`, ssh in batch mode). `decide()` updates only when the checkout is on `main`, has no
  uncommitted change to a tracked file, and `origin/main` is strictly ahead of `HEAD`; anything else is logged and
  skipped, so local work is never discarded. An update stops the bot, then `git merge --ff-only`, `uv sync
  --frozen`, a `sqlite3` backup of `personal.db` and `alembic upgrade head`; a failure puts back the old commit
  (`git reset --keep`), the old packages and the DB copy, and holds that commit until GitHub moves on. When the
  update changed this runner, it exits with `RESTART_EXIT` and `run.cmd` starts the new one; otherwise the bot
  starts again on the new code.
- A clone without a local `main` gets one at `origin/main` (create-only `update-ref`, HEAD and the working tree are
  untouched), as the supervisor does; it is then skipped as "not on main".

Logs: JSON lines in `DATA\\logs\\runner.log` (the same `core.config.load_paths(platform="win32")` resolution as the
bot; a stdlib copy of it when `yordamchi` is not importable), plus one short line per event on the terminal.
Standard library only. Every seam (`CommandRunner`, `spawn`, the clock) is injectable, so the tests run on macOS.
"""

import argparse
import contextlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess  # a stdlib program (no venv assumptions): argv lists only, never a shell
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol, TextIO

REPO: Final = Path(__file__).resolve().parents[2]
WINDOWS: Final = "win32"
REMOTE: Final = "origin"
MAIN: Final = "main"
UPSTREAM_REF: Final = f"refs/remotes/{REMOTE}/{MAIN}"
CHECK_EVERY: Final = timedelta(days=3)
FIRST_CHECK_AFTER: Final = timedelta(minutes=1)
RETRY_AFTER: Final = timedelta(hours=1)
STOP_GRACE_S: Final = 20.0
BACKOFF_START_S: Final = 2.0
BACKOFF_MAX_S: Final = 300.0
STABLE_AFTER_S: Final = 600.0
TICK_S: Final = 1.0
REFUSED_EXIT: Final = 78  # the bot refused to start (bots/personal/app.py)
RESTART_EXIT: Final = 3  # this runner was updated: run.cmd / run.ps1 start it again
TIMEOUT_GIT_S: Final = 60.0
TIMEOUT_FETCH_S: Final = 120.0
TIMEOUT_SYNC_S: Final = 1800.0
TIMEOUT_MIGRATE_S: Final = 300.0
LOG_MAX_BYTES: Final = 5_000_000
SELF_PATHS: Final = ("scripts/windows/",)
BOT_MODULE: Final = "yordamchi.bots.personal.app"
APP_DIR_NAME: Final = "Yordamchi"
# Never a prompt: no terminal credential question, ssh in batch mode (supervisor/upstream.py FETCH_ENV).
FETCH_ENV: Final = {"GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes"}
STATE_FILE: Final = "updater.json"
TimeoutExpired: Final = subprocess.TimeoutExpired  # what a `Child.wait` raises (tests raise it without subprocess)


# --- where things are ----------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Dirs:
    data: Path
    logs: Path

    @property
    def run(self) -> Path:
        return self.data / "run"

    @property
    def db(self) -> Path:
        return self.data / "db" / "personal.db"

    @property
    def backups(self) -> Path:
        return self.data / "backups" / "pre-update"


def resolve_dirs(environ: Mapping[str, str], *, home: Path, platform: str = WINDOWS) -> Dirs:
    """DATA and the log dir exactly as the bot finds them: `core.config.load_paths` when `yordamchi` imports (the
    venv), else the same Windows rules in stdlib (`YORDAMCHI_DATA_DIR`, else `%LOCALAPPDATA%\\Yordamchi`)."""
    try:
        from yordamchi.core import config  # noqa: PLC0415  # optional: this script must start without the venv
    except ImportError:
        local = environ.get("LOCALAPPDATA")
        data = Path(
            environ.get("YORDAMCHI_DATA_DIR") or (Path(local) if local else home / "AppData" / "Local") / APP_DIR_NAME
        )
        logs = environ.get("YORDAMCHI_LOG_DIR")
        return Dirs(data=data.expanduser(), logs=Path(logs).expanduser() if logs else data.expanduser() / "logs")
    paths = config.load_paths(environ, home=home, platform=platform)
    return Dirs(data=paths.data, logs=paths.logs)


# --- logging -------------------------------------------------------------------------------------------------------
class RunLog:
    """JSON lines in `runner.log` (rotated once at `LOG_MAX_BYTES`) and a short line on the terminal. Events and ids
    only, never message content or env values."""

    def __init__(self, file: Path | None, stream: TextIO | None, now: Callable[[], datetime]) -> None:
        self._file = file
        self._stream = stream
        self._now = now

    def _write(self, level: str, event: str, fields: Mapping[str, object]) -> None:
        at = self._now()
        if self._file is not None:
            record = {"ts": at.isoformat(timespec="seconds"), "level": level, "event": event, **fields}
            with contextlib.suppress(OSError):
                self._file.parent.mkdir(parents=True, exist_ok=True)
                if self._file.exists() and self._file.stat().st_size > LOG_MAX_BYTES:
                    self._file.replace(self._file.with_name(self._file.name + ".1"))
                with self._file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
        if self._stream is not None:
            extra = " ".join(f"{key}={value}" for key, value in fields.items())
            with contextlib.suppress(OSError, ValueError):
                self._stream.write(f"[runner {at.strftime('%H:%M:%S')}] {event} {extra}".rstrip() + "\n")
                self._stream.flush()

    def info(self, event: str, **fields: object) -> None:
        self._write("info", event, fields)

    def warning(self, event: str, **fields: object) -> None:
        self._write("warning", event, fields)


# --- commands ------------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self) -> str:
        lines = (self.stderr or self.stdout).strip().splitlines()
        return lines[-1][:200] if lines else ""


class CommandRunner(Protocol):
    def __call__(
        self, argv: Sequence[str], *, cwd: Path, timeout: float, env: Mapping[str, str] | None = None
    ) -> Completed: ...


def subprocess_runner(
    argv: Sequence[str], *, cwd: Path, timeout: float, env: Mapping[str, str] | None = None
) -> Completed:
    merged = {**os.environ, **env} if env else None
    try:
        done = subprocess.run(
            list(argv),
            cwd=cwd,
            env=merged,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return Completed(returncode=124, stderr="timed out")
    except OSError as exc:
        return Completed(returncode=127, stderr=type(exc).__name__)
    return Completed(done.returncode, done.stdout, done.stderr)


# --- the update decision (pure) ------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Decision:
    update: bool
    reason: str


def decide(
    *, branch: str | None, dirty: bool, head: str, target: str, head_is_ancestor: bool, held: str | None = None
) -> Decision:
    """Update only when every condition holds; the first that fails names the skip. Local work always wins."""
    if branch != MAIN:
        return Decision(update=False, reason="not_on_main")
    if dirty:
        return Decision(update=False, reason="dirty")
    if head == target:
        return Decision(update=False, reason="up_to_date")
    if not head_is_ancestor:
        return Decision(update=False, reason="local_ahead")  # ahead of or diverged from origin/main
    if held == target:
        return Decision(update=False, reason="held")  # this commit already failed to apply
    return Decision(update=True, reason="behind")


# --- timing (pure) -------------------------------------------------------------------------------------------------
def parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def check_due(state: Mapping[str, object], *, now: datetime, started: datetime) -> bool:
    """Due once `next_at` has passed (missing → now), never sooner than `FIRST_CHECK_AFTER` after this runner
    started, so the bot is up first. A clock set backwards never pushes a check more than one period away."""
    if now < started + FIRST_CHECK_AFTER:
        return False
    at = parse_instant(state.get("next_at"))
    return at is None or now >= at or at - now > CHECK_EVERY


def backoff_delay(failures: int) -> float:
    """Seconds before restart number `failures` (1, 2, …): 2, 4, 8 … capped at `BACKOFF_MAX_S`."""
    if failures <= 0:
        return 0.0
    return min(BACKOFF_MAX_S, BACKOFF_START_S * float(2 ** min(failures - 1, 30)))


@dataclass(slots=True)
class Backoff:
    failures: int = 0

    def after_exit(self, *, ran_s: float, code: int | None) -> float:
        """The wait before the next start, given how long the bot ran and its exit code."""
        if ran_s >= STABLE_AFTER_S:
            self.failures = 0
        self.failures += 1
        if code == REFUSED_EXIT:
            return BACKOFF_MAX_S
        return backoff_delay(self.failures)


# --- state ---------------------------------------------------------------------------------------------------------
def read_state(path: Path) -> dict[str, object]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_state(path: Path, state: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# --- the updater ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Check:
    decision: Decision
    head: str = ""
    target: str = ""


@dataclass(frozen=True, slots=True)
class Applied:
    ok: bool
    restart_runner: bool = False


class Updater:
    def __init__(
        self,
        *,
        repo: Path,
        dirs: Dirs,
        runner: CommandRunner,
        log: RunLog,
        python: Path,
        uv: str | None,
        git: str = "git",
    ) -> None:
        self.repo = repo
        self.git = git
        self.dirs = dirs
        self.runner = runner
        self.log = log
        self.python = python
        self.uv = uv
        self.state_path = dirs.run / STATE_FILE

    def _git(self, *args: str, timeout: float = TIMEOUT_GIT_S, env: Mapping[str, str] | None = None) -> Completed:
        return self.runner([self.git, *args], cwd=self.repo, timeout=timeout, env={**FETCH_ENV, **(env or {})})

    def _sha(self, ref: str) -> str | None:
        result = self._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        sha = result.stdout.strip()
        return sha if result.ok and len(sha) == 40 else None

    # --- check ---
    def check(self, now: datetime) -> Check:
        """Fetch, then decide. Writes the next check time into the state file whatever happens."""
        state = read_state(self.state_path)
        if not self._git("remote", "get-url", REMOTE).ok:
            return self._skip(state, now, CHECK_EVERY, "no_origin")
        fetched = self._git(
            "fetch", "--quiet", "--no-tags", REMOTE, f"+refs/heads/{MAIN}:{UPSTREAM_REF}", timeout=TIMEOUT_FETCH_S
        )
        target = self._sha(UPSTREAM_REF) if fetched.ok else None
        if target is None:
            failures = state.get("failures")
            state["failures"] = (failures if isinstance(failures, int) else 0) + 1
            self.log.warning("update_fetch_failed", failures=state["failures"], detail=fetched.tail())
            return self._skip(state, now, RETRY_AFTER, "fetch_failed", log=False)
        state["failures"] = 0
        state["checked_at"] = now.isoformat()
        if self._sha(f"refs/heads/{MAIN}") is None and self._git("update-ref", f"refs/heads/{MAIN}", target, "").ok:
            self.log.info("update_main_created", commit=target[:12])
        branch_result = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
        branch = branch_result.stdout.strip() if branch_result.ok else None
        head = self._sha("HEAD") or ""
        status = self._git("status", "--porcelain", "--untracked-files=no")
        dirty = not status.ok or bool(status.stdout.strip())
        ancestor = bool(head) and self._git("merge-base", "--is-ancestor", head, target).ok
        held = state.get("held")
        decision = decide(
            branch=branch,
            dirty=dirty,
            head=head,
            target=target,
            head_is_ancestor=ancestor,
            held=held if isinstance(held, str) else None,
        )
        state["next_at"] = (now + CHECK_EVERY).isoformat()
        write_state(self.state_path, state)
        if decision.update:
            self.log.info("update_available", head=head[:12], target=target[:12])
        else:
            self.log.info("update_skipped", reason=decision.reason, head=head[:12], target=target[:12])
        return Check(decision=decision, head=head, target=target)

    def _skip(
        self, state: dict[str, object], now: datetime, after: timedelta, reason: str, *, log: bool = True
    ) -> Check:
        state["next_at"] = (now + after).isoformat()
        write_state(self.state_path, state)
        if log:
            self.log.info("update_skipped", reason=reason)
        return Check(decision=Decision(update=False, reason=reason))

    # --- apply (the bot is stopped) ---
    def apply(self, check: Check, now: datetime) -> Applied:
        old, target = check.head, check.target
        merged = self._git("merge", "--ff-only", "--quiet", target)
        if not merged.ok:
            return self._failed(target, "merge", merged, restore=None)
        if not self._sync():
            return self._failed(target, "uv_sync", None, restore=old)
        backup = self._backup_db(old)
        migrated = self.runner(
            [
                str(self.python),
                "-I",
                "-m",
                "alembic",
                "-c",
                str(self.repo / "alembic.ini"),
                "-x",
                f"db={self.dirs.db}",
                "upgrade",
                "head",
            ],
            cwd=self.repo,
            timeout=TIMEOUT_MIGRATE_S,
        )
        if not migrated.ok:
            self._restore_db(backup)
            return self._failed(target, "migrate", migrated, restore=old)
        changed = self._git("diff", "--name-only", old, target)
        restart = changed.ok and any(line.startswith(SELF_PATHS) for line in changed.stdout.splitlines())
        state = read_state(self.state_path)
        state.pop("held", None)
        state["last_update"] = {"from": old, "to": target, "at": now.isoformat()}
        write_state(self.state_path, state)
        self.log.info("update_applied", old=old[:12], new=target[:12], runner_changed=restart)
        return Applied(ok=True, restart_runner=restart)

    def _sync(self) -> bool:
        if self.uv is None:
            self.log.warning("update_uv_missing")
            return False
        result = self.runner([self.uv, "sync", "--frozen"], cwd=self.repo, timeout=TIMEOUT_SYNC_S)
        if not result.ok:
            self.log.warning("update_uv_sync_failed", detail=result.tail())
        return result.ok

    def _backup_db(self, old: str) -> Path | None:
        if not self.dirs.db.exists():
            return None
        target = self.dirs.backups / f"personal-{old[:12]}.db"
        target.parent.mkdir(parents=True, exist_ok=True)
        source = sqlite3.connect(f"file:{self.dirs.db.as_posix()}?mode=ro", uri=True)
        try:
            copy = sqlite3.connect(target)
            try:
                source.backup(copy)
            finally:
                copy.close()
        finally:
            source.close()
        return target

    def _restore_db(self, backup: Path | None) -> None:
        if backup is None:
            return
        db = self.dirs.db
        for extra in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm")):
            extra.unlink(missing_ok=True)
        shutil.copyfile(backup, db)
        self.log.info("update_db_restored", backup=backup.name)

    def _failed(self, target: str, step: str, result: Completed | None, *, restore: str | None) -> Applied:
        self.log.warning("update_failed", step=step, target=target[:12], detail=result.tail() if result else "")
        if restore is not None:
            reset = self._git("reset", "--keep", "--quiet", restore)
            synced = reset.ok and self._sync()
            self.log.info("update_rolled_back", commit=restore[:12], ok=synced)
        state = read_state(self.state_path)
        state["held"] = target
        write_state(self.state_path, state)
        return Applied(ok=False)


# --- the bot child -------------------------------------------------------------------------------------------------
class Child(Protocol):
    @property
    def pid(self) -> int: ...

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def send_signal(self, sig: int) -> None: ...

    def kill(self) -> None: ...


def spawn_bot(python: Path, repo: Path, *, platform: str = sys.platform) -> Child:
    argv = [str(python), "-m", BOT_MODULE]
    if platform == WINDOWS:
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)
        return subprocess.Popen(argv, cwd=repo, creationflags=flags)
    return subprocess.Popen(argv, cwd=repo, start_new_session=True)


def stop_signal(platform: str = sys.platform) -> int:
    """Windows: `CTRL_BREAK_EVENT` to the child's own process group (the bot handles SIGBREAK); elsewhere SIGTERM."""
    if platform == WINDOWS:
        return int(getattr(signal, "CTRL_BREAK_EVENT", 1))
    return int(signal.SIGTERM)


def stop_child(child: Child, *, grace_s: float, log: RunLog, platform: str = sys.platform) -> int | None:
    """Ask the bot to stop, wait `grace_s`, then kill it. Returns its exit code (None if it could not be reaped)."""
    if child.poll() is not None:
        return child.poll()
    with contextlib.suppress(OSError):
        child.send_signal(stop_signal(platform))
    try:
        return child.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        log.warning("bot_kill", pid=child.pid, grace_s=grace_s)
        with contextlib.suppress(OSError):
            child.kill()
        try:
            return child.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            return None


# --- the loop ------------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Runner:
    spawn: Callable[[], Child]
    updater: Updater
    log: RunLog
    now: Callable[[], datetime]
    monotonic: Callable[[], float]
    platform: str = sys.platform
    grace_s: float = STOP_GRACE_S
    started: datetime = field(init=False)
    child: Child | None = None
    child_started: float = 0.0
    restart_at: float = 0.0
    backoff: Backoff = field(default_factory=Backoff)

    def __post_init__(self) -> None:
        self.started = self.now()

    def start_bot(self) -> None:
        self.child = self.spawn()
        self.child_started = self.monotonic()
        self.log.info("bot_started", pid=self.child.pid)

    def stop_bot(self) -> None:
        if self.child is None:
            return
        code = stop_child(self.child, grace_s=self.grace_s, log=self.log, platform=self.platform)
        self.log.info("bot_stopped", pid=self.child.pid, code=code)
        self.child = None

    def tick(self) -> int | None:
        """One pass: reap or (re)start the bot, then the update check. Returns an exit code when the runner must
        end (`RESTART_EXIT` after it updated itself), else None."""
        mono = self.monotonic()
        if self.child is not None and (code := self.child.poll()) is not None:
            ran = mono - self.child_started
            delay = self.backoff.after_exit(ran_s=ran, code=code)
            hint = {"reason_file": "DATA\\run\\personal.lasterror"} if code == REFUSED_EXIT else {}
            self.log.warning("bot_exited", code=code, ran_s=round(ran), restart_in_s=round(delay), **hint)
            self.child = None
            self.restart_at = mono + delay
        if self.child is None and mono >= self.restart_at:
            self.start_bot()
        now = self.now()
        if not check_due(read_state(self.updater.state_path), now=now, started=self.started):
            return None
        check = self.updater.check(now)
        if not check.decision.update:
            return None
        self.stop_bot()
        applied = self.updater.apply(check, now)
        if applied.restart_runner:
            return RESTART_EXIT
        self.backoff = Backoff()
        self.start_bot()
        return None

    def loop(self, sleep: Callable[[float], None] = time.sleep) -> int:
        try:
            while True:
                code = self.tick()
                if code is not None:
                    return code
                sleep(TICK_S)
        except KeyboardInterrupt:
            self.log.info("runner_stopping")
            return 0
        finally:
            self.stop_bot()


def _break_to_interrupt(signum: int, frame: object) -> None:
    del signum, frame
    raise KeyboardInterrupt


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="scripts\\windows\\run.py",
        description="Run the Yordamchi personal bot in this terminal and update it from GitHub every 3 days.",
        epilog=f"Ctrl+C stops it. Exit {RESTART_EXIT}: the runner updated itself (run.cmd starts it again).",
    )
    parser.parse_args(argv)
    if sys.platform != WINDOWS:
        sys.stderr.write("this runner is for Windows; on a Mac the bots run under launchd (scripts/install.py)\n")
        return 2
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(errors="replace")
    sigbreak = getattr(signal, "SIGBREAK", None)
    if sigbreak is not None:  # Ctrl+Break at the terminal: stop the bot cleanly instead of dying at once
        signal.signal(sigbreak, _break_to_interrupt)

    def now() -> datetime:
        return datetime.now(UTC)

    dirs = resolve_dirs(os.environ, home=Path.home())
    log = RunLog(dirs.logs / "runner.log", sys.stderr, now)
    python = Path(sys.executable)
    updater = Updater(
        repo=REPO,
        dirs=dirs,
        runner=subprocess_runner,
        log=log,
        python=python,
        uv=os.environ.get("UV") or shutil.which("uv"),
        git=shutil.which("git") or "git",
    )
    log.info("runner_started", repo=str(REPO), data=str(dirs.data))
    runner = Runner(spawn=lambda: spawn_bot(python, REPO), updater=updater, log=log, now=now, monotonic=time.monotonic)
    return runner.loop()


if __name__ == "__main__":
    raise SystemExit(main())
