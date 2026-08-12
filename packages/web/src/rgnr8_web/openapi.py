"""The OpenAPI 3.0 description of the RGNR8 API surface.

Generated from a single route table so the spec can't drift from what the app
actually serves (the same table doubles as human documentation). Partners fetch
it at ``GET /openapi.json``. Security is a bearer scheme (JWT session tokens or
``rgk_`` partner API keys); every ``/t/`` and ``/api/`` path is tenant-scoped and
permission-gated at runtime — the ``x-permission`` extension records which.
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_openapi", "API_VERSION"]

API_VERSION = "0.1.0"

# (method, path, summary, permission|None) — the authoritative surface.
_ROUTES: list[tuple[str, str, str, str | None]] = [
    ("get", "/health", "Liveness probe", None),
    ("get", "/ready", "Readiness + provisioning posture", None),
    ("get", "/openapi.json", "This API description", None),
    ("get", "/api/{tenant}/me", "Who am I in this business (identity + permissions)", None),
    ("get", "/api/{tenant}/today", "Today's verified cash snapshot", "view_cash"),
    ("get", "/api/{tenant}/briefing.txt", "This week's briefing (text)", "view_briefing"),
    ("post", "/api/{tenant}/ask", "Ask-your-CFO question", "ask_cfo"),
    ("post", "/api/{tenant}/assumptions", "Set an assumption (min-cash / payment)", "edit_assumptions"),
    ("get", "/api/{tenant}/decisions", "List recorded decisions", "view_cash"),
    ("post", "/api/{tenant}/decisions", "Record a decision", "record_decision"),
    ("get", "/api/{tenant}/transactions", "Bank register summary + for-review queue", "view_transactions"),
    ("post", "/api/{tenant}/transactions", "Categorize / accept a bank line", "categorize_transactions"),
    ("get", "/api/{tenant}/close", "Month-end close status", "manage_close"),
    ("post", "/api/{tenant}/close", "Advance a close task", "manage_close"),
    ("post", "/api/{tenant}/close/publish", "Seal the period", "publish_close"),
    ("get", "/api/{tenant}/packages", "List sealed financial packages", "view_package"),
    ("get", "/api/{tenant}/packages/{period}", "A sealed package (integrity-verified)", "view_package"),
    ("get", "/api/{tenant}/users", "List members", "manage_users"),
    ("post", "/api/{tenant}/users", "Add / change / remove a member", "manage_users"),
]


def _operation(method: str, summary: str, permission: str | None) -> dict[str, Any]:
    op: dict[str, Any] = {
        "summary": summary,
        "responses": {
            "200": {"description": "OK"},
            "401": {"description": "Missing or invalid credentials"},
            "403": {"description": "Insufficient role / not authorized for tenant"},
        },
    }
    if permission is not None:
        op["security"] = [{"bearerAuth": []}]
        op["x-permission"] = permission
    elif method != "get" or summary.startswith("Who am I"):
        op["security"] = [{"bearerAuth": []}]
    return op


def build_openapi(*, server_url: str = "/") -> dict[str, Any]:
    paths: dict[str, dict[str, Any]] = {}
    for method, path, summary, permission in _ROUTES:
        paths.setdefault(path, {})[method] = _operation(method, summary, permission)
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "RGNR8 Financial OS API",
            "version": API_VERSION,
            "description": "Owner-first financial operating platform. All /t/ and /api/ "
                           "routes are tenant-scoped and permission-gated (see x-permission).",
        },
        "servers": [{"url": server_url}],
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http", "scheme": "bearer", "bearerFormat": "JWT",
                    "description": "A session JWT or a partner API key (rgk_<id>_<secret>).",
                }
            }
        },
        "paths": paths,
    }
