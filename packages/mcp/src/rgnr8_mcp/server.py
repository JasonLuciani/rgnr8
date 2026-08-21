"""RGNR8 MCP server — the agent-native surface.

This is a *thin adapter*, not a second implementation: every MCP tool call is
turned into an internal `Request` against the same `WebApp` a browser hits, so it
flows through the identical auth + RBAC + tenant-scoping. An agent (Claude or
otherwise) presents the caller's credential (a session JWT or an ``rgk_`` partner
API key) and gets exactly the access that principal has — no privileged side-door.
The tool catalog covers the owner's core questions (where's my cash, this week's
briefing, what needs review, close status, ask-your-CFO) AND the agent-native
accounting pipeline — parse/ingest → match/categorize → post a balanced journal
→ report the statements → export the sealed package. Every write flows through
the same RBAC + scoped credential + tenant isolation as the browser, and the
ledger enforces balance and period locks, so an agent can run the books without a
privileged side-door.

The server speaks a minimal MCP-shaped JSON-RPC: ``tools/list`` and ``tools/call``.
Wiring it to a real stdio/SSE transport is a few lines around ``handle`` — the
protocol adapter is deliberately transport-free so it stays unit-testable.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from rgnr8_web import Request, Response, WebApp


@dataclass(frozen=True, slots=True)
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    # (app, tenant, args) -> Response
    invoke: Callable[[WebApp, str, dict[str, Any], dict[str, str]], Response]


def _tenant_prop(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    props: dict[str, Any] = {"tenant": {"type": "string", "description": "The business (tenant) id."}}
    if extra:
        props.update(extra)
    return props


def _get(path: str) -> Callable[[WebApp, str, dict[str, Any], dict[str, str]], Response]:
    def run(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
        return app.handle(Request("GET", path.format(tenant=tenant), headers))
    return run


def _cash(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    return app.handle(Request("GET", f"/api/{tenant}/today", headers))


def _briefing(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    return app.handle(Request("GET", f"/api/{tenant}/briefing.txt", headers))


def _review(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    return app.handle(Request("GET", f"/api/{tenant}/transactions", headers))


def _close(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    return app.handle(Request("GET", f"/api/{tenant}/close", headers))


def _ask(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    body = json.dumps({"text": str(args.get("question", ""))})
    h = {**headers, "content-type": "application/json"}
    return app.handle(Request("POST", f"/api/{tenant}/ask", h, body))


# --- agent-native accounting pipeline: parse → match/post → report -----------

def _parse_ingest(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    """PARSE step: ingest a batch of parsed feed transactions into the review
    queue. The ledger de-duplicates by transaction id."""
    payload = {"transactions": args.get("transactions", []),
               "source": str(args.get("source", "feed"))}
    h = {**headers, "content-type": "application/json"}
    return app.handle(Request("POST", f"/api/{tenant}/ingest", h, json.dumps(payload)))


def _categorize(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    """MATCH step: categorize / accept for-review transactions."""
    h = {**headers, "content-type": "application/json"}
    return app.handle(Request("POST", f"/api/{tenant}/transactions", h,
                              json.dumps(args.get("actions", args))))


def _post_entry(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    """POST step: post a balanced journal entry. Balance + period locks are
    enforced by the ledger; a scope-insufficient credential is refused."""
    payload = {"date": str(args.get("date", "")), "memo": str(args.get("memo", "")),
               "lines": args.get("lines", [])}
    h = {**headers, "content-type": "application/json"}
    return app.handle(Request("POST", f"/api/{tenant}/entries", h, json.dumps(payload)))


def _split_books(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    """SPLIT step: route one commingled statement into two+ balanced sets of books
    by rule (e.g. Business vs Personal, or per client). Read-only analysis — returns
    the proposed split, each book's balance, and a full audit trail; nothing posts."""
    payload = {"transactions": args.get("transactions", []),
               "default_book": str(args.get("default_book", "unassigned")),
               "rules": args.get("rules", [])}
    h = {**headers, "content-type": "application/json"}
    return app.handle(Request("POST", f"/api/{tenant}/split", h, json.dumps(payload)))


def _report(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    """REPORT step: the three statements for a period, every figure traceable to
    the posted ledger."""
    frm = str(args.get("from", "")).strip()
    to = str(args.get("to", "")).strip()
    qs = f"?from={frm}&to={to}" if frm and to else ""
    return app.handle(Request("GET", f"/api/{tenant}/statements{qs}", headers))


def _package(app: WebApp, tenant: str, args: dict[str, Any], headers: dict[str, str]) -> Response:
    """EXPORT step: the sealed, fingerprinted financial package for a period (the
    accountant handoff), verified on read."""
    period = str(args.get("period", "")).strip()
    return app.handle(Request("GET", f"/api/{tenant}/packages/{period}", headers))


TOOLS: list[Tool] = [
    Tool("rgnr8_cash_position",
         "The business's verified cash today, the minimum-cash floor, the 13-week trough, and whether/when it breaches.",
         {"type": "object", "properties": _tenant_prop(), "required": ["tenant"]}, _cash),
    Tool("rgnr8_weekly_briefing",
         "This week's owner briefing as text: status, headline, and the recommended action.",
         {"type": "object", "properties": _tenant_prop(), "required": ["tenant"]}, _briefing),
    Tool("rgnr8_review_transactions",
         "Bank register summary plus the for-review queue (uncategorized / unmatched lines needing action).",
         {"type": "object", "properties": _tenant_prop(), "required": ["tenant"]}, _review),
    Tool("rgnr8_close_status",
         "Month-end close status: tasks, progress, whether the period is sealed.",
         {"type": "object", "properties": _tenant_prop(), "required": ["tenant"]}, _close),
    Tool("rgnr8_ask_cfo",
         "Ask a plain-language question about the business's cash; answers are grounded in the forecast.",
         {"type": "object",
          "properties": _tenant_prop({"question": {"type": "string", "description": "The question to ask."}}),
          "required": ["tenant", "question"]}, _ask),
    # --- agent-native accounting pipeline (parse → match/post → report → export)
    Tool("rgnr8_ingest_transactions",
         "PARSE: ingest a batch of parsed feed transactions into the review queue "
         "(the ledger de-duplicates by id). Requires POST_JOURNAL scope.",
         {"type": "object",
          "properties": _tenant_prop({
              "transactions": {"type": "array", "description": "Parsed transactions to ingest.",
                               "items": {"type": "object"}},
              "source": {"type": "string", "description": "Feed source label (default 'feed')."}}),
          "required": ["tenant", "transactions"]}, _parse_ingest),
    Tool("rgnr8_categorize",
         "MATCH: categorize / accept for-review transactions. Requires CATEGORIZE_TXNS scope.",
         {"type": "object",
          "properties": _tenant_prop({
              "actions": {"type": "array", "description": "Categorize/accept actions.",
                          "items": {"type": "object"}}}),
          "required": ["tenant"]}, _categorize),
    Tool("rgnr8_post_entry",
         "POST: post a balanced journal entry {date, memo?, lines:[{code, side, amount_minor, dimensions?}]}. "
         "Balance and period locks are enforced by the ledger. Requires POST_JOURNAL scope.",
         {"type": "object",
          "properties": _tenant_prop({
              "date": {"type": "string", "description": "Entry date YYYY-MM-DD."},
              "memo": {"type": "string"},
              "lines": {"type": "array", "items": {"type": "object"},
                        "description": "At least two balanced lines."}}),
          "required": ["tenant", "date", "lines"]}, _post_entry),
    Tool("rgnr8_split_books",
         "SPLIT: route one commingled bank statement into two+ balanced sets of "
         "books by rule (e.g. Business vs Personal, or per client). Each rule "
         "{book, name, category?, description_regex?, counterparty?, amount_sign?} "
         "both routes a matching transaction to its book and categorizes it. "
         "Read-only: returns the proposed split, per-book balance, and an audit "
         "trail — nothing is posted. Requires VIEW_TRANSACTIONS scope.",
         {"type": "object",
          "properties": _tenant_prop({
              "transactions": {"type": "array", "description":
                               "Statement transactions {id, description, counterparty, amount_minor, currency?}.",
                               "items": {"type": "object"}},
              "default_book": {"type": "string",
                               "description": "Book for transactions no rule matches."},
              "rules": {"type": "array", "items": {"type": "object"},
                        "description": "Routing+categorization rules, first match wins."}}),
          "required": ["tenant", "transactions"]}, _split_books),
    Tool("rgnr8_report_statements",
         "REPORT: the P&L, balance sheet, and cash flow for a period — every figure "
         "traceable to the posted ledger. Requires VIEW_TRANSACTIONS scope.",
         {"type": "object",
          "properties": _tenant_prop({
              "from": {"type": "string", "description": "Period start YYYY-MM-DD."},
              "to": {"type": "string", "description": "Period end YYYY-MM-DD."}}),
          "required": ["tenant"]}, _report),
    Tool("rgnr8_financial_package",
         "EXPORT: the sealed, fingerprinted financial package for a period (the "
         "accountant handoff), verified on read. Requires VIEW_PACKAGE scope.",
         {"type": "object",
          "properties": _tenant_prop({
              "period": {"type": "string", "description": "Period YYYY-MM."}}),
          "required": ["tenant", "period"]}, _package),
]

_BY_NAME = {t.name: t for t in TOOLS}


class McpServer:
    """Wraps a `WebApp`. `handle` speaks tools/list + tools/call; each call carries
    the agent's credential (a JWT or an rgk_ API key) so RBAC applies unchanged."""

    def __init__(self, app: WebApp) -> None:
        self._app = app

    def list_tools(self) -> list[dict[str, Any]]:
        return [{"name": t.name, "description": t.description, "inputSchema": t.input_schema}
                for t in TOOLS]

    def call_tool(self, name: str, arguments: dict[str, Any], *, authorization: str) -> dict[str, Any]:
        tool = _BY_NAME.get(name)
        if tool is None:
            return self._error(f"unknown tool {name!r}")
        tenant = str(arguments.get("tenant", "")).strip()
        if not tenant:
            return self._error("`tenant` is required")
        headers = {"authorization": authorization}
        resp: Response = tool.invoke(self._app, tenant, arguments, headers)
        return self._result(resp)

    def handle(self, message: dict[str, Any], *, authorization: str = "") -> dict[str, Any]:
        """Minimal MCP JSON-RPC: {"method": "tools/list"|"tools/call", ...}."""
        method = message.get("method")
        rid = message.get("id")
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": self.list_tools()}}
        if method == "tools/call":
            params = message.get("params", {})
            if not isinstance(params, dict):
                params = {}
            # The credential is bound to the authenticated transport (the
            # `authorization` argument) ONLY. A body-supplied credential is
            # ignored — accepting `params["_authorization"]` was a confused-deputy
            # hole that let a caller override its own identity (pre-launch review).
            out = self.call_tool(str(params.get("name", "")),
                                 dict(params.get("arguments", {})), authorization=authorization)
            return {"jsonrpc": "2.0", "id": rid, "result": out}
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32601, "message": f"method not found: {method}"}}

    # --- MCP tool-result shaping ---------------------------------------------
    @staticmethod
    def _result(resp: Response) -> dict[str, Any]:
        is_error = not (200 <= resp.status < 300)
        return {"content": [{"type": "text", "text": resp.body}], "isError": is_error,
                "_status": resp.status}

    @staticmethod
    def _error(message: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": message}], "isError": True}
