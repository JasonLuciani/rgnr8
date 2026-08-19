"""One-call schema bootstrap for the Python side of a deployment.

The engines each own a `create_schema()` for their table; a deployer shouldn't
have to remember all of them. `bootstrap_python_schemas(conn)` creates every
Python-owned table (web tenant-state, briefing subscriptions, the fleet roster,
users/memberships, invitations, API keys, the audit log, billing accounts +
usage, end-user credentials + email-verification tokens, and the scheduler lease)
over one DB-API connection, idempotently. The TypeScript ledger +
financial-package tables ship through `@rgnr8/migrations` separately; this covers
exactly the Python-owned state so a fresh Postgres/sqlite is ready to serve.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, cast

from rgnr8_billing import SqlAccountStore
from rgnr8_qbo import SqlConnectionStore, cipher_from_env
from rgnr8_reports import SqlReportScheduleStore, SqlSavedReportStore
from rgnr8_runtime.subscriptions import DbApiConnection, SqlSubscriptionStore
from rgnr8_web import (
    SqlApiKeyStore,
    SqlAuditLog,
    SqlCredentialStore,
    SqlInvitationStore,
    SqlTenantStore,
    SqlUserDirectory,
    SqlVerificationTokenStore,
    SqlWebhookEndpointStore,
    SqlWebhookOutbox,
)

from .scheduler import SqlLeaseStore
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
    # Public-track end-user auth: password credentials + single-use verify/reset
    # tokens — so a fresh DB can serve /signup → /verify → /login (login-ready).
    credentials = SqlCredentialStore(cast("Any", conn), placeholder=placeholder)
    verify_tokens = SqlVerificationTokenStore(cast("Any", conn), placeholder=placeholder)
    # Scheduler lease: at-most-one distributed dispatcher runner (LeasedDispatcher).
    lease = SqlLeaseStore(conn, placeholder=placeholder)
    # Reporting: saved custom report specs + standing scheduled-report cadences.
    saved_reports = SqlSavedReportStore(cast("Any", conn), placeholder=placeholder)
    report_schedules = SqlReportScheduleStore(cast("Any", conn), placeholder=placeholder)
    # QuickBooks Online connections (OAuth tokens per tenant). The tokens are
    # encrypted at rest by a Fernet cipher when RGNR8_SECRET_KEY is set; without
    # it a NullCipher preserves the plaintext dev behaviour. This is the reference
    # wiring — any runtime that constructs the store should pass the same cipher.
    qbo_connections = SqlConnectionStore(
        cast("Any", conn), placeholder=placeholder,
        cipher=cipher_from_env(os.environ),
    )
    # Outbound webhooks: per-tenant endpoints + the durable delivery outbox.
    webhook_endpoints = SqlWebhookEndpointStore(cast("Any", conn), placeholder=placeholder)
    webhook_outbox = SqlWebhookOutbox(cast("Any", conn), placeholder=placeholder)

    tenant_store.create_schema()
    sub_store.create_schema()
    fleet_store.create_schema()
    users.create_schema()
    invitations.create_schema()
    api_keys.create_schema()
    audit.create_schema()
    accounts.create_schema()
    credentials.create_schema()
    verify_tokens.create_schema()
    lease.create_schema()
    saved_reports.create_schema()
    report_schedules.create_schema()
    qbo_connections.create_schema()
    webhook_endpoints.create_schema()
    webhook_outbox.create_schema()
    return ["web_tenant_state", "briefing_subscription", "fleet_tenant", "rgnr8_user",
            "rgnr8_membership", "rgnr8_invitation", "rgnr8_api_key", "audit_event",
            "billing_account", "billing_usage", "rgnr8_credential",
            "rgnr8_verification_token", "scheduler_lease", "rgnr8_saved_report",
            "rgnr8_report_schedule", "rgnr8_qbo_connection", "webhook_endpoint",
            "webhook_outbox"]
