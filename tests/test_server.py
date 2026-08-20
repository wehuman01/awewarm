"""awewarm serve: the resident server half of remote delegation.

Covers the claim/auth model, the RAM-only secret rule (nothing secret ever
reaches the data dir), and the tick semantics that differ from local:
held-not-failed activations while a key is missing, then a catch-up fire or
skip once it returns.
"""
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from helpers import account_connection, plan_connection

from awewarm import remote as remote_client
from awewarm import server, transport

TZ = "Asia/Shanghai"


def at(hhmm, seconds=0):
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 8, 20, hour, minute, seconds, tzinfo=ZoneInfo(TZ))


class ServerCase(unittest.TestCase):
    """A real WarmServer on an ephemeral port, plus its URL."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "server"
        self.token = "awt_" + "t" * 40
        self.make_server(claim=True)

    def make_server(self, fixed_token=None, claim=True):
        self.warm, self.httpd = server.make_server(
            self.data_dir, "127.0.0.1", 0, fixed_token=fixed_token
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        if claim:
            remote_client.claim(self.url, self.token)

    def push_plan(self, conn_id="glm", fixed_at=("03:00",), days="every-day"):
        conn = plan_connection(fixed_at=fixed_at, days=days)
        return remote_client.push_connection(self.url, self.token, conn_id, conn, "sk-test", TZ)

    def view(self):
        return remote_client.fetch_state(self.url, self.token)

    def tick(self, now):
        return self.warm.tick(now_fn=lambda conn: now)


class AuthTests(ServerCase):
    def test_healthz_reports_claim_state(self):
        self.assertTrue(remote_client.healthz(self.url)["claimed"])

    def test_unclaimed_server_rejects_api_calls(self):
        self.make_server(claim=False)  # replaces self.url/warm with a fresh one
        self.assertFalse(remote_client.healthz(self.url)["claimed"])
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, "awt_" + "x" * 40)
        self.assertIn("401", str(ctx.exception))
        remote_client.claim(self.url, self.token)
        self.assertTrue(remote_client.fetch_state(self.url, self.token)["connections"] == {})

    def test_second_claim_with_other_token_is_rejected(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.claim(self.url, "awt_" + "other" * 10)
        self.assertIn("403", str(ctx.exception))

    def test_reclaim_with_same_token_is_idempotent(self):
        remote_client.claim(self.url, self.token)
        self.assertTrue(self.warm.claimed)

    def test_fixed_token_mode_ignores_claim_flow(self):
        self.make_server(fixed_token="awt_" + "f" * 40, claim=False)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.claim(self.url, self.token)
        self.assertIn("403", str(ctx.exception))
        remote_client.claim(self.url, "awt_" + "f" * 40)
        self.assertTrue(remote_client.fetch_state(self.url, "awt_" + "f" * 40)["connections"] == {})


class ConnectionTests(ServerCase):
    def test_push_stores_connection_and_key_without_secrets_on_disk(self):
        result = self.push_plan()
        self.assertTrue(result["ok"])
        self.assertEqual(self.view()["connections"]["glm"]["config"]["timezone"], TZ)
        self.assertFalse(self.view()["connections"]["glm"]["keyMissing"])
        on_disk = (self.data_dir / "config.json").read_text()
        self.assertNotIn("sk-test", on_disk)
        stored = json.loads(on_disk)["connections"]["glm"]
        self.assertIsNone(stored["auth"]["apiKeyRef"])  # no secret ref to resolve
        self.assertFalse((self.data_dir / "secrets.json").exists())

    def test_push_rejects_account_connections(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.push_connection(
                self.url, self.token, "claude", account_connection(), "sk", TZ
            )
        self.assertIn("subscription", str(ctx.exception))

    def test_push_rejects_unknown_timezone(self):
        conn = plan_connection()
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.push_connection(self.url, self.token, "glm", conn, "sk", "Mars/Olympus")
        self.assertIn("timezone", str(ctx.exception))

    def test_push_resets_state_and_reports_next_due(self):
        self.push_plan()
        self.warm.state["connections"]["glm"]["history"].append({"at": "x"})
        self.push_plan()
        self.assertEqual(self.view()["connections"]["glm"]["state"]["history"], [])
        self.assertIsNotNone(self.view()["connections"]["glm"]["state"])

    def test_delete_removes_connection(self):
        self.push_plan()
        remote_client.delete_connection(self.url, self.token, "glm")
        self.assertEqual(self.view()["connections"], {})
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.delete_connection(self.url, self.token, "glm")
        self.assertIn("404", str(ctx.exception))

    def test_restart_wipes_ram_secrets_but_keeps_disk_state(self):
        self.push_plan()
        self.warm.state["connections"]["glm"]["lastResult"] = "success"
        self.warm._save(self.warm.state_path, self.warm.state)
        warm2, _ = server.make_server(self.data_dir)  # simulates a restart
        self.assertFalse(warm2.claimed)
        self.assertEqual(warm2.config["connections"]["glm"]["activation"]["model"], "glm-4.7")
        self.assertEqual(warm2.state["connections"]["glm"]["lastResult"], "success")
        view = warm2.view()
        self.assertTrue(view["connections"]["glm"]["keyMissing"])

    def test_put_keys_restores_keyring(self):
        self.push_plan()
        warm2, _ = server.make_server(self.data_dir)
        self.assertTrue(warm2.view()["connections"]["glm"]["keyMissing"])
        remote_client.push_keys(self.url, self.token, {"glm": "sk-test"})
        self.assertFalse(self.view()["connections"]["glm"]["keyMissing"])


class TickTests(ServerCase):
    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_due_fixed_slot_fires(self, send):
        self.push_plan()
        result = self.tick(at("03:00", seconds=30))
        self.assertEqual(result["fired"], 1)
        send.assert_called_once()
        conn_arg, key_arg = send.call_args[0]
        self.assertEqual(key_arg, "sk-test")
        self.assertIn("03:00", self.warm.state["connections"]["glm"]["completedSlots"]["2026-08-20"])

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_activations_capped_so_a_dead_endpoint_cannot_stall_the_tick(self, send):
        # The tick holds the server lock while sending; a short per-request
        # timeout bounds how long API calls can queue behind it.
        self.push_plan()
        self.tick(at("03:00", seconds=30))
        self.assertEqual(send.call_args.kwargs["timeout_seconds"], server.ACTIVATION_TIMEOUT_SECONDS)

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_missing_key_holds_then_catchup_fires(self, send):
        self.push_plan()
        self.warm.keys.clear()  # a restart wiped the RAM keyring
        result = self.tick(at("03:00", seconds=30))
        self.assertEqual(result, {"fired": 0, "held": ["glm"]})
        send.assert_not_called()
        cs = self.warm.state["connections"]["glm"]
        self.assertIsNone(cs["lastResult"])  # held is not a failure
        self.warm.keys["glm"] = "sk-test"
        result = self.tick(at("03:20"))
        self.assertEqual(result["fired"], 1)
        self.assertIn("03:00", cs["completedSlots"]["2026-08-20"])

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_missing_key_past_catchup_skips_slot(self, send):
        self.push_plan()
        self.warm.keys.clear()
        self.tick(at("03:31"))  # past the 30-minute catch-up window
        self.assertIn("03:00", self.warm.state["connections"]["glm"]["skippedSlots"]["2026-08-20"])
        self.warm.keys["glm"] = "sk-test"
        result = self.tick(at("03:35"))
        self.assertEqual(result["fired"], 0)  # the slot closed; it will not refire
        send.assert_not_called()

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": False, "detail": "HTTP 401"})
    def test_failed_activation_counts_node_not_hold(self, send):
        self.push_plan()
        self.tick(at("03:01"))
        cs = self.warm.state["connections"]["glm"]
        self.assertEqual(cs["lastResult"], "failure")
        self.assertEqual(cs["nodeAttempts"], 1)

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_run_endpoint_fires_with_manual_semantics(self, send):
        self.push_plan()
        result = remote_client.run_connection(self.url, self.token, "glm")
        self.assertTrue(result["ok"])
        send.assert_called_once()
        history = self.warm.state["connections"]["glm"]["history"]
        self.assertEqual(history[-1]["kind"], "manual")

    def test_run_unknown_connection_is_404(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.run_connection(self.url, self.token, "nope")
        self.assertIn("404", str(ctx.exception))
