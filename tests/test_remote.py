"""The client half: pairing, auto re-claim after a server restart, and the
honest failure modes (unreachable server, unconfigured remote, impostor 200s).
"""
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from helpers import IsolatedTestCase, plan_connection, start_http_server, stop_http_server

from awewarm import keystore, remote, server


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
        self.server_thread = start_http_server(self.httpd)
        self.addCleanup(stop_http_server, self.httpd, self.server_thread)
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
        remote.store_token("awt_" + "c" * 40)
        self.assertEqual(remote.load_token(), "awt_" + "c" * 40)

    def test_unreachable_server_raises_remote_error(self):
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.healthz("http://127.0.0.1:9")
        self.assertIn("cannot reach", str(ctx.exception))

    def test_ensure_session_requires_a_configured_remote(self):
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.ensure_session({"remote": {}})
        self.assertIn("awewarm remote connect", str(ctx.exception))

    def test_token_survives_a_connection_named_like_the_legacy_key(self):
        remote.store_token("awt_" + "a" * 40)
        keystore.store_api_key("remote-token", "sk-test")  # a legit connection id
        self.assertEqual(remote.load_token(), "awt_" + "a" * 40)

    def test_legacy_token_key_migrates_on_load(self):
        keystore.store_api_key(remote.LEGACY_TOKEN_SECRET_ID, "awt_" + "b" * 40)
        self.assertEqual(remote.load_token(), "awt_" + "b" * 40)
        self.assertIsNone(keystore.load_api_key(f"file:{remote.LEGACY_TOKEN_SECRET_ID}"))
        self.assertEqual(keystore.load_api_key(f"file:{remote.TOKEN_SECRET_ID}"), "awt_" + "b" * 40)


class RunTimeoutTests(LiveServerCase):
    def test_run_connection_waits_out_the_server_activation_cap(self):
        # The server fires the real request before answering a run (its cap is
        # ACTIVATION_TIMEOUT_SECONDS); a client that gives up sooner reports
        # "unreachable" while the request actually went out, inviting a retry.
        self.assertGreaterEqual(remote.RUN_TIMEOUT_SECONDS, server.ACTIVATION_TIMEOUT_SECONDS)
        with mock.patch.object(remote, "_request") as request:
            remote.run_connection(self.url, "awt_" + "t" * 40, "glm")
        self.assertEqual(request.call_args.kwargs["timeout"], remote.RUN_TIMEOUT_SECONDS)


class ImpostorTests(IsolatedTestCase):
    """A 200 answer that is not awewarm's protocol must fail as a RemoteError."""

    def test_non_json_answer_raises_remote_error_not_a_traceback(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html>welcome page</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        thread = start_http_server(httpd)
        self.addCleanup(stop_http_server, httpd, thread)
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.healthz(url)
        self.assertIn("not like an awewarm server", str(ctx.exception))

    def test_requests_identify_themselves_not_as_python_urllib(self):
        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen["user-agent"] = self.headers.get("User-Agent")
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        thread = start_http_server(httpd)
        self.addCleanup(stop_http_server, httpd, thread)
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        self.assertTrue(remote.healthz(url)["ok"])
        # Cloudflare Bot Fight Mode bans the default "Python-urllib/3.x" UA
        # outright (error 1010) — the client must name itself instead.
        self.assertTrue(seen["user-agent"].startswith("awewarm/"))
        self.assertNotIn("urllib", seen["user-agent"])

    def test_403_without_detail_hints_at_a_proxy_not_the_server(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"error code: 1010"
                self.send_response(403)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        httpd = HTTPServer(("127.0.0.1", 0), Handler)
        thread = start_http_server(httpd)
        self.addCleanup(stop_http_server, httpd, thread)
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.healthz(url)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertIn("proxy or WAF", str(ctx.exception))


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
        stop_http_server(self.httpd, self.server_thread)  # restart: RAM token and keys are gone
        self.start_server()  # new port, same data dir
        config["remote"]["url"] = self.url  # DNS would repoint the same name
        self.assertFalse(remote.healthz(self.url)["claimed"])
        view = remote.ensure_session(config)  # re-claims with the stored token
        self.assertTrue(view["connections"]["glm"]["keyMissing"])
        remote.push_keys(self.url, self.token, {"glm": "sk-test"})
        self.assertFalse(remote.ensure_session(config)["connections"]["glm"]["keyMissing"])

    def test_wrong_stored_token_is_reported_not_swallowed(self):
        config = self.pair()
        stop_http_server(self.httpd, self.server_thread)
        self.start_server()
        remote.claim(self.url, remote.generate_token())  # someone else claimed it first
        config["remote"]["url"] = self.url
        with self.assertRaises(remote.RemoteError) as ctx:
            remote.ensure_session(config)
        self.assertIn("401", str(ctx.exception))  # honest rejection, re-pair hint attached
