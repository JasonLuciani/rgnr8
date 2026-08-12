"""Open a DB-API connection from RGNR8_DATABASE_URL.

Postgres in production (psycopg), sqlite for local/UAT, and an in-memory sqlite
when no URL is set (dev only — not durable). Returns `(conn, dialect, placeholder)`.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def open_connection(database_url: str | None) -> tuple[Any, str, str]:
    if not database_url:
        return sqlite3.connect(":memory:"), "sqlite", "?"
    low = database_url.lower()
    if low.startswith(("postgres://", "postgresql://")):
        import psycopg  # imported lazily so sqlite/dev doesn't need the driver

        return psycopg.connect(database_url), "postgres", "%s"
    if low.startswith("sqlite://"):
        path = database_url[len("sqlite://") :] or ":memory:"
        return sqlite3.connect(path), "sqlite", "?"
    raise SystemExit(f"unsupported RGNR8_DATABASE_URL scheme: {database_url!r}")
