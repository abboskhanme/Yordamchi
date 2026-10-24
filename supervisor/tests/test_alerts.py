"""Telegram alerts through urllib (SPEC §2.2): request shape, retry after a network drop, the token never logged, and
the token only ever from the Keychain item `dev.env` names (owner requirement 2026-10-24)."""

import io
import sys
import unittest
import urllib.error
from collections.abc import Sequence
from dataclasses import dataclass, field

from yordamchi_supervisor.keychain import DEV_TOKEN_REFERENCE, KeychainError, SecurityKeychain, dev_token_argv
from yordamchi_supervisor.log import MASK, JsonLog, Scrubber, install_excepthook
from yordamchi_supervisor.proc import RunResult
from yordamchi_supervisor.telegram import AlertConfigError, Alerter, parse_env_file, read_alert_target
from yordamchi_supervisor.testing import GROUP_ID, START, TOKEN, FakeClock, FakeOpener, TempData, http_error

NEW_TOKEN = "8123456789:AARotatedTokenForSupervisorTests_xyz12"  # noqa: S105 - a fake


class AlertCase(unittest.TestCase):
    def setUp(self) -> None:
        self.data = TempData()
        self.addCleanup(self.data.cleanup)
        self.clock = FakeClock(START)
        self.opener = FakeOpener()
        self.alerter = Alerter(
            self.data.paths.env_file("dev"),
            self.data.log,
            self.data.scrubber,
            self.clock,
            keychain=self.data.keychain,
            opener=self.opener,
        )

    def assert_token_absent(self) -> None:
        text = self.data.log_text()
        self.assertTrue(text, "the log should not be empty")
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(TOKEN.split(":", 1)[1], text)  # not even the secret half
        self.assertNotIn("api.telegram.org", text)


class RequestShape(AlertCase):
    def test_a_plain_text_post_to_the_dev_group(self) -> None:
        self.alerter.send("bot_restarted", "Dev botni qayta ishga tushirdim.")
        self.assertEqual(len(self.opener.sent), 1)
        sent = self.opener.sent[0]
        self.assertEqual(sent.url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(
            sent.body, {"chat_id": GROUP_ID, "text": "[Supervisor 15:00] Dev botni qayta ishga tushirdim."}
        )
        self.assertNotIn("reply_markup", sent.body)
        self.assertNotIn("parse_mode", sent.body)
        self.assertLessEqual(sent.timeout, 30)

    def test_the_text_of_an_alert_is_never_logged(self) -> None:
        self.alerter.send("crash_loop", "secret-ish owner content 12345")
        self.assertNotIn("owner content", self.data.log_text())
        self.assertIn('"kind": "crash_loop"', self.data.log_text())

    def test_a_long_text_is_cut_to_the_telegram_limit(self) -> None:
        self.alerter.send("crash_loop", "x" * 9000)
        self.assertLessEqual(len(str(self.opener.sent[0].body["text"])), 4096)


class Delivery(AlertCase):
    def test_a_network_drop_keeps_the_alert_for_the_next_tick(self) -> None:
        self.opener.fail = lambda request: urllib.error.URLError("nodename nor servname provided")
        self.alerter.send("bot_restarted", "a")
        self.alerter.send("crash_loop", "b")
        self.assertEqual(self.alerter.pending, 2)
        self.clock.advance(minutes=3)
        self.opener.fail = None
        self.alerter.flush()
        self.assertEqual(self.alerter.pending, 0)
        # In order, and stamped with the time each one happened, not when it was finally delivered.
        self.assertEqual(self.opener.texts(), ["[Supervisor 15:00] a", "[Supervisor 15:00] b"])

    def test_429_and_5xx_are_retried(self) -> None:
        for status in (429, 502):
            self.opener.fail = lambda request, status=status: http_error(request, status)
            self.alerter.send("bot_restarted", "a")
            self.assertEqual(self.alerter.pending, 1)
            self.opener.fail = None
            self.alerter.flush()
            self.assertEqual(self.alerter.pending, 0)

    def test_a_definite_refusal_is_dropped_and_logged(self) -> None:
        self.opener.fail = lambda request: http_error(request, 403)
        self.alerter.send("bot_restarted", "a")
        self.assertEqual(self.alerter.pending, 0)
        self.assertIn('"status": 403', self.data.log_text())

    def test_the_queue_is_bounded(self) -> None:
        self.opener.fail = lambda request: TimeoutError()
        for n in range(30):
            self.alerter.send("bot_restarted", str(n))
        self.assertEqual(self.alerter.pending, 20)
        self.assertIn("alert_dropped_queue_full", self.data.log_text())


class EnvFile(AlertCase):
    def test_an_env_file_others_can_read_is_refused(self) -> None:
        self.data.write_env(f"DEV_BOT_TOKEN={DEV_TOKEN_REFERENCE}\nDEV_GROUP_ID={GROUP_ID}\n", mode=0o644)
        self.alerter.send("bot_restarted", "a")
        self.assertEqual(self.opener.sent, [])
        self.assertIn("env_mode_unsafe", self.data.log_text())
        self.assertEqual(self.alerter.pending, 1)  # kept until the file is fixed
        self.data.paths.env_file("dev").chmod(0o600)
        self.alerter.flush()
        self.assertEqual(len(self.opener.sent), 1)

    def test_a_missing_or_incomplete_env_file_sends_nothing(self) -> None:
        self.data.write_env("DEV_BOT_TOKEN=<token-from-BotFather>\nDEV_GROUP_ID=<dev-group-id-negative>\n")
        self.alerter.send("bot_restarted", "a")
        self.assertIn("env_incomplete", self.data.log_text())
        self.data.paths.env_file("dev").unlink()
        self.alerter.flush()
        self.assertIn("env_missing", self.data.log_text())
        self.assertEqual(self.opener.sent, [])

    def test_the_env_format_matches_the_app(self) -> None:
        values = parse_env_file("# comment\nexport DEV_BOT_TOKEN='abc'\n\nDEV_GROUP_ID = \"-100\"\nbroken line\n")
        self.assertEqual(values, {"DEV_BOT_TOKEN": "abc", "DEV_GROUP_ID": "-100"})

    def test_the_target_repr_hides_the_token(self) -> None:
        target = read_alert_target(self.data.paths.env_file("dev"), self.data.keychain)
        self.assertEqual(target.token, TOKEN)
        self.assertNotIn(TOKEN, repr(target))


class KeychainToken(AlertCase):
    def test_a_plaintext_token_in_dev_env_is_refused_and_never_used(self) -> None:
        self.data.write_env(f"DEV_BOT_TOKEN={TOKEN}\nDEV_GROUP_ID={GROUP_ID}\n")
        self.alerter.send("bot_restarted", "a")
        self.assertEqual(self.opener.sent, [])
        self.assertIn("env_token_plaintext", self.data.log_text())
        self.assertEqual(self.data.keychain.reads, 0)
        self.assertEqual(self.alerter.pending, 1)  # kept until the installer fixes the file
        self.assert_token_absent()

    def test_a_reference_to_any_other_item_is_refused(self) -> None:
        for reference in ("@keychain:uz.yordamchi/personal-bot-token", "@keychain:other.service/dev-bot-token"):
            self.data.write_env(f"DEV_BOT_TOKEN={reference}\nDEV_GROUP_ID={GROUP_ID}\n")
            with self.assertRaises(AlertConfigError) as caught:
                read_alert_target(self.data.paths.env_file("dev"), self.data.keychain)
            self.assertEqual(caught.exception.problem, "env_token_reference_refused")
        self.assertEqual(self.data.keychain.reads, 0)

    def test_a_missing_item_or_a_locked_keychain_keeps_the_alert(self) -> None:
        self.data.keychain.token = None
        self.alerter.send("bot_restarted", "a")
        self.assertIn("keychain_item_missing", self.data.log_text())
        self.data.keychain.token = TOKEN
        self.data.keychain.problem = "keychain_unavailable"
        self.alerter.flush()
        self.assertIn("keychain_unavailable", self.data.log_text())
        self.assertEqual(self.opener.sent, [])
        self.data.keychain.problem = None
        self.alerter.flush()
        self.assertEqual(len(self.opener.sent), 1)

    def test_the_token_is_read_at_send_time_so_a_rotation_needs_no_restart(self) -> None:
        self.alerter.send("bot_restarted", "a")
        self.data.keychain.token = NEW_TOKEN
        self.alerter.send("bot_restarted", "b")
        self.assertEqual(
            [sent.url for sent in self.opener.sent],
            [
                f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                f"https://api.telegram.org/bot{NEW_TOKEN}/sendMessage",
            ],
        )
        self.assertNotIn(NEW_TOKEN, self.data.log_text())


@dataclass
class RecordingRunner:
    answer: RunResult
    calls: list[list[str]] = field(default_factory=list)
    timeouts: list[float] = field(default_factory=list)

    def run(self, argv: Sequence[str], *, timeout: float) -> RunResult:
        self.calls.append(list(argv))
        self.timeouts.append(timeout)
        return self.answer


class SecurityCommand(unittest.TestCase):
    def test_it_asks_security_for_exactly_the_dev_token_item_and_never_puts_the_token_on_argv(self) -> None:
        runner = RecordingRunner(RunResult(returncode=0, stdout=f"{TOKEN}\n"))
        self.assertEqual(SecurityKeychain(runner).dev_token(), TOKEN)
        self.assertEqual(
            runner.calls,
            [["/usr/bin/security", "find-generic-password", "-s", "uz.yordamchi", "-a", "dev-bot-token", "-w"]],
        )
        self.assertEqual(runner.calls, [dev_token_argv()])
        self.assertLessEqual(max(runner.timeouts), 30)

    def test_exit_codes_become_problem_codes(self) -> None:
        for answer, problem in (
            (RunResult(returncode=44), "keychain_item_missing"),
            (RunResult(returncode=0, stdout="\n"), "keychain_item_missing"),
            (RunResult(returncode=51), "keychain_unavailable"),
            (RunResult(returncode=None, stderr="timeout"), "keychain_unavailable"),
        ):
            with self.assertRaises(KeychainError) as caught:
                SecurityKeychain(RecordingRunner(answer)).dev_token()
            self.assertEqual(caught.exception.problem, problem)


class TokenNeverLogged(AlertCase):
    def test_not_when_telegram_refuses_the_token(self) -> None:
        self.opener.fail = lambda request: http_error(request, 401)  # the HTTPError carries the URL with the token
        self.alerter.send("bot_restarted", "a")
        self.assert_token_absent()

    def test_not_when_the_network_error_quotes_the_url(self) -> None:
        self.opener.fail = lambda request: urllib.error.URLError(f"cannot reach {request.full_url}")
        self.alerter.send("bot_restarted", "a")
        self.opener.fail = lambda request: ValueError(f"bad url {request.full_url}")
        self.alerter.flush()
        self.assert_token_absent()

    def test_not_when_an_alert_text_contains_it(self) -> None:
        self.alerter.send("crash_loop", f"Traceback: https://api.telegram.org/bot{TOKEN}/getUpdates")
        self.assertNotIn(TOKEN, self.opener.texts()[0])
        self.assert_token_absent()

    def test_the_scrubber_masks_the_pattern_and_known_values(self) -> None:
        scrubber = Scrubber()
        scrubber.add("short")  # too short to mask by value
        scrubber.add("a-very-specific-vault-value")
        line = scrubber(f"url=/bot{TOKEN}/x other=a-very-specific-vault-value short")
        self.assertEqual(line, f"url=/bot{MASK}/x other={MASK} short")

    def test_log_fields_are_scrubbed(self) -> None:
        stream = io.StringIO()
        log = JsonLog(None, Scrubber(), stream=stream, clock=self.clock.now)
        log.error("job_failed", detail=f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        log.close()
        self.assertNotIn(TOKEN, stream.getvalue())
        self.assertIn(MASK, stream.getvalue())

    def test_uncaught_tracebacks_are_scrubbed(self) -> None:
        stream = io.StringIO()
        previous = sys.excepthook
        self.addCleanup(setattr, sys, "excepthook", previous)
        install_excepthook(Scrubber(), stream)
        try:
            raise RuntimeError(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage failed")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        self.assertIn("RuntimeError", stream.getvalue())
        self.assertNotIn(TOKEN, stream.getvalue())


if __name__ == "__main__":
    unittest.main()
