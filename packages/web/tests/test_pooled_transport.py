"""The keep-alive pooled ledger transport, exercised against a real local server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from rgnr8_web import PooledHttpTransport


class _Echo(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # keep tests quiet
        pass

    def _reply(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self.server.hits += 1  # type: ignore[attr-defined]
        payload = json.dumps({"path": self.path, "echo": body, "hits": self.server.hits}).encode()  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        self._reply()

    def do_POST(self) -> None:
        self._reply()


@pytest.fixture()
def server() -> "tuple[str, HTTPServer]":
    httpd = HTTPServer(("127.0.0.1", 0), _Echo)
    httpd.hits = 0  # type: ignore[attr-defined]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    host, port = httpd.server_address
    try:
        yield f"http://{host}:{port}", httpd
    finally:
        httpd.shutdown()


def test_pooled_transport_round_trips_get_and_post(server: "tuple[str, HTTPServer]") -> None:
    base, _ = server
    tx = PooledHttpTransport(base)
    r = tx.request("GET", "/t/acme/settings", "", {})
    assert r.status == 200
    assert r.body["path"] == "/t/acme/settings"

    w = tx.request("POST", "/t/acme/settings", '{"x":1}', {"content-type": "application/json"})
    assert w.status == 200
    assert w.body["echo"] == '{"x":1}'


def test_it_reuses_one_keep_alive_connection_across_calls(server: "tuple[str, HTTPServer]") -> None:
    base, _ = server
    tx = PooledHttpTransport(base)
    tx.request("GET", "/a", "", {})
    conn1 = tx._connection()  # type: ignore[attr-defined]
    tx.request("GET", "/b", "", {})
    conn2 = tx._connection()  # type: ignore[attr-defined]
    assert conn1 is conn2, "the same thread reuses one keep-alive connection"


def test_it_recovers_transparently_when_the_socket_was_dropped(server: "tuple[str, HTTPServer]") -> None:
    base, _ = server
    tx = PooledHttpTransport(base)
    tx.request("GET", "/first", "", {})
    # simulate the server having closed our idle keep-alive: close the socket out
    # from under the transport. The next call must re-establish and succeed.
    tx._connection().close()  # type: ignore[attr-defined]
    r = tx.request("GET", "/second", "", {})
    assert r.status == 200
    assert r.body["path"] == "/second"


def test_unreachable_server_is_a_503_not_a_crash() -> None:
    tx = PooledHttpTransport("http://127.0.0.1:1")  # nothing listens on port 1
    r = tx.request("GET", "/x", "", {})
    assert r.status == 503
    assert "unreachable" in str(r.body.get("error", ""))
