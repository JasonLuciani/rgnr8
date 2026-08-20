"""Retention must not delete control/close evidence (H2-5).

A tenant retention sweep may drop routine aged rows, but close seals, erasures,
membership/role changes, impersonation, settings and integration config, and the
retention actions themselves are the accounting/security trail — a controller
must not be able to shrink the horizon and purge them.
"""

import sqlite3
from typing import Any, cast

from rgnr8_web import InMemoryAuditLog
from rgnr8_web.audit import SqlAuditLog, is_protected_action

NOW = 1_760_000_000
OLD = NOW - 400 * 86400   # older than any horizon


def test_is_protected_action_classification() -> None:
    for a in ("close.sealed", "data.erased", "membership.set", "support.impersonate",
              "platform_role.granted", "retention.swept", "settings.saved",
              "integration.endpoint.saved"):
        assert is_protected_action(a) is True
    for a in ("user.signup", "capture.bill_created", "invoice.posted"):
        assert is_protected_action(a) is False


def _seed(log: Any) -> None:
    log.record("op", "close.sealed", OLD, tenant_id="acme", target="2026-07")     # evidence
    log.record("op", "support.impersonate", OLD, tenant_id="acme")                # evidence
    log.record("u", "user.signup", OLD, tenant_id="acme")                         # routine, old
    log.record("u", "capture.bill_created", NOW, tenant_id="acme")                # routine, recent
    log.record("u", "user.signup", OLD, tenant_id="beta")                         # other tenant


def test_inmemory_purge_keeps_evidence_drops_routine_aged() -> None:
    log = InMemoryAuditLog()
    _seed(log)
    removed = log.purge_older_than("acme", NOW - 90 * 86400)
    assert removed == 1                                    # only the routine, old, acme row
    actions = {e.action for e in log.events(tenant_id="acme")}
    assert "close.sealed" in actions and "support.impersonate" in actions
    assert "user.signup" not in actions                    # the old routine row is gone
    assert "capture.bill_created" in actions               # recent routine kept
    assert log.events(tenant_id="beta")                    # other tenant untouched


def test_sql_purge_keeps_evidence_drops_routine_aged() -> None:
    conn = sqlite3.connect(":memory:")
    log = SqlAuditLog(cast("Any", conn), placeholder="?")
    log.create_schema()
    _seed(log)
    removed = log.purge_older_than("acme", NOW - 90 * 86400)
    assert removed == 1
    actions = {e.action for e in log.events(tenant_id="acme")}
    assert "close.sealed" in actions and "support.impersonate" in actions
    assert "user.signup" not in actions
    assert "capture.bill_created" in actions
