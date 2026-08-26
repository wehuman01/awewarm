"""credstore: reading the local CLI logins for delegation.

Values stay inside the module under test — assertions compare fingerprints
and shapes, never echo a secret into a failure message.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import account_connection

from awewarm import credstore

CLAUDE_CREDENTIALS = json.dumps(
    {"claudeOAuthAccessToken": {"accessToken": "sk-ant-oat01-test", "refreshToken": "r"}}
)
CODEX_AUTH = json.dumps({"tokens": {"access_token": "at", "refresh_token": "rt"}})


def codex_account_connection():
    conn = account_connection()
    conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
    return conn


class CodexReadTests(unittest.TestCase):
    def test_reads_auth_json_under_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "auth.json").write_text(CODEX_AUTH)
            with mock.patch.dict(os.environ, {credstore.CODEX_HOME_ENV: tmp}):
                credential = credstore.read_credential(codex_account_connection())
        self.assertEqual(credential.raw, CODEX_AUTH)

    def test_missing_login_is_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {credstore.CODEX_HOME_ENV: tmp}):
                with self.assertRaises(credstore.CredentialError) as ctx:
                    credstore.read_credential(codex_account_connection())
        message = str(ctx.exception)
        self.assertIn("codex login", message)
        self.assertIn("awewarm remote push", message)

    def test_empty_file_is_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "auth.json").write_text("   ")
            with mock.patch.dict(os.environ, {credstore.CODEX_HOME_ENV: tmp}):
                with self.assertRaises(credstore.CredentialError):
                    credstore.read_credential(codex_account_connection())


class ClaudeReadTests(unittest.TestCase):
    def test_reads_the_keychain_on_macos(self):
        proc = mock.Mock(returncode=0, stdout=CLAUDE_CREDENTIALS + "\n", stderr="")
        with mock.patch("sys.platform", "darwin"), mock.patch.object(
            credstore.subprocess, "run", return_value=proc
        ) as run:
            credential = credstore.read_credential(account_connection())
        self.assertEqual(credential.raw, CLAUDE_CREDENTIALS)
        self.assertEqual(run.call_args[0][0][1:3], ["find-generic-password", "-s"])

    def test_keychain_failure_is_an_actionable_error(self):
        proc = mock.Mock(returncode=44, stdout="", stderr="could not be found")
        with mock.patch("sys.platform", "darwin"), mock.patch.object(
            credstore.subprocess, "run", return_value=proc
        ):
            with self.assertRaises(credstore.CredentialError) as ctx:
                credstore.read_credential(account_connection())
        self.assertIn("Keychain", str(ctx.exception))
        self.assertIn("claude /login", str(ctx.exception))

    def test_reads_the_credentials_file_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, ".claude").mkdir()
            Path(tmp, ".claude", ".credentials.json").write_text(CLAUDE_CREDENTIALS)
            # USERPROFILE matters too: Windows expanduser() ignores HOME.
            with mock.patch("sys.platform", "linux"), mock.patch.dict(
                os.environ, {"HOME": tmp, "USERPROFILE": tmp}, clear=False
            ):
                credential = credstore.read_credential(account_connection())
        self.assertEqual(credential.raw, CLAUDE_CREDENTIALS)

    def test_missing_file_is_an_actionable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("sys.platform", "linux"), mock.patch.dict(
                os.environ, {"HOME": tmp, "USERPROFILE": tmp}, clear=False
            ):
                with self.assertRaises(credstore.CredentialError) as ctx:
                    credstore.read_credential(account_connection())
        self.assertIn("claude /login", str(ctx.exception))


class FingerprintTests(unittest.TestCase):
    def test_fingerprint_is_sixteen_hex_and_stable(self):
        one = credstore.Credential(CODEX_AUTH)
        again = credstore.Credential(CODEX_AUTH)
        self.assertEqual(one.fingerprint, again.fingerprint)
        self.assertEqual(len(one.fingerprint), 16)
        int(one.fingerprint, 16)  # hex

    def test_fingerprint_moves_when_the_credential_rotates(self):
        self.assertNotEqual(
            credstore.Credential(CODEX_AUTH).fingerprint,
            credstore.Credential(CODEX_AUTH.replace("at", "at2")).fingerprint,
        )


class AccessTokenTests(unittest.TestCase):
    def test_extracts_the_oauth_access_token(self):
        self.assertEqual(
            credstore.claude_access_token(CLAUDE_CREDENTIALS), "sk-ant-oat01-test"
        )

    def test_accepts_a_top_level_access_token(self):
        self.assertEqual(
            credstore.claude_access_token(json.dumps({"accessToken": "plain"})), "plain"
        )

    def test_non_json_is_rejected_with_a_re_push_hint(self):
        with self.assertRaises(ValueError) as ctx:
            credstore.claude_access_token("<html>not json</html>")
        self.assertIn("not recognized", str(ctx.exception))
        self.assertIn("awewarm remote push", str(ctx.exception))

    def test_json_without_a_token_is_rejected(self):
        with self.assertRaises(ValueError):
            credstore.claude_access_token(json.dumps({"something": "else"}))


if __name__ == "__main__":
    unittest.main()
