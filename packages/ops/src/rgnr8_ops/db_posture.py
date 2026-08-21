"""Deployment-posture checks for the database connection itself.

Some security controls are configuration, not code: they can be perfectly
implemented and still do nothing because of *how* the app connects. This module
checks for that class of problem at boot, so it shows up in the logs instead of
being discovered during an incident.
"""

from __future__ import annotations

from typing import Any


def rls_bypass_warnings(conn: Any, placeholder: str) -> list[str]:
    """Warn when row-level security cannot possibly be enforced on this connection.

    Postgres does not apply RLS to superusers or to roles with BYPASSRLS, and
    `FORCE ROW LEVEL SECURITY` does not change that -- FORCE only extends RLS to a
    table's OWNER. So a deployment that connects as a superuser has tenant
    isolation policies that are defined, tested, and completely inert.

    Returns human-readable warnings; empty when the posture is sound, and empty on
    sqlite (no RLS to bypass). ``placeholder`` is this codebase's dialect signal.
    """
    if placeholder != "%s":
        return []
    cur = conn.cursor()
    try:
        cur.execute(
            "SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001 - a posture check must never break boot
        conn.rollback()
        return []
    finally:
        cur.close()
    if not rows:
        return []
    role, is_super, bypasses = str(rows[0][0]), bool(rows[0][1]), bool(rows[0][2])
    reason = "is a SUPERUSER" if is_super else ("has BYPASSRLS" if bypasses else "")
    if not reason:
        return []
    return [
        f"database role {role!r} {reason} — row-level security is NOT enforced "
        f"against it, so every tenant-isolation policy on this database is inert. "
        f"Connect the application as a non-superuser role without BYPASSRLS."
    ]
