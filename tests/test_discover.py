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
        # USERPROFILE matters too: Windows expanduser() ignores HOME.
        self._saved_home = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
        home = str(self.tmp_path / "home")
        os.makedirs(home)
        os.environ["HOME"] = home
        os.environ["USERPROFILE"] = home
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        for key, value in self._saved_home.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

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


class AweswitchFindingsTests(IsolatedTestCase):
    """aweswitch official accounts appear as their own findings — one per
    login dir holding its credential file — so each can become a connection
    with its own authHome."""

    def setUp(self):
        super().setUp()
        self._saved_home = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
        home = str(self.tmp_path / "home")
        os.makedirs(home)
        os.environ["HOME"] = home
        os.environ["USERPROFILE"] = home
        self.addCleanup(self._restore_home)

    def _restore_home(self):
        for key, value in self._saved_home.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _which(self, found):
        return mock.patch("awewarm.discover.shutil.which", side_effect=lambda cmd: found.get(cmd))

    def _accounts_dir(self, provider, name, cred_file):
        account = os.path.join(os.environ["HOME"], ".config", "aweswitch", "accounts", provider, name)
        os.makedirs(account, exist_ok=True)
        open(os.path.join(account, cred_file), "w").close()
        return account

    def _scan(self):
        fake_run = mock.Mock(returncode=0, stdout="1.0.0\n", stderr="")
        with self._which({"claude": "/usr/local/bin/claude", "codex": "/usr/local/bin/codex"}):
            with mock.patch("awewarm.discover.subprocess.run", return_value=fake_run):
                return discover.discover_accounts()

    def test_codex_account_becomes_its_own_finding(self):
        self._accounts_dir("codex", "cxo-peng", "auth.json")
        findings = self._scan()
        extra = [f for f in findings if f["provider"] == "codex"]
        self.assertEqual(len(extra), 2)  # the default login + the aweswitch one
        peng = extra[1]
        self.assertEqual(peng["label"], "Codex (cxo-peng)")
        self.assertTrue(peng["authFound"])
        self.assertTrue(peng["authHome"].endswith("cxo-peng"))
        self.assertIn("auth.json", peng["authDetail"])

    def test_claude_account_becomes_its_own_finding(self):
        self._accounts_dir("claude", "work", ".credentials.json")
        findings = self._scan()
        extras = [f for f in findings if f["label"] == "Claude Code (work)"]
        self.assertEqual(len(extras), 1)
        self.assertTrue(extras[0]["authHome"].endswith("work"))

    def test_account_dir_without_a_login_is_skipped(self):
        self._accounts_dir("codex", "logged-out", "config.toml")  # no auth.json
        findings = self._scan()
        self.assertFalse(any("logged-out" in (f["authHome"] or "") for f in findings))

    def test_no_accounts_dir_means_only_base_findings(self):
        findings = self._scan()
        self.assertEqual([f["label"] for f in findings], ["Claude Code", "Codex"])

    def test_base_findings_carry_no_auth_home(self):
        findings = self._scan()
        for finding in findings:
            self.assertIsNone(finding["authHome"])


if __name__ == "__main__":
    unittest.main()
