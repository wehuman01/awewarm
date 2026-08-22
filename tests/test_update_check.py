import json
import os
import unittest
from unittest import mock

from helpers import IsolatedTestCase

from awewarm import update_check
from awewarm.update_check import _should_skip, check_async, version_gte


class VersionCompareTests(unittest.TestCase):
    def test_version_gte_orders_releases(self):
        self.assertTrue(version_gte("0.2.0", "0.1.5"))
        self.assertFalse(version_gte("0.1.5", "0.2.0"))
        self.assertTrue(version_gte("0.1.5", "0.1.5"))

    def test_version_gte_ignores_pre_release_suffixes(self):
        # "0.2.0a1" parses as (0, 2, 0), like aweswitch's checker.
        self.assertTrue(version_gte("0.2.0a1", "0.1.9"))
        self.assertFalse(version_gte("0.2.0a1", "0.2.1"))


class ShouldSkipTests(unittest.TestCase):
    def test_run_tick_is_skipped(self):
        # launchd invokes `awewarm run` every minute; it must never check PyPI.
        self.assertTrue(_should_skip(["run"]))
        self.assertTrue(_should_skip(["run", "--dry-run"]))

    def test_help_version_and_update_are_skipped(self):
        for args in (["-h"], ["--help"], ["-v"], ["--version"], ["update"], ["update", "--check"], ["self-update"], ["self-update", "--check"]):
            self.assertTrue(_should_skip(args), args)

    def test_interactive_commands_are_checked(self):
        self.assertFalse(_should_skip(["status"]))
        self.assertFalse(_should_skip(["install"]))
        self.assertFalse(_should_skip([]))


class ReminderTests(IsolatedTestCase):
    @mock.patch.object(update_check, "get_pypi_latest", return_value="9.9.9")
    def test_check_returns_reminder_for_newer_release(self, _pypi):
        reminder = update_check._check()
        self.assertIn("9.9.9", reminder)
        self.assertIn("awewarm self-update", reminder)

    @mock.patch.object(update_check, "get_pypi_latest", return_value="9.9.9")
    def test_reminder_at_most_once_per_day(self, _pypi):
        self.assertIn("9.9.9", update_check._check())
        self.assertIsNone(update_check._check())

    @mock.patch.object(update_check, "get_pypi_latest", return_value="0.0.1")
    def test_no_reminder_when_current_is_newest(self, _pypi):
        self.assertIsNone(update_check._check())

    @mock.patch.object(update_check, "get_pypi_latest", side_effect=OSError("offline"))
    def test_network_failure_backs_off_without_reminder(self, pypi):
        self.assertIsNone(update_check._check())
        update_check._check()  # inside the backoff window: no second hit
        self.assertEqual(pypi.call_count, 1)

    @mock.patch.object(update_check, "get_pypi_latest", return_value="9.9.9")
    def test_check_result_is_cached_for_a_day(self, pypi):
        update_check._check()
        update_check._check()
        self.assertEqual(pypi.call_count, 1)
        cache = json.loads(update_check._cache_path().read_text())
        self.assertEqual(cache["latestVersion"], "9.9.9")

    def test_env_opt_out_disables_check(self):
        os.environ["AWEWARM_NO_UPDATE_CHECK"] = "1"
        self.addCleanup(os.environ.pop, "AWEWARM_NO_UPDATE_CHECK", None)
        with mock.patch.object(update_check, "get_pypi_latest") as pypi:
            self.assertIsNone(check_async(["status"])())
        pypi.assert_not_called()

    def test_check_async_skips_run_without_starting_a_thread(self):
        with mock.patch.object(update_check.threading, "Thread") as thread:
            self.assertIsNone(check_async(["run"])())
        thread.assert_not_called()


if __name__ == "__main__":
    unittest.main()
