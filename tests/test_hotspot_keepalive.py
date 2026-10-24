import argparse
import http.client
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

from local_hotspot_proxy import (
    HotspotProxyHandler, HotspotProxyServer, serve_hotspot_requests,
)


class CountingServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args):
        self.accepts = 0
        self.posts = 0
        super().__init__(*args)

    def get_request(self):
        request = super().get_request()
        self.accepts += 1
        return request


class UpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        body = json.dumps({"path": self.path,
                           "authorization": self.headers.get("Authorization"),
                           "hop": self.headers.get("X-Local-Hop")}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close, X-Upstream-Hop")
        self.send_header("X-Upstream-Hop", "not-end-to-end")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_POST(self):
        self.server.posts += 1
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        # The operation happened, but its response was lost. A proxy must not
        # repeat a mutating request in an attempt to recover the connection.
        self.close_connection = True


class PersistentProxyTests(unittest.TestCase):
    def setUp(self):
        self.upstream = CountingServer(("127.0.0.1", 0), UpstreamHandler)
        self.proxy = CountingServer(("127.0.0.1", 0), HotspotProxyHandler)
        self.proxy.upstream = self.upstream.server_address
        self.proxy.allowed_hosts = {"codex-pocket.local"}
        for server in (self.upstream, self.proxy):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        self.client = http.client.HTTPConnection(*self.proxy.server_address, timeout=2)
        self.addCleanup(self.client.close)

    def request(self, path="/health", **headers):
        self.client.request("GET", path, headers={"Host": "codex-pocket.local",
                            "Authorization": "Bearer fixture", **headers})
        response = self.client.getresponse()
        payload = response.read()
        return response, json.loads(payload)

    def test_repeated_requests_reuse_client_connection_and_keep_auth(self):
        for index in range(3):
            response, payload = self.request(f"/health?probe={index}")
            self.assertEqual(response.status, 200)
            self.assertFalse(response.will_close)
            self.assertEqual(payload["authorization"], "Bearer fixture")
            self.assertEqual(payload["path"], f"/health?probe={index}")
        self.assertEqual(self.proxy.accepts, 1)

    def test_client_close_and_request_cap_are_respected(self):
        with patch("local_hotspot_proxy.MAX_KEEPALIVE_REQUESTS", 2):
            self.assertFalse(self.request()[0].will_close)
            self.assertTrue(self.request()[0].will_close)
        self.assertTrue(self.request(Connection="close")[0].will_close)

    def test_connection_specific_headers_are_not_forwarded(self):
        response, payload = self.request(Connection="keep-alive, X-Local-Hop",
                                         **{"X-Local-Hop": "not-end-to-end"})
        self.assertIsNone(payload["hop"])
        self.assertIsNone(response.getheader("X-Upstream-Hop"))

    def test_close_token_is_respected_inside_connection_list(self):
        response, payload = self.request(Connection="close, X-Local-Hop",
                                         **{"X-Local-Hop": "not-end-to-end"})
        self.assertTrue(response.will_close)
        self.assertIsNone(payload["hop"])

    def test_repeated_connection_headers_are_all_consumed(self):
        self.client.putrequest("GET", "/health", skip_host=True)
        self.client.putheader("Host", "codex-pocket.local")
        self.client.putheader("Connection", "keep-alive")
        self.client.putheader("Connection", "close, X-Local-Hop")
        self.client.putheader("X-Local-Hop", "not-end-to-end")
        self.client.endheaders()
        response = self.client.getresponse()
        self.assertTrue(response.will_close)
        self.assertIsNone(json.loads(response.read())["hop"])

    def test_lost_write_response_is_not_automatically_replayed(self):
        self.client.request("POST", "/send", body=b"fixture",
                            headers={"Host": "codex-pocket.local"})
        response = self.client.getresponse()
        self.assertEqual(response.status, 502)
        self.assertEqual(json.loads(response.read())["error"], "bridge_unavailable")
        self.assertEqual(self.upstream.posts, 1)

    def test_ambiguous_body_framing_closes_connection(self):
        self.client.putrequest("GET", "/health", skip_host=True)
        self.client.putheader("Host", "codex-pocket.local")
        self.client.putheader("Content-Length", "0")
        self.client.putheader("Content-Length", "1")
        self.client.endheaders()
        response = self.client.getresponse()
        self.assertEqual(response.status, 400)
        self.assertTrue(response.will_close)
        response.read()
        self.assertEqual(self.upstream.accepts, 0)


class HotspotLifecycleTests(unittest.TestCase):
    def test_network_checks_follow_elapsed_time_not_request_count(self):
        server = Mock()
        clock = iter([0.0, 0.1, 0.2, 0.9, 1.0])
        with patch("local_hotspot_proxy.time.monotonic", side_effect=lambda: next(clock)), \
                patch("local_hotspot_proxy.hotspot_matches", side_effect=[True, False]) as matches:
            serve_hotspot_requests(server, argparse.Namespace())
        self.assertEqual(server.handle_request.call_count, 4)
        self.assertEqual(matches.call_count, 2)

    def test_network_shutdown_retires_existing_keepalive_sockets(self):
        connection = Mock()
        server = object.__new__(HotspotProxyServer)
        server._connections = {connection}
        server._connections_lock = threading.Lock()
        with patch.object(ThreadingHTTPServer, "server_close"):
            server.server_close()
        connection.shutdown.assert_any_call(socket.SHUT_RDWR)
        connection.close.assert_called_once()
        self.assertFalse(server._connections)


if __name__ == "__main__":
    unittest.main()
