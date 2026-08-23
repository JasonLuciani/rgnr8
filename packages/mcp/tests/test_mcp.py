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


# --- agent-native accounting pipeline (A-1) ----------------------------------

class _FakeLedgerTransport:
    """Records requests and replays a canned OK response, so we can assert the
    MCP pipeline tools reach the right ledger endpoints with the right payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: object) -> object:
        from rgnr8_web.ledger_client import LedgerResponse
        self.calls.append((method, path, body))
        return LedgerResponse(200, {"ok": True, "path": path})


def _server_with_ledger() -> tuple[McpServer, "_FakeLedgerTransport"]:
    from rgnr8_web.ledger_client import LedgerClient
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
    fake = _FakeLedgerTransport()
    app.set_ledger(LedgerClient(fake))
    return McpServer(app), fake


def test_pipeline_tools_are_in_the_manifest() -> None:
    names = {t["name"] for t in _server().list_tools()}
    assert {"rgnr8_ingest_transactions", "rgnr8_categorize", "rgnr8_post_entry",
            "rgnr8_report_statements", "rgnr8_financial_package"} <= names


def test_post_entry_tool_reaches_the_ledger() -> None:
    srv, fake = _server_with_ledger()
    out = srv.call_tool("rgnr8_post_entry", {
        "tenant": "acme", "date": "2026-08-05", "memo": "sale",
        "lines": [
            {"code": "1000", "side": "DEBIT", "amount_minor": "500000"},
            {"code": "4000", "side": "CREDIT", "amount_minor": "500000"},
        ],
    }, authorization=_tok("owner@acme.com"))
    assert out["isError"] is False
    method, path, body = fake.calls[-1]
    assert method == "POST" and path == "/t/acme/entries"
    assert json.loads(body)["lines"][0]["code"] == "1000"


def test_report_statements_tool_reaches_the_ledger() -> None:
    srv, fake = _server_with_ledger()
    out = srv.call_tool("rgnr8_report_statements",
                        {"tenant": "acme", "from": "2026-08-01", "to": "2026-08-31"},
                        authorization=_tok("owner@acme.com"))
    assert out["isError"] is False
    method, path, _ = fake.calls[-1]
    assert method == "GET" and path.startswith("/t/acme/statements")


def test_split_books_tool_is_in_the_manifest() -> None:
    assert "rgnr8_split_books" in {t["name"] for t in _server().list_tools()}


def test_split_books_tool_routes_into_two_balanced_books() -> None:
    # No ledger needed — the split is pure in-process analysis.
    srv = _server()
    out = srv.call_tool("rgnr8_split_books", {
        "tenant": "acme",
        "default_book": "personal",
        "rules": [
            {"book": "business", "name": "payroll", "category": "Wages", "counterparty": "GUSTO"},
            {"book": "business", "name": "sales", "category": "Sales", "description_regex": "STRIPE"},
        ],
        "transactions": [
            {"id": "t1", "description": "STRIPE PAYOUT", "counterparty": "STRIPE", "amount_minor": "250000"},
            {"id": "t2", "description": "GUSTO PAYROLL", "counterparty": "GUSTO", "amount_minor": "-120000"},
            {"id": "t3", "description": "WHOLE FOODS", "counterparty": "WHOLEFOODS", "amount_minor": "-8000"},
        ],
    }, authorization=_tok("owner@acme.com"))
    assert out["isError"] is False
    body = json.loads(out["content"][0]["text"])
    assert body["all_balanced"] is True
    books = {b["book"]: b for b in body["books"]}
    assert books["business"]["entry_count"] == 2
    assert books["personal"]["entry_count"] == 1
    # the split is auditable: every transaction shows where it landed
    landed = {row["txn_id"]: row["book"] for row in body["audit"]}
    assert landed == {"t1": "business", "t2": "business", "t3": "personal"}


def test_pipeline_respects_rbac_scopes() -> None:
    # A viewer lacks POST_JOURNAL, so the post-entry tool is refused BEFORE it
    # ever reaches the ledger — the agent surface inherits the same RBAC.
    srv, fake = _server_with_ledger()
    out = srv.call_tool("rgnr8_post_entry", {
        "tenant": "acme", "date": "2026-08-05",
        "lines": [
            {"code": "1000", "side": "DEBIT", "amount_minor": "1"},
            {"code": "4000", "side": "CREDIT", "amount_minor": "1"},
        ],
    }, authorization=_tok("view@acme.com"))
    assert out["isError"] is True
    assert not any(p == "/t/acme/entries" for _, p, _ in fake.calls)
