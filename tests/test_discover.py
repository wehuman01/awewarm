import os
import unittest
from unittest import mock

from helpers import IsolatedTestCase

from awewarm import discover


class DiscoverTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        # Point ~ at the temp home so credential-file checks stay isolated;
        # `security` is made unreachable so the keychain path is skipped.
        self._home = os.environ.get("HOME")
        os.environ["HOME"] = str(self.tmp_path / "home")
        os.makedirs(os.environ["HOME"])
        self.addCleanup(lambda: os.environ.__setitem__("HOME", self._home) if self._home else os.environ.pop("HOME", None))

    def _which(self, found):
        return mock.patch("awewarm.discover.shutil.which", side_effect=lambda cmd: found.get(cmd))

    def test_missing_clis_reported_not_installed(self):
        with self._which({}):
            findings = discover.discover_accounts()
        for finding in findings:
            self.assertFalse(finding["installed"])
            self.assertFalse(finding["authFound"])
            self.assertIsNone(finding["cliPath"])
            lines = discover.describe_finding(finding)
            self.assertIn("not found", lines[0])

    def test_installed_cli_reports_absolute_path(self):
        # The absolute path is what goes into config: launchd's minimal PATH
        # cannot resolve bare names like "claude" installed in ~/.local/bin.
        with self._which({"claude": "/Users/x/.local/bin/claude"}):
            with mock.patch("awewarm.discover.subprocess.run", return_value=mock.Mock(returncode=0, stdout="1.0.66\n", stderr="")):
                findings = discover.discover_accounts()
        self.assertEqual(findings[0]["cliPath"], "/Users/x/.local/bin/claude")

    def test_claude_found_with_credentials_file(self):
        cred = os.path.join(os.environ["HOME"], ".claude", ".credentials.json")
        os.makedirs(os.path.dirname(cred), exist_ok=True)
        open(cred, "w").close()
        fake_run = mock.Mock(returncode=0, stdout="1.0.66 (Claude Code)\n", stderr="")
        with self._which({"claude": "/usr/local/bin/claude"}):
            with mock.patch("awewarm.discover.subprocess.run", return_value=fake_run):
                findings = discover.discover_accounts()
        claude = findings[0]
        self.assertEqual(claude["provider"], "claude-code")
        self.assertTrue(claude["installed"])
        self.assertEqual(claude["version"], "1.0.66 (Claude Code)")
        self.assertTrue(claude["authFound"])
        self.assertIn(".credentials.json", claude["authDetail"])

    def test_claude_found_via_keychain_on_macos(self):
        fake_run = mock.Mock(returncode=0, stdout="1.0.66\n", stderr="")
        with mock.patch("sys.platform", "darwin"):
            with self._which({"claude": "/usr/local/bin/claude", "security": "/usr/bin/security"}):
                with mock.patch("awewarm.discover.subprocess.run", return_value=fake_run):
                    findings = discover.discover_accounts()
        claude = findings[0]
        self.assertTrue(claude["authFound"])
        self.assertIn("keychain", claude["authDetail"])

    def test_codex_without_login(self):
        with self._which({"codex": "/usr/local/bin/codex"}):
            with mock.patch("awewarm.discover.subprocess.run", return_value=mock.Mock(returncode=0, stdout="0.20.0\n", stderr="")):
                findings = discover.discover_accounts()
        codex = findings[1]
        self.assertTrue(codex["installed"])
        self.assertFalse(codex["authFound"])

    def test_codex_with_auth_file(self):
        auth = os.path.join(os.environ["HOME"], ".codex", "auth.json")
        os.makedirs(os.path.dirname(auth), exist_ok=True)
        open(auth, "w").close()
        with self._which({"codex": "/usr/local/bin/codex"}):
            with mock.patch("awewarm.discover.subprocess.run", return_value=mock.Mock(returncode=0, stdout="0.20.0\n", stderr="")):
                findings = discover.discover_accounts()
        codex = findings[1]
        self.assertTrue(codex["authFound"])

    def test_version_probe_failure_is_tolerated(self):
        with self._which({"codex": "/usr/local/bin/codex"}):
            with mock.patch("awewarm.discover.subprocess.run", side_effect=OSError("nope")):
                findings = discover.discover_accounts()
        self.assertIsNone(findings[1]["version"])

    def test_describe_verified_window(self):
        finding = {
            "provider": "claude-code",
            "label": "Claude Code",
            "cliCommand": "claude",
            "installed": True,
            "version": "1.0.66",
            "authFound": True,
            "authDetail": "file",
            "builtinWindow": discover.BUILTIN_WINDOWS["claude-code"],
        }
        text = "\n".join(discover.describe_finding(finding))
        self.assertIn("5 hours", text)
        self.assertIn("✓", text)

    def test_builtin_windows_shape(self):
        self.assertEqual(discover.BUILTIN_WINDOWS["claude-code"]["durationMinutes"], 300)
        self.assertEqual(discover.BUILTIN_WINDOWS["codex"]["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
