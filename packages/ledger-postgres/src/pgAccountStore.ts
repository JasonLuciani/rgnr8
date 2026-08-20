import {
  AccountType,
  AccountSubtype,
  ChartOfAccounts,
  USD,
  getCurrency,
  templateAccounts,
  type Account,
  type AccountId,
  type BusinessCategory,
  type Currency,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import { ACCOUNT_DDL } from "./accountSchema.js";
import type { Pool, Queryable } from "./pgLedgerStore.js";

/**
 * Persistent, per-tenant chart of accounts.
 *
 * Closes the last "in-memory only" gap in the core: accounts now live in a
 * table instead of being rebuilt at startup. `upsert` is idempotent (insert or
 * update by id), `loadChart` rehydrates a full {@link ChartOfAccounts} for a
 * tenant (which re-runs the kernel's structural validation — subtype↔type,
 * parent integrity — so a persisted chart is always well-formed), and
 * `deactivate` soft-hides an account without deleting its history.
 */
export class PgAccountStore {
  constructor(private readonly pool: Pool) {}

  /** Create the account table if absent. Safe to run repeatedly. */
  async migrate(): Promise<void> {
    await this.pool.query(ACCOUNT_DDL);
  }

  private async withTenant<T>(tenant: TenantId, fn: (db: Queryable) => Promise<T>): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [tenant]);
      const result = await fn(client);
      await client.query("COMMIT");
      return result;
    } catch (err) {
      try {
        await client.query("ROLLBACK");
      } catch {
        /* ignore */
      }
      throw err;
    } finally {
      client.release();
    }
  }

  /** Insert or update one account for a tenant. */
  async upsert(tenant: TenantId, account: Account): Promise<void> {
    await this.withTenant(tenant, (db) => upsertOne(db, tenant, account));
  }

  /**
   * Persist every account in a chart in ONE transaction (used to seed a tenant
   * and by go-live). Atomic: either the whole chart is written or none of it,
   * so a partially-written chart can never be observed. Implements the kernel's
   * `ChartStore` contract.
   */
  async saveChart(tenant: TenantId, coa: ChartOfAccounts): Promise<void> {
    await this.withTenant(tenant, async (db) => {
      for (const account of coa.list()) await upsertOne(db, tenant, account);
    });
  }

  /**
   * Seed a new tenant's chart of accounts from a business-category template
   * (onboarding). Returns the accounts that were written. Idempotent via
   * `upsert`, so re-seeding the same category is safe.
   */
  async seedFromTemplate(
    tenant: TenantId,
    category: BusinessCategory,
    currency: Currency = USD,
  ): Promise<Account[]> {
    const accounts = templateAccounts(category, currency);
    await this.withTenant(tenant, async (db) => {
      for (const account of accounts) await upsertOne(db, tenant, account);
    });
    return accounts;
  }

  /** All persisted accounts for a tenant as domain objects. */
  async listAccounts(tenant: TenantId): Promise<Account[]> {
    return this.withTenant(tenant, async (db) => {
      const res = await db.query(
        "SELECT id, code, name, type, currency_code, subtype, parent_id, active FROM account WHERE tenant_id = $1",
        [tenant],
      );
      return res.rows.map((r) => rowToAccount(r));
    });
  }

  /** Rehydrate a validated ChartOfAccounts for a tenant. */
  async loadChart(tenant: TenantId): Promise<ChartOfAccounts> {
    const accounts = await this.listAccounts(tenant);
    return new ChartOfAccounts(accounts);
  }

  /** Soft-deactivate an account (keeps history; hidden from active pickers). */
  async deactivate(tenant: TenantId, id: AccountId): Promise<void> {
    await this.withTenant(tenant, async (db) => {
      await db.query("UPDATE account SET active = false WHERE tenant_id = $1 AND id = $2", [tenant, id]);
    });
  }
}

/** Upsert one account on an already-open (tenant-bound) connection. */
async function upsertOne(db: Queryable, tenant: TenantId, account: Account): Promise<void> {
  await db.query(
    `INSERT INTO account (tenant_id, id, code, name, type, currency_code, subtype, parent_id, active)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
     ON CONFLICT (tenant_id, id) DO UPDATE SET
       code = EXCLUDED.code, name = EXCLUDED.name, type = EXCLUDED.type,
       currency_code = EXCLUDED.currency_code, subtype = EXCLUDED.subtype,
       parent_id = EXCLUDED.parent_id, active = EXCLUDED.active`,
    [
      tenant,
      account.id,
      account.code,
      account.name,
      account.type,
      account.currency.code,
      account.subtype ?? null,
      account.parentId ?? null,
      account.active ?? true,
    ],
  );
}

function rowToAccount(r: Record<string, unknown>): Account {
  const subtype = r["subtype"] as string | null;
  const parentId = r["parent_id"] as string | null;
  return {
    id: String(r["id"]) as AccountId,
    code: String(r["code"]),
    name: String(r["name"]),
    type: String(r["type"]) as AccountType,
    currency: getCurrency(String(r["currency_code"])),
    ...(subtype ? { subtype: subtype as AccountSubtype } : {}),
    ...(parentId ? { parentId: parentId as AccountId } : {}),
    active: r["active"] === undefined || r["active"] === null ? true : Boolean(r["active"]),
  };
}
