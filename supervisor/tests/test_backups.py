"""Daily backups (SPEC §9.2): sqlite3 backup API, integrity_check on every copy, retention, and a tested restore."""

import os
import shutil
import sqlite3
import stat
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from yordamchi_supervisor.backups import Backups, integrity
from yordamchi_supervisor.state import StateFile
from yordamchi_supervisor.telegram import Alerter
from yordamchi_supervisor.testing import FakeClock, FakeOpener, TempData

# 04:30 in Tashkent (UTC+5).
SATURDAY_0430 = datetime(2026, 10, 23, 23, 30, tzinfo=UTC)  # local 2026-10-24, a Saturday
SUNDAY_0430 = datetime(2026, 10, 24, 23, 30, tzinfo=UTC)  # local 2026-10-25, a Sunday


def make_db(path: Path, rows: int) -> None:
    """A WAL-mode DB like the bots keep, with uncheckpointed rows still in the -wal file."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.executemany("INSERT INTO kv_state VALUES (?, ?)", [(f"k{n}", "v" * 50) for n in range(rows)])
    conn.commit()
    conn.close()


def rows(path: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT key, value FROM kv_state ORDER BY key").fetchall()
    finally:
        conn.close()


class BackupCase(unittest.TestCase):
    def setUp(self) -> None:
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.clock = FakeClock(SATURDAY_0430)
        self.opener = FakeOpener()
        self.alerter = Alerter(
            self.data.paths.env_file("dev"), self.data.log, self.data.scrubber, self.clock, opener=self.opener
        )
        self.state = StateFile(self.data.paths.state_file)
        self.dev_db = self.data.paths.db_file("dev")
        make_db(self.dev_db, 200)
        self.backups = Backups(self.data.paths, self.alerter, self.data.log, self.state)

    @property
    def root(self) -> Path:
        return self.data.paths.backups

    def events(self, name: str) -> list[dict[str, object]]:
        return [event for event in self.data.log_events() if event["event"] == name]


class DailyCopy(BackupCase):
    def test_a_verified_0600_copy_with_every_row(self) -> None:
        self.backups.tick(self.clock.now())
        copy = self.root / "daily" / "2026-10-24" / "dev.db"
        self.assertTrue(copy.exists())
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertEqual(integrity(copy), "ok")
        self.assertEqual(rows(copy), rows(self.dev_db))
        leftovers = sorted(p.name for p in copy.parent.iterdir())
        self.assertEqual(leftovers, ["dev.db"])  # no .tmp, no -wal, no -shm
        self.assertEqual(len(self.events("backup_done")), 1)
        self.assertEqual(self.opener.sent, [])

    def test_the_personal_db_is_copied_once_it_exists(self) -> None:
        make_db(self.data.paths.db_file("personal"), 10)
        self.backups.tick(self.clock.now())
        names = sorted(p.name for p in (self.root / "daily" / "2026-10-24").iterdir())
        self.assertEqual(names, ["dev.db", "personal.db"])

    def test_the_source_is_not_changed(self) -> None:
        before = rows(self.dev_db)
        self.backups.tick(self.clock.now())
        self.assertEqual(rows(self.dev_db), before)
        self.assertEqual(integrity(self.dev_db), "ok")

    def test_not_before_04_00_local(self) -> None:
        self.backups.tick(SATURDAY_0430 - timedelta(minutes=31))  # 03:59 in Tashkent
        self.assertFalse((self.root / "daily").exists())

    def test_once_per_day_and_again_the_next_day(self) -> None:
        for minutes in (0, 1, 60, 600):
            self.backups.tick(SATURDAY_0430 + timedelta(minutes=minutes))
        self.assertEqual(len(self.events("backup_done")), 1)
        self.backups.tick(SUNDAY_0430)
        self.assertEqual(len(self.events("backup_done")), 2)

    def test_a_mac_that_slept_through_04_00_backs_up_on_waking(self) -> None:
        self.backups.tick(SATURDAY_0430 + timedelta(hours=7))  # 11:30 local, first tick after wake
        self.assertTrue((self.root / "daily" / "2026-10-24" / "dev.db").exists())

    def test_the_day_survives_a_supervisor_restart(self) -> None:
        self.backups.tick(self.clock.now())
        again = Backups(self.data.paths, self.alerter, self.data.log, StateFile(self.data.paths.state_file))
        again.tick(self.clock.now() + timedelta(minutes=5))
        self.assertEqual(len(self.events("backup_done")), 1)

    def test_damaged_saved_times_do_not_stop_the_backup(self) -> None:
        self.state.save_section("backups", {"last_attempt_at": "not a time", "last_ok_date": 7})
        self.backups.tick(self.clock.now())
        self.assertEqual(len(self.events("backup_done")), 1)


class Weekly(BackupCase):
    def test_sunday_also_writes_a_verified_weekly_copy(self) -> None:
        self.backups.tick(SUNDAY_0430)
        weekly = self.root / "weekly" / "2026-10-25" / "dev.db"
        self.assertEqual(integrity(weekly), "ok")
        self.assertEqual(rows(weekly), rows(self.dev_db))

    def test_other_days_do_not(self) -> None:
        self.backups.tick(SATURDAY_0430)
        self.assertFalse((self.root / "weekly").exists())


class Retention(BackupCase):
    def seed(self, kind: str, days: int, end: datetime) -> None:
        for n in range(1, days + 1):
            (self.root / kind / (end - timedelta(days=n)).date().isoformat()).mkdir(parents=True)

    def test_daily_keeps_7_and_weekly_keeps_4(self) -> None:
        self.seed("daily", 10, datetime(2026, 10, 25, tzinfo=UTC))
        for weeks in range(1, 7):
            (
                self.root / "weekly" / (datetime(2026, 10, 25, tzinfo=UTC) - timedelta(weeks=weeks)).date().isoformat()
            ).mkdir(parents=True)
        (self.root / "daily" / "keep-me").mkdir()  # not ours to delete
        self.backups.tick(SUNDAY_0430)
        daily = sorted(p.name for p in (self.root / "daily").iterdir())
        self.assertEqual(daily, [f"2026-10-{d}" for d in range(19, 26)] + ["keep-me"])
        weekly = sorted(p.name for p in (self.root / "weekly").iterdir())
        self.assertEqual(weekly, ["2026-10-04", "2026-10-11", "2026-10-18", "2026-10-25"])

    def test_pre_update_keeps_the_5_newest(self) -> None:
        base = SATURDAY_0430.timestamp()
        for n in range(7):
            directory = self.root / "pre-update" / f"u{n}"
            directory.mkdir(parents=True)
            os.utime(directory, (base - 3600 * (7 - n), base - 3600 * (7 - n)))
        self.backups.tick(SATURDAY_0430)
        kept = sorted(p.name for p in (self.root / "pre-update").iterdir())
        self.assertEqual(kept, ["u2", "u3", "u4", "u5", "u6"])

    def test_pre_update_is_not_pruned_while_an_update_is_in_progress(self) -> None:
        for n in range(7):
            (self.root / "pre-update" / f"u{n}").mkdir(parents=True)
        update = self.data.paths.updates / "u6"
        update.mkdir(parents=True)
        (update / "request.json").write_text("{}", encoding="utf-8")
        (update / "status.json").write_text('{"status": "probation"}', encoding="utf-8")
        for name in ("request.json", "status.json"):
            os.utime(update / name, (SATURDAY_0430.timestamp(),) * 2)
        self.backups.tick(SATURDAY_0430)
        self.assertEqual(len(list((self.root / "pre-update").iterdir())), 7)


class Failures(BackupCase):
    def test_a_copy_that_fails_integrity_check_alerts_and_keeps_old_copies(self) -> None:
        self.seed_old_days(9)
        backups = Backups(
            self.data.paths,
            self.alerter,
            self.data.log,
            self.state,
            check=lambda path: "*** in database main ***\nPage 3: btreeInitPage() returns error code 11",
        )
        backups.tick(SATURDAY_0430)
        day = self.root / "daily" / "2026-10-24"
        self.assertEqual(sorted(p.name for p in day.iterdir()), ["dev.db.failed"])
        self.assertEqual(len(self.opener.sent), 1)
        self.assertIn("dev.db", self.opener.texts()[0])
        self.assertIn("integrity_check", self.opener.texts()[0])
        self.assertEqual(len(list((self.root / "daily").iterdir())), 10)  # nothing pruned on a bad day
        # Retried after an hour, without a second alert the same day.
        backups.tick(SATURDAY_0430 + timedelta(minutes=30))
        self.assertEqual(len(self.events("backup_failed")), 1)
        backups.tick(SATURDAY_0430 + timedelta(minutes=61))
        self.assertEqual(len(self.events("backup_failed")), 2)
        self.assertEqual(len(self.opener.sent), 1)

    def test_a_retry_that_succeeds_replaces_the_failed_round(self) -> None:
        verdicts = iter(["corrupt", "ok"])
        backups = Backups(self.data.paths, self.alerter, self.data.log, self.state, check=lambda path: next(verdicts))
        backups.tick(SATURDAY_0430)
        backups.tick(SATURDAY_0430 + timedelta(hours=1))
        self.assertTrue((self.root / "daily" / "2026-10-24" / "dev.db").exists())
        self.assertEqual(len(self.events("backup_done")), 1)

    def test_integrity_check_detects_a_damaged_file(self) -> None:
        damaged = self.data.paths.data / "damaged.db"
        conn = sqlite3.connect(damaged)
        conn.execute("PRAGMA page_size=4096")
        conn.execute("CREATE TABLE kv_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany("INSERT INTO kv_state VALUES (?, ?)", [(f"k{n}", "v" * 50) for n in range(500)])
        conn.commit()
        conn.close()
        self.assertEqual(integrity(damaged), "ok")
        with damaged.open("r+b") as handle:
            handle.seek(4096)  # the b-tree header of page 2
            handle.write(b"\x07" * 64)
        self.assertNotEqual(integrity(damaged), "ok")
        self.assertEqual(integrity(self.data.paths.data / "absent.db")[:6], "error:")

    def test_a_source_that_cannot_be_read_alerts_instead_of_crashing(self) -> None:
        for suffix in ("-wal", "-shm"):
            Path(f"{self.dev_db}{suffix}").unlink(missing_ok=True)
        self.dev_db.write_bytes(b"this is not an sqlite database" * 200)
        self.assertFalse(self.backups.run(SATURDAY_0430))
        self.assertEqual(len(self.opener.sent), 1)
        self.assertIn("DatabaseError", self.opener.texts()[0])
        self.assertEqual(list((self.root / "daily" / "2026-10-24").iterdir()), [])

    def test_low_disk_skips_the_backup_and_alerts(self) -> None:
        backups = Backups(self.data.paths, self.alerter, self.data.log, self.state, free_bytes=lambda path: 10 * 2**20)
        backups.tick(SATURDAY_0430)
        self.assertFalse((self.root / "daily").exists())
        self.assertEqual(len(self.opener.sent), 1)
        self.assertIn("bo'sh joy", self.opener.texts()[0])

    def seed_old_days(self, days: int) -> None:
        for n in range(1, days + 1):
            (self.root / "daily" / (datetime(2026, 10, 24, tzinfo=UTC) - timedelta(days=n)).date().isoformat()).mkdir(
                parents=True
            )


class Restore(BackupCase):
    def test_the_documented_restore_brings_back_every_row(self) -> None:
        """The DEPLOY.md restore: stop the bot, copy the backup over db/<name>.db, drop -wal/-shm, start the bot."""
        self.backups.tick(SATURDAY_0430)
        expected = rows(self.dev_db)
        conn = sqlite3.connect(self.dev_db)  # data loss after the backup
        conn.execute("DELETE FROM kv_state")
        conn.commit()
        conn.close()
        for suffix in ("-wal", "-shm"):
            Path(f"{self.dev_db}{suffix}").unlink(missing_ok=True)
        shutil.copyfile(self.root / "daily" / "2026-10-24" / "dev.db", self.dev_db)
        self.assertEqual(integrity(self.dev_db), "ok")
        self.assertEqual(rows(self.dev_db), expected)


if __name__ == "__main__":
    unittest.main()
