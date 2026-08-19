/**
 * Per-account settings — the options an account administrator turns on or off.
 *
 * These are deliberately server-side and tenant-scoped: the ledger engine reads
 * them (an inventory issue must know whether the account costs at moving average
 * or FIFO; consolidation must know the base currency), so they cannot live only
 * in the web app. One row per tenant, RLS-protected like everything else, with a
 * safe default for every field so a tenant that has never opened the settings
 * screen still behaves exactly as it always did.
 *
 * Authorization is the web app's job (the `MANAGE_SETTINGS` /
 * `MANAGE_DATA_RETENTION` permissions gate who may write these); the store's job
 * is only to persist them per tenant.
 */

import type { Pool, Queryable } from "@rgnr8/ledger-postgres";

/**
 * The inventory cost-flow assumption an account uses.
 *
 * - `MOVING_AVERAGE` — a single blended unit cost, recomputed on every receipt.
 *   No layers to maintain; always internally consistent. The historical default.
 * - `FIFO` — first in, first out: issues relieve the oldest cost layer first.
 * - `LIFO` — last in, first out: issues relieve the newest layer first. Permitted
 *   under US GAAP, prohibited under IFRS — an account-level choice, never forced.
 * - `SPECIFIC` — specific identification: an issue names the exact lot it draws
 *   from (for serialised or high-value goods). Absent a named lot, falls back to
 *   oldest-first so a shrinkage write-down still has a defined cost.
 *
 * `FIFO`, `LIFO`, and `SPECIFIC` are *lot-based* — they keep per-receipt cost
 * layers. `MOVING_AVERAGE` keeps one blended figure and no layers.
 */
export type InventoryCostingMethod =
  | "MOVING_AVERAGE"
  | "FIFO"
  | "LIFO"
  | "SPECIFIC";

export const INVENTORY_COSTING_METHODS: readonly InventoryCostingMethod[] = [
  "MOVING_AVERAGE",
  "FIFO",
  "LIFO",
  "SPECIFIC",
];

/** Methods that maintain per-receipt cost layers (as opposed to a blended cost). */
export const LOT_BASED_METHODS: readonly InventoryCostingMethod[] = [
  "FIFO",
  "LIFO",
  "SPECIFIC",
];

export function usesLots(method: InventoryCostingMethod): boolean {
  return LOT_BASED_METHODS.includes(method);
}

export interface AccountSettings {
  /** How inventory is costed. Moving average is the historical default. */
  readonly inventoryCostingMethod: InventoryCostingMethod;
  /** The currency the account's books and consolidated group report in. */
  readonly baseCurrency: string;
  /** Whether group members may report in a currency other than the base. */
  readonly multiCurrencyEnabled: boolean;
  /** Days to keep audit-log rows before a retention sweep removes them; 0 = keep forever. */
  readonly retentionAuditDays: number;
  /** Days to keep soft-deleted rows before a sweep hard-removes them; 0 = keep forever. */
  readonly retentionSoftDeleteDays: number;
}

/** The behaviour a brand-new account gets — identical to the pre-settings world. */
export function defaultSettings(baseCurrency = "USD"): AccountSettings {
  return {
    inventoryCostingMethod: "MOVING_AVERAGE",
    baseCurrency,
    multiCurrencyEnabled: false,
    retentionAuditDays: 0,
    retentionSoftDeleteDays: 0,
  };
}

export class SettingsError extends Error {}

/**
 * Validate and normalise a partial patch against the current settings. Keys are
 * the snake_case wire names (the same shape `settingsJson` emits), so a caller
 * reads and writes settings in one vocabulary. Only the fields present in the
 * patch change; every value is validated, never silently coerced to a default.
 */
export function mergeSettings(
  current: AccountSettings, patch: Record<string, unknown>,
): AccountSettings {
  const next = { ...current } as {
    inventoryCostingMethod: InventoryCostingMethod;
    baseCurrency: string;
    multiCurrencyEnabled: boolean;
    retentionAuditDays: number;
    retentionSoftDeleteDays: number;
  };
  if (patch["inventory_costing_method"] !== undefined) {
    const m = String(patch["inventory_costing_method"]).trim().toUpperCase() as InventoryCostingMethod;
    if (!INVENTORY_COSTING_METHODS.includes(m)) {
      throw new SettingsError(`inventory costing method must be one of ${INVENTORY_COSTING_METHODS.join(", ")}`);
    }
    next.inventoryCostingMethod = m;
  }
  if (patch["base_currency"] !== undefined) {
    const c = String(patch["base_currency"]).trim().toUpperCase();
    if (!/^[A-Z]{3}$/.test(c)) throw new SettingsError("base currency must be a 3-letter code");
    next.baseCurrency = c;
  }
  if (patch["multi_currency_enabled"] !== undefined) {
    next.multiCurrencyEnabled = toBool(patch["multi_currency_enabled"]);
  }
  if (patch["retention_audit_days"] !== undefined) {
    next.retentionAuditDays = toDays(patch["retention_audit_days"], "audit retention");
  }
  if (patch["retention_soft_delete_days"] !== undefined) {
    next.retentionSoftDeleteDays = toDays(patch["retention_soft_delete_days"], "soft-delete retention");
  }
  return next;
}

function toBool(v: unknown): boolean {
  if (typeof v === "boolean") return v;
  const s = String(v).trim().toLowerCase();
  return s === "true" || s === "1" || s === "yes" || s === "on";
}

function toDays(v: unknown, label: string): number {
  const n = Number(v);
  if (!Number.isInteger(n) || n < 0) throw new SettingsError(`${label} must be a whole number of days, 0 or more`);
  return n;
}

export function settingsJson(s: AccountSettings): Record<string, unknown> {
  return {
    inventory_costing_method: s.inventoryCostingMethod,
    base_currency: s.baseCurrency,
    multi_currency_enabled: s.multiCurrencyEnabled,
    retention_audit_days: s.retentionAuditDays,
    retention_soft_delete_days: s.retentionSoftDeleteDays,
  };
}

// --- the store seam ----------------------------------------------------------

export interface SettingsStore {
  migrate(): Promise<void>;
  get(tenant: string): Promise<AccountSettings>;
  save(tenant: string, settings: AccountSettings): Promise<void>;
}

export class InMemorySettingsStore implements SettingsStore {
  private readonly rows = new Map<string, AccountSettings>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  get(tenant: string): Promise<AccountSettings> {
    return Promise.resolve(this.rows.get(tenant) ?? defaultSettings());
  }

  save(tenant: string, settings: AccountSettings): Promise<void> {
    this.rows.set(tenant, settings);
    return Promise.resolve();
  }
}

export const SETTINGS_DDL = `
CREATE TABLE IF NOT EXISTS account_settings (
  tenant_id                  text PRIMARY KEY,
  inventory_costing_method   text    NOT NULL DEFAULT 'MOVING_AVERAGE',
  base_currency              text    NOT NULL DEFAULT 'USD',
  multi_currency_enabled     boolean NOT NULL DEFAULT false,
  retention_audit_days       integer NOT NULL DEFAULT 0,
  retention_soft_delete_days integer NOT NULL DEFAULT 0
);
`;

export class PgSettingsStore implements SettingsStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(SETTINGS_DDL);
  }

  private async tx<T>(tenant: string, fn: (db: Queryable) => Promise<T>): Promise<T> {
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

  async get(tenant: string): Promise<AccountSettings> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT inventory_costing_method, base_currency, multi_currency_enabled,
                retention_audit_days, retention_soft_delete_days
         FROM account_settings WHERE tenant_id=$1`,
        [tenant],
      );
      const r = res.rows[0];
      if (!r) return defaultSettings();
      return {
        inventoryCostingMethod: String(r["inventory_costing_method"] ?? "MOVING_AVERAGE") as InventoryCostingMethod,
        baseCurrency: String(r["base_currency"] ?? "USD"),
        multiCurrencyEnabled: r["multi_currency_enabled"] === true || r["multi_currency_enabled"] === "t",
        retentionAuditDays: Number(r["retention_audit_days"] ?? 0),
        retentionSoftDeleteDays: Number(r["retention_soft_delete_days"] ?? 0),
      };
    });
  }

  async save(tenant: string, s: AccountSettings): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO account_settings
           (tenant_id, inventory_costing_method, base_currency, multi_currency_enabled,
            retention_audit_days, retention_soft_delete_days)
         VALUES ($1, $2, $3, $4, $5, $6)
         ON CONFLICT (tenant_id) DO UPDATE SET
           inventory_costing_method=EXCLUDED.inventory_costing_method,
           base_currency=EXCLUDED.base_currency,
           multi_currency_enabled=EXCLUDED.multi_currency_enabled,
           retention_audit_days=EXCLUDED.retention_audit_days,
           retention_soft_delete_days=EXCLUDED.retention_soft_delete_days`,
        [
          tenant, s.inventoryCostingMethod, s.baseCurrency, s.multiCurrencyEnabled,
          s.retentionAuditDays, s.retentionSoftDeleteDays,
        ],
      ),
    );
  }
}
