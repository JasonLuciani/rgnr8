"""The MCP server: tools/list + tools/call flow through the same RBAC as the API."""

import json
from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_mcp import McpServer
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "mcp-secret"
NOW = 1_760_000_000


def _server() -> McpServer:
    users = InMemoryUserDirectory()
    users.upsert_user(User("owner@acme.com", "owner@acme.com", "Ada"))
    users.set_membership("owner@acme.com", "acme", Role.OWNER)
    users.upsert_user(User("view@acme.com", "view@acme.com", "Val"))
    users.set_membership("view@acme.com", "acme", Role.VIEWER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW)
    app.add_tenant("acme", "Acme Co",
                   ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                  available=Money.from_decimal("80000.00"))),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return McpServer(app)


def _tok(sub: str) -> str:
    return "Bearer " + sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)


def test_tools_list_manifest() -> None:
    tools = _server().list_tools()
    names = {t["name"] for t in tools}
    assert {"rgnr8_cash_position", "rgnr8_weekly_briefing", "rgnr8_review_transactions",
            "rgnr8_close_status", "rgnr8_ask_cfo"} <= names
    # every tool advertises a JSON schema requiring a tenant
    assert all("tenant" in t["inputSchema"]["properties"] for t in tools)


def test_call_cash_position_returns_verified_snapshot() -> None:
    srv = _server()
    out = srv.call_tool("rgnr8_cash_position", {"tenant": "acme"}, authorization=_tok("owner@acme.com"))
    assert out["isError"] is False
    body = json.loads(out["content"][0]["text"])
    assert body["tenant"] == "acme" and "cash_today" in body


def test_ask_cfo_tool() -> None:
    srv = _server()
    out = srv.call_tool("rgnr8_ask_cfo", {"tenant": "acme", "question": "am I going to run short?"},
                        authorization=_tok("owner@acme.com"))
    assert out["isError"] is False
    assert "answer" in json.loads(out["content"][0]["text"])


def test_rbac_applies_through_mcp() -> None:
    # a viewer's token can read cash but the MCP layer enforces the same 403s
    srv = _server()
    ok = srv.call_tool("rgnr8_cash_position", {"tenant": "acme"}, authorization=_tok("view@acme.com"))
    assert ok["isError"] is False
    # wrong tenant → not authorized
    bad = srv.call_tool("rgnr8_cash_position", {"tenant": "other"}, authorization=_tok("view@acme.com"))
    assert bad["isError"] is True


def test_jsonrpc_handle_list_and_call() -> None:
    srv = _server()
    listed = srv.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "tools" in listed["result"]
    called = srv.handle({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": "rgnr8_close_status", "arguments": {"tenant": "acme"}},
    }, authorization=_tok("owner@acme.com"))
    assert called["result"]["isError"] is False

    # a body-supplied credential must be IGNORED (confused-deputy guard)
    forged = srv.handle({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": "rgnr8_cash_position", "arguments": {"tenant": "acme"},
                   "_authorization": _tok("owner@acme.com")},
    }, authorization="")
    assert forged["result"]["isError"] is True


def test_unknown_tool_is_error() -> None:
    srv = _server()
    out = srv.call_tool("nope", {"tenant": "acme"}, authorization=_tok("owner@acme.com"))
    assert out["isError"] is True
