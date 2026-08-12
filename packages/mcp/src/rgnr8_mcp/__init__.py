"""RGNR8 MCP server — expose the owner surface to agents over the same RBAC-gated
web API. A thin protocol adapter: `McpServer(app)` turns tools/list + tools/call
into internal `Request`s against the `WebApp`, so an agent gets exactly the access
its presented credential (JWT or `rgk_` API key) grants — nothing more.
"""

from __future__ import annotations

from .server import McpServer, Tool, TOOLS

__version__ = "0.1.0"

__all__ = ["McpServer", "Tool", "TOOLS"]
