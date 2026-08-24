#!/usr/bin/env python3
"""Release-phase migration: create/upgrade the Python-owned schema.

Runs the forward-only, drift-checked migrations (web tenant-state, subscriptions,
fleet — plus per-tenant RLS on Postgres). Idempotent: safe to run on every
deploy. The TypeScript ledger + financial-package migrations run separately via
`@rgnr8/migrations` (see DEPLOYMENT.md) — this covers the Python side.

    python deploy/migrate.py
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import _pathsetup  # noqa: F401  (wires sys.path)

from rgnr8_ops import bootstrap_python_schemas, run_migrations
from db import open_connection


def main() -> int:
    conn, dialect, placeholder = open_connection(os.environ.get("RGNR8_DATABASE_URL"))
    now = datetime.now(timezone.utc).isoformat()
    result = run_migrations(conn, dialect=dialect, placeholder=placeholder, applied_at=now)
    print(
        f"migrations: applied {list(result.applied)}, skipped {list(result.skipped)} "
        f"({dialect}), now at version {result.current_version}"
    )
    # The versioned migrations above own the core tenant/RBAC tables (+ RLS). The
    # remaining Python-owned tables — credentials, invitations, API keys, audit,
    # billing, reports, QBO connections, and the webhook endpoint/outbox — are
    # created idempotently here so a fresh DB is fully serve-ready before any
    # service (web OR the cron worker) takes traffic. Without this the worker
    # crashed on a missing `webhook_outbox`, and any webhook write from the web
    # app would have hit the same gap. CREATE TABLE IF NOT EXISTS throughout, so
    # tables the migrations already made are no-ops here.
    ensured = bootstrap_python_schemas(conn, placeholder=placeholder)
    print(f"schema bootstrap: ensured {len(ensured)} Python-owned tables")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
