"""Daily DB backups (SPEC §9.2): the stdlib `sqlite3` backup API, `PRAGMA integrity_check` on every copy, retention.

- Due once per local (Asia/Tashkent) day, from 04:00. A Mac that slept through 04:00 backs up when it wakes.
- `db/dev.db` and `db/personal.db` (whichever exist) → `backups/daily/<YYYY-MM-DD>/<name>.db`. On Sunday the verified
  daily copies are also copied to `backups/weekly/<date>/` and checked again.
- Each copy is written as `<name>.db.tmp` and renamed only after `integrity_check` returns `ok`. A copy that fails
  is kept as `<name>.db.failed` for diagnosis, the owner is alerted (once per day), and the next attempt is an hour
  later. Retention runs only after a fully good day, so a bad day never deletes the last good copies.
- Retention: daily keep 7, weekly keep 4 (dated dirs only), `pre-update` keep 5 (newest by mtime; never while an
  update is in progress, because the updater may need that snapshot to roll back). Nothing that does not match
  those names is ever deleted.
- The source is opened read-only (`mode=ro`); the bots stay the only writers (SPEC §2.3).

Restore (documented for T28's `docs/DEPLOY.md`): stop the bot (`launchctl bootout`), copy the `.db` over
`db/<name>.db`, remove its `-wal` and `-shm`, then `launchctl bootstrap` it. `restore_check` in the tests does this on
a scratch copy.
"""

import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Final

from yordamchi_supervisor import strings as S
from yordamchi_supervisor.clock import TASHKENT
from yordamchi_supervisor.log import JsonLog
from yordamchi_supervisor.paths import BOTS, SupervisorPaths
from yordamchi_supervisor.state import StateFile, parse_instant
from yordamchi_supervisor.telegram import Alerter
from yordamchi_supervisor.updates import scan

RUN_AT_HOUR: Final = 4
RETRY_AFTER: Final = timedelta(hours=1)
KEEP_DAILY: Final = 7
KEEP_WEEKLY: Final = 4
KEEP_PRE_UPDATE: Final = 5
SUNDAY: Final = 6
BUSY_TIMEOUT_S: Final = 30.0
DISK_MARGIN_BYTES: Final = 512 * 1024 * 1024
STATE_SECTION: Final = "backups"
_DATED: Final = re.compile(r"\d{4}-\d{2}-\d{2}")

type Check = Callable[[Path], str]
type FreeBytes = Callable[[Path], int]


@dataclass(frozen=True, slots=True)
class CopyResult:
    name: str  # `dev.db`
    where: str  # `daily` or `weekly`
    ok: bool
    problem: str = ""
    size: int = 0


def _ro_uri(path: Path) -> str:
    return path.absolute().as_uri() + "?mode=ro"


def integrity(path: Path) -> str:
    """`ok`, or the first problem `PRAGMA integrity_check` reports (or the error class when it cannot even open)."""
    try:
        conn = sqlite3.connect(_ro_uri(path), uri=True, timeout=BUSY_TIMEOUT_S)
        try:
            rows = conn.execute("PRAGMA integrity_check").fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"error:{type(exc).__name__}"
    if rows == [("ok",)]:
        return "ok"
    return str(rows[0][0])[:200] if rows else "no_result"


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _finish(tmp: Path, final: Path, check: Check) -> CopyResult:
    tmp.chmod(0o600)
    verdict = check(tmp)
    if verdict != "ok":
        tmp.replace(final.with_name(final.name + ".failed"))
        return CopyResult(
            name=final.name, where=final.parent.parent.name, ok=False, problem=f"integrity_check: {verdict}"
        )
    tmp.replace(final)
    return CopyResult(name=final.name, where=final.parent.parent.name, ok=True, size=final.stat().st_size)


def backup_db(source: Path, final: Path, check: Check = integrity) -> CopyResult:
    """One consistent copy through the backup API, in rollback-journal mode so no `-wal` file is left beside it."""
    tmp = final.with_name(final.name + ".tmp")
    tmp.unlink(missing_ok=True)
    try:
        src = sqlite3.connect(_ro_uri(source), uri=True, timeout=BUSY_TIMEOUT_S)
        try:
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                dst.execute("PRAGMA journal_mode=DELETE")
            finally:
                dst.close()
        finally:
            src.close()
    except (sqlite3.Error, OSError) as exc:
        tmp.unlink(missing_ok=True)
        return CopyResult(name=final.name, where=final.parent.parent.name, ok=False, problem=type(exc).__name__)
    return _finish(tmp, final, check)


def copy_verified(source: Path, final: Path, check: Check = integrity) -> CopyResult:
    tmp = final.with_name(final.name + ".tmp")
    try:
        shutil.copyfile(source, tmp)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return CopyResult(name=final.name, where=final.parent.parent.name, ok=False, problem=type(exc).__name__)
    return _finish(tmp, final, check)


def prune_dated(directory: Path, keep: int) -> list[str]:
    if not directory.is_dir():
        return []
    dated = sorted((p for p in directory.iterdir() if p.is_dir() and _DATED.fullmatch(p.name)), reverse=True)
    removed = []
    for old in dated[keep:]:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def prune_by_mtime(directory: Path, keep: int, protect: str | None) -> list[str]:
    if not directory.is_dir():
        return []
    entries = sorted((p for p in directory.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = []
    for old in entries[keep:]:
        if old.name == protect:
            continue
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


class Backups:
    def __init__(
        self,
        paths: SupervisorPaths,
        alerter: Alerter,
        log: JsonLog,
        state: StateFile,
        *,
        check: Check = integrity,
        free_bytes: FreeBytes = _free_bytes,
    ) -> None:
        self._paths = paths
        self._check = check
        self._free_bytes = free_bytes
        self._alerter = alerter
        self._log = log
        self._state = state

    def tick(self, now: datetime) -> None:
        local = now.astimezone(TASHKENT)
        saved = self._state.section(STATE_SECTION)
        today = local.date().isoformat()
        if local.hour < RUN_AT_HOUR or saved.get("last_ok_date") == today:
            return
        last_attempt = parse_instant(saved.get("last_attempt_at"))
        # A clock set backwards must not postpone the backup until it catches up.
        if last_attempt is not None and timedelta(0) <= now - last_attempt < RETRY_AFTER:
            return
        self.run(now)

    def run(self, now: datetime) -> bool:
        """One backup round for the local date of `now`. True when every existing DB was copied and verified."""
        day = now.astimezone(TASHKENT).date()
        saved = self._state.section(STATE_SECTION)
        saved["last_attempt_at"] = now.isoformat()
        self._state.save_section(STATE_SECTION, saved)

        sources = [path for path in (self._paths.db_file(bot) for bot in BOTS) if path.exists()]
        if not sources:
            self._log.warning("backup_skipped", reason="no_databases")
            return False
        need = sum(path.stat().st_size for path in sources) * 2 + DISK_MARGIN_BYTES
        self._paths.backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        free = self._free_bytes(self._paths.backups)
        if free < need:
            self._log.error("backup_skipped", reason="disk_low", free_mb=free // 2**20, need_mb=need // 2**20)
            self._alert_once(day, "backup_disk_low", S.BACKUP_DISK_LOW(free_mb=free // 2**20, need_mb=need // 2**20))
            return False

        results = self._copy_all(sources, day)
        failed = [result for result in results if not result.ok]
        for result in failed:
            self._log.error("backup_failed", file=f"{result.where}/{result.name}", problem=result.problem)
        if failed:
            first = failed[0]
            text = S.BACKUP_FAILED(db=first.name, date=day.isoformat(), problem=first.problem)
            self._alert_once(day, "backup_failed", text)
            return False

        update = scan(self._paths.updates, now).active
        removed = {
            "daily": prune_dated(self._paths.backups / "daily", KEEP_DAILY),
            "weekly": prune_dated(self._paths.backups / "weekly", KEEP_WEEKLY),
            "pre_update": [] if update else prune_by_mtime(self._paths.backups / "pre-update", KEEP_PRE_UPDATE, None),
        }
        saved = self._state.section(STATE_SECTION)
        saved["last_ok_date"] = day.isoformat()
        self._state.save_section(STATE_SECTION, saved)
        self._log.info(
            "backup_done",
            date=day.isoformat(),
            files={f"{result.where}/{result.name}": result.size for result in results},
            pruned=removed,
        )
        return True

    def _copy_all(self, sources: list[Path], day: date) -> list[CopyResult]:
        daily = self._paths.backups / "daily" / day.isoformat()
        daily.mkdir(parents=True, exist_ok=True, mode=0o700)
        results = [backup_db(source, daily / source.name, self._check) for source in sources]
        if day.weekday() == SUNDAY and all(result.ok for result in results):
            weekly = self._paths.backups / "weekly" / day.isoformat()
            weekly.mkdir(parents=True, exist_ok=True, mode=0o700)
            results += [copy_verified(daily / source.name, weekly / source.name, self._check) for source in sources]
        return results

    def _alert_once(self, day: date, kind: str, text: str) -> None:
        saved = self._state.section(STATE_SECTION)
        if saved.get("alerted_date") == day.isoformat():
            return
        saved["alerted_date"] = day.isoformat()
        self._state.save_section(STATE_SECTION, saved)
        self._alerter.send(kind, text)
