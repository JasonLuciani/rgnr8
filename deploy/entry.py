"""WSGI entrypoint for the owner web app — `gunicorn deploy.entry:application`.

Builds `Settings` from the environment, opens the configured database, and wires
the production `WebApp` (auth mode from config, durable tenant store, the
persisted fleet's tenants registered so routes resolve after a restart). Exposes
a module-level `application` WSGI callable.
"""

from __future__ import annotations

import os

import _pathsetup  # noqa: F401

from rgnr8_ops import Settings, create_application
from db import open_connection


def _build():  # pragma: no cover - exercised in deployment, not unit tests
    settings = Settings.from_env(os.environ)
    for w in settings.warnings:
        print(f"[config] warning: {w}")
    print(f"[config] {settings.redacted()}")
    conn, _dialect, _ph = open_connection(settings.database_url)
    return create_application(os.environ, conn=conn if settings.is_production_db else None)


application = _build()
