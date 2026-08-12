"""One-call schema bootstrap for the Python side of a deployment.

The engines each own a `create_schema()` for their table; a deployer shouldn't
have to remember all of them. `bootstrap_python_schemas(conn)` creates every
Python-owned table (web tenant-state, briefing subscriptions, the fleet roster,
users/memberships, invitations, API keys, the audit log, and billing accounts +
usage) over one DB-API connection, idempotently. The TypeScript ledger +
financial-package tables ship through `@rgnr8/migrations` separately; this covers
exactly the Python-owned state so a fresh Postgres/sqlite is ready to serve.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from rgnr8_runtime.subscriptions import DbApiConnection, SqlSubscriptionStore
from rgnr8_web import (
    SqlApiKeyStore,
    SqlAuditLog,
    SqlInvitationStore,
    SqlTenantStore,
    SqlUserDirectory,
)
from rgnr8_billing import SqlAccountStore

from .store import SqlFleetStore

if TYPE_CHECKING:
    from rgnr8_web.sql_store import DbApiConnection as WebDbApiConnection


def bootstrap_python_schemas(
    conn: DbApiConnection,
    *,
    placeholder: str = "?",
) -> list[str]:
    """Create every Python-owned table idempotently. Returns the table names
    created/ensured, in order. ``placeholder`` is the driver's marker
    (``"?"`` sqlite3, ``"%s"`` psycopg). One DB-API connection satisfies all the
    stores' structurally-identical connection protocols."""
    tenant_store = SqlTenantStore(cast("WebDbApiConnection", conn), placeholder=placeholder)
    sub_store = SqlSubscriptionStore(conn, placeholder=placeholder)
    fleet_store = SqlFleetStore(conn, placeholder=placeholder)
    users = SqlUserDirectory(cast("Any", conn), placeholder=placeholder)
    invitations = SqlInvitationStore(cast("Any", conn), placeholder=placeholder)
    api_keys = SqlApiKeyStore(cast("Any", conn), placeholder=placeholder)
    audit = SqlAuditLog(cast("Any", conn), placeholder=placeholder)
    accounts = SqlAccountStore(cast("Any", conn), placeholder=placeholder)

    tenant_store.create_schema()
    sub_store.create_schema()
    fleet_store.create_schema()
    users.create_schema()
    invitations.create_schema()
    api_keys.create_schema()
    audit.create_schema()
    accounts.create_schema()
    return ["web_tenant_state", "briefing_subscription", "fleet_tenant", "rgnr8_user",
            "rgnr8_membership", "rgnr8_invitation", "rgnr8_api_key", "audit_event",
            "billing_account", "billing_usage"]
