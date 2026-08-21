"""Forward-only, drift-checked migrations for the Python-owned tables.

Mirrors the TypeScript `@rgnr8/migrations` runner for the tables the Python side
owns (web tenant-state, briefing subscriptions, the fleet roster): each
migration is applied **once, in order, inside a transaction**, and its checksum
is recorded — re-running is a no-op, and if a previously-applied migration's SQL
changed it raises `MigrationDriftError` rather than silently diverging.

Postgres gets **per-tenant Row-Level Security**: every tenant-scoped table
enables RLS with a policy keyed on a session GUC (`app.tenant_id`), so even a
compromised query can't read across tenants. This is the *same* GUC the
TypeScript ledger writer (`PgLedgerStore`) binds, so both stacks enforce tenant
isolation through one setting rather than two divergent names. RLS statements are Postgres-only
and are skipped on sqlite (dev/UAT), while the portable table DDL runs on both —
so the same migration list drives both, staying on one version line for the
statements each dialect actually executes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from hashlib import sha256

from rgnr8_runtime.subscriptions import DbApiConnection


class MigrationError(Exception):
    pass


class MigrationDriftError(MigrationError):
    """A previously-applied migration's SQL no longer matches its checksum."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    dialects: frozenset[str] = field(default_factory=lambda: frozenset({"sqlite", "postgres"}))

    @property
    def checksum(self) -> str:
        return sha256("\n".join(self.statements).encode("utf-8")).hexdigest()


def _rls_policy(table: str) -> tuple[str, ...]:
    """Enable RLS on a tenant-scoped table with a GUC-keyed policy (Postgres).

    Strict: a row is visible only when `app.tenant_id` is set to its tenant. Use
    for tables read inside a tenant-scoped request, where the store sets the GUC
    before every query (web_tenant_state, rgnr8_membership)."""
    return (
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}",
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (tenant_id = current_setting('app.tenant_id', true))",
    )


def _rls_policy_backend(table: str) -> tuple[str, ...]:
    """Relax a platform-operational table's policy so a trusted backend job that
    reads it CROSS-tenant (no GUC set → sees all rows) keeps working, while a
    request that DOES set the tenant GUC is still scoped to its own tenant.

    fleet_tenant (the fleet loader registers every tenant at boot) and
    briefing_subscription (the worker iterates all due subscriptions) are read by
    trusted backends across tenants; a strict FORCE'd policy silently returned
    zero rows and broke them. This keeps FORCE on (still applies to any request
    that sets a tenant) without breaking the cross-tenant reader."""
    return (
        f"DROP POLICY IF EXISTS {table}_tenant_isolation ON {table}",
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        f"USING (coalesce(current_setting('app.tenant_id', true), '') = '' "
        f"OR tenant_id = current_setting('app.tenant_id', true))",
    )


# The Python-owned schema, as versioned migrations.
PYTHON_MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version=1,
        name="core_tables",
        statements=(
            "CREATE TABLE IF NOT EXISTS web_tenant_state "
            "(tenant_id TEXT PRIMARY KEY, state_json TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS briefing_subscription "
            "(tenant_id TEXT NOT NULL, recipient TEXT NOT NULL, weekday INTEGER NOT NULL, "
            "hour INTEGER NOT NULL, minute INTEGER NOT NULL, timezone TEXT NOT NULL, "
            "last_sent TEXT, active INTEGER NOT NULL DEFAULT 1, PRIMARY KEY (tenant_id, recipient))",
            "CREATE TABLE IF NOT EXISTS fleet_tenant "
            "(tenant_id TEXT PRIMARY KEY, name TEXT NOT NULL, recipient TEXT NOT NULL, "
            "inputs_json TEXT NOT NULL, currency TEXT NOT NULL, minimum_cash_minor INTEGER NOT NULL, "
            "horizon_weeks INTEGER NOT NULL, weekday INTEGER NOT NULL, hour INTEGER NOT NULL, "
            "minute INTEGER NOT NULL, timezone TEXT NOT NULL, status_json TEXT)",
        ),
    ),
    Migration(
        version=2,
        name="row_level_security",
        statements=(
            *_rls_policy("web_tenant_state"),
            *_rls_policy("briefing_subscription"),
            *_rls_policy("fleet_tenant"),
        ),
        dialects=frozenset({"postgres"}),  # RLS is Postgres-only; skipped on sqlite
    ),
    Migration(
        version=3,
        name="rbac_tables",
        statements=(
            "CREATE TABLE IF NOT EXISTS rgnr8_user "
            "(id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', platform_role TEXT)",
            "CREATE TABLE IF NOT EXISTS rgnr8_membership "
            "(user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, role TEXT NOT NULL, "
            "PRIMARY KEY (user_id, tenant_id))",
        ),
    ),
    Migration(
        version=4,
        name="rbac_row_level_security",
        statements=(*_rls_policy("rgnr8_membership"),),  # scope memberships per tenant
        dialects=frozenset({"postgres"}),
    ),
    Migration(
        version=5,
        name="backend_operational_rls_relax",
        # fleet_tenant + briefing_subscription are read cross-tenant by trusted
        # backends (fleet load, briefing delivery). The strict v2 policies broke
        # those reads (GUC never set → zero rows). Relax to "all rows when no
        # tenant context, else scoped" so the backends work and a tenant-scoped
        # request is still isolated. FORCE (from v2) stays on.
        statements=(
            *_rls_policy_backend("fleet_tenant"),
            *_rls_policy_backend("briefing_subscription"),
        ),
        dialects=frozenset({"postgres"}),
    ),
)


_MIGRATIONS_TABLE_DDL = (
    "CREATE TABLE schema_migrations_py "
    "(version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
)


def _ensure_migrations_table(conn: DbApiConnection) -> None:
    cur = conn.cursor()
    try:
        try:  # existence probe (avoids a repeated CREATE IF NOT EXISTS quirk)
            cur.execute("SELECT 1 FROM schema_migrations_py LIMIT 0")
            cur.fetchall()
            return
        except Exception:  # noqa: BLE001 - table absent → create it
            pass
    finally:
        cur.close()

    # Postgres aborts the WHOLE transaction as soon as any statement fails, so once
    # the probe above 404s every later statement raises InFailedSqlTransaction --
    # including the CREATE below. Catching the Python exception is not enough; the
    # server-side transaction has to be discarded. sqlite has no such behaviour, so
    # this is a harmless no-op there.
    #
    # This is safe unconditionally: _ensure_migrations_table runs before
    # run_migrations() has applied anything, so there is never real work to lose.
    conn.rollback()

    cur = conn.cursor()
    try:
        cur.execute(_MIGRATIONS_TABLE_DDL)
    finally:
        cur.close()
    conn.commit()


def _applied(conn: DbApiConnection) -> dict[int, str]:
    cur = conn.cursor()
    try:
        cur.execute("SELECT version, checksum FROM schema_migrations_py")
        rows = cur.fetchall()
    finally:
        cur.close()
    return {int(str(r[0])): str(r[1]) for r in rows}


@dataclass(frozen=True, slots=True)
class MigrationResult:
    applied: tuple[int, ...]
    skipped: tuple[int, ...]  # non-matching dialect
    current_version: int


def run_migrations(
    conn: DbApiConnection,
    migrations: Sequence[Migration] = PYTHON_MIGRATIONS,
    *,
    dialect: str = "sqlite",
    placeholder: str = "?",
    applied_at: str,
) -> MigrationResult:
    """Apply pending migrations for `dialect`, in version order, idempotently.
    Raises `MigrationDriftError` if an applied migration's SQL changed."""
    _ensure_migrations_table(conn)
    done = _applied(conn)
    applied: list[int] = []
    skipped: list[int] = []
    p = placeholder

    for m in sorted(migrations, key=lambda x: x.version):
        if dialect not in m.dialects:
            skipped.append(m.version)
            continue
        if m.version in done:
            if done[m.version] != m.checksum:
                raise MigrationDriftError(
                    f"migration {m.version} ({m.name}) checksum drift — SQL changed after apply"
                )
            continue
        cur = conn.cursor()
        try:
            for stmt in m.statements:
                cur.execute(stmt)
            cur.execute(
                f"INSERT INTO schema_migrations_py (version, name, checksum, applied_at) "
                f"VALUES ({p}, {p}, {p}, {p})",
                (m.version, m.name, m.checksum, applied_at),
            )
        finally:
            cur.close()
        conn.commit()
        applied.append(m.version)

    current = max([*done.keys(), *applied], default=0)
    return MigrationResult(applied=tuple(applied), skipped=tuple(skipped), current_version=current)


def with_tenant(conn: DbApiConnection, tenant_id: str) -> None:
    """Set the session's tenant GUC (`app.tenant_id`) so RLS policies scope every
    subsequent query to `tenant_id` (Postgres). This is the same GUC the RLS
    policies and the TypeScript ledger writer use. A no-op safety net on sqlite
    (which ignores it)."""
    cur = conn.cursor()
    try:
        cur.execute("SELECT set_config('app.tenant_id', ?, false)".replace("?", "%s"), (tenant_id,))
    except Exception:  # noqa: BLE001 - sqlite has no set_config; RLS not enforced there
        pass
    finally:
        cur.close()
