"""awewarm serve: the resident server half of remote delegation.

Covers the claim/auth model, the RAM-only secret rule (nothing secret ever
reaches the data dir), and the tick semantics that differ from local:
held-not-failed activations while a key is missing, then a catch-up fire or
skip once it returns.
"""
import http.client
import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from helpers import account_connection, plan_connection, start_http_server, stop_http_server

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
        self.server_thread = start_http_server(self.httpd)
        self.addCleanup(stop_http_server, self.httpd, self.server_thread)
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


class ReleaseTests(ServerCase):
    def test_release_opens_the_server_for_a_new_claim(self):
        remote_client.release(self.url, self.token)
        self.assertFalse(self.warm.claimed)
        remote_client.claim(self.url, "awt_" + "n" * 40)  # a different machine can pair
        self.assertTrue(self.warm.claimed)

    def test_release_requires_the_claiming_token(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.release(self.url, "awt_" + "x" * 40)
        self.assertIn("401", str(ctx.exception))
        self.assertTrue(self.warm.claimed)

    def test_release_on_a_fixed_token_server_is_a_no_op(self):
        self.make_server(fixed_token="awt_" + "f" * 40, claim=False)
        remote_client.claim(self.url, "awt_" + "f" * 40)
        self.assertFalse(remote_client.release(self.url, "awt_" + "f" * 40)["released"])
        self.assertTrue(self.warm.claimed)  # the claim is pinned by --token


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

    def test_push_accepts_an_account_without_a_cli_as_native(self):
        conn = account_connection()
        with mock.patch("awewarm.server.shutil.which", return_value=None):
            result = remote_client.push_connection(
                self.url, self.token, "claude", conn,
                '{"claudeOAuthAccessToken": {"accessToken": "c"}}', TZ,
                fingerprint="abcd1234abcd1234",
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["exec"], "native")
        entry = self.view()["connections"]["claude"]
        self.assertEqual(entry["config"]["transport"]["exec"], "native")
        self.assertEqual(entry["config"]["transport"]["cliCommand"], "claude")  # untouched
        log = (self.data_dir / "awewarm-server.log").read_text()
        self.assertIn("natively over HTTPS", log)

    def test_native_push_rejects_a_credential_that_cannot_ever_fire(self):
        with mock.patch("awewarm.server.shutil.which", return_value=None):
            with self.assertRaises(remote_client.RemoteError) as ctx:
                remote_client.push_connection(
                    self.url, self.token, "claude", account_connection(), '{"token": "c"}', TZ
                )
        self.assertIn("claude /login", str(ctx.exception))
        self.assertEqual(self.view()["connections"], {})

    def test_resolve_cli_understands_a_windows_path_pushed_to_posix(self):
        command = r"C:\Users\Peng\AppData\Roaming\npm\codex.cmd"
        with mock.patch(
            "awewarm.server.shutil.which",
            side_effect=lambda candidate: "/usr/local/bin/codex" if candidate == "codex" else None,
        ):
            self.assertEqual(self.warm._resolve_cli(command), "/usr/local/bin/codex")

    def test_repush_flips_a_native_account_back_to_the_cli_when_installed(self):
        conn = account_connection()
        with mock.patch("awewarm.server.shutil.which", return_value=None):
            remote_client.push_connection(
                self.url, self.token, "claude", conn,
                '{"claudeOAuthAccessToken": {"accessToken": "c"}}', TZ,
            )
        self.assertEqual(self.view()["connections"]["claude"]["config"]["transport"]["exec"], "native")
        with mock.patch("awewarm.server.shutil.which", return_value="/usr/local/bin/claude"):
            remote_client.push_connection(
                self.url, self.token, "claude", conn, '{"token": "c"}', TZ
            )
        transport_block = self.view()["connections"]["claude"]["config"]["transport"]
        self.assertNotIn("exec", transport_block)
        self.assertEqual(transport_block["cliCommand"], "/usr/local/bin/claude")

    def test_push_accepts_an_account_and_resolves_the_cli_server_side(self):
        conn = account_connection()
        conn["transport"]["cliCommand"] = "/Users/local/bin/claude"  # the delegating box's path
        with mock.patch("awewarm.server.shutil.which", return_value="/usr/local/bin/claude"):
            result = remote_client.push_connection(
                self.url, self.token, "claude", conn, '{"token": "c"}', TZ,
                fingerprint="abcd1234abcd1234",
            )
        self.assertTrue(result["ok"])
        entry = self.view()["connections"]["claude"]
        self.assertEqual(entry["config"]["transport"]["cliCommand"], "/usr/local/bin/claude")
        self.assertEqual(entry["credentialFingerprint"], "abcd1234abcd1234")
        self.assertFalse(entry["keyMissing"])
        self.assertEqual(self.warm.keys["claude"], '{"token": "c"}')

    def test_account_fingerprint_survives_a_restart(self):
        conn = account_connection()
        with mock.patch("awewarm.server.shutil.which", return_value="/usr/local/bin/claude"):
            remote_client.push_connection(
                self.url, self.token, "claude", conn, '{"token": "c"}', TZ,
                fingerprint="abcd1234abcd1234",
            )
        warm2 = server.WarmServer(self.data_dir)  # simulates a restart
        entry = warm2.view()["connections"]["claude"]
        self.assertEqual(entry["credentialFingerprint"], "abcd1234abcd1234")
        self.assertTrue(entry["keyMissing"])  # the credential itself left with the RAM

    def test_delete_removes_the_sandbox_and_fingerprint(self):
        conn = account_connection()
        conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
        with mock.patch("awewarm.server.shutil.which", return_value="/usr/local/bin/codex"):
            remote_client.push_connection(
                self.url, self.token, "codex", conn, '{"token": "c"}', TZ,
                fingerprint="abcd1234abcd1234",
            )
        transport.activation_env(conn, '{"token": "c"}', sandbox_root=self.warm.sandbox_root, conn_id="codex")
        remote_client.delete_connection(self.url, self.token, "codex")
        self.assertNotIn("codex", self.view()["connections"])
        self.assertFalse((self.warm.data_dir / "codex-home" / "codex").exists())

    def test_connection_id_cannot_escape_the_sandbox(self):
        victim = self.data_dir.parent / "victim"
        victim.mkdir()
        (victim / "sentinel.txt").write_text("keep")
        malicious = str(victim)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            self.push_plan(malicious)
        self.assertIn("connection id", str(ctx.exception))
        self.assertTrue((victim / "sentinel.txt").exists())

    def test_connection_id_may_keep_existing_safe_punctuation(self):
        result = self.push_plan("team.alpha_v2")
        self.assertTrue(result["ok"])
        self.assertIn("team.alpha_v2", self.view()["connections"])

    def test_push_rejects_unknown_timezone(self):
        conn = plan_connection()
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.push_connection(self.url, self.token, "glm", conn, "sk", "Mars/Olympus")
        self.assertIn("timezone", str(ctx.exception))

    def test_push_accepts_a_fixed_offset_timezone(self):
        conn = plan_connection()
        result = remote_client.push_connection(self.url, self.token, "glm", conn, "sk-test", "UTC+08:00")
        self.assertTrue(result["ok"])
        now = self.warm._now(self.warm.config["connections"]["glm"])
        self.assertEqual(now.utcoffset(), timedelta(hours=8))

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
        warm2 = server.WarmServer(self.data_dir)  # simulates a restart
        self.assertFalse(warm2.claimed)
        self.assertEqual(warm2.config["connections"]["glm"]["activation"]["model"], "glm-4.7")
        self.assertEqual(warm2.state["connections"]["glm"]["lastResult"], "success")
        view = warm2.view()
        self.assertTrue(view["connections"]["glm"]["keyMissing"])

    def test_put_keys_restores_keyring(self):
        self.push_plan()
        warm2 = server.WarmServer(self.data_dir)
        self.assertTrue(warm2.view()["connections"]["glm"]["keyMissing"])
        remote_client.push_keys(self.url, self.token, {"glm": "sk-test"})
        self.assertFalse(self.view()["connections"]["glm"]["keyMissing"])

    def test_put_keys_restores_an_account_credential(self):
        conn = account_connection()
        with mock.patch("awewarm.server.shutil.which", return_value="/usr/local/bin/claude"):
            remote_client.push_connection(
                self.url, self.token, "claude", conn, '{"token": "c"}', TZ
            )
        warm2 = server.WarmServer(self.data_dir)  # restart: RAM credential wiped
        self.assertTrue(warm2.view()["connections"]["claude"]["keyMissing"])
        remote_client.push_keys(self.url, self.token, {"claude": '{"token": "c"}'})
        self.assertFalse(self.view()["connections"]["claude"]["keyMissing"])


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
        # The tick fires outside the server lock; a short per-request timeout
        # still bounds how long HTTP API calls can queue behind it.
        self.push_plan()
        self.tick(at("03:00", seconds=30))
        self.assertEqual(send.call_args.kwargs["timeout_seconds"], server.ACTIVATION_TIMEOUT_SECONDS)

    def _push_codex_account(self, credential='{"token": "c"}', fingerprint="abcd1234abcd1234",
                            cli_path="/usr/local/bin/codex"):
        conn = account_connection(fixed_at=("03:00",), days="every-day")
        conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
        with mock.patch("awewarm.server.shutil.which", return_value=cli_path):
            return remote_client.push_connection(
                self.url, self.token, "codex", conn, credential, TZ, fingerprint=fingerprint
            )

    def _push_native_codex_account(self):
        # The same codex account, but on a server with no codex installed.
        return self._push_codex_account(
            credential='{"tokens": {"access_token": "c", "account_id": "acc"}}',
            cli_path=None,
        )

    @mock.patch("awewarm.transport.send_native", return_value={"ok": True, "detail": ""})
    def test_native_account_ticks_without_the_cli_or_a_sandbox(self, native):
        self._push_native_codex_account()
        with mock.patch("awewarm.transport.send_activation") as cli_path:
            result = self.tick(at("03:00", seconds=30))
        self.assertEqual(result["fired"], 1)
        cli_path.assert_not_called()  # no CLI fires when none is installed
        self.assertFalse(self.warm.sandbox_root.exists())  # and no sandbox is built
        kwargs = native.call_args.kwargs
        self.assertEqual(kwargs.get("timeout_seconds"), server.ACTIVATION_TIMEOUT_SECONDS)
        self.assertEqual(native.call_args.args[0]["transport"]["kind"], "codex-cli")

    @mock.patch("awewarm.transport.send_native", return_value={"ok": False, "detail": "credential rejected"})
    def test_native_failure_lands_in_the_ladder_like_any_other(self, native):
        self._push_native_codex_account()
        result = self.tick(at("03:00", seconds=30))
        self.assertEqual(result["fired"], 1)
        cs = self.warm.state["connections"]["codex"]
        self.assertEqual(cs["lastResult"], "failure")
        self.assertIn("credential rejected", (cs.get("lastError") or ""))

    @mock.patch("awewarm.transport.send_native", return_value={"ok": True, "detail": ""})
    def test_run_now_uses_the_native_path_too(self, native):
        self._push_native_codex_account()
        result = self.warm.run_now("codex")
        self.assertTrue(result["ok"])
        native.assert_called_once()

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_delegated_account_fires_with_credential_and_sandbox(self, send):
        self._push_codex_account()
        result = self.tick(at("03:00", seconds=30))
        self.assertEqual(result["fired"], 1)
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs.get("credential"), '{"token": "c"}')
        self.assertEqual(kwargs.get("conn_id"), "codex")
        self.assertEqual(Path(kwargs.get("sandbox_root")), self.warm.sandbox_root)
        # a CLI activation runs at its own cap (120 s), never the 15 s HTTP one
        self.assertNotIn("timeout_seconds", kwargs)

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_missing_credential_holds_an_account_too(self, send):
        self._push_codex_account()
        self.warm.keys.clear()  # a restart wiped the RAM credential
        result = self.tick(at("03:00", seconds=30))
        self.assertEqual(result, {"fired": 0, "held": ["codex"]})
        send.assert_not_called()

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

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_explicit_run_fires_and_clears_auto_disabled(self, send):
        # Same contract as the local CLI: a successful manual run of one
        # connection clears the auto-disabled ladder.
        self.push_plan()
        self.warm.state["connections"]["glm"]["autoDisabledAt"] = "2026-08-20T00:00:00+08:00"
        result = remote_client.run_connection(self.url, self.token, "glm", allow_auto_disabled=True)
        self.assertTrue(result["ok"])
        send.assert_called_once()
        self.assertIsNone(self.warm.state["connections"]["glm"]["autoDisabledAt"])

    def test_bulk_run_still_refuses_auto_disabled(self):
        # `run` (no id) skips auto-disabled connections locally; the server
        # keeps that refusal for the bulk path.
        self.push_plan()
        self.warm.state["connections"]["glm"]["autoDisabledAt"] = "2026-08-20T00:00:00+08:00"
        result = remote_client.run_connection(self.url, self.token, "glm")
        self.assertFalse(result["ok"])
        self.assertIn("auto-disabled", result["detail"])

    def test_run_unknown_connection_is_404(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.run_connection(self.url, self.token, "nope")
        self.assertIn("404", str(ctx.exception))


class KeepaliveTests(ServerCase):
    def test_an_idle_keepalive_connection_is_closed(self):
        # HTTP/1.1 keep-alive parks a thread per idle connection and the
        # socket never times out by itself; the handler's read timeout is
        # what reclaims one (a proxy pooling origin connections is the
        # honest client this bites — urllib closes after every request).
        host, port = self.httpd.server_address
        with mock.patch.object(server._Handler, "timeout", 0.3):
            conn = http.client.HTTPConnection(host, port, timeout=5)
            self.addCleanup(conn.close)
            conn.request("GET", "/healthz")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)  # the answer arrives...
            response.read()
            self.assertEqual(conn.sock.recv(4096), b"")  # ...then silence ends the connection


class HttpInputTests(ServerCase):
    def test_json_body_must_be_an_object(self):
        host, port = self.httpd.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        conn.request(
            "POST", "/v1/claim", body="null",
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn(b"JSON object", response.read())

    def test_negative_content_length_is_rejected_and_closed(self):
        host, port = self.httpd.server_address
        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/v1/claim")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn(b"non-negative", response.read())
        self.assertEqual(response.getheader("Connection"), "close")

    def test_overload_returns_503(self):
        hold = threading.Event()
        ready = threading.Event()
        state = {"entered": 0}
        state_lock = threading.Lock()

        class HoldHandler(server._Handler):
            def do_POST(self):
                if self.path == "/v1/test/hold":
                    with state_lock:
                        state["entered"] += 1
                        if state["entered"] >= 2:
                            ready.set()
                    hold.wait(timeout=5)
                    self._send(200, {"ok": True})
                else:
                    super().do_POST()

        test_dir = self.data_dir / "overload"
        warm = server.WarmServer(test_dir, fixed_token=None)
        handler = type("BoundHandler", (HoldHandler,), {"warm": warm})
        httpd = server.BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), handler, max_request_threads=2,
        )
        thread = start_http_server(httpd)
        self.addCleanup(stop_http_server, httpd, thread)
        host, port = httpd.server_address

        def hold_request():
            conn = http.client.HTTPConnection(host, port, timeout=10)
            conn.request("POST", "/v1/test/hold", body="{}")
            conn.getresponse()

        t1 = threading.Thread(target=hold_request, daemon=True)
        t2 = threading.Thread(target=hold_request, daemon=True)
        t1.start()
        t2.start()

        ready.wait(timeout=3)

        conn = http.client.HTTPConnection(host, port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/v1/state")
        response = conn.getresponse()
        self.assertEqual(response.status, 503)

        hold.set()
        t1.join(timeout=3)
        t2.join(timeout=3)


class FakeCliAccountTests(ServerCase):
    """A delegated account, fired for real: the server materializes the pushed
    credential into a codex sandbox and runs the CLI with CODEX_HOME pointing
    at it (a stand-in script stands in for the codex binary)."""

    def _fake_cli(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        if sys.platform == "win32":
            # A stand-in the real shutil.which resolves (PATHEXT) and
            # CreateProcess executes: a batch file echoing %CODEX_HOME%.
            script = Path(tmp.name) / "codex.bat"
            script.write_text("@echo %CODEX_HOME%\r\n")
            return str(script)
        script = Path(tmp.name) / "codex"
        script.write_text('#!/bin/sh\necho "$CODEX_HOME"\n')
        script.chmod(0o755)
        return str(script)

    def test_run_fires_the_cli_with_the_materialized_credential(self):
        conn = account_connection(fixed_at=("03:00",), days="every-day")
        conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
        with mock.patch("awewarm.server.shutil.which", return_value=self._fake_cli()):
            remote_client.push_connection(
                self.url, self.token, "codex", conn, '{"token": "c"}', TZ,
                fingerprint="abcd1234abcd1234",
            )
        result = remote_client.run_connection(self.url, self.token, "codex")
        self.assertTrue(result["ok"], result)
        sandbox = Path(result["detail"])  # the script echoes $CODEX_HOME back
        self.assertEqual(sandbox, self.warm.sandbox_root / "codex")
        self.assertEqual((sandbox / "auth.json").read_text(), '{"token": "c"}')
        self.assertEqual(self.warm.state["connections"]["codex"]["lastResult"], "success")

    def test_run_reports_a_missing_credential_for_accounts(self):
        conn = account_connection()
        conn["transport"] = {"kind": "codex-cli", "baseUrl": None, "cliCommand": "codex"}
        with mock.patch("awewarm.server.shutil.which", return_value=self._fake_cli()):
            remote_client.push_connection(self.url, self.token, "codex", conn, '{"token": "c"}', TZ)
        self.warm.keys.clear()  # a restart wiped the RAM credential
        result = remote_client.run_connection(self.url, self.token, "codex")
        self.assertFalse(result["ok"])
        self.assertIn("credential not pushed yet", result["detail"])
        self.assertIn("awewarm remote push", result["detail"])
