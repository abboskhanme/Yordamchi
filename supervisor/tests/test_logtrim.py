"""launchd log copy-truncate (SPEC §9.2, NFR-CAP-05)."""

import stat
import unittest

from yordamchi_supervisor.logtrim import KEEP_TAIL_BYTES, MAX_BYTES, trim_file, trim_launchd_logs
from yordamchi_supervisor.paths import PROCESSES
from yordamchi_supervisor.testing import TempData


def fill(path_bytes: int) -> bytes:
    line = b"2026-10-24 10:00:00 some launchd stderr line from the bot process ............\n"
    return line * (path_bytes // len(line) + 1)


class LaunchdLogTrim(unittest.TestCase):
    def setUp(self) -> None:
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.log_path = self.data.paths.launchd_log("dev")

    def test_a_log_over_5_mb_is_copy_truncated(self) -> None:
        body = fill(MAX_BYTES + 1000) + b"the very last line\n"
        self.log_path.write_bytes(body)
        trim_launchd_logs(self.data.paths, self.data.log)
        self.assertEqual(self.log_path.stat().st_size, 0)
        copy = self.log_path.with_name(self.log_path.name + ".1")
        tail = copy.read_bytes()
        self.assertLessEqual(len(tail), KEEP_TAIL_BYTES)
        self.assertGreater(len(tail), KEEP_TAIL_BYTES - 200)
        self.assertTrue(tail.endswith(b"the very last line\n"))
        self.assertTrue(tail.startswith(b"2026-10-24 10:00:00"))  # starts on a line boundary
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        self.assertIn("launchd_log_trimmed", self.data.log_text())

    def test_a_small_log_is_left_alone(self) -> None:
        self.log_path.write_bytes(b"short\n")
        self.assertIsNone(trim_file(self.log_path))
        self.assertEqual(self.log_path.read_bytes(), b"short\n")
        self.assertFalse(self.log_path.with_name(self.log_path.name + ".1").exists())

    def test_missing_logs_are_fine(self) -> None:
        trim_launchd_logs(self.data.paths, self.data.log)
        self.assertNotIn("launchd_log_trim_failed", self.data.log_text())

    def test_an_append_mode_writer_continues_at_the_start(self) -> None:
        # launchd holds the file open with O_APPEND, so writes after the truncate do not leave a sparse hole.
        with self.log_path.open("ab") as writer:
            writer.write(fill(MAX_BYTES + 10))
            writer.flush()
            trim_file(self.log_path)
            writer.write(b"after trim\n")
            writer.flush()
        self.assertEqual(self.log_path.read_bytes(), b"after trim\n")

    def test_every_process_log_is_covered(self) -> None:
        for process in PROCESSES:
            self.data.paths.launchd_log(process).write_bytes(fill(MAX_BYTES + 10))
        trim_launchd_logs(self.data.paths, self.data.log)
        for process in PROCESSES:
            self.assertEqual(self.data.paths.launchd_log(process).stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
