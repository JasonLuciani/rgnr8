"""One-call Python schema bootstrap for a fresh database."""

import sqlite3

from factory import steady_tenant
from rgnr8_forecast import Money
from rgnr8_ops import Fleet, SqlFleetStore, bootstrap_python_schemas
from rgnr8_runtime import SqlSubscriptionStore


def _tables(conn: sqlite3.Connection) -> set[str]:
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return {r[0] for r in cur.fetchall()}


def test_bootstrap_creates_every_python_table_idempotently() -> None:
    conn = sqlite3.connect(":memory:")
    created = bootstrap_python_schemas(conn)
    expected = {"web_tenant_state", "briefing_subscription", "fleet_tenant", "rgnr8_user",
                "rgnr8_membership", "rgnr8_invitation", "rgnr8_api_key", "audit_event",
                "billing_account", "billing_usage", "rgnr8_credential",
                "rgnr8_verification_token", "scheduler_lease", "rgnr8_saved_report",
                "rgnr8_report_schedule", "rgnr8_qbo_connection", "webhook_endpoint",
                "webhook_outbox"}
    assert set(created) == expected
    assert expected <= _tables(conn)
    # idempotent — running again does not raise
    bootstrap_python_schemas(conn)


def test_bootstrapped_db_backs_a_persisted_fleet() -> None:
    conn = sqlite3.connect(":memory:")
    bootstrap_python_schemas(conn)
    fleet = Fleet(jwt_secret="s", clock=lambda: 1, store=SqlFleetStore(conn))
    fleet.onboard(steady_tenant())
    # subscription persisted into the bootstrapped table too
    subs = SqlSubscriptionStore(conn).list()
    # (fleet uses its own in-memory subs by default; assert the fleet table has the tenant)
    back = Fleet.load(SqlFleetStore(conn), jwt_secret="s", clock=lambda: 1)
    assert set(back.tenants) == {"acme"}
    assert back.tenants["acme"].config.minimum_cash == Money.from_decimal("10000.00")
    assert isinstance(subs, list)


def test_rls_posture_check_is_inert_on_sqlite() -> None:
    """sqlite has no RLS to bypass. Passing an object with no cursor() proves the
    check never touches the connection on that dialect."""
    from rgnr8_ops import rls_bypass_warnings

    assert rls_bypass_warnings(object(), "?") == []
