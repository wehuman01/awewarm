"""awewarm serve --hub: many users behind one hub process.

Covers the invite→token pairing flow, tenant isolation (two users may both
delegate a connection named glm), quotas, the restart story (pairings persist
via hashed tokens while API keys stay RAM-only), revocation, and the client
`remote connect` flow against a live hub.
"""
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from click.testing import CliRunner
from helpers import IsolatedTestCase, plan_connection

import awewarm
from awewarm import remote as remote_client
from awewarm import server, transport
from awewarm.cli import cli

TZ = "Asia/Shanghai"
RUNNER = CliRunner()


def invoke(*args, **kwargs):
    kwargs.setdefault("prog_name", "awewarm")
    return RUNNER.invoke(cli, *args, **kwargs)


def at(hhmm, seconds=0):
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 8, 20, hour, minute, seconds, tzinfo=ZoneInfo(TZ))


class HubCase(unittest.TestCase):
    """A real Hub on an ephemeral port, plus its URL."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "hub"
        self.make_hub()

    def make_hub(self, max_tenants=50, max_conns_per_tenant=5):
        self.hub, self.httpd = server.make_server(
            self.data_dir, "127.0.0.1", 0, hub=True,
            max_tenants=max_tenants, max_conns_per_tenant=max_conns_per_tenant,
        )
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def join(self, note=None):
        """The user flow: mint an invite, burn it for a personal token."""
        invite = self.hub.mint_invite(note)
        joined = remote_client.join(self.url, invite)
        return joined["token"], joined["tenantId"]

    def push_plan(self, token, conn_id="glm", fixed_at=("03:00",)):
        conn = plan_connection(fixed_at=fixed_at, days="every-day")
        return remote_client.push_connection(self.url, token, conn_id, conn, "sk-test", TZ)

    def registry(self):
        return json.loads((self.data_dir / "tenants.json").read_text())


class PairingTests(HubCase):
    def test_healthz_advertises_hub_mode(self):
        health = remote_client.healthz(self.url)
        self.assertTrue(health["ok"])
        self.assertTrue(health["hub"])
        self.assertTrue(health["claimed"])  # auth is per-token; nothing to pre-claim

    def test_join_returns_a_working_token(self):
        token, tenant_id = self.join("alice")
        self.assertTrue(tenant_id.startswith("t_"))
        view = remote_client.fetch_state(self.url, token)
        self.assertEqual(view["connections"], {})
        self.assertEqual(view["tenant"], tenant_id)

    def test_invite_is_single_use(self):
        invite = self.hub.mint_invite("alice")
        remote_client.join(self.url, invite)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(self.url, invite)
        self.assertIn("403", str(ctx.exception))

    def test_expired_invite_is_refused_and_burned(self):
        invite = self.hub.mint_invite("alice")
        digest = server._hash_secret(invite)
        from awewarm import schedule
        self.hub.registry["invites"][digest]["expiresAt"] = schedule.iso(
            datetime.now().astimezone()
        )
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(self.url, invite)
        self.assertIn("expired", str(ctx.exception))
        self.assertNotIn(digest, self.registry()["invites"])

    def test_malformed_invite_is_a_400(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(self.url, "not-an-invite")
        self.assertIn("400", str(ctx.exception))

    def test_claim_flow_does_not_exist_on_a_hub(self):
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.claim(self.url, "awt_" + "t" * 40)
        self.assertIn("invite", str(ctx.exception))

    def test_join_does_not_exist_on_a_single_tenant_server(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        warm, httpd = server.make_server(Path(tmp.name) / "single", "127.0.0.1", 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.join(url, "awi_" + "i" * 30)
        self.assertIn("404", str(ctx.exception))

    def test_registry_stores_hashes_never_plaintext(self):
        token, _ = self.join("alice")
        on_disk = (self.data_dir / "tenants.json").read_text()
        self.assertNotIn(token, on_disk)
        for record in self.registry()["tenants"].values():
            self.assertEqual(len(record["tokenHash"]), 64)  # sha256 hex
        self.assertFalse(list(self.data_dir.rglob("secrets.json")))

    def test_tenant_cap(self):
        self.make_hub(max_tenants=1)
        self.join("alice")
        with self.assertRaises(remote_client.RemoteError) as ctx:
            self.join("bob")
        self.assertIn("full", str(ctx.exception))


class IsolationTests(HubCase):
    def test_same_connection_id_in_two_tenants_stays_separate(self):
        alice, alice_id = self.join("alice")
        bob, bob_id = self.join("bob")
        self.push_plan(alice)
        self.push_plan(bob)
        alice_view = remote_client.fetch_state(self.url, alice)
        bob_view = remote_client.fetch_state(self.url, bob)
        self.assertEqual(list(alice_view["connections"]), ["glm"])
        self.assertEqual(list(bob_view["connections"]), ["glm"])
        self.assertEqual(alice_view["tenant"], alice_id)
        self.assertEqual(bob_view["tenant"], bob_id)
        # each workspace is its own directory; deleting one leaves the other
        remote_client.delete_connection(self.url, alice, "glm")
        self.assertEqual(remote_client.fetch_state(self.url, bob)["connections"]["glm"]["config"]["timezone"], TZ)

    def test_one_tenant_cannot_touch_another(self):
        alice, _ = self.join("alice")
        bob, _ = self.join("bob")
        self.push_plan(bob)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.delete_connection(self.url, alice, "glm")
        self.assertIn("404", str(ctx.exception))

    def test_connection_quota_per_tenant(self):
        self.make_hub(max_conns_per_tenant=1)
        alice, _ = self.join("alice")
        bob, _ = self.join("bob")
        self.push_plan(alice, "glm")
        self.push_plan(bob, "glm")  # quotas are per tenant, not global
        with self.assertRaises(remote_client.RemoteError) as ctx:
            self.push_plan(alice, "kimi")
        self.assertIn("quota", str(ctx.exception))
        self.push_plan(alice, "glm")  # replacing an existing id never counts


class LifecycleTests(HubCase):
    def test_pairing_survives_a_restart_keys_do_not(self):
        token, _ = self.join("alice")
        self.push_plan(token)
        self.httpd.shutdown()
        self.make_hub()  # same data dir, fresh process
        view = remote_client.fetch_state(self.url, token)  # no re-claim needed
        self.assertTrue(view["connections"]["glm"]["keyMissing"])
        remote_client.push_keys(self.url, token, {"glm": "sk-test"})
        self.assertFalse(remote_client.fetch_state(self.url, token)["connections"]["glm"]["keyMissing"])

    def test_revoke_kills_the_token_and_the_workspace(self):
        token, tenant_id = self.join("alice")
        self.push_plan(token)
        self.hub.revoke(tenant_id)
        self.assertFalse((self.data_dir / "tenants" / tenant_id).exists())
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token)
        self.assertIn("401", str(ctx.exception))
        self.assertNotIn(tenant_id, self.registry()["tenants"])

    def test_release_keeps_the_pairing(self):
        token, _ = self.join("alice")
        result = remote_client.release(self.url, token)
        self.assertFalse(result["released"])  # capacity is the operator's call
        self.assertEqual(remote_client.fetch_state(self.url, token)["connections"], {})

    def test_rate_limit_blocks_a_looping_client(self):
        token, _ = self.join("alice")
        for _ in range(server.HUB_RATE_PER_MINUTE):
            remote_client.fetch_state(self.url, token)
        with self.assertRaises(remote_client.RemoteError) as ctx:
            remote_client.fetch_state(self.url, token)
        self.assertIn("429", str(ctx.exception))


class UsageTests(HubCase):
    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_tick_counts_activations_per_tenant(self, send):
        alice, alice_id = self.join("alice")
        bob, bob_id = self.join("bob")
        self.push_plan(alice)
        self.push_plan(bob, fixed_at=("04:00",))
        result = self.hub.tick(now_fn=lambda conn: at("03:00", seconds=30))
        self.assertEqual(result["fired"], 1)  # only alice's 03:00 slot was due
        usage = self.registry()["tenants"][alice_id]["usage"]
        self.assertEqual(usage["today"], 1)
        self.assertEqual(usage["total"], 1)
        self.assertEqual(self.registry()["tenants"][bob_id]["usage"]["total"], 0)

    @mock.patch("awewarm.transport.send_activation", return_value={"ok": True, "detail": ""})
    def test_manual_run_counts_as_usage(self, send):
        token, tenant_id = self.join("alice")
        self.push_plan(token)
        self.assertTrue(remote_client.run_connection(self.url, token, "glm", allow_auto_disabled=True)["ok"])
        self.assertEqual(self.registry()["tenants"][tenant_id]["usage"]["total"], 1)


class HubCliTests(IsolatedTestCase):
    """The operator side: hub invite / list / revoke against a data dir."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = str(Path(tmp.name) / "hub")
        self.dir_opt = ["--data-dir", self.data_dir]

    def test_invite_mints_one_use_code(self):
        result = invoke(["hub", "invite"] + self.dir_opt + ["--note", "alice"])
        self.assertEqual(result.exit_code, 0)
        code = next(line.strip() for line in result.output.splitlines() if line.strip().startswith("awi_"))
        self.assertTrue(server.INVITE_RE.match(code))
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertIn(server._hash_secret(code), registry["invites"])
        self.assertEqual(registry["invites"][server._hash_secret(code)]["note"], "alice")
        self.assertNotIn(code, Path(self.data_dir, "tenants.json").read_text())

    def test_list_shows_tenants_and_totals(self):
        engine = server.Hub(self.data_dir)
        invite = engine.mint_invite("alice")
        engine.join(invite)  # a tenant with no connections yet
        result = invoke(["hub", "list"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("alice", result.output)
        self.assertIn("TENANT", result.output)
        self.assertIn("LAST SEEN", result.output)
        self.assertNotIn("https://", result.output)  # the API table needs --api

    def test_list_api_shows_each_connection_endpoint(self):
        engine = server.Hub(self.data_dir)
        joined = engine.join(engine.mint_invite("alice"))
        engine.tenants[joined["tenantId"]].warm.put_connection("glm", {
            "connection": plan_connection(), "apiKey": "sk-test", "timezone": TZ,
        })
        result = invoke(["hub", "list"] + self.dir_opt + ["--api"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("API", result.output)
        self.assertIn("glm", result.output)
        self.assertIn("https://open.bigmodel.cn/api/anthropic", result.output)
        self.assertIn("anthropic-messages", result.output)
        self.assertIn("connected", result.output)

    def test_list_json_is_redacted(self):
        engine = server.Hub(self.data_dir)
        joined = engine.join(engine.mint_invite("alice"))
        result = invoke(["hub", "list"] + self.dir_opt + ["--json"])
        self.assertEqual(result.exit_code, 0)
        rows = json.loads(result.output)
        self.assertEqual(rows[0]["note"], "alice")
        self.assertNotIn(joined["token"], result.output)

    def test_revoke_removes_the_tenant(self):
        engine = server.Hub(self.data_dir)
        joined = engine.join(engine.mint_invite("alice"))
        result = invoke(["hub", "revoke", joined["tenantId"]] + self.dir_opt, input="y\n")
        self.assertEqual(result.exit_code, 0)
        registry = json.loads(Path(self.data_dir, "tenants.json").read_text())
        self.assertNotIn(joined["tenantId"], registry["tenants"])

    def test_list_with_no_tenants_still_shows_pending_invites(self):
        engine = server.Hub(self.data_dir)
        engine.mint_invite("alice")
        result = invoke(["hub", "list"] + self.dir_opt)
        self.assertEqual(result.exit_code, 0)
        self.assertIn("No tenants paired yet", result.output)
        self.assertIn("1 invite(s) minted and unused", result.output)
        self.assertIn("shown only once", result.output)

    def test_revoke_unknown_tenant_lists_known_ones(self):
        result = invoke(["hub", "revoke", "t_nope"] + self.dir_opt, input="y\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("no such tenant", result.output)


class ConnectHubTests(IsolatedTestCase):
    """The user side: `remote connect` against a live hub."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "hub"
        self.hub, self.httpd = server.make_server(self.data_dir, "127.0.0.1", 0, hub=True)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def test_connect_with_invite_stores_a_working_token(self):
        code = self.hub.mint_invite("alice")
        result = invoke(["remote", "connect", self.url, "--invite", code])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Joined", result.output)
        from awewarm import config as cfg
        self.assertEqual(cfg.load_config()["remote"]["url"], self.url)
        view = remote_client.ensure_session(cfg.load_config())  # no invite needed anymore
        self.assertTrue(view["tenant"].startswith("t_"))

    def test_connect_prompts_for_the_invite_when_not_given(self):
        code = self.hub.mint_invite("alice")
        result = invoke(["remote", "connect", self.url], input=code + "\n")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("Joined", result.output)

    def test_connect_reuses_a_working_stored_token(self):
        joined = remote_client.join(self.url, self.hub.mint_invite("alice"))
        remote_client.store_token(joined["token"])
        self.hub.mint_invite("bob")  # must stay unused: no invite was needed
        result = invoke(["remote", "connect", self.url])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("already paired", result.output)
        self.assertEqual(len(self.hub.registry["invites"]), 1)

    def test_connect_recovers_from_a_revoked_stored_token(self):
        joined = remote_client.join(self.url, self.hub.mint_invite("alice"))
        remote_client.store_token(joined["token"])
        self.hub.revoke(joined["tenantId"])
        fresh = self.hub.mint_invite("alice")
        result = invoke(["remote", "connect", self.url, "--invite", fresh])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("rejected", result.output)  # the old token's 401 was reported
        self.assertIn("Joined", result.output)
