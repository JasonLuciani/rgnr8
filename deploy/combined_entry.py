"""Combined WSGI entrypoint — ONE service, the whole front door.

`gunicorn combined_entry:application` serves both the owner web app and the
RGNR8-staff operator console from a single origin (acctg.rgnr8ventures.com),
dispatched by path:

    /operator, /operator/*   ->  the operator console (staff control plane)
    everything else          ->  the owner web app + API

Both apps share the same database, JWT secret, and session secret, so one login
is one sign-on: the console accepts the owner app's `rgnr8_session` cookie (see
OperatorApp.session_secret), and the owner app's post-login chooser links to
`/operator` on the same origin. Two DB connections (one per app) keep each app's
per-request row-level-security scoping independent — no GUC bleed between the
tenant-scoped web requests and the cross-tenant console reads.

Exposes a module-level `application` WSGI callable.
"""

from __future__ import annotations

import os

import _pathsetup  # noqa: F401

from rgnr8_ops import (
    Settings,
    check_rls_posture,
    create_application,
    create_operator_application,
)
from db import open_connection


def _guarded_conn(settings: "Settings", label: str):  # pragma: no cover - deploy
    """Open a DB connection and refuse to boot if RGNR8_REQUIRE_RLS=1 and this
    connection can't enforce row-level security (same posture gate as the single-
    app entrypoints)."""
    conn, _dialect, ph = open_connection(settings.database_url)
    posture, fatal = check_rls_posture(conn, ph, os.environ)
    for w in posture:
        print(f"[security] {label}: {'FATAL' if fatal else 'warning'}: {w}")
    if fatal:
        raise SystemExit(
            f"[security] combined front door ({label}) refusing to boot: "
            "RGNR8_REQUIRE_RLS=1 and this connection cannot enforce row-level "
            "security (see above)."
        )
    return conn


def dispatch(web_app, operator_app):
    """Return a WSGI app that routes `/operator` and `/operator/*` to the operator
    console and everything else to the owner web app. Pure and dependency-free so
    the routing is unit-testable with fake apps."""

    def application(environ, start_response):
        path = environ.get("PATH_INFO", "") or "/"
        if path == "/operator" or path.startswith("/operator/"):
            return operator_app(environ, start_response)
        return web_app(environ, start_response)

    return application


def _build():  # pragma: no cover - exercised in deployment, not unit tests
    settings = Settings.from_env(os.environ)
    for w in settings.warnings:
        print(f"[config] warning: {w}")
    print(f"[config] combined front door {settings.redacted()}")
    prod = settings.is_production_db
    web_conn = _guarded_conn(settings, "web") if prod else None
    op_conn = _guarded_conn(settings, "operator") if prod else None
    web_app = create_application(os.environ, conn=web_conn)
    operator_app = create_operator_application(os.environ, conn=op_conn)
    return dispatch(web_app, operator_app)


application = _build()
