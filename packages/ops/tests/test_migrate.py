"""Forward-only, drift-checked Python migrations + RLS dialect handling."""

import sqlite3

import pytest
from rgnr8_ops import (
    PYTHON_MIGRATIONS,
    Migration,
    MigrationDriftError,
    run_migrations,
    with_tenant,
)

AT = "2026-09-01T00:00:00Z"


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_migrations_apply_once_and_are_idempotent() -> None:
    conn = sqlite3.connect(":memory:")
    r1 = run_migrations(conn, dialect="sqlite", applied_at=AT)
    assert r1.applied == (1, 3)  # core tables + rbac tables (both portable)
    assert r1.skipped == (2, 4)  # RLS migrations are postgres-only, skipped on sqlite
    assert {"web_tenant_state", "briefing_subscription", "fleet_tenant",
            "rgnr8_user", "rgnr8_membership"} <= _tables(conn)
    assert r1.current_version == 3

    r2 = run_migrations(conn, dialect="sqlite", applied_at=AT)
    assert r2.applied == ()  # nothing new the second time


def test_rls_migration_runs_under_postgres_dialect() -> None:
    # We can't run Postgres here, but the runner must select the RLS migration
    # for the postgres dialect. Use a recording fake connection.
    executed: list[str] = []

    class FakeCursor:
        def execute(self, sql: str, params: object = None) -> None:
            executed.append(sql)
            if sql.startswith("SELECT 1 FROM schema_migrations_py"):
                raise RuntimeError("no table")  # force create on first probe
            if sql.startswith("SELECT version"):
                self._rows: list = []

        def fetchall(self) -> list:
            return getattr(self, "_rows", [])

        def close(self) -> None:
            pass

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

        def commit(self) -> None:
            pass

    result = run_migrations(FakeConn(), dialect="postgres", placeholder="%s", applied_at=AT)  # type: ignore[arg-type]
    assert result.applied == (1, 2, 3, 4)
    joined = "\n".join(executed)
    assert "ENABLE ROW LEVEL SECURITY" in joined
    assert "current_setting('app.tenant_id'" in joined  # unified GUC name across TS + Python
    assert "current_setting('rgnr8.tenant_id'" not in joined  # old divergent name is gone
    assert "CREATE POLICY fleet_tenant_tenant_isolation" in joined
    assert "CREATE POLICY rgnr8_membership_tenant_isolation" in joined


def test_checksum_drift_is_detected() -> None:
    conn = sqlite3.connect(":memory:")
    run_migrations(conn, dialect="sqlite", applied_at=AT)
    # a v1 whose SQL changed after being applied
    tampered = (
        Migration(version=1, name="core_tables", statements=("CREATE TABLE IF NOT EXISTS x (a TEXT)",)),
    )
    with pytest.raises(MigrationDriftError):
        run_migrations(conn, tampered, dialect="sqlite", applied_at=AT)


def test_migration_list_is_ordered_and_versioned() -> None:
    versions = [m.version for m in PYTHON_MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)  # unique


def test_with_tenant_sets_unified_app_guc() -> None:
    # `with_tenant` must bind the SAME GUC name the RLS policies (and the TS
    # ledger writer) key on — `app.tenant_id`, not the old `rgnr8.tenant_id`.
    executed: list[tuple[str, object]] = []

    class FakeCursor:
        def execute(self, sql: str, params: object = None) -> None:
            executed.append((sql, params))

        def close(self) -> None:
            pass

    class FakeConn:
        def cursor(self) -> FakeCursor:
            return FakeCursor()

    with_tenant(FakeConn(), "acme")  # type: ignore[arg-type]
    assert len(executed) == 1
    sql, params = executed[0]
    assert "set_config('app.tenant_id'" in sql
    assert "rgnr8.tenant_id" not in sql
    assert params == ("acme",)
