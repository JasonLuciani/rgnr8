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

from rgnr8_ops import Settings, create_operator_application
from db import open_connection


def _build():  # pragma: no cover - exercised in deployment, not unit tests
    settings = Settings.from_env(os.environ)
    for w in settings.warnings:
        print(f"[config] warning: {w}")
    print(f"[config] operator console {settings.redacted()}")
    conn, _dialect, _ph = open_connection(settings.database_url)
    return create_operator_application(
        os.environ, conn=conn if settings.is_production_db else None
    )


application = _build()
