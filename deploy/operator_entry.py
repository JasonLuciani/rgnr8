"""WSGI entrypoint for the RGNR8-staff operator console —
`gunicorn deploy.operator_entry:application`.

This is the control-plane surface (fleet, onboarding, go-live), separate from the
owner web app in `entry.py`. It wires the DURABLE onboarding registry so a
tenant's chosen COA template and cutover / go-live status survive a restart —
the composition that was previously missing (D5). Exposes a module-level
`application` WSGI callable.
"""

from __future__ import annotations

import os

import _pathsetup  # noqa: F401

from rgnr8_ops import Settings, check_rls_posture, create_operator_application
from db import open_connection


def _build():  # pragma: no cover - exercised in deployment, not unit tests
    settings = Settings.from_env(os.environ)
    for w in settings.warnings:
        print(f"[config] warning: {w}")
    print(f"[config] operator console {settings.redacted()}")
    conn, _dialect, ph = open_connection(settings.database_url)
    # Same posture gate as the owner app (entry.py). This console reads and writes
    # ACROSS every client's books, so it is the surface least tolerable to boot on a
    # superuser / BYPASSRLS role where the FORCE'd row-level security is silently
    # inert. RGNR8_REQUIRE_RLS=1 makes that a refusal to start, not a log line.
    posture, fatal = check_rls_posture(conn, ph, os.environ)
    for w in posture:
        print(f"[security] {'FATAL' if fatal else 'warning'}: {w}")
    if fatal:
        raise SystemExit(
            "[security] operator console refusing to boot: RGNR8_REQUIRE_RLS=1 and "
            "this connection cannot enforce row-level security (see above)."
        )
    return create_operator_application(
        os.environ, conn=conn if settings.is_production_db else None
    )


application = _build()
