"""Real row-level security on the Python-owned DB (H1-8).

Runs ONLY when RGNR8_TEST_DATABASE_URL points at a real Postgres (sqlite has no
RLS). Proves the two things the review said were missing: with FORCE'd policies
active, (a) the app still works because the stores set the per-request tenant GUC,
and (b) isolation actually engages — a query under tenant A's context cannot see
tenant B's rows, and with no context the strict tables reveal nothing.
"""

import os

import pytest

pytest.importorskip("psycopg")
import psycopg  # noqa: E402

from rgnr8_web import SqlTenantStore  # noqa: E402
from rgnr8_web.store import TenantState  # noqa: E402
from rgnr8_ops import rls_bypass_warnings, run_migrations  # noqa: E402

DSN = os.environ.get("RGNR8_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DSN, reason="RGNR8_TEST_DATABASE_URL not set (needs real Postgres)")


# Postgres does not apply row-level security to superusers or to roles with
# BYPASSRLS -- FORCE does not change that; FORCE only extends RLS to a table's
# OWNER. CI connects as POSTGRES_USER, which the official postgres image creates
# as a superuser, so every isolation assertion in this file passed through a role
# for which the policies are never consulted. The test proved nothing about RLS.
# Switch to a deliberately unprivileged role before asserting isolation.
PROBE_ROLE = "rls_probe"


@pytest.fixture()
def conn() -> "psycopg.Connection[object]":
    c = psycopg.connect(DSN)
    # Isolated schema per run so repeated runs don't collide.
    c.execute("DROP SCHEMA IF EXISTS rls_test CASCADE")
    c.execute("CREATE SCHEMA rls_test")
    c.execute("SET search_path TO rls_test")
    c.execute(
        f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{PROBE_ROLE}') "
        f"THEN CREATE ROLE {PROBE_ROLE} NOSUPERUSER NOBYPASSRLS NOINHERIT; END IF; END $$"
    )
    c.execute(f"GRANT USAGE ON SCHEMA rls_test TO {PROBE_ROLE}")
    c.commit()
    try:
        yield c
    finally:
        c.execute("RESET ROLE")            # the probe role may not drop the schema
        c.execute("DROP SCHEMA IF EXISTS rls_test CASCADE")
        c.commit()
        c.close()


def _as_unprivileged_tenant_role(c: "psycopg.Connection[object]") -> None:
    """Stop being a superuser, so the RLS policies are actually evaluated.

    Call AFTER run_migrations() -- the grants need the tables to exist. A plain
    non-owner role is also the realistic production shape: the app should never
    connect to this database as a superuser, or RLS is inert no matter how many
    policies are defined.
    """
    c.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA rls_test TO {PROBE_ROLE}")
    c.commit()
    c.execute(f"SET ROLE {PROBE_ROLE}")


def _set_guc(c: "psycopg.Connection[object]", tenant: str) -> None:
    c.execute("SELECT set_config('app.tenant_id', %s, false)", (tenant,))


def test_rls_is_not_bypassed_by_the_connecting_role(conn: "psycopg.Connection[object]") -> None:
    """Guard the guard: if the probe role ever gains superuser/BYPASSRLS, every
    isolation assertion below silently becomes vacuous. Fail loudly instead."""
    run_migrations(conn, dialect="postgres", placeholder="%s", applied_at="now")
    _as_unprivileged_tenant_role(conn)
    row = conn.execute(
        "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
    ).fetchone()
    assert row is not None
    assert row[0] is False, "connecting role is a superuser — RLS is not enforced against it"
    assert row[1] is False, "connecting role has BYPASSRLS — RLS is not enforced against it"


def test_stores_work_and_isolate_under_forced_rls(conn: "psycopg.Connection[object]") -> None:
    run_migrations(conn, dialect="postgres", placeholder="%s", applied_at="now")
    _as_unprivileged_tenant_role(conn)
    store = SqlTenantStore(conn, placeholder="%s")

    # The app works under FORCE'd RLS because save/load set the GUC per call.
    store.save("acme", TenantState())
    store.save("beta", TenantState())
    conn.commit()
    assert store.load("acme") is not None
    assert store.load("beta") is not None

    # Isolation: under tenant acme's context, a raw unfiltered read sees ONLY acme.
    _set_guc(conn, "acme")
    rows = conn.execute("SELECT tenant_id FROM web_tenant_state").fetchall()
    assert {r[0] for r in rows} == {"acme"}          # beta is invisible — RLS enforced

    _set_guc(conn, "beta")
    rows = conn.execute("SELECT tenant_id FROM web_tenant_state").fetchall()
    assert {r[0] for r in rows} == {"beta"}

    # With no tenant context, the strict table reveals nothing (FORCE is active).
    conn.execute("SELECT set_config('app.tenant_id', '', false)")
    rows = conn.execute("SELECT tenant_id FROM web_tenant_state").fetchall()
    assert rows == []


def test_backend_tables_readable_cross_tenant_but_scoped_when_context_set(
    conn: "psycopg.Connection[object]",
) -> None:
    run_migrations(conn, dialect="postgres", placeholder="%s", applied_at="now")
    _as_unprivileged_tenant_role(conn)
    # Seed two tenants' fleet rows directly (backend-owned table).
    for t in ("acme", "beta"):
        conn.execute(
            "INSERT INTO fleet_tenant (tenant_id, name, recipient, inputs_json, currency, "
            "minimum_cash_minor, horizon_weeks, weekday, hour, minute, timezone) "
            "VALUES (%s, %s, %s, %s, 'USD', 0, 13, 0, 8, 0, 'UTC')",
            (t, t, f"{t}@x.com", "{}"),
        )
    conn.commit()

    # No context (trusted backend, e.g. the fleet loader): sees ALL tenants.
    conn.execute("SELECT set_config('app.tenant_id', '', false)")
    allrows = conn.execute("SELECT tenant_id FROM fleet_tenant").fetchall()
    assert {r[0] for r in allrows} == {"acme", "beta"}

    # With a tenant context set, the same table is scoped to that tenant.
    _set_guc(conn, "acme")
    scoped = conn.execute("SELECT tenant_id FROM fleet_tenant").fetchall()
    assert {r[0] for r in scoped} == {"acme"}


def test_posture_check_flags_a_connection_that_cannot_enforce_rls(
    conn: "psycopg.Connection[object]",
) -> None:
    """The deployment mistake this check exists to catch, exercised for real: CI
    connects as POSTGRES_USER, which the postgres image creates as a superuser."""
    warned = rls_bypass_warnings(conn, "%s")
    assert warned, "a superuser connection must be flagged"
    assert "SUPERUSER" in warned[0]
    assert "inert" in warned[0]

    # ...and stays quiet once the connection is what production should look like.
    run_migrations(conn, dialect="postgres", placeholder="%s", applied_at="now")
    _as_unprivileged_tenant_role(conn)
    assert rls_bypass_warnings(conn, "%s") == []
