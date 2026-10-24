"""`caffeinate -s -w <pid>` kept alive by the supervisor (SPEC §9.2)."""

import unittest
from datetime import timedelta

from yordamchi_supervisor.caffeinate import Caffeinate
from yordamchi_supervisor.testing import START, FakeSpawner, TempData


class CaffeinateKeeper(unittest.TestCase):
    def setUp(self) -> None:
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.spawner = FakeSpawner()
        self.keeper = Caffeinate(self.spawner, self.data.log, owner_pid=777)

    def test_it_starts_bound_to_the_supervisor_pid(self) -> None:
        self.keeper.ensure(START)
        self.assertEqual(self.spawner.spawned, [["/usr/bin/caffeinate", "-s", "-w", "777"]])

    def test_a_running_child_is_left_alone(self) -> None:
        for n in range(5):
            self.keeper.ensure(START + timedelta(seconds=15 * n))
        self.assertEqual(len(self.spawner.spawned), 1)

    def test_a_child_that_exited_is_restarted(self) -> None:
        self.keeper.ensure(START)
        self.spawner.children[0].returncode = 1
        self.keeper.ensure(START + timedelta(seconds=15))
        self.assertEqual(len(self.spawner.spawned), 2)
        self.assertEqual(self.keeper.restarts, 1)
        self.assertIs(self.keeper.child, self.spawner.children[1])
        self.assertIn("caffeinate_exited", self.data.log_text())

    def test_a_spawn_failure_is_retried_but_logged_once_per_hour(self) -> None:
        self.spawner.fail = True
        for n in range(10):
            self.keeper.ensure(START + timedelta(seconds=15 * n))
        self.assertEqual(self.data.log_text().count("caffeinate_spawn_failed"), 1)
        self.keeper.ensure(START + timedelta(hours=1, seconds=1))
        self.assertEqual(self.data.log_text().count("caffeinate_spawn_failed"), 2)
        self.spawner.fail = False
        self.keeper.ensure(START + timedelta(hours=1, seconds=16))
        self.assertIsNotNone(self.keeper.child)

    def test_stop_terminates_the_child(self) -> None:
        self.keeper.ensure(START)
        child = self.spawner.children[0]
        self.keeper.stop()
        self.assertTrue(child.terminated)
        self.assertIsNone(self.keeper.child)


if __name__ == "__main__":
    unittest.main()
