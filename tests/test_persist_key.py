"""The owner-opt-in key persistence (keys.json) and its confirmation gates.

Server side: the on-disk keyring lifecycle — write on a persistKey push,
write-through on re-key, removal on opt-out and takeback, survival across a
restart, purge. Client side: the gates around every action that starts or
stops persistence (never the background sync), the --yes escape for
non-interactive shells, and the backup/restore device move that carries
machine-id across machines.
"""
import io
import json
import os
import stat
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from click.testing import CliRunner
from helpers import IsolatedTestCase, account_connection, plan_connection

from awewarm import config as cfg, keystore, remote, server
from awewarm.cli import cli

RUNNER = CliRunner()
TZ = "Asia/Shanghai"


def invoke(*args, **kwargs):
    kwargs.setdefault("prog_name", "awewarm")
    return RUNNER.invoke(cli, *args, **kwargs)


def output_of(result):
    text = result.output
    try:
        text += result.stderr
    except (ValueError, AttributeError):
        pass
    return text


def interactive():
    """Make the gates treat the runner's stdin as a terminal."""
    return mock.patch("awewarm.cli._stdin_is_interactive", return_value=True)


class WarmServerPersistenceTests(unittest.TestCase):
    """Direct WarmServer calls over a temp dir — the file-level lifecycle."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "server"
        self.warm = server.WarmServer(self.data_dir)

    def keys_file(self):
        return self.data_dir / "keys.json"

    def push(self, persist=False, key="sk-test"):
        return self.warm.put_connection("glm", {
            "connection": plan_connection(fixed_at=("03:00",), days="every-day"),
            "apiKey": key,
            "timezone": TZ,
            **({"persistKey": True} if persist else {}),
        })

    def test_persist_push_writes_keys_json_0600(self):
        self.push(persist=True)
        self.assertTrue(self.keys_file().exists())
        self.assertEqual(json.loads(self.keys_file().read_text()), {"glm": "sk-test"})
        if os.name != "nt":  # NTFS has no POSIX mode bits; chmod is advisory only.
            self.assertEqual(stat.S_IMODE(self.keys_file().stat().st_mode), 0o600)
        self.assertTrue(self.warm.view()["connections"]["glm"]["keyPersisted"])

    def test_plain_push_leaves_no_file_and_views_ram_only(self):
        self.push(persist=False)
        self.assertFalse(self.keys_file().exists())
        entry = self.warm.view()["connections"]["glm"]
        self.assertFalse(entry["keyPersisted"])
        self.assertFalse(entry["keyMissing"])

    def test_persisted_key_survives_a_restart(self):
        self.push(persist=True)
        revived = server.WarmServer(self.data_dir)
        entry = revived.view()["connections"]["glm"]
        self.assertFalse(entry["keyMissing"])  # no client re-push needed
        self.assertTrue(entry["keyPersisted"])

    def test_repush_without_the_flag_removes_the_disk_copy(self):
        self.push(persist=True)
        self.push(persist=False)
        self.assertFalse(self.keys_file().exists())
        self.assertFalse(self.warm.view()["connections"]["glm"]["keyPersisted"])

    def test_rekey_rotates_the_persisted_copy(self):
        self.push(persist=True)
        self.warm.put_keys({"glm": "sk-rotated"})
        self.assertEqual(json.loads(self.keys_file().read_text()), {"glm": "sk-rotated"})

    def test_rekey_of_a_ram_connection_never_lands_on_disk(self):
        self.push(persist=False)
        self.warm.put_keys({"glm": "sk-rotated"})
        self.assertFalse(self.keys_file().exists())

    def test_takeback_purges_the_disk_copy(self):
        self.push(persist=True)
        self.warm.delete_connection("glm")
        self.assertFalse(self.keys_file().exists())

    def test_purge_clears_disk_but_keeps_the_ram_key(self):
        self.push(persist=True)
        self.warm.purge_persisted_keys("operator switched storage off")
        self.assertFalse(self.keys_file().exists())
        self.assertEqual(self.warm.missing_keys(), [])  # RAM copy still ticking

    def test_changed_key_overwrites_the_persisted_copy(self):
        self.push(persist=True, key="sk-one")
        self.push(persist=True, key="sk-two")
        self.assertEqual(json.loads(self.keys_file().read_text()), {"glm": "sk-two"})


class GateTests(IsolatedTestCase):
    """The confirmation boundary: every state-changing user action gates,
    decline changes nothing, --yes is the non-interactive escape."""

    def setUp(self):
        super().setUp()
        remote._machine_cache = None  # the per-install id caches across tests otherwise
        conn = plan_connection(fixed_at=("03:00",), days="every-day")
        conn["auth"]["apiKeyRef"] = keystore.store_api_key("glm", "sk-test")
        data = cfg.empty_config()
        data["connections"]["glm"] = conn
        data["remote"] = {"url": "http://127.0.0.1:1", "tokenRef": "file:awewarm-remote-token"}
        cfg.save_config(data)
        self.pushed = []

    def remote_mock(self, view=None):
        ensure = mock.MagicMock(return_value=view or {"connections": {}})
        push = mock.MagicMock(
            side_effect=lambda *a, **kw: self.pushed.append((a, kw)) or {"ok": True}
        )
        patcher = mock.patch.multiple(
            "awewarm.cli.remote",
            ensure_session=ensure,
            load_token=mock.MagicMock(return_value="awt_" + "t" * 40),
            push_connection=push,
        )
        return patcher, push

    def conn(self):
        return cfg.load_config()["connections"]["glm"]

    def test_on_gate_decline_leaves_the_flag_off(self):
        with interactive(), self.remote_mock()[0]:
            result = invoke(["config", "set", "glm", "--persist-key", "on"], input="n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertNotIn("persistKey", self.conn())
        self.assertEqual(self.pushed, [])  # nothing pushed, nothing changed

    def test_on_gate_accept_sets_the_flag_and_pushes_persist(self):
        data = cfg.load_config()
        data["connections"]["glm"]["location"] = "remote"
        cfg.save_config(data)
        with interactive(), self.remote_mock()[0]:
            result = invoke(["config", "set", "glm", "--persist-key", "on"], input="y\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertTrue(self.conn().get("persistKey"))
        self.assertEqual(self.pushed[-1][1].get("persist"), True)

    def test_on_without_yes_in_a_pipe_dies_with_guidance(self):
        result = invoke(["config", "set", "glm", "--persist-key", "on"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--yes", output_of(result))

    def test_yes_flags_the_gate_in_a_pipe(self):
        with self.remote_mock()[0]:
            result = invoke(["config", "set", "glm", "--persist-key", "on", "--yes"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertTrue(self.conn().get("persistKey"))

    def test_off_gate_accepts_by_default_and_pushes_without_persist(self):
        data = cfg.load_config()
        data["connections"]["glm"]["persistKey"] = True
        data["connections"]["glm"]["location"] = "remote"
        cfg.save_config(data)
        with interactive(), self.remote_mock()[0]:
            result = invoke(["config", "set", "glm", "--persist-key", "off"], input="\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertNotIn("persistKey", self.conn())
        self.assertEqual(self.pushed[-1][1].get("persist"), False)
        self.assertIn("deletes the key from its disk", result.output)

    def test_off_gate_decline_keeps_persistence(self):
        data = cfg.load_config()
        data["connections"]["glm"]["persistKey"] = True
        cfg.save_config(data)
        with interactive(), self.remote_mock()[0]:
            result = invoke(["config", "set", "glm", "--persist-key", "off"], input="n\n")
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertTrue(self.conn().get("persistKey"))
        self.assertEqual(self.pushed, [])

    def test_delegation_of_a_flagged_connection_gates(self):
        with interactive(), self.remote_mock()[0]:
            result = invoke(["config", "set", "glm", "--persist-key", "on", "--yes"])
            self.assertEqual(result.exit_code, 0, output_of(result))
            decline = invoke(["config", "set", "glm", "--remote"], input="n\n")
        self.assertEqual(decline.exit_code, 0, output_of(decline))
        conn = self.conn()
        self.assertEqual(conn.get("location"), "remote")  # delegated anyway
        self.assertNotIn("persistKey", conn)  # ...but downgraded to RAM-only
        self.assertEqual(self.pushed[-1][1].get("persist"), False)
        self.assertIn("RAM only", decline.output)

    def test_delegation_accepts_persistence(self):
        with interactive(), self.remote_mock()[0]:
            invoke(["config", "set", "glm", "--persist-key", "on", "--yes"])
            accept = invoke(["config", "set", "glm", "--remote"], input="y\n")
        self.assertEqual(accept.exit_code, 0, output_of(accept))
        self.assertEqual(self.pushed[-1][1].get("persist"), True)
        self.assertTrue(self.conn().get("persistKey"))

    def test_persist_key_allows_accounts_with_the_harder_notice(self):
        # persistKey is credential-agnostic: an account may opt in too, but
        # the notice must say plainly that an account-wide login — not a
        # scoped key — would sit in plaintext on the server's disk.
        conn = account_connection()
        conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
        data = cfg.load_config()
        data["connections"]["codex-account"] = conn
        cfg.save_config(data)
        result = invoke(["config", "set", "codex-account", "--persist-key", "on", "--yes"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertIn("LOGIN CREDENTIAL", output_of(result))
        self.assertTrue(
            cfg.load_config()["connections"]["codex-account"].get("persistKey")
        )

    def test_duplicate_remote_inherits_the_gate(self):
        with interactive(), self.remote_mock()[0]:
            invoke(["config", "set", "glm", "--persist-key", "on", "--yes"])
            decline = invoke(["config", "set", "glm", "--duplicate", "--remote"], input="n\n")
        self.assertEqual(decline.exit_code, 0, output_of(decline))
        copy = cfg.load_config()["connections"]["glm-copy"]
        self.assertEqual(copy.get("location"), "remote")
        self.assertNotIn("persistKey", copy)  # the copy was downgraded, the gate named it
        self.assertEqual(self.pushed[-1][0][2], "glm-copy")  # pushed under the copy's id


class BackupRestoreTests(IsolatedTestCase):
    def setUp(self):
        super().setUp()
        remote._machine_cache = None  # the per-install id caches across tests otherwise
        conn = plan_connection(fixed_at=("03:00",), days="every-day")
        conn["auth"]["apiKeyRef"] = keystore.store_api_key("glm", "sk-test")
        data = cfg.empty_config()
        data["connections"]["glm"] = conn
        cfg.save_config(data)
        keystore.store_api_key("awewarm-remote-token", "awt_" + "t" * 40)
        self.machine_id = remote.machine_id()
        cfg.save_state({"version": 1, "connections": {}})
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Path(self.tmp.name) / "awewarm.tar.gz"

    def make(self):
        result = invoke(["config", "backup", "--output", str(self.archive)])
        self.assertEqual(result.exit_code, 0, output_of(result))
        return result

    def names(self):
        with tarfile.open(self.archive) as tar:
            return sorted(member.name for member in tar.getmembers() if member.isfile())

    def test_backup_packs_identity_and_warns_about_plaintext(self):
        result = self.make()
        self.assertEqual(
            self.names(),
            ["config.json", "machine-id", "manifest.json", "secrets.json", "state.json"],
        )
        if os.name != "nt":  # NTFS has no POSIX mode bits; chmod is advisory only.
            self.assertEqual(stat.S_IMODE(self.archive.stat().st_mode), 0o600)
        self.assertIn("PLAINTEXT", result.output)
        with tarfile.open(self.archive) as tar:
            manifest = json.loads(tar.extractfile("manifest.json").read())
        self.assertEqual(manifest["format"], 1)
        self.assertIn("machine-id", manifest["files"])

    def test_restore_round_trips_into_a_fresh_machine(self):
        self.make()
        for path in (cfg.config_path(), keystore.secrets_path(), cfg.state_path(),
                     cfg.config_path().parent / "machine-id"):
            Path(path).unlink()
        result = invoke(["config", "restore", str(self.archive)])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertEqual(keystore.load_api_key("file:glm"), "sk-test")
        self.assertEqual(remote.machine_id(), self.machine_id)  # same machine to any hub
        self.assertIn("scheduler install", result.output)

    def test_restore_refuses_existing_files_without_force(self):
        self.make()
        result = invoke(["config", "restore", str(self.archive)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--force", output_of(result))

    def test_restore_overwrites_with_force(self):
        self.make()
        Path(cfg.config_path().parent / "machine-id").write_text("awm_fresh\n")
        result = invoke(["config", "restore", str(self.archive), "--force"])
        self.assertEqual(result.exit_code, 0, output_of(result))
        self.assertEqual(remote.machine_id(), self.machine_id)

    def test_restore_gates_on_persisted_key_connections(self):
        data = cfg.load_config()
        data["connections"]["glm"]["persistKey"] = True
        cfg.save_config(data)
        self.make()
        for path in (cfg.config_path(), keystore.secrets_path(), cfg.state_path(),
                     cfg.config_path().parent / "machine-id"):
            Path(path).unlink()
        with interactive():
            decline = invoke(["config", "restore", str(self.archive)], input="n\n")
        self.assertNotEqual(decline.exit_code, 0)
        self.assertFalse(cfg.config_path().exists())  # aborted: nothing restored
        with interactive():
            accept = invoke(["config", "restore", str(self.archive)], input="y\n")
        self.assertEqual(accept.exit_code, 0, output_of(accept))
        self.assertTrue(cfg.load_config()["connections"]["glm"].get("persistKey"))
        result = invoke(["config", "restore", str(self.archive), "--force", "--yes"])
        self.assertEqual(result.exit_code, 0, output_of(result))  # --yes skips the gate

    def test_restore_rejects_foreign_archives(self):
        crafted = Path(self.tmp.name) / "evil.tar.gz"
        with tarfile.open(crafted, "w:gz") as tar:
            info = tarfile.TarInfo("../escape.txt")
            payload = b"x"
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        result = invoke(["config", "restore", str(crafted)])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("does not look like an awewarm backup", output_of(result))
