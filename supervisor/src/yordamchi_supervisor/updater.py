"""The self-updater (SPEC §3.2.12, UC-19, R-30…R-33): build, test, drain, snapshot, migrate, switch, probation, and
either healthy (merge) or an automatic rollback. One `tick(now)` every `INTERVAL`, as the supervisor's `updater` job.

The dev bot writes `updates/<id>/request.json` (`dev/selfupdate`, T56). This module answers in
`updates/<id>/status.json` (and `updates/latest.json` once the outcome is final) and journals every step in the
`update` section of `supervisor/state.json` **before** it acts, so `kill -9` of anything at any moment has one outcome:

| journal step | on the next supervisor start |
|---|---|
| building, testing, draining, snapshot | the step runs again (nothing is switched yet; each step is idempotent) |
| migrating, switching, probation | roll back: `current` back to the previous release, the pre-update DB copies back |
| finishing (healthy) | finish: clear the flags, merge the branch, report |
| rolling_back, verifying | go on rolling back |
| self_switching (`kind=supervisor`) | this process is the new supervisor → healthy; the old one → switched back |

`kind=app`, in order:
1. **building**: `git archive <commit>` → `releases/<version>/`, `uv sync` with the installer's flags, marker,
   `chmod -R a-w` (`releases.py`). Nothing runs from the working copy.
2. **testing**: a throw-away workspace of the same commit with the dev dependencies runs `make check` with the
   network closed (`sandbox-exec`, `UV_OFFLINE=1`, `YORDAMCHI_DATA_DIR=<scratch>`), then `pytest --collect-only`.
   A failing stage, or fewer collected tests (app or supervisor) than the last good release had, is **refused**
   (CONVENTIONS §11): nothing is switched.
3. **draining**: `run/flags/drain.json`; every loaded bot finishes or parks its turns, pauses polling and writes
   `run/<bot>.drained` with this update's id. Telegram holds new owner messages meanwhile.
4. **snapshot**: each DB file → `backups/pre-update/<id>/` (sqlite3 backup API, `integrity_check`).
5. **migrating**: `migration_ran` is journaled first, then `<release>/.venv/bin/python -m alembic upgrade head`
   per DB file. A failure rolls back (the DB copy comes back; the bots never ran the new code).
6. **switching**: `run/flags/probation.json {version, nonce}`, `releases/current` swapped atomically, `launchctl
   kickstart -k` each loaded bot.
7. **probation** (≤ `health_timeout_s`): healthy = a heartbeat of the new version in `probation` mode with a
   successful peek poll after the switch, then `selftest(nonce)` over its socket. The bots only *peek* at Telegram
   (the offset never moves), so a rollback that restores the DB loses no owner message. A crash loop, a failed
   self-test, a bot that ignores probation, or the deadline → roll back.
8. **finishing**: `last_good` is recorded, the flags are cleared (the bots go normal and work off the held
   messages; one still in probation after 60 s is kickstarted), the branch is merged into `main` (fast-forward
   only; pushed when an `origin` exists), old releases are pruned, `status: merged`.
9. **rolling back**: `bootout`, `current` → the previous release, the DB copies restored if a migration ran, the
   flags cleared, `bootstrap`. Then **verifying**: the previous version must come back with fresh polls within the
   health timeout → `S.ROLLED_BACK`, else `S.MANUAL_REPAIR` (R-32).

`kind=supervisor` (R-32: never together with the app): build `supervisor/releases/<version>/`, run its unittest
suite and a `--selftest` trial run of the new code, then swap `supervisor/current` and exit; launchd's KeepAlive
starts the new supervisor, which confirms itself on its first tick (heartbeat with its own `release`). If it does
not come up, the dev bot sees no supervisor heartbeat of the new release within 60 s and switches the link back
(`dev/selfupdate`); the old supervisor then reports `rolled_back`.

Long steps (builds, tests, migrations) run on a `Worker` thread so the watchdog, the heartbeat and SIGTERM stay
responsive; a restart re-runs them from the journal.
"""

import json
import re
import secrets
import shutil
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Final, Protocol

from yordamchi_supervisor import releases as rel
from yordamchi_supervisor import strings as S
from yordamchi_supervisor.backups import backup_db
from yordamchi_supervisor.clock import from_timestamp
from yordamchi_supervisor.control import SelfTester
from yordamchi_supervisor.launchd import LaunchCtl
from yordamchi_supervisor.log import JsonLog, Scrubber
from yordamchi_supervisor.paths import BOTS, Bot, SupervisorPaths, label
from yordamchi_supervisor.proc import CommandRunner, tool_env
from yordamchi_supervisor.state import StateFile, parse_instant
from yordamchi_supervisor.telegram import Alerter
from yordamchi_supervisor.updates import TERMINAL

INTERVAL: Final = timedelta(seconds=5)
STATE_SECTION: Final = "update"
STATUS_REFRESH: Final = timedelta(seconds=60)  # keeps `updates.scan` from calling a long drain stale
HEALTH_TIMEOUT_S: Final = (30, 120, 600)  # min, default, max (the bot sends `selfupdate.health_timeout_s`)
DRAIN_MAX_MIN: Final = (1, 30, 240)  # the bot sends `selfupdate.drain_max_min`
DRAIN_ASK_GRACE: Final = timedelta(minutes=30)  # after drain_max_min the dev bot asks; the owner gets this long
NORMAL_MODE_WAIT: Final = timedelta(seconds=60)
CRASH_RUNS: Final = 3  # launchd starts during probation that make a crash loop
KEEP_RELEASES: Final = 3
MAX_DETAIL: Final = 300
TIMEOUT_CHECK_S: Final = 1800.0
TIMEOUT_MIGRATE_S: Final = 300.0
TIMEOUT_TRIAL_S: Final = 60.0
TIMEOUT_GIT_PUSH_S: Final = 120.0
MAKE: Final = "/usr/bin/make"
SANDBOX_EXEC: Final = "/usr/bin/sandbox-exec"
# The test run's network is closed (CONVENTIONS §11): no IP traffic at all; unix sockets stay (pytest-socket allows
# them, the control-socket tests need them).
NO_NETWORK_PROFILE: Final = "(version 1)(allow default)(deny network-outbound (remote ip))(deny network-inbound)"
MODE_PROBATION: Final = "probation"
MODE_NORMAL: Final = "normal"

_ID: Final = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_BRANCH: Final = re.compile(r"selfupdate/[A-Za-z0-9._-][A-Za-z0-9._/-]{0,119}")
_SHA: Final = re.compile(r"[0-9a-f]{40}")
_COLLECTED: Final = re.compile(r"(\d+)(?:/\d+)? tests? collected")
_RAN: Final = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)
_PYTEST_FAILED: Final = re.compile(r"^(?:FAILED|ERROR) (\S+)", re.MULTILINE)
_UNITTEST_FAILED: Final = re.compile(r"^(?:FAIL|ERROR): (\S+)", re.MULTILINE)
_MAKE_TARGET: Final = re.compile(r"make: \*\*\* \[([\w-]+)\]")


class Step(StrEnum):
    BUILDING = "building"
    TESTING = "testing"
    DRAINING = "draining"
    SNAPSHOT = "snapshot"
    MIGRATING = "migrating"
    SWITCHING = "switching"
    PROBATION = "probation"
    FINISHING = "finishing"
    ROLLING_BACK = "rolling_back"
    VERIFYING = "verifying"
    SELF_SWITCHING = "self_switching"


# A supervisor that starts and finds one of these journaled was killed while the new code may be live: roll back.
RESUME_ROLLS_BACK: Final = frozenset({Step.MIGRATING, Step.SWITCHING, Step.PROBATION})


# --- the request ----------------------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class UpdateRequest:
    id: str
    kind: str  # app | supervisor
    commit: str
    branch: str
    health_timeout_s: int
    drain_max_min: int


def _bounded(value: object, bounds: tuple[int, int, int]) -> int:
    low, default, high = bounds
    if not isinstance(value, int) or isinstance(value, bool):
        return default
    return min(high, max(low, value))


def parse_request(directory: Path) -> UpdateRequest | str:  # noqa: PLR0911 - one return per refused field
    """`request.json`, or the problem code. The supervisor trusts only its shape: the commit must also exist in the
    working copy, and the kind is checked against the diff."""
    try:
        raw = json.loads((directory / "request.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "bad_request"
    if not isinstance(raw, dict):
        return "bad_request"
    update_id, kind, commit, branch = raw.get("id"), raw.get("kind"), raw.get("commit"), raw.get("branch")
    if not (isinstance(update_id, str) and _ID.fullmatch(update_id) and update_id == directory.name):
        return "bad_request"
    if kind not in ("app", "supervisor") or not isinstance(kind, str):
        return "bad_request"
    if not (isinstance(commit, str) and _SHA.fullmatch(commit)):
        return "bad_request"
    if not (isinstance(branch, str) and _BRANCH.fullmatch(branch) and ".." not in branch):
        return "bad_request"
    return UpdateRequest(
        id=update_id,
        kind=kind,
        commit=commit,
        branch=branch,
        health_timeout_s=_bounded(raw.get("health_timeout_s"), HEALTH_TIMEOUT_S),
        drain_max_min=_bounded(raw.get("drain_max_min"), DRAIN_MAX_MIN),
    )


def read_status(directory: Path) -> dict[str, object] | None:
    try:
        raw = json.loads((directory / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {str(key): value for key, value in raw.items()} if isinstance(raw, dict) else None


# --- output parsing -------------------------------------------------------------------------------------------------
def collected_count(output: str) -> int | None:
    """`pytest --collect-only -q`: `1234 tests collected` or `1200/1234 tests collected (34 deselected)`."""
    found = _COLLECTED.findall(output)
    return int(found[-1]) if found else None


def unittest_count(output: str) -> int | None:
    found = _RAN.findall(output)
    return int(found[-1]) if found else None


def failure_summary(output: str, *, limit: int = 5) -> str:
    """What failed, for the owner: test ids if any, else the `make` stage."""
    names = [*_PYTEST_FAILED.findall(output), *_UNITTEST_FAILED.findall(output)]
    if names:
        shown = list(dict.fromkeys(names))
        more = f" (+{len(shown) - limit})" if len(shown) > limit else ""
        return ", ".join(shown[:limit]) + more
    stages = _MAKE_TARGET.findall(output)
    return f"make {stages[-1]}" if stages else "make check"


# --- the journal ----------------------------------------------------------------------------------------------------
@dataclass(slots=True)
class Journal:
    id: str
    kind: str
    commit: str
    branch: str
    version: str
    previous: str  # the release `current` pointed at when the update began: the rollback target
    step: str
    started_at: str
    health_timeout_s: int
    drain_max_min: int
    bots: list[str] = field(default_factory=list)  # the bots loaded when the drain began
    tests: dict[str, int] = field(default_factory=dict)
    baseline: dict[str, int] = field(default_factory=dict)  # the last good release's test counts
    snapshot: list[str] = field(default_factory=list)  # DB file names copied to backups/pre-update/<id>/
    migration_ran: bool = False
    nonce: str = ""
    drain_since: str = ""
    switched_at: str = ""
    deadline: str = ""
    runs: dict[str, int] = field(default_factory=dict)
    selftest_ok: list[str] = field(default_factory=list)
    reason: str = ""
    detail: str = ""
    rolled_back_at: str = ""
    healthy_at: str = ""
    normal_kicked: bool = False
    status_at: str = ""

    def to_json(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_json(cls, raw: object) -> "Journal | None":
        if not isinstance(raw, dict):
            return None

        def text(key: str) -> str:
            value = raw.get(key, "")
            return value if isinstance(value, str) else ""

        def number(key: str) -> int:
            value = raw.get(key, 0)
            return value if isinstance(value, int) and not isinstance(value, bool) else 0

        def names(key: str) -> list[str]:
            value = raw.get(key, [])
            return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []

        def counts(key: str) -> dict[str, int]:
            value = raw.get(key, {})
            if not isinstance(value, dict):
                return {}
            return {str(k): v for k, v in value.items() if isinstance(v, int) and not isinstance(v, bool)}

        journal = cls(
            id=text("id"),
            kind=text("kind"),
            commit=text("commit"),
            branch=text("branch"),
            version=text("version"),
            previous=text("previous"),
            step=text("step"),
            started_at=text("started_at"),
            health_timeout_s=number("health_timeout_s") or HEALTH_TIMEOUT_S[1],
            drain_max_min=number("drain_max_min") or DRAIN_MAX_MIN[1],
            bots=names("bots"),
            tests=counts("tests"),
            baseline=counts("baseline"),
            snapshot=names("snapshot"),
            migration_ran=raw.get("migration_ran") is True,
            nonce=text("nonce"),
            drain_since=text("drain_since"),
            switched_at=text("switched_at"),
            deadline=text("deadline"),
            runs=counts("runs"),
            selftest_ok=names("selftest_ok"),
            reason=text("reason"),
            detail=text("detail"),
            rolled_back_at=text("rolled_back_at"),
            healthy_at=text("healthy_at"),
            normal_kicked=raw.get("normal_kicked") is True,
            status_at=text("status_at"),
        )
        valid = journal.id and journal.kind in ("app", "supervisor") and journal.step in set(Step)
        return journal if valid else None


def _at(value: str) -> datetime | None:
    return parse_instant(value) if value else None


# --- long steps on a thread -----------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class StepResult:
    ok: bool
    code: str = ""
    detail: str = ""
    counts: Mapping[str, int] = field(default_factory=dict)


type Job = Callable[[], StepResult]


class Worker(Protocol):
    def run(self, key: str, job: Job) -> StepResult | None:
        """Start `job` under `key` unless it is running or done; its result once done (then forgotten), else None."""
        ...


class ThreadWorker:
    """One job at a time on a daemon thread. A supervisor that exits mid-job loses it; the journal re-runs it."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._key: str | None = None
        self._thread: threading.Thread | None = None
        self._result: StepResult | None = None

    def run(self, key: str, job: Job) -> StepResult | None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return None
            if self._key == key and self._result is not None:
                result, self._key, self._result, self._thread = self._result, None, None, None
                return result
            self._key, self._result = key, None
            self._thread = threading.Thread(target=self._body, args=(job,), name=f"updater-{key}", daemon=True)
            self._thread.start()
            return None

    def _body(self, job: Job) -> None:
        try:
            result = job()
        except Exception as exc:  # noqa: BLE001 - a crashed step is a failed step, never a dead updater
            result = StepResult(ok=False, code="internal_error", detail=type(exc).__name__)
        with self._lock:
            self._result = result


# --- heartbeats as the updater reads them ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Beat:
    written_at: datetime
    version: str
    mode: str
    last_poll_ok_at: datetime | None
    release: str = ""  # the supervisor's own heartbeat


def read_beat(path: Path) -> Beat | None:
    try:
        written_at = from_timestamp(path.stat().st_mtime)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None

    def text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    return Beat(
        written_at=written_at,
        version=text("version"),
        mode=text("mode"),
        last_poll_ok_at=parse_instant(raw.get("last_poll_ok_at")),
        release=text("release"),
    )


def write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def restore_db(copy: Path, target: Path) -> None:
    """Put the pre-update copy back (the bot is booted out). Its `-wal`/`-shm` go first: a WAL left beside the
    restored file would be replayed onto it."""
    for suffix in ("-wal", "-shm"):
        target.with_name(target.name + suffix).unlink(missing_ok=True)
    tmp = target.with_name(target.name + ".restore.tmp")
    shutil.copyfile(copy, tmp)
    tmp.chmod(0o600)
    tmp.replace(target)


_BOT_NAMES: Final[dict[str, Bot]] = {bot: bot for bot in BOTS}


def known_bots(names: Sequence[str]) -> list[Bot]:
    """The journal's bot names as `Bot`s (an unknown name from a hand-edited journal is skipped)."""
    return [_BOT_NAMES[name] for name in names if name in _BOT_NAMES]


def default_network_guard() -> tuple[str, ...]:
    return (SANDBOX_EXEC, "-p", NO_NETWORK_PROFILE) if Path(SANDBOX_EXEC).exists() else ()


# --- the updater ----------------------------------------------------------------------------------------------------
class Updater:
    def __init__(
        self,
        paths: SupervisorPaths,
        *,
        runner: CommandRunner,
        launchctl: LaunchCtl,
        selftester: SelfTester,
        alerter: Alerter,
        log: JsonLog,
        scrubber: Scrubber,
        state: StateFile,
        worker: Worker,
        release: str = "",
        network_guard: Sequence[str] = (),
        bots: tuple[Bot, ...] = BOTS,
    ) -> None:
        self._paths = paths
        self._runner = runner
        self._launchctl = launchctl
        self._selftester = selftester
        self._alerter = alerter
        self._log = log
        self._scrub = scrubber
        self._state = state
        self._worker = worker
        self._release = release  # this supervisor's own release folder name ("" when not run from one)
        self._guard = tuple(network_guard)
        self._bots = bots
        self.exit_requested = False  # set after a supervisor self-switch: the loop stops, launchd starts the new one
        self._resuming = self._journal() is not None

    # --- hooks for the fault tests (`kill -9` at a named point) ------------------------------------------------------
    def checkpoint(self, name: str) -> None:
        """Called at the points a crash matters most; does nothing in production."""

    # --- persistence ---------------------------------------------------------------------------------------------
    def _section(self) -> dict[str, object]:
        return self._state.section(STATE_SECTION)

    def _journal(self) -> Journal | None:
        return Journal.from_json(self._section().get("journal"))

    def _save(self, journal: Journal | None) -> None:
        section = self._section()
        if journal is None:
            section.pop("journal", None)
        else:
            section["journal"] = journal.to_json()
        self._state.save_section(STATE_SECTION, section)

    def last_good(self, kind: str) -> dict[str, object]:
        good = self._section().get("last_good")
        entry = good.get(kind) if isinstance(good, dict) else None
        return {str(k): v for k, v in entry.items()} if isinstance(entry, dict) else {}

    def _set_last_good(self, journal: Journal) -> None:
        section = self._section()
        good = section.get("last_good")
        table = {str(k): v for k, v in good.items()} if isinstance(good, dict) else {}
        table[journal.kind] = {"version": journal.version, "tests": dict(journal.tests), "commit": journal.commit}
        section["last_good"] = table
        self._state.save_section(STATE_SECTION, section)

    def _enter(self, journal: Journal, step: Step, now: datetime) -> None:
        journal.step = step.value
        self._save(journal)
        self._log.info("update_step", update_id=journal.id, step=step.value)
        self.checkpoint(f"entered_{step.value}")

    def _status(self, journal: Journal, status: str, now: datetime, *, final: bool = False, **extra: object) -> None:
        body: dict[str, object] = {
            "id": journal.id,
            "status": status,
            "kind": journal.kind,
            "version": journal.version,
            "previous": journal.previous,
            "commit": journal.commit,
            "branch": journal.branch,
            "tests": dict(journal.tests),
            "previous_tests": dict(journal.baseline),
            "migration_ran": journal.migration_ran,
            "result_code": journal.reason,
            "detail": self._scrub(journal.detail)[:MAX_DETAIL],
            "final": final,
            "updated_at": now.isoformat(),
        }
        body.update(extra)
        write_json_atomic(self._paths.update_dir(journal.id) / "status.json", body)
        if final:
            write_json_atomic(self._paths.latest_update, body)
        journal.status_at = now.isoformat()
        self._log.info("update_status", update_id=journal.id, status=status, code=journal.reason or None)

    def _previous_tests(self, journal: Journal) -> dict[str, int]:
        tests = self.last_good(journal.kind).get("tests")
        if not isinstance(tests, dict):
            return {}
        return {str(k): v for k, v in tests.items() if isinstance(v, int) and not isinstance(v, bool)}

    def _env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        return tool_env(self._paths.home, extra)

    # --- the job --------------------------------------------------------------------------------------------------
    def tick(self, now: datetime) -> None:
        journal = self._journal()
        if journal is not None and self._resuming:
            journal = self._resume(journal, now)
        self._resuming = False
        if journal is None:
            journal = self._begin_next(now)
        if journal is None or self.exit_requested:
            return
        self._advance(journal, now)
        current = self._journal()
        if current is not None and current.status_at:
            touched = _at(current.status_at)
            if touched is None or now - touched >= STATUS_REFRESH:
                self._touch(current, now)

    def _touch(self, journal: Journal, now: datetime) -> None:
        directory = self._paths.update_dir(journal.id)
        status = read_status(directory) or {}
        status["updated_at"] = now.isoformat()
        write_json_atomic(directory / "status.json", status)
        journal.status_at = now.isoformat()
        self._save(journal)

    def _advance(self, journal: Journal, now: datetime) -> None:
        handlers: dict[str, Callable[[Journal, datetime], None]] = {
            Step.BUILDING: self._building,
            Step.TESTING: self._testing,
            Step.DRAINING: self._draining,
            Step.SNAPSHOT: self._snapshot,
            Step.MIGRATING: self._migrating,
            Step.SWITCHING: self._switching,
            Step.PROBATION: self._probation,
            Step.FINISHING: self._finishing,
            Step.ROLLING_BACK: self._rolling_back,
            Step.VERIFYING: self._verifying,
            Step.SELF_SWITCHING: self._self_switching,
        }
        handlers[journal.step](journal, now)

    def _resume(self, journal: Journal, now: datetime) -> Journal | None:
        """The first tick of a supervisor that found a journal: it was killed, or it is the new supervisor."""
        self._log.warning("update_resumed", update_id=journal.id, step=journal.step)
        if journal.step in RESUME_ROLLS_BACK:
            self._start_rollback(journal, "supervisor_restarted", f"step {journal.step}", now)
        elif journal.step == Step.SELF_SWITCHING:
            return self._confirm_self_switch(journal, now)
        return self._journal()

    # --- begin ----------------------------------------------------------------------------------------------------
    def _pending(self) -> list[Path]:
        root = self._paths.updates
        if not root.is_dir():
            return []
        found = [p for p in root.iterdir() if p.is_dir() and (p / "request.json").exists()]
        return sorted(found, key=lambda p: ((p / "request.json").stat().st_mtime, p.name))

    def _begin_next(self, now: datetime) -> Journal | None:
        for directory in self._pending():
            status = read_status(directory)
            current = status.get("status") if status else None
            if current in TERMINAL:
                continue
            request = parse_request(directory)
            stub = Journal(
                id=directory.name,
                kind=request.kind if isinstance(request, UpdateRequest) else "app",
                commit=request.commit if isinstance(request, UpdateRequest) else "",
                branch=request.branch if isinstance(request, UpdateRequest) else "",
                version="",
                previous="",
                step=Step.BUILDING.value,
                started_at=now.isoformat(),
                health_timeout_s=HEALTH_TIMEOUT_S[1],
                drain_max_min=DRAIN_MAX_MIN[1],
            )
            if current not in (None, "requested"):
                # Mid-way with no journal (state.json lost): nothing can say what to undo. Stop and tell the owner.
                stub.reason = "journal_lost"
                self._status(stub, "failed", now, final=True)
                self._alerter.send("update_journal_lost", S.UPDATE_JOURNAL_LOST(update_id=stub.id))
                continue
            if isinstance(request, str):
                stub.reason = request
                self._status(stub, "failed", now, final=True)
                continue
            journal = self._begin(request, now)
            if journal is not None:
                return journal
        return None

    def _refuse_early(self, journal: Journal, code: str, detail: str, now: datetime) -> None:
        journal.reason, journal.detail = code, detail
        self._status(journal, "refused", now, final=True)
        self._back_to_main(journal)

    def _begin(self, request: UpdateRequest, now: datetime) -> Journal | None:
        env = self._env()
        repo = self._paths.repo
        app = request.kind == "app"
        link = self._paths.current_release if app else self._paths.supervisor_current
        releases = self._paths.releases if app else self._paths.supervisor_releases
        journal = Journal(
            id=request.id,
            kind=request.kind,
            commit=request.commit,
            branch=request.branch,
            version="",
            previous=rel.link_target(link) or "",
            step=Step.BUILDING.value,
            started_at=now.isoformat(),
            health_timeout_s=request.health_timeout_s,
            drain_max_min=request.drain_max_min,
        )
        sha = rel.resolve_commit(self._runner, repo, request.commit, env=env)
        if sha != request.commit:
            self._refuse_early(journal, "commit_missing", request.commit[:12], now)
            return None
        version = rel.release_version(self._runner, repo, sha, env=env)
        if version is None:
            self._refuse_early(journal, "git_failed", "commit date", now)
            return None
        journal.version = version
        if not journal.previous or not (releases / journal.previous).is_dir():
            self._refuse_early(journal, "no_current_release", str(link), now)
            return None
        if version == journal.previous:
            self._refuse_early(journal, "already_current", version, now)
            return None
        journal.baseline = self._previous_tests(journal)
        problem = self._kind_problem(request, releases / journal.previous, env)
        if problem:
            self._refuse_early(journal, problem, "", now)
            return None
        self._save(journal)
        self._status(journal, "building", now)
        self._log.info("update_started", update_id=journal.id, kind=journal.kind, version=version)
        return journal

    def _kind_problem(self, request: UpdateRequest, previous: Path, env: Mapping[str, str]) -> str:
        """R-32 again, from git itself: an update touches `supervisor/` or the app, never both."""
        base = rel.marker_commit(previous)
        if base is None:
            return ""  # an installer release from before markers carried the sha: the bot's check stands
        changed = rel.changed_paths(self._runner, self._paths.repo, base, request.commit, env=env)
        if changed is None:
            return "git_failed"
        if not changed:
            return "no_changes"
        supervisor = any(path.startswith("supervisor/") for path in changed)
        other = any(not path.startswith("supervisor/") for path in changed)
        if supervisor and other:
            return "split_needed"
        if (request.kind == "app") == supervisor:
            return "kind_mismatch"
        return ""

    # --- build and test -------------------------------------------------------------------------------------------
    def _release_dir(self, journal: Journal, name: str | None = None) -> Path:
        root = self._paths.releases if journal.kind == "app" else self._paths.supervisor_releases
        return root / (name or journal.version)

    def _refuse(self, journal: Journal, code: str, detail: str, now: datetime) -> None:
        """Refused before anything was switched: the bots never noticed."""
        journal.reason, journal.detail = code, detail
        self._status(journal, "refused", now, final=True)
        target = self._release_dir(journal)
        if target.name != journal.previous and rel.link_target(self._link(journal)) != target.name:
            rel.remove_tree(target)
        rel.remove_tree(self._paths.update_dir(journal.id) / "check")
        self._save(None)
        self._back_to_main(journal)
        self._log.warning("update_refused", update_id=journal.id, code=code)

    def _link(self, journal: Journal) -> Path:
        return self._paths.current_release if journal.kind == "app" else self._paths.supervisor_current

    def _building(self, journal: Journal, now: datetime) -> None:
        treeish = journal.commit if journal.kind == "app" else f"{journal.commit}:supervisor"
        target = self._release_dir(journal)

        def job() -> StepResult:
            built = rel.build_release(
                self._runner,
                repo=self._paths.repo,
                treeish=treeish,
                target=target,
                home=self._paths.home,
                env=self._env(),
            )
            return StepResult(ok=built.ok, code="" if built.ok else "build_failed", detail=built.problem)

        result = self._worker.run(f"{journal.id}:build", job)
        if result is None:
            return
        if not result.ok:
            self._refuse(journal, result.code, result.detail, now)
            return
        self._enter(journal, Step.TESTING, now)

    def _testing(self, journal: Journal, now: datetime) -> None:
        job = self._test_app if journal.kind == "app" else self._test_supervisor
        result = self._worker.run(f"{journal.id}:test", lambda: job(journal))
        if result is None:
            return
        if not result.ok:
            self._refuse(journal, result.code, result.detail, now)
            return
        journal.tests = dict(result.counts)
        dropped = [
            f"{name} {count} < {before}"
            for name, before in journal.baseline.items()
            if (count := journal.tests.get(name)) is not None and count < before
        ]
        if dropped:
            self._refuse(journal, "tests_dropped", "; ".join(dropped), now)
            return
        if not journal.baseline:
            self._log.warning("tests_baseline_unknown", update_id=journal.id, kind=journal.kind)
        self._enter(journal, Step.DRAINING if journal.kind == "app" else Step.SELF_SWITCHING, now)

    def _test_app(self, journal: Journal) -> StepResult:
        directory = self._paths.update_dir(journal.id)
        workspace, scratch = directory / "check", directory / "check-data"
        built = rel.check_workspace(
            self._runner,
            repo=self._paths.repo,
            treeish=journal.commit,
            target=workspace,
            home=self._paths.home,
            env=self._env(),
        )
        try:
            if not built.ok:
                return StepResult(ok=False, code="build_failed", detail=built.problem)
            scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
            env = self._env(
                {"UV_OFFLINE": "1", "YORDAMCHI_DATA_DIR": str(scratch), "YORDAMCHI_LOG_DIR": str(scratch / "logs")}
            )
            check = self._runner.run([*self._guard, MAKE, "check"], timeout=TIMEOUT_CHECK_S, cwd=workspace, env=env)
            self._keep_log(directory / "check.log", check.output())
            if not check.ok:
                return StepResult(ok=False, code="tests_failed", detail=failure_summary(check.output()))
            collect = self._runner.run(
                [*self._guard, rel.uv_binary(self._paths.home), "run", "--locked", "pytest", "--collect-only", "-q"],
                timeout=TIMEOUT_CHECK_S,
                cwd=workspace,
                env=env,
            )
            app_count, supervisor_count = collected_count(collect.output()), unittest_count(check.output())
            if not collect.ok or app_count is None or supervisor_count is None:
                return StepResult(ok=False, code="tests_uncounted", detail="pytest --collect-only")
            return StepResult(ok=True, counts={"app": app_count, "supervisor": supervisor_count})
        finally:
            rel.remove_tree(workspace)
            if scratch.exists():
                shutil.rmtree(scratch, ignore_errors=True)

    def _test_supervisor(self, journal: Journal) -> StepResult:
        release = self._release_dir(journal)
        python = str(release / ".venv" / "bin" / "python")
        directory = self._paths.update_dir(journal.id)
        tests = self._runner.run(
            [*self._guard, python, "-I", "-m", "unittest", "discover", "-s", "tests", "-t", "."],
            timeout=TIMEOUT_CHECK_S,
            cwd=release,
            env=self._env(),
        )
        self._keep_log(directory / "check.log", tests.output())
        count = unittest_count(tests.output())
        if not tests.ok:
            return StepResult(ok=False, code="tests_failed", detail=failure_summary(tests.output()))
        if count is None:
            return StepResult(ok=False, code="tests_uncounted", detail="unittest")
        scratch = directory / "trial"
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            trial = self._runner.run(
                [python, "-I", "-m", "yordamchi_supervisor", "--selftest"],
                timeout=TIMEOUT_TRIAL_S,
                cwd=scratch,
                env=self._env({"YORDAMCHI_DATA_DIR": str(scratch / "data"), "YORDAMCHI_LOG_DIR": str(scratch)}),
            )
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        if not trial.ok:
            return StepResult(ok=False, code="trial_failed", detail=" ".join(trial.tail(1)))
        return StepResult(ok=True, counts={"supervisor": count})

    def _keep_log(self, path: Path, text: str) -> None:
        lines = text.splitlines()[-400:]
        write = "\n".join(self._scrub(line) for line in lines) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text(write, encoding="utf-8")
        path.chmod(0o600)

    # --- drain, snapshot, migrate ---------------------------------------------------------------------------------
    def _loaded_bots(self) -> list[Bot]:
        loaded: list[Bot] = []
        for bot in self._bots:
            status = self._launchctl.status(label(bot))
            if status is not None and status.loaded:
                loaded.append(bot)
        return loaded

    def _drained(self, bot: Bot, update_id: str) -> bool:
        path = self._paths.drained_marker(bot)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(raw, dict) and raw.get("id") == update_id

    def _clear_flags(self) -> None:
        for path in (self._paths.drain_flag, self._paths.probation_flag):
            path.unlink(missing_ok=True)
        for bot in self._bots:
            self._paths.drained_marker(bot).unlink(missing_ok=True)

    def _fail(self, journal: Journal, code: str, detail: str, now: datetime) -> None:
        """Stopped before the switch (drain or snapshot): the bots resume on the same release, nothing to undo."""
        self._clear_flags()
        journal.reason, journal.detail = code, detail
        self._status(journal, "failed", now, final=True)
        self._save(None)
        self._back_to_main(journal)
        self._log.error("update_failed", update_id=journal.id, code=code)

    def _draining(self, journal: Journal, now: datetime) -> None:
        if not journal.drain_since:
            journal.bots = list(self._loaded_bots())
            if not journal.bots:  # nothing could prove the new release healthy: never switch blind
                self._fail(journal, "bots_not_loaded", "", now)
                return
            for bot in self._bots:
                self._paths.drained_marker(bot).unlink(missing_ok=True)
            write_json_atomic(
                self._paths.drain_flag,
                {"id": journal.id, "since": now.isoformat(), "drain_max_min": journal.drain_max_min},
            )
            journal.drain_since = now.isoformat()
            self._save(journal)
            self._status(journal, "draining", now)
            return
        waiting = [
            bot for bot in known_bots(journal.bots) if not self._drained(bot, journal.id) and self._is_loaded(bot)
        ]
        if not waiting:
            self._enter(journal, Step.SNAPSHOT, now)
            return
        since = _at(journal.drain_since) or now
        if now - since > timedelta(minutes=journal.drain_max_min) + DRAIN_ASK_GRACE:
            self._fail(journal, "drain_timeout", ",".join(waiting), now)

    def _is_loaded(self, bot: Bot) -> bool:
        status = self._launchctl.status(label(bot))
        return status is None or status.loaded

    def _snapshot(self, journal: Journal, now: datetime) -> None:
        target = self._paths.pre_update_backup(journal.id)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        copied: list[str] = []
        for bot in BOTS:
            source = self._paths.db_file(bot)
            if not source.exists():
                continue
            result = backup_db(source, target / source.name)
            if not result.ok:
                self._fail(journal, "snapshot_failed", f"{source.name}: {result.problem}", now)
                return
            copied.append(source.name)
        journal.snapshot = copied
        journal.migration_ran = True  # journaled before the first statement runs: a crash from here restores
        self._enter(journal, Step.MIGRATING, now)

    def _migrating(self, journal: Journal, now: datetime) -> None:
        release = self._release_dir(journal)

        def job() -> StepResult:
            python = str(release / ".venv" / "bin" / "python")
            for name in journal.snapshot:
                db = self._paths.data / "db" / name
                argv = [python, "-I", "-m", "alembic", "-c", str(release / "alembic.ini"), "-x", f"db={db}"]
                result = self._runner.run(
                    [*argv, "upgrade", "head"], timeout=TIMEOUT_MIGRATE_S, cwd=release, env=self._env()
                )
                if not result.ok:
                    return StepResult(ok=False, code="migration_failed", detail=f"{name}: {' '.join(result.tail(1))}")
            return StepResult(ok=True)

        result = self._worker.run(f"{journal.id}:migrate", job)
        if result is None:
            return
        if not result.ok:
            self._start_rollback(journal, result.code, result.detail, now)
            return
        self._enter(journal, Step.SWITCHING, now)

    # --- switch and probation -------------------------------------------------------------------------------------
    def _switching(self, journal: Journal, now: datetime) -> None:
        if not journal.nonce:
            journal.nonce = secrets.token_hex(16)
            self._save(journal)
        write_json_atomic(
            self._paths.probation_flag, {"id": journal.id, "version": journal.version, "nonce": journal.nonce}
        )
        self.checkpoint("before_swap")
        rel.swap_symlink(self._paths.current_release, self._release_dir(journal))
        self.checkpoint("after_swap")
        journal.switched_at = now.isoformat()
        journal.deadline = (now + timedelta(seconds=journal.health_timeout_s)).isoformat()
        self._save(journal)
        for bot in known_bots(journal.bots):
            if not self._launchctl.kickstart(label(bot)):
                self._start_rollback(journal, "switch_failed", f"kickstart {bot}", now)
                return
            status = self._launchctl.status(label(bot))
            if status is not None and status.runs is not None:
                journal.runs[bot] = status.runs
        self.checkpoint("after_kickstart")
        self._enter(journal, Step.PROBATION, now)
        self._status(journal, "probation", now)

    def _probation_problem(self, journal: Journal, bot: Bot, switched_at: datetime) -> str:  # noqa: PLR0911
        status = self._launchctl.status(label(bot))
        base = journal.runs.get(bot)
        if status is not None and status.runs is not None and base is not None and status.runs - base >= CRASH_RUNS:
            return "crash_loop"
        beat = read_beat(self._paths.heartbeat(bot))
        if beat is None or beat.version != journal.version or beat.written_at < switched_at:
            return "no_heartbeat"
        if beat.mode != MODE_PROBATION:
            return "probation_ignored"  # it would consume owner messages into a DB that may be restored
        if beat.last_poll_ok_at is None or beat.last_poll_ok_at < switched_at:
            return "polling_not_started"
        if bot in journal.selftest_ok:
            return ""
        answer = self._selftester.selftest(bot, journal.nonce)
        if not answer.answered:
            return "selftest_pending"
        if not answer.ok:
            journal.detail = answer.failed_step
            return "selftest_failed"
        journal.selftest_ok.append(bot)
        self._save(journal)
        self._log.info("update_selftest_ok", update_id=journal.id, bot=bot)
        return ""

    def _probation(self, journal: Journal, now: datetime) -> None:
        switched_at = _at(journal.switched_at) or now
        problems = {bot: self._probation_problem(journal, bot, switched_at) for bot in known_bots(journal.bots)}
        problems = {bot: code for bot, code in problems.items() if code}
        if not problems:
            self._enter(journal, Step.FINISHING, now)
            return
        immediate = [
            code for code in problems.values() if code in ("crash_loop", "selftest_failed", "probation_ignored")
        ]
        deadline = _at(journal.deadline) or now
        if immediate or now >= deadline:
            code = immediate[0] if immediate else next(iter(problems.values()))
            code = "selftest_failed" if code == "selftest_pending" else code
            bots = ",".join(sorted(problems))
            self._start_rollback(journal, code, f"{bots}: {journal.detail}" if journal.detail else bots, now)

    # --- healthy --------------------------------------------------------------------------------------------------
    def _finishing(self, journal: Journal, now: datetime) -> None:
        if not journal.healthy_at:
            self._set_last_good(journal)
            self._clear_flags()
            journal.healthy_at = now.isoformat()
            self._save(journal)
            self._status(journal, "healthy", now)
            self._log.info("update_healthy", update_id=journal.id, version=journal.version)
            return
        healthy_at = _at(journal.healthy_at) or now
        stuck = [bot for bot in known_bots(journal.bots) if not self._normal(bot, journal.version)]
        if stuck and now - healthy_at < NORMAL_MODE_WAIT:
            return
        if stuck and not journal.normal_kicked:
            for bot in stuck:  # it missed the cleared flag: a restart without the flag starts it normal
                self._launchctl.kickstart(label(bot))
            journal.normal_kicked = True
            self._save(journal)
            self._log.warning("update_probation_stuck", update_id=journal.id, bots=stuck)
        outcome = self._merge(journal)
        journal.reason = "" if outcome == "merged" else outcome
        self._status(journal, "merged" if outcome == "merged" else "healthy", now, final=True)
        protect = {journal.version, journal.previous}
        removed = rel.prune(self._release_dir(journal).parent, keep=KEEP_RELEASES, protect=protect)
        self._save(None)
        self._log.info("update_done", update_id=journal.id, merge=outcome, pruned=removed)

    def _normal(self, bot: Bot, version: str) -> bool:
        beat = read_beat(self._paths.heartbeat(bot))
        return beat is not None and beat.version == version and beat.mode == MODE_NORMAL

    # --- git: merge a healthy branch, return the working copy to main ---------------------------------------------
    def _git(self, *args: str, timeout: float = rel.TIMEOUT_GIT_S) -> tuple[bool, str]:
        result = self._runner.run([rel.GIT, "-C", str(self._paths.repo), *args], timeout=timeout, env=self._env())
        return result.ok, result.text()

    def _head_branch(self) -> str | None:
        ok, branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD")
        return branch if ok and branch else None

    def _merge(self, journal: Journal) -> str:
        """Fast-forward `main` to the healthy commit (never a merge commit, never a force); the working copy ends on
        `main` with the same tree. `main` that moved meanwhile is left alone and reported."""
        ok, main = self._git("rev-parse", "--verify", "--quiet", "refs/heads/main")
        if not ok or not _SHA.fullmatch(main):
            return "merge_failed"
        if main != journal.commit:
            ancestor, _ = self._git("merge-base", "--is-ancestor", main, journal.commit)
            if not ancestor:
                self._log.warning("update_merge_not_ff", update_id=journal.id)
                return "merge_not_ff"
            head = self._head_branch()
            moved, _ = (
                self._git("merge", "--ff-only", journal.commit)
                if head == "main"
                else self._git("update-ref", "refs/heads/main", journal.commit, main)
            )
            if not moved:
                return "merge_failed"
        if self._head_branch() == journal.branch:
            self._git("switch", "main")  # the same commit: switching carries any local change and loses none
        if self._head_branch() != journal.branch:
            self._git("branch", "-d", journal.branch)
        has_origin, _ = self._git("remote", "get-url", "origin")
        if has_origin:
            pushed, _ = self._git("push", "origin", "main", timeout=TIMEOUT_GIT_PUSH_S)
            self._git("push", "origin", "--delete", journal.branch, timeout=TIMEOUT_GIT_PUSH_S)
            self._log.info("update_pushed", update_id=journal.id, ok=pushed)
        return "merged"

    def _back_to_main(self, journal: Journal) -> None:
        """A refused or rolled-back update leaves its branch for a fix, and the working copy on `main`, so the next
        self-update starts from `main`. A copy with local changes is left exactly as it is."""
        if not journal.branch or self._head_branch() != journal.branch:
            return
        ok, porcelain = self._git("status", "--porcelain")
        if not ok or porcelain:
            self._log.warning("update_copy_left_on_branch", update_id=journal.id, dirty=bool(porcelain))
            return
        self._git("switch", "main")

    # --- rollback -------------------------------------------------------------------------------------------------
    def _start_rollback(self, journal: Journal, code: str, detail: str, now: datetime) -> None:
        journal.reason, journal.detail = code, detail
        self._log.error("update_rolling_back", update_id=journal.id, code=code)
        self._enter(journal, Step.ROLLING_BACK, now)
        self._rolling_back(journal, now)

    def _rolling_back(self, journal: Journal, now: datetime) -> None:
        for bot in known_bots(journal.bots):
            self._launchctl.bootout(label(bot))
        self.checkpoint("rollback_booted_out")
        previous = self._release_dir(journal, journal.previous)
        if rel.link_target(self._paths.current_release) != journal.previous and previous.is_dir():
            rel.swap_symlink(self._paths.current_release, previous)
        if journal.migration_ran:
            copies = self._paths.pre_update_backup(journal.id)
            for bot in BOTS:
                db = self._paths.db_file(bot)
                if db.name in journal.snapshot:
                    restore_db(copies / db.name, db)
                elif db.exists():  # created by the failed migration: set aside, not deleted
                    db.replace(db.with_name(f"{db.name}.update-{journal.id}"))
        self._clear_flags()
        for bot in known_bots(journal.bots):
            plist = self._paths.plist(bot)
            if not self._launchctl.bootstrap(str(plist)):
                self._launchctl.kickstart(label(bot))
        journal.rolled_back_at = now.isoformat()
        journal.deadline = (now + timedelta(seconds=journal.health_timeout_s)).isoformat()
        self._enter(journal, Step.VERIFYING, now)
        self._status(journal, "switching", now, rolling_back=True)

    def _back_on_previous(self, journal: Journal, bot: Bot, since: datetime) -> bool:
        beat = read_beat(self._paths.heartbeat(bot))
        return (
            beat is not None
            and beat.version == journal.previous
            and beat.mode == MODE_NORMAL
            and beat.last_poll_ok_at is not None
            and beat.last_poll_ok_at >= since
        )

    def _verifying(self, journal: Journal, now: datetime) -> None:
        since = _at(journal.rolled_back_at) or now
        back = all(self._back_on_previous(journal, bot, since) for bot in known_bots(journal.bots))
        deadline = _at(journal.deadline) or now
        if not back and now < deadline:
            return
        reason = S.update_reason(journal.reason)
        if back:
            db = S.ROLLED_BACK_DB() if journal.migration_ran else ""
            text = S.ROLLED_BACK(version=journal.version, reason=reason, previous=journal.previous, db=db)
            self._alerter.send("update_rolled_back", text)
            self._status(journal, "rolled_back", now, final=True, alerted=True, repair_needed=False)
        else:
            text = S.MANUAL_REPAIR(
                version=journal.version, reason=reason, previous=journal.previous, update_id=journal.id
            )
            self._alerter.send("update_manual_repair", text)
            self._status(journal, "rolled_back", now, final=True, alerted=True, repair_needed=True)
        self._save(None)
        self._back_to_main(journal)
        self._log.warning("update_rolled_back", update_id=journal.id, code=journal.reason, healthy=back)

    # --- kind=supervisor ------------------------------------------------------------------------------------------
    def _self_switching(self, journal: Journal, now: datetime) -> None:
        if journal.switched_at:
            return  # waiting for this process to exit
        journal.switched_at = now.isoformat()
        journal.deadline = (now + timedelta(seconds=60)).isoformat()
        self._save(journal)
        self._status(journal, "switching", now, switched_at=journal.switched_at)
        rel.swap_symlink(self._paths.supervisor_current, self._release_dir(journal))
        self.checkpoint("after_self_swap")
        self.exit_requested = True
        self._log.info("supervisor_self_switch", update_id=journal.id, version=journal.version)

    def _confirm_self_switch(self, journal: Journal, now: datetime) -> Journal | None:
        if not journal.switched_at:
            return journal  # killed before the swap: do it now
        link = rel.link_target(self._paths.supervisor_current)
        if self._release == journal.version and link == journal.version:
            self._set_last_good(journal)
            journal.healthy_at = now.isoformat()
            self._status(journal, "healthy", now)
            outcome = self._merge(journal)
            journal.reason = "" if outcome == "merged" else outcome
            self._status(journal, "merged" if outcome == "merged" else "healthy", now, final=True)
            rel.prune(self._paths.supervisor_releases, keep=KEEP_RELEASES, protect={journal.version, journal.previous})
            self._save(None)
            self._log.info("supervisor_self_switch_ok", update_id=journal.id, version=journal.version)
            return None
        if link == journal.version:
            self.exit_requested = True  # an old process still: let launchd start the new one
            return None
        journal.reason = "supervisor_not_started"
        self._alerter.send(
            "supervisor_switched_back",
            S.SUPERVISOR_SWITCHED_BACK(version=journal.version, previous=link or journal.previous),
        )
        self._status(journal, "rolled_back", now, final=True, alerted=True, repair_needed=False)
        self._save(None)
        self._back_to_main(journal)
        return None
