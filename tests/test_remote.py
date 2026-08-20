"""The client half: pairing, auto re-claim after a server restart, and the
honest failure modes (unreachable server, unconfigured remote).
"""
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import IsolatedTestCase, plan_connection

from awewarm import remote


class LiveServerCase(IsolatedTestCase):
    """Real server + env-isolated local secrets, like a paired machine."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data_dir = Path(tmp.name) / "server"
        self.start_server()

    def start_server(self):
        from awewarm import server
        self.warm, self.httpd = server.make_server(self.data_dir, "127.0.0.1", 0)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def pair(self):
        """The `awewarm remote connect` flow: generate, claim, store."""
        self.token = remote.generate_token()
        remote.claim(self.url, self.token)
        remote.store_token(self.token)
        return {"remote": {"url": self.url, "tokenRef": "file:remote-token"}}


class PairingTests(LiveServerCase):
    def test_generated_tokens_match_server_rules(self):
        token = remote.generate_token()
        self.assertTrue(token.startswith("awt_"))
        self.assertGreaterEqual(len(token), 20)

    def test_token_round_trips_through_secrets_file(self):
        remote.store_token("awt_" + "a" * 40)
        self.assertEqual(remote.load_token(), "awt_" + "a" * 40)
        remote.delete_token()
        self.assertIsNone(remote.load_token())

    def test_unreachable_server_raises_remote_error(self):
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.healthz("http://127.0.0.1:9")
        self.assertIn("cannot reach", str(ctx.exception))

    def test_ensure_session_requires_a_configured_remote(self):
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.ensure_session({"remote": {}})
        self.assertIn("awewarm remote connect", str(ctx.exception))


class SessionTests(LiveServerCase):
    def test_ensure_session_returns_view(self):
        config = self.pair()
        view = remote.ensure_session(config)
        self.assertEqual(view["connections"], {})

    def test_ensure_session_reclaims_after_server_restart(self):
        config = self.pair()
        remote.push_connection(
            self.url, self.token, "glm", plan_connection(), "sk-test", "Asia/Shanghai"
        )
        self.httpd.shutdown()  # restart: RAM token and keys are gone
        self.start_server()  # new port, same data dir
        config["remote"]["url"] = self.url  # DNS would repoint the same name
        self.assertFalse(remote.healthz(self.url)["claimed"])
        view = remote.ensure_session(config)  # re-claims with the stored token
        self.assertTrue(view["connections"]["glm"]["keyMissing"])
        remote.push_keys(self.url, self.token, {"glm": "sk-test"})
        self.assertFalse(remote.ensure_session(config)["connections"]["glm"]["keyMissing"])

    def test_wrong_stored_token_is_reported_not_swallowed(self):
        config = self.pair()
        self.httpd.shutdown()
        self.start_server()
        remote.claim(self.url, remote.generate_token())  # someone else claimed it first
        config["remote"]["url"] = self.url
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.ensure_session(config)
        self.assertIn("401", str(ctx.exception))  # honest rejection, re-pair hint attached
