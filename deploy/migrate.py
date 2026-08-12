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

from rgnr8_ops import run_migrations
from db import open_connection


def main() -> int:
    conn, dialect, placeholder = open_connection(os.environ.get("RGNR8_DATABASE_URL"))
    now = datetime.now(timezone.utc).isoformat()
    result = run_migrations(conn, dialect=dialect, placeholder=placeholder, applied_at=now)
    print(
        f"migrations: applied {list(result.applied)}, skipped {list(result.skipped)} "
        f"({dialect}), now at version {result.current_version}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
