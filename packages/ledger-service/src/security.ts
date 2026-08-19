/**
 * Row-level security — the second lock on multi-tenant data.
 *
 * Every query the service issues is already tenant-scoped in SQL. That is the
 * first lock, and it is tested. But it depends on every present and future
 * query being written correctly, forever, by everyone. One forgotten
 * `WHERE tenant_id = $1` and a client sees another client's books.
 *
 * So the database enforces it too. Each tenant-scoped table gets a policy keyed
 * on `app.tenant_id`, a session setting the service binds inside every
 * transaction. A query that forgets the tenant filter returns **nothing**
 * instead of everything, because `current_setting('app.tenant_id', true)` is
 * NULL when unset and `tenant_id = NULL` matches no row. It fails closed.
 *
 * Two details make this real rather than decorative:
 *
 * - **FORCE ROW LEVEL SECURITY.** Without it, the table's owner bypasses every
 *   policy — and the service usually *is* the owner, which is precisely how RLS
 *   ends up defined but never actually applied. FORCE closes that.
 * - **A least-privilege role.** `appRoleDdl` creates a role with DML but no DDL
 *   and no ownership, so a compromised connection can read and write rows
 *   within its tenant but cannot drop a policy, alter a table, or read the
 *   catalog's way around one.
 */

/** Every table holding tenant data, whichever module owns it. */
export const TENANT_TABLES: readonly string[] = Object.freeze([
  // the ledger core
  "journal_entry",
  "journal_line",
  "ledger_tenant_seq",
  "ledger_period",
  "account",
  // AR/AP
  "party",
  "doc",
  "doc_line",
  "doc_payment",
  // bank reconciliation
  "cleared_status",
  "reconciled_through",
  // the bank feed inbox
  "feed_txn",
  "feed_rule",
  // payroll
  "payroll_employee",
  "payroll_run",
  "payroll_run_line",
  // budgets and reporting dimensions
  "budget_line",
  "dimension_def",
  // evidence
  "attachment",
  // memorized transactions
  "recurring_txn",
  "recurring_txn_line",
  // the project layer
  "cost_code",
  "job",
  "job_budget",
  "estimate",
  "estimate_line",
  "sales_order",
  "sales_order_line",
  "work_order",
  "work_order_entry",
  "purchase_order",
  "purchase_order_line",
  "goods_receipt",
  "goods_receipt_line",
  "schedule_of_values",
  "billing_milestone",
  "inventory_item",
  "inventory_movement",
  "inventory_lot",
  "crm_lead",
  "crm_opportunity",
  "crm_event",
  "entity_group",
  "entity_group_member",
  "elimination_entry",
  "elimination_line",
  // per-account settings
  "account_settings",
  // debt
  "loan",
  "loan_payment",
  // fixed assets
  "fixed_asset",
  "fixed_asset_period",
]);

/**
 * Enable, force, and (re)create the isolation policy on every tenant table.
 *
 * Written to be safe to run repeatedly: policies are dropped and recreated
 * rather than assumed absent, and each table is guarded so a schema that
 * predates one of them still migrates.
 */
export function rlsDdl(tables: readonly string[] = TENANT_TABLES): string {
  return tables
    .map(
      (table) => `
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name = '${table}') THEN
    EXECUTE 'ALTER TABLE ${table} ENABLE ROW LEVEL SECURITY';
    -- FORCE is the point: without it the table owner silently bypasses the policy.
    EXECUTE 'ALTER TABLE ${table} FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS ${table}_tenant_isolation ON ${table}';
    EXECUTE 'CREATE POLICY ${table}_tenant_isolation ON ${table}'
         || ' USING (tenant_id = current_setting(''app.tenant_id'', true))'
         || ' WITH CHECK (tenant_id = current_setting(''app.tenant_id'', true))';
  END IF;
END $$;`,
    )
    .join("\n");
}

/** Quote an identifier for interpolation, refusing anything exotic outright. */
function ident(name: string): string {
  if (!/^[a-z_][a-z0-9_]*$/i.test(name)) {
    throw new Error(`unsafe SQL identifier: ${name}`);
  }
  return `"${name}"`;
}

/**
 * Create (or update) the least-privilege role the service should connect as.
 *
 * The role gets row-level DML and nothing else: no ownership, no CREATE, no
 * ability to alter or drop the policies that constrain it. Run this as the
 * owner, once, after `migrate()`.
 *
 * The password is interpolated, so it must come from the deployment's own
 * configuration and never from a request.
 */
export function appRoleDdl(
  role: string,
  password: string,
  tables: readonly string[] = TENANT_TABLES,
): string {
  const quoted = ident(role);
  if (password.includes("'")) {
    throw new Error("the app role password cannot contain a single quote");
  }
  const grants = tables
    .map(
      (table) => `
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = current_schema() AND table_name = '${table}') THEN
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON ${table} TO ${quoted}';
  END IF;`,
    )
    .join("\n");

  return `
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '${role}') THEN
    EXECUTE 'CREATE ROLE ${quoted} LOGIN PASSWORD ''${password}''';
  ELSE
    EXECUTE 'ALTER ROLE ${quoted} LOGIN PASSWORD ''${password}''';
  END IF;
  -- No CREATE: this role can change rows, never the shape of the database.
  EXECUTE 'GRANT USAGE ON SCHEMA ' || current_schema() || ' TO ${quoted}';
  EXECUTE 'REVOKE CREATE ON SCHEMA ' || current_schema() || ' FROM ${quoted}';
${grants}
END $$;`;
}

/**
 * Is this connection exempt from every policy?
 *
 * Postgres excuses superusers and `BYPASSRLS` roles from row-level security
 * unconditionally — FORCE does not reach them. A deployment that connects as
 * `postgres` therefore has RLS defined, enabled, forced, and doing absolutely
 * nothing. That is a silent failure, so the service checks at boot and says so
 * loudly rather than letting it pass.
 *
 * Returns a warning string, or null when the connection is properly constrained.
 */
export async function superuserWarning(pool: {
  query(text: string, values?: unknown[]): Promise<{ rows: Record<string, unknown>[] }>;
}): Promise<string | null> {
  const res = await pool.query(
    "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user",
  );
  const row = res.rows[0];
  if (!row) return null;
  const superuser = row["rolsuper"] === true || row["rolsuper"] === "t";
  const bypass = row["rolbypassrls"] === true || row["rolbypassrls"] === "t";
  if (!superuser && !bypass) return null;
  return (
    `the ledger service is connected as a ${superuser ? "superuser" : "BYPASSRLS"} role, `
    + "which is exempt from row-level security — tenant isolation is resting on "
    + "application SQL alone. Create a least-privilege role (see appRoleDdl) and "
    + "point the service at it before real client data."
  );
}
