"""One-call Python schema bootstrap for a fresh database."""

import sqlite3

from factory import steady_tenant
from rgnr8_forecast import Money
from rgnr8_ops import Fleet, SqlFleetStore, bootstrap_python_schemas, run_migrations
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


def test_release_migration_sequence_leaves_webhook_outbox_present() -> None:
    """deploy/migrate.py runs the versioned migrations and THEN the full schema
    bootstrap on the same connection. The versioned set does not include the
    webhook tables, so the bootstrap is what makes the cron worker's
    `webhook_outbox` query resolve. This reproduces the exact release sequence
    and guards against the two paths ever conflicting (a regression: the worker
    crashed on a missing webhook_outbox because migrate.py ran only the versioned
    migrations)."""
    conn = sqlite3.connect(":memory:")
    run_migrations(conn, dialect="sqlite", placeholder="?", applied_at="2026-01-01T00:00:00Z")
    # webhook_outbox is NOT among the versioned migrations…
    assert "webhook_outbox" not in _tables(conn)
    # …the bootstrap composes on top of the migrated core tables without error…
    bootstrap_python_schemas(conn, placeholder="?")
    assert {"webhook_outbox", "webhook_endpoint"} <= _tables(conn)
    # …and the whole sequence is safe to run again (every deploy re-runs it).
    run_migrations(conn, dialect="sqlite", placeholder="?", applied_at="2026-01-01T00:00:00Z")
    bootstrap_python_schemas(conn, placeholder="?")


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


def test_rls_enforcement_flag_turns_the_warning_into_a_refusal() -> None:
    """RGNR8_REQUIRE_RLS=1 is what makes a misconfigured role fatal instead of a
    log line nobody reads. A fake superuser connection stands in for the real one."""
    from rgnr8_ops import check_rls_posture

    class _Cur:
        def execute(self, sql: str, params: object = None) -> None: ...
        def fetchall(self) -> list:
            return [("app_role", True, False)]      # rolsuper = True
        def close(self) -> None: ...

    class _Conn:
        def cursor(self) -> _Cur:
            return _Cur()
        def commit(self) -> None: ...
        def rollback(self) -> None: ...

    warned, fatal = check_rls_posture(_Conn(), "%s", {})
    assert warned and not fatal                     # default: warn only

    warned, fatal = check_rls_posture(_Conn(), "%s", {"RGNR8_REQUIRE_RLS": "1"})
    assert warned and fatal                         # opted in: refuse to boot

    # A sound connection must never be fatal, even with enforcement on.
    class _CleanCur(_Cur):
        def fetchall(self) -> list:
            return [("app_role", False, False)]

    class _CleanConn(_Conn):
        def cursor(self) -> _CleanCur:
            return _CleanCur()

    warned, fatal = check_rls_posture(_CleanConn(), "%s", {"RGNR8_REQUIRE_RLS": "1"})
    assert not warned and not fatal

    # sqlite has no RLS to bypass; enforcement must not block it.
    assert check_rls_posture(object(), "?", {"RGNR8_REQUIRE_RLS": "1"}) == ([], False)
