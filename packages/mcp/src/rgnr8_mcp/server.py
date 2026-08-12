"""RGNR8 MCP server — the agent-native surface.

This is a *thin adapter*, not a second implementation: every MCP tool call is
turned into an internal `Request` against the same `WebApp` a browser hits, so it
flows through the identical auth + RBAC + tenant-scoping. An agent (Claude or
otherwise) presents the caller's credential (a session JWT or an ``rgk_`` partner
API key) and gets exactly the access that principal has — no privileged side-door.
The tool catalog covers the owner's core questions: where's my cash, this week's
briefing, what needs review, close status, and ask-your-CFO.

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
