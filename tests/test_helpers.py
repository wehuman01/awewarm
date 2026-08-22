import socket
import tempfile
import unittest
from pathlib import Path

from helpers import start_http_server, stop_http_server

from awewarm import server


class HttpServerFixtureTests(unittest.TestCase):
    def test_stop_releases_the_listening_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            _engine, httpd = server.make_server(Path(tmp), "127.0.0.1", 0)
            thread = start_http_server(httpd)
            host, port = httpd.server_address

            stop_http_server(httpd, thread)

            replacement = socket.socket()
            try:
                replacement.bind((host, port))
            finally:
                replacement.close()
