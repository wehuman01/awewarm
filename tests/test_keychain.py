import unittest
from unittest import mock

from helpers import IsolatedTestCase

from awewarm import keychain


class EnvRefTests(unittest.TestCase):
    def test_env_ref_format(self):
        self.assertEqual(keychain.env_ref_for("glm-coding-plan"), "${AWEWARM_TOKEN_GLM_CODING_PLAN}")

    def test_load_env_ref_present(self):
        with mock.patch.dict("os.environ", {"AWEWARM_TOKEN_GLM": "sekret"}):
            self.assertEqual(keychain.load_token("${AWEWARM_TOKEN_GLM}"), "sekret")

    def test_load_env_ref_missing(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            import os

            os.environ.pop("AWEWARM_TOKEN_GLM", None)
            self.assertIsNone(keychain.load_token("${AWEWARM_TOKEN_GLM}"))

    def test_unrecognized_ref_dies(self):
        with self.assertRaises(SystemExit):
            keychain.load_token("plain-secret")

    def test_empty_ref_is_none(self):
        self.assertIsNone(keychain.load_token(None))
        self.assertIsNone(keychain.load_token(""))


class KeychainTests(unittest.TestCase):
    @mock.patch("awewarm.keychain.is_keychain_available", return_value=False)
    def test_store_falls_back_to_env_ref(self, available):
        self.assertEqual(keychain.store_token("x", "sekret"), "${AWEWARM_TOKEN_X}")

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=True)
    @mock.patch("awewarm.keychain.subprocess.run")
    def test_store_uses_security_stdin_not_argv(self, run, available):
        run.return_value = mock.Mock(returncode=0, stdout="", stderr="")
        ref = keychain.store_token("glm", "my secret token")
        self.assertEqual(ref, "keychain:awewarm/glm")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["security", "-i"])
        stdin = run.call_args[1]["input"]
        self.assertIn("my secret token", stdin)
        self.assertIn("add-generic-password", stdin)
        # the token must never ride along in the outer argv
        self.assertNotIn("my secret token", argv)

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=True)
    @mock.patch("awewarm.keychain.subprocess.run")
    def test_store_security_failure_falls_back(self, run, available):
        run.return_value = mock.Mock(returncode=44, stdout="", stderr="error")
        self.assertEqual(keychain.store_token("x", "t"), "${AWEWARM_TOKEN_X}")

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=True)
    @mock.patch("awewarm.keychain.subprocess.run")
    def test_load_reads_password(self, run, available):
        run.return_value = mock.Mock(returncode=0, stdout="sekret\n", stderr="")
        self.assertEqual(keychain.load_token("keychain:awewarm/glm"), "sekret")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["security", "find-generic-password", "-s", "awewarm/glm", "-w"])

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=True)
    @mock.patch("awewarm.keychain.subprocess.run")
    def test_load_missing_item_returns_none(self, run, available):
        run.return_value = mock.Mock(returncode=44, stdout="", stderr="not found")
        self.assertIsNone(keychain.load_token("keychain:awewarm/glm"))

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=False)
    def test_load_keychain_ref_unavailable(self, available):
        self.assertIsNone(keychain.load_token("keychain:awewarm/glm"))

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=True)
    @mock.patch("awewarm.keychain.subprocess.run")
    def test_delete_ignores_failure(self, run, available):
        run.return_value = mock.Mock(returncode=44, stdout="", stderr="not found")
        keychain.delete_token("glm")  # must not raise

    @mock.patch("awewarm.keychain.is_keychain_available", return_value=False)
    def test_delete_without_keychain_is_noop(self, available):
        keychain.delete_token("glm")


if __name__ == "__main__":
    unittest.main()
