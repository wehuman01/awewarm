"""Egress policy: awewarm's own requests go direct unless a proxyUrl is set.

Machine proxy environment variables must never capture the control channel
or warm-up requests; an explicit proxyUrl routes everything through it."""
import json
import os
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock
import urllib.request

from helpers import IsolatedTestCase, start_http_server, stop_http_server

from awewarm import net


class ProxyEnv(dict):
    """A hostile proxy environment: proxies point at a dead port, no_proxy
    exempts nothing — exactly the ambient state the policy must ignore."""

    def __init__(self):
        super().__init__(
            http_proxy="http://127.0.0.1:1",
            https_proxy="http://127.0.0.1:1",
            all_proxy="socks5://127.0.0.1:1",
            no_proxy="",
            HTTP_PROXY="http://127.0.0.1:1",
            HTTPS_PROXY="http://127.0.0.1:1",
        )


class ClientProxyTests(IsolatedTestCase):
    def test_absent_means_none(self):
        self.assertIsNone(net.client_proxy())
        self.assertIsNone(net.client_proxy({}))

    def test_reads_proxy_url_from_config(self):
        config = {"proxyUrl": "http://127.0.0.1:7890"}
        self.assertEqual(net.client_proxy(config), "http://127.0.0.1:7890")

    def test_malformed_values_fall_back_to_direct(self):
        for bad in ("socks5://127.0.0.1:7890", "127.0.0.1:7890", "", 7, ["http://x"], "http://"):
            self.assertIsNone(net.client_proxy({"proxyUrl": bad}), bad)

    def test_loads_from_the_config_file(self):
        path = os.environ["AWEWARM_CONFIG"]
        with open(path, "w") as handle:
            json.dump({"version": 3, "connections": {}, "proxyUrl": "http://10.0.0.8:3128"}, handle)
        self.assertEqual(net.client_proxy(), "http://10.0.0.8:3128")


class OpenerTests(unittest.TestCase):
    def test_direct_opener_carries_no_proxy_despite_environment(self):
        with mock.patch.dict(os.environ, ProxyEnv()):
            opener = net.opener()
            handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
            self.assertEqual(handlers, [])

    def test_explicit_proxy_covers_http_and_https(self):
        opener = net.opener("http://127.0.0.1:7890")
        handlers = [h for h in opener.handlers if isinstance(h, urllib.request.ProxyHandler)]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(
            handlers[0].proxies,
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


class WireTests(unittest.TestCase):
    """The egress policy proven on the wire, not just by handler wiring."""

    def setUp(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _Handler)
        self.thread = start_http_server(self.httpd)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/healthz"

    def tearDown(self):
        stop_http_server(self.httpd, self.thread)

    def test_direct_ignores_environment_proxies(self):
        with mock.patch.dict(os.environ, ProxyEnv()):
            with net.urlopen(urllib.request.Request(self.url), timeout=5) as response:
                self.assertEqual(response.read(), b"{}")

    def test_environment_proxies_would_have_broken_urlopen(self):
        # The regression this policy fixes: stock urlopen dies under the same
        # environment the direct opener above ignores.
        with mock.patch.dict(os.environ, ProxyEnv()):
            with self.assertRaises(urllib.error.URLError):
                urllib.request.urlopen(urllib.request.Request(self.url), timeout=5)

    def test_explicit_proxy_routes_through_it(self):
        # A proxy pointing at the test server rewrites the request's host, so
        # an unresolvable target still reaches it.
        request = urllib.request.Request("http://egress-policy-test.invalid/x")
        with net.urlopen(request, timeout=5, proxy=f"http://127.0.0.1:{self.httpd.server_address[1]}") as response:
            self.assertEqual(response.read(), b"{}")

    def test_dead_explicit_proxy_fails(self):
        # A non-local target, so an ambient no_proxy list cannot bypass the
        # proxy and turn this into a direct (succeeding) request.
        request = urllib.request.Request("http://egress-dead-proxy.invalid/x")
        with self.assertRaises(urllib.error.URLError):
            net.urlopen(request, timeout=3, proxy="http://127.0.0.1:1")


class HintTests(unittest.TestCase):
    def test_direct_hint_names_the_policy_and_the_fix(self):
        hint = net.egress_hint()
        self.assertIn("direct", hint)
        self.assertIn("awewarm config proxy", hint)

    def test_proxy_hint_never_contains_the_url(self):
        # A proxy URL can carry credentials; a failure hint must not echo it.
        hint = net.egress_hint("http://user:secret@127.0.0.1:7890")
        self.assertNotIn("secret", hint)
        self.assertNotIn("7890", hint)


if __name__ == "__main__":
    unittest.main()
