import type { PeriodKey, PeriodStatus, PeriodStore, TenantId } from "@rgnr8/ledger-kernel";
import type { Pool, PoolClient, Queryable } from "./pgLedgerStore.js";
import { CORE_DDL } from "./schema.js";

/**
 * PostgreSQL implementation of the kernel's PeriodStore — durable month-end
 * period locks backed by the `ledger_period` table.
 *
 * Why this exists: an in-memory lock (a process-local Map) re-opens on restart,
 * so a period sealed at close could silently accept late postings after a
 * deploy. Persisting the lock closes that hole: `status` reads the table, so a
 * FRESH store instance (a new process) sees a period still LOCKED.
 *
 * Semantics:
 * - Default OPEN: `status` returns OPEN when no row exists for (tenant, period).
 * - `lock` upserts status = 'LOCKED' with a `locked_at` audit timestamp.
 * - `unlock` upserts status = 'OPEN' (a controlled prior-period re-open).
 *
 * Tenant isolation: every statement runs inside a transaction that first binds
 * the `app.tenant_id` GUC, exactly like PgLedgerStore, so the RLS policy on
 * `ledger_period` engages and this store is RLS-consistent with the ledger.
 */
export class SqlPeriodStore implements PeriodStore {
  constructor(private readonly pool: Pool) {}

  /** Create the ledger tables (incl. `ledger_period`) if absent. Idempotent. */
  async migrate(): Promise<void> {
    await this.pool.query(CORE_DDL);
  }

  async status(tenantId: TenantId, period: PeriodKey): Promise<PeriodStatus> {
    return this.withTenant(tenantId, async (q) => {
      const res = await q.query(
        `SELECT status FROM ledger_period WHERE tenant_id = $1 AND period = $2`,
        [tenantId, period],
      );
      const row = res.rows[0] as { status?: string } | undefined;
      // No row → the period was never sealed → OPEN.
      return row && String(row.status) === "LOCKED" ? "LOCKED" : "OPEN";
    });
  }

  async lock(tenantId: TenantId, period: PeriodKey): Promise<void> {
    const lockedAt = new Date().toISOString();
    await this.withTenant(tenantId, async (q) => {
      await q.query(
        `INSERT INTO ledger_period (tenant_id, period, status, locked_at)
         VALUES ($1, $2, 'LOCKED', $3)
         ON CONFLICT (tenant_id, period)
         DO UPDATE SET status = 'LOCKED', locked_at = $3`,
        [tenantId, period, lockedAt],
      );
    });
  }

  async unlock(tenantId: TenantId, period: PeriodKey): Promise<void> {
    await this.withTenant(tenantId, async (q) => {
      await q.query(
        `INSERT INTO ledger_period (tenant_id, period, status, locked_at)
         VALUES ($1, $2, 'OPEN', NULL)
         ON CONFLICT (tenant_id, period)
         DO UPDATE SET status = 'OPEN', locked_at = NULL`,
        [tenantId, period],
      );
    });
  }

  /**
   * Run work inside a transaction that first binds the tenant GUC
   * (`app.tenant_id`) so RLS policies engage; `is_local = true` scopes the
   * binding to this transaction so a pooled connection never leaks it. Mirrors
   * PgLedgerStore.withTenant so period locks are isolated the same way.
   */
  private async withTenant<T>(tenant: TenantId, fn: (q: Queryable) => Promise<T>): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      await client.query(SET_TENANT_GUC, [tenant]);
      const result = await fn(client);
      await client.query("COMMIT");
      return result;
    } catch (err) {
      await safeRollback(client);
      throw err;
    } finally {
      client.release();
    }
  }
}

/** Bind the tenant GUC the RLS policies key on (see `RLS_DDL` in schema.ts). */
const SET_TENANT_GUC = `SELECT set_config('app.tenant_id', $1, true)`;

async function safeRollback(client: PoolClient): Promise<void> {
  try {
    await client.query("ROLLBACK");
  } catch {
    /* connection may already be aborted */
  }
}
