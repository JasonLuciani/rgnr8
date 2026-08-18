"""Uploading and serving receipts.

Two things are being defended. First, the bytes: a file that arrives as a
multipart body, travels through the app as a `str`, and comes back down must be
the same file — a receipt corrupted in transit looks fine in a list and is
useless when it matters. Second, the serving: an uploaded file is arbitrary
content, and rendering it in the app's own origin turns an upload into a script.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)
from rgnr8_web.multipart import boundary_of, parse_multipart, to_bytes

SECRET = "att-secret"
NOW = 1_760_000_000

# A PNG header followed by bytes that are not valid UTF-8 — the case a naive
# decode silently destroys.
BINARY = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + bytes(range(256))


def multipart_body(
    boundary: str, files: list[tuple[str, str, str, bytes]], fields: dict[str, str]
) -> str:
    """Build a browser-shaped multipart body, as the str the app would receive."""
    out = b""
    for name, value in fields.items():
        out += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()
    for name, filename, content_type, content in files:
        out += (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + content + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out.decode("utf-8", "surrogateescape")


# --- the parser --------------------------------------------------------------

def test_binary_survives_the_round_trip_through_a_str() -> None:
    body = multipart_body("X", [("file", "logo.png", "image/png", BINARY)], {})
    _fields, files = parse_multipart(body, "multipart/form-data; boundary=X")
    assert len(files) == 1
    assert files[0].content == BINARY, "every byte, including the invalid-UTF-8 ones"
    assert files[0].filename == "logo.png"
    assert files[0].content_type == "image/png"


def test_text_fields_and_several_files_come_through_together() -> None:
    body = multipart_body(
        "X",
        [("file", "a.pdf", "application/pdf", b"one"),
         ("file", "b.pdf", "application/pdf", b"two")],
        {"note": "Adobe renewal"},
    )
    fields, files = parse_multipart(body, "multipart/form-data; boundary=X")
    assert fields["note"] == "Adobe renewal"
    assert [f.filename for f in files] == ["a.pdf", "b.pdf"]


def test_an_untouched_file_input_is_not_an_empty_file() -> None:
    body = multipart_body("X", [("file", "", "application/octet-stream", b"")], {})
    _fields, files = parse_multipart(body, "multipart/form-data; boundary=X")
    assert files == []


def test_a_body_this_parser_does_not_understand_is_refused_not_guessed() -> None:
    import pytest

    from rgnr8_web.multipart import MultipartError

    with pytest.raises(MultipartError):
        parse_multipart("a=1", "application/x-www-form-urlencoded")
    with pytest.raises(MultipartError):
        parse_multipart("nothing here", "multipart/form-data; boundary=X")
    with pytest.raises(MultipartError):
        parse_multipart(
            multipart_body("X", [("file", "a", "text/plain", b"x" * 100)], {}),
            "multipart/form-data; boundary=X", max_bytes=10,
        )


def test_the_boundary_is_read_including_the_quoted_form() -> None:
    assert boundary_of('multipart/form-data; boundary="abc123"') == "abc123"
    assert boundary_of("multipart/form-data; boundary=abc123") == "abc123"
    assert boundary_of("application/json") == ""


def test_to_bytes_is_the_exact_inverse_of_the_decode() -> None:
    assert to_bytes(BINARY.decode("utf-8", "surrogateescape")) == BINARY


# --- through the app ---------------------------------------------------------

class FakeTransport:
    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path, body))
        status, payload = self.routes.get(
            f"{method} {path.split('?')[0]}", (404, {"error": "not found"})
        )
        return LedgerResponse(status, payload)


import base64

ATTACHMENTS = {
    "attachments": [{
        "id": "att-1", "subject_kind": "feed", "subject_id": "bk-1",
        "filename": "receipt.pdf", "content_type": "application/pdf",
        "bytes": 264, "uploaded_at": "2026-08-21T00:00:00Z", "note": "Adobe renewal",
    }],
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/attachments": (200, ATTACHMENTS),
    "POST /t/acme/attachments": (201, {"attachment": ATTACHMENTS["attachments"][0]}),
    "DELETE /t/acme/attachments/att-1": (200, {"removed": "att-1"}),
    "GET /t/acme/attachments/att-1/content": (200, {
        "filename": "receipt.pdf", "content_type": "application/pdf",
        "content_base64": base64.b64encode(BINARY).decode("ascii"),
    }),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("0.00"))
    )


def _app(
    routes: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> tuple[WebApp, FakeTransport, InMemoryAuditLog]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-view", "acme", Role.VIEWER)
    audit = InMemoryAuditLog()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
                 users=users, audit=audit)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    transport = FakeTransport(routes if routes is not None else dict(DEFAULT_ROUTES))
    app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport, audit


def _req(app: WebApp, path: str, sub: str = "u-owner",
         method: str = "GET", body: str = "", content_type: str = "") -> Any:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    if content_type:
        headers["content-type"] = content_type
    return app.handle(Request(method, path, headers, body))


def _upload(app: WebApp, sub: str = "u-owner", content: bytes = BINARY) -> Any:
    body = multipart_body(
        "BOUNDARY", [("file", "receipt.pdf", "application/pdf", content)],
        {"note": "Adobe renewal"},
    )
    return _req(app, "/t/acme/files/feed/bk-1", sub, "POST", body,
                "multipart/form-data; boundary=BOUNDARY")


def test_an_uploaded_file_reaches_the_ledger_byte_for_byte() -> None:
    app, transport, audit = _app()
    r = _upload(app)
    assert r.status == 302
    sent = json.loads([c for c in transport.calls if c[0] == "POST"][0][2])
    assert sent["subject_kind"] == "feed" and sent["subject_id"] == "bk-1"
    assert sent["filename"] == "receipt.pdf"
    assert sent["note"] == "Adobe renewal"
    assert base64.b64decode(sent["content_base64"]) == BINARY
    assert any(e.action == "attachment.added" for e in audit.events(tenant_id="acme"))


def test_uploading_nothing_is_reported_and_never_sent() -> None:
    app, transport, _a = _app()
    body = multipart_body("B", [], {"note": "x"})
    r = _req(app, "/t/acme/files/feed/bk-1", method="POST", body=body,
             content_type="multipart/form-data; boundary=B")
    assert "Choose%20a%20file" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_the_ledgers_refusal_reaches_the_owner() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/attachments"] = (
        400, {"error": "the file is 9.0 MB — the limit is 8 MB"}
    )
    app, _t, audit = _app(routes)
    r = _upload(app)
    assert "the%20limit%20is%208%20MB" in str(r.headers.get("Location", ""))
    assert not [e for e in audit.events(tenant_id="acme") if e.action == "attachment.added"]


def test_downloading_returns_the_exact_bytes_and_never_renders_them() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/files/att-1/download")
    assert r.status == 200
    assert r.body == BINARY, "the bytes come back unchanged"
    headers = r.headers
    # an uploaded file rendered in the app's own origin is a script, not a receipt
    assert headers["Content-Disposition"].startswith("attachment;")
    assert 'filename="receipt.pdf"' in headers["Content-Disposition"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_a_missing_file_is_a_404_not_an_empty_download() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/attachments/att-1/content"] = (404, {"error": "unknown attachment"})
    app, _t, _a = _app(routes)
    assert _req(app, "/t/acme/files/att-1/download").status == 404


def test_the_list_shows_what_is_attached_and_links_back_to_the_thing() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/files/feed/bk-1")
    assert r.status == 200
    assert "Attached to bank transaction bk-1" in r.body
    assert "receipt.pdf" in r.body
    assert "Adobe renewal" in r.body
    assert "/t/acme/files/att-1/download" in r.body
    assert "/t/acme/inbox" in r.body, "back goes to the queue the line lives in"


def test_an_empty_list_says_so() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/attachments"] = (200, {"attachments": []})
    app, _t, _a = _app(routes)
    assert "Nothing attached yet" in _req(app, "/t/acme/files/bill/BILL-1").body


def test_one_can_be_removed() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/files/feed/bk-1/att-1/delete", method="POST")
    assert r.status == 302
    assert any(c[0] == "DELETE" for c in transport.calls)
    assert any(e.action == "attachment.removed" for e in audit.events(tenant_id="acme"))


def test_a_viewer_can_see_and_download_but_not_upload_or_remove() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/files/feed/bk-1", sub="u-view")
    assert page.status == 200
    assert "receipt.pdf" in page.body
    assert "Attach a receipt" not in page.body
    assert _req(app, "/t/acme/files/att-1/download", sub="u-view").status == 200

    assert _upload(app, sub="u-view").status == 403
    assert _req(app, "/t/acme/files/feed/bk-1/att-1/delete", sub="u-view",
                method="POST").status == 403
    assert not [c for c in transport.calls if c[0] in ("POST", "DELETE")]
