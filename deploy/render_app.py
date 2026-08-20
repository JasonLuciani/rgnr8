"""DEPRECATED — use `deploy/entry.py`.

This module used to be a second, independent production composition (in-memory
stores, a single hardcoded tenant, its own login/QBO wiring) that disagreed with
`deploy/entry.py` on auth, durability, tenant loading, and ledger wiring. Two
"production" entrypoints with different trust properties is a footgun, so the
composition now lives in one place: `rgnr8_ops.create_application`, driven
entirely by environment variables (see `deploy/entry.py` and the provisioning
runbook). Browser login, QBO, Ask RGNR8, and the ledger are all wired there and
gated by their env vars.

This thin alias is kept only so a process manager still pointing at
`deploy.render_app:application` keeps working; it delegates to the one
composition. Point new deployments at `deploy.entry:application`.
"""

from __future__ import annotations

import _pathsetup  # noqa: F401

from entry import application  # the single production WSGI app

__all__ = ["application"]
