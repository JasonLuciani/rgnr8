import json
import threading
import urllib.error
import urllib.request

from factory import app_with_two_tenants
from rgnr8_web import serve


def _request(port: int, path: str, token: str | None = None, method: str = "GET", body: str | None = None):
    url = f"http://127.0.0.1:{port}{path}"
    data = body.encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_live_server_round_trip() -> None:
    httpd = serve(app_with_two_tenants(), port=0)  # ephemeral port
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _request(port, "/health")
        assert status == 200 and json.loads(body)["status"] == "ok"

        status, _ = _request(port, "/t/bright")  # no token
        assert status == 401

        status, body = _request(port, "/api/bright/today", token="tok-bright")
        assert status == 200
        assert json.loads(body)["tenant"] == "bright"

        status, body = _request(
            port, "/api/bright/ask", token="tok-bright", method="POST",
            body=json.dumps({"text": "what's my biggest cost?"}),
        )
        assert status == 200 and json.loads(body)["supported"] is True

        status, _ = _request(port, "/api/acme/today", token="tok-bright")
        assert status == 403
    finally:
        httpd.shutdown()
        thread.join(timeout=5)
