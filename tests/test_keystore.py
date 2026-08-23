import os
import sys
import unittest
from pathlib import Path
from unittest import mock

from helpers import IsolatedTestCase

from awewarm import keystore


class EnvRefTests(unittest.TestCase):
    def test_env_ref_dies_with_migration_hint(self):
        # Env-var refs were removed: the launchd scheduler cannot read shell
        # variables, so refs must die with guidance instead of silently failing.
        with self.assertRaises(SystemExit):
            keystore.load_api_key("${AWEWARM_API_KEY_GLM}")

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
        if os.name == "nt":
            # NTFS has no POSIX mode bits; chmod is advisory only.
            # Checked via os.name because tests may spoof sys.platform.
            self.assertTrue(os.path.exists(keystore.secrets_path()))
            return
        self.assertEqual(os.stat(keystore.secrets_path()).st_mode & 0o777, 0o600)

    def test_load_missing_entry_returns_none(self):
        self.assertIsNone(keystore.load_api_key("file:nobody"))

    def test_malformed_secrets_file_is_refused_without_overwriting_it(self):
        path = Path(keystore.secrets_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{broken")

        with self.assertRaises(SystemExit) as ctx:
            keystore.store_api_key("glm", "k" * 32)

        self.assertIn("cannot read secrets", str(ctx.exception))
        self.assertEqual(path.read_text(), "{broken")

    def test_non_object_secrets_file_is_refused_without_overwriting_it(self):
        path = Path(keystore.secrets_path())
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("[]\n")

        with self.assertRaises(SystemExit) as ctx:
            keystore.store_api_key("glm", "k" * 32)

        self.assertIn("must contain a JSON object", str(ctx.exception))
        self.assertEqual(path.read_text(), "[]\n")

    def test_failed_atomic_replace_keeps_the_previous_secrets_file(self):
        keystore.store_api_key("glm", "old-key")
        path = Path(keystore.secrets_path())
        temp_modes = []

        def fail_replace(source, _target):
            temp_modes.append(os.stat(source).st_mode & 0o777)
            raise OSError("disk full")

        with mock.patch("awewarm.config.os.chmod", side_effect=OSError("denied")), \
             mock.patch("awewarm.config.os.replace", side_effect=fail_replace):
            with self.assertRaises(OSError):
                keystore.store_api_key("other", "new-key")

        # POSIX mkstemp creates the temp file 0600, so a secret is never
        # group/world-readable mid-write; Windows has no POSIX permission
        # bits to assert (its mkstemp reports 0o666).
        if sys.platform != "win32":
            self.assertEqual(temp_modes, [0o600])
        self.assertEqual(keystore.load_api_key("file:glm"), "old-key")
        self.assertIsNone(keystore.load_api_key("file:other"))
        self.assertEqual(list(path.parent.glob(path.name + ".*.tmp")), [])

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
        with mock.patch.object(keystore.subprocess, "run", return_value=fake), \
             mock.patch.object(keystore.sys, "platform", "darwin"):
            self.assertEqual(keystore.load_api_key("keychain:awewarm/glm"), "legacy-key-123")
        self.assertEqual(keystore.load_api_key("file:glm"), "legacy-key-123")
