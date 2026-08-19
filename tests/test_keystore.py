import os
import unittest
from unittest import mock

from helpers import IsolatedTestCase

from awewarm import keystore


class EnvRefTests(unittest.TestCase):
    def test_env_ref_format(self):
        self.assertEqual(keystore.env_ref_for("glm-coding-plan"), "${AWEWARM_API_KEY_GLM_CODING_PLAN}")

    def test_load_env_ref_present(self):
        with mock.patch.dict("os.environ", {"AWEWARM_API_KEY_GLM": "sekret"}):
            self.assertEqual(keystore.load_api_key("${AWEWARM_API_KEY_GLM}"), "sekret")

    def test_load_env_ref_missing(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            os.environ.pop("AWEWARM_API_KEY_GLM", None)
            self.assertIsNone(keystore.load_api_key("${AWEWARM_API_KEY_GLM}"))

    def test_unrecognized_ref_dies(self):
        with self.assertRaises(SystemExit):
            keystore.load_api_key("plain-secret")

    def test_empty_ref_is_none(self):
        self.assertIsNone(keystore.load_api_key(None))
        self.assertIsNone(keystore.load_api_key(""))


class SecretsFileTests(IsolatedTestCase):
    def test_store_and_load_roundtrip(self):
        ref = keystore.store_api_key("glm", "sk-live-abcdef123456")
        self.assertEqual(ref, "file:glm")
        self.assertEqual(keystore.load_api_key(ref), "sk-live-abcdef123456")
        with open(keystore.secrets_path()) as handle:
            self.assertIn("glm", handle.read())

    def test_secrets_file_created_with_0600(self):
        keystore.store_api_key("glm", "k" * 32)
        self.assertEqual(os.stat(keystore.secrets_path()).st_mode & 0o777, 0o600)

    def test_load_missing_entry_returns_none(self):
        self.assertIsNone(keystore.load_api_key("file:nobody"))

    def test_store_empty_key_dies(self):
        with self.assertRaises(SystemExit):
            keystore.store_api_key("glm", "")

    def test_store_multiline_key_dies(self):
        with self.assertRaises(SystemExit):
            keystore.store_api_key("glm", "abc\ndef")

    def test_delete_removes_entry(self):
        keystore.store_api_key("glm", "k" * 32)
        keystore.delete_api_key("glm", "file:glm")
        self.assertIsNone(keystore.load_api_key("file:glm"))

    def test_legacy_keychain_ref_migrates_to_secrets(self):
        fake = mock.Mock(returncode=0, stdout="legacy-key-123\n")
        with mock.patch.object(keystore.subprocess, "run", return_value=fake):
            self.assertEqual(keystore.load_api_key("keychain:awewarm/glm"), "legacy-key-123")
        self.assertEqual(keystore.load_api_key("file:glm"), "legacy-key-123")


class BareDollarRefTests(unittest.TestCase):
    def test_normalize_both_notations(self):
        self.assertEqual(keystore.normalize_env_ref("$GLM_API_KEY"), "${GLM_API_KEY}")
        self.assertEqual(keystore.normalize_env_ref("${GLM_API_KEY}"), "${GLM_API_KEY}")
        self.assertIsNone(keystore.normalize_env_ref("GLM_API_KEY"))
        self.assertIsNone(keystore.normalize_env_ref("sk-literal"))

    def test_load_bare_ref(self):
        with mock.patch.dict("os.environ", {"GLM_API_KEY": "v"}):
            self.assertEqual(keystore.load_api_key("$GLM_API_KEY"), "v")
