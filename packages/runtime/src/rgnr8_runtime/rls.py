"""Per-request tenant context for row-level security.

The Python-owned tables (tenant state, memberships, fleet, subscriptions) carry
FORCE'd RLS policies keyed on the `app.tenant_id` GUC. A policy only isolates if
that GUC is actually set before the query — otherwise, on Postgres, legitimate
reads return nothing (and, worse, the isolation the policy promises never engages
because the WHERE clause is doing all the work). `apply_tenant` sets it,
transaction-locally, so it can't leak across pooled connections; it is a no-op on
sqlite, which has neither `set_config` nor RLS.
"""

from __future__ import annotations

from typing import Protocol


class _Cursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...


def apply_tenant(cur: _Cursor, tenant_id: str, *, placeholder: str = "?") -> None:
    """On Postgres (``placeholder == "%s"``), set the transaction-local
    ``app.tenant_id`` GUC so the RLS policies scope this transaction. No-op on
    sqlite. Must be called on the SAME cursor/connection, inside the same
    transaction, immediately before the tenant-scoped query."""
    if placeholder != "%s":
        return
    cur.execute("SELECT set_config('app.tenant_id', %s, true)", (tenant_id,))


__all__ = ["apply_tenant"]
