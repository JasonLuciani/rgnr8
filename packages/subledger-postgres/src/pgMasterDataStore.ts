import { Money, getCurrency, type AccountId } from "@rgnr8/ledger-kernel";
import type {
  Address,
  Customer,
  Item,
  ItemType,
  MasterDataStore,
  Vendor,
} from "@rgnr8/subledger";
import { MASTER_DATA_DDL } from "./schema.js";

/** Minimal query seam (satisfied by node-postgres Pool and pg-mem). */
export interface QueryResult {
  rows: Record<string, unknown>[];
}
export interface Queryable {
  query(text: string, values?: unknown[]): Promise<QueryResult>;
}
export interface PoolClient extends Queryable {
  release(): void;
}
export interface Pool extends Queryable {
  connect(): Promise<PoolClient>;
}

/**
 * Durable, per-tenant master-data store — the SQL counterpart to the in-memory
 * `InMemoryMasterDataStore`. Because persistence is inherently asynchronous, the
 * API is async (the in-memory `MasterDataStore` interface is synchronous); use
 * {@link hydrate} to load a tenant's records into an in-memory store for the
 * synchronous document/posting paths, and write through this store on edits.
 *
 * Money is stored as minor units + currency; addresses as JSONB. Every access is
 * tenant-scoped (RLS is the second isolation layer).
 */
export class PgMasterDataStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(MASTER_DATA_DDL);
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

  // --- customers ------------------------------------------------------------

  async upsertCustomer(tenant: string, c: Customer): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO md_customer (tenant_id, id, name, email, phone, billing_address, terms_days, tax_exempt, notes, active)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           name=EXCLUDED.name, email=EXCLUDED.email, phone=EXCLUDED.phone,
           billing_address=EXCLUDED.billing_address, terms_days=EXCLUDED.terms_days,
           tax_exempt=EXCLUDED.tax_exempt, notes=EXCLUDED.notes, active=EXCLUDED.active`,
        [
          tenant, c.id, c.name, c.email ?? null, c.phone ?? null,
          c.billingAddress ? JSON.stringify(c.billingAddress) : null,
          c.termsDays ?? null, c.taxExempt ?? null, c.notes ?? null, c.active ?? true,
        ],
      ),
    );
  }

  async getCustomer(tenant: string, id: string): Promise<Customer | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM md_customer WHERE tenant_id=$1 AND id=$2", [tenant, id]);
      return res.rows[0] ? rowToCustomer(res.rows[0]) : undefined;
    });
  }

  async listCustomers(tenant: string): Promise<Customer[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM md_customer WHERE tenant_id=$1", [tenant]);
      return res.rows.map(rowToCustomer);
    });
  }

  // --- vendors --------------------------------------------------------------

  async upsertVendor(tenant: string, v: Vendor): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO md_vendor (tenant_id, id, name, email, phone, address, terms_days, is_1099, tax_id, default_expense_account_id, active)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           name=EXCLUDED.name, email=EXCLUDED.email, phone=EXCLUDED.phone, address=EXCLUDED.address,
           terms_days=EXCLUDED.terms_days, is_1099=EXCLUDED.is_1099, tax_id=EXCLUDED.tax_id,
           default_expense_account_id=EXCLUDED.default_expense_account_id, active=EXCLUDED.active`,
        [
          tenant, v.id, v.name, v.email ?? null, v.phone ?? null,
          v.address ? JSON.stringify(v.address) : null,
          v.termsDays ?? null, v.is1099 ?? null, v.taxId ?? null,
          v.defaultExpenseAccountId ?? null, v.active ?? true,
        ],
      ),
    );
  }

  async getVendor(tenant: string, id: string): Promise<Vendor | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM md_vendor WHERE tenant_id=$1 AND id=$2", [tenant, id]);
      return res.rows[0] ? rowToVendor(res.rows[0]) : undefined;
    });
  }

  async listVendors(tenant: string): Promise<Vendor[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM md_vendor WHERE tenant_id=$1", [tenant]);
      return res.rows.map(rowToVendor);
    });
  }

  // --- items ----------------------------------------------------------------

  async upsertItem(tenant: string, i: Item): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO md_item (tenant_id, id, name, sku, type, unit_price_minor, unit_price_currency, income_account_id, expense_account_id, asset_account_id, taxable, active)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           name=EXCLUDED.name, sku=EXCLUDED.sku, type=EXCLUDED.type,
           unit_price_minor=EXCLUDED.unit_price_minor, unit_price_currency=EXCLUDED.unit_price_currency,
           income_account_id=EXCLUDED.income_account_id, expense_account_id=EXCLUDED.expense_account_id,
           asset_account_id=EXCLUDED.asset_account_id, taxable=EXCLUDED.taxable, active=EXCLUDED.active`,
        [
          tenant, i.id, i.name, i.sku ?? null, i.type,
          i.unitPrice ? i.unitPrice.minorUnits.toString() : null,
          i.unitPrice ? i.unitPrice.currency.code : null,
          i.incomeAccountId ?? null, i.expenseAccountId ?? null, i.assetAccountId ?? null,
          i.taxable ?? null, i.active ?? true,
        ],
      ),
    );
  }

  async getItem(tenant: string, id: string): Promise<Item | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM md_item WHERE tenant_id=$1 AND id=$2", [tenant, id]);
      return res.rows[0] ? rowToItem(res.rows[0]) : undefined;
    });
  }

  async listItems(tenant: string): Promise<Item[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM md_item WHERE tenant_id=$1", [tenant]);
      return res.rows.map(rowToItem);
    });
  }

  /** Load a tenant's full master data into a synchronous in-memory store. */
  async hydrate(tenant: string, into: MasterDataStore): Promise<void> {
    for (const c of await this.listCustomers(tenant)) into.upsertCustomer(tenant, c);
    for (const v of await this.listVendors(tenant)) into.upsertVendor(tenant, v);
    for (const i of await this.listItems(tenant)) into.upsertItem(tenant, i);
  }
}

// --- row mappers ------------------------------------------------------------

function parseJson<T>(v: unknown): T | undefined {
  if (v === null || v === undefined) return undefined;
  if (typeof v === "object") return v as T;
  try {
    return JSON.parse(String(v)) as T;
  } catch {
    return undefined;
  }
}
function optStr(v: unknown): string | undefined {
  return v === null || v === undefined ? undefined : String(v);
}
function optInt(v: unknown): number | undefined {
  return v === null || v === undefined ? undefined : Number(v);
}
function optBool(v: unknown): boolean | undefined {
  return v === null || v === undefined ? undefined : Boolean(v);
}

function rowToCustomer(r: Record<string, unknown>): Customer {
  return {
    id: String(r["id"]),
    name: String(r["name"]),
    ...(optStr(r["email"]) !== undefined ? { email: optStr(r["email"])! } : {}),
    ...(optStr(r["phone"]) !== undefined ? { phone: optStr(r["phone"])! } : {}),
    ...(parseJson<Address>(r["billing_address"]) ? { billingAddress: parseJson<Address>(r["billing_address"])! } : {}),
    ...(optInt(r["terms_days"]) !== undefined ? { termsDays: optInt(r["terms_days"])! } : {}),
    ...(optBool(r["tax_exempt"]) !== undefined ? { taxExempt: optBool(r["tax_exempt"])! } : {}),
    ...(optStr(r["notes"]) !== undefined ? { notes: optStr(r["notes"])! } : {}),
    active: optBool(r["active"]) ?? true,
  };
}

function rowToVendor(r: Record<string, unknown>): Vendor {
  return {
    id: String(r["id"]),
    name: String(r["name"]),
    ...(optStr(r["email"]) !== undefined ? { email: optStr(r["email"])! } : {}),
    ...(optStr(r["phone"]) !== undefined ? { phone: optStr(r["phone"])! } : {}),
    ...(parseJson<Address>(r["address"]) ? { address: parseJson<Address>(r["address"])! } : {}),
    ...(optInt(r["terms_days"]) !== undefined ? { termsDays: optInt(r["terms_days"])! } : {}),
    ...(optBool(r["is_1099"]) !== undefined ? { is1099: optBool(r["is_1099"])! } : {}),
    ...(optStr(r["tax_id"]) !== undefined ? { taxId: optStr(r["tax_id"])! } : {}),
    ...(optStr(r["default_expense_account_id"]) !== undefined
      ? { defaultExpenseAccountId: optStr(r["default_expense_account_id"])! as AccountId }
      : {}),
    active: optBool(r["active"]) ?? true,
  };
}

function rowToItem(r: Record<string, unknown>): Item {
  const priceMinor = r["unit_price_minor"];
  const priceCcy = optStr(r["unit_price_currency"]);
  const unitPrice =
    priceMinor !== null && priceMinor !== undefined && priceCcy
      ? Money.fromMinorUnits(BigInt(String(priceMinor)), getCurrency(priceCcy))
      : undefined;
  return {
    id: String(r["id"]),
    name: String(r["name"]),
    ...(optStr(r["sku"]) !== undefined ? { sku: optStr(r["sku"])! } : {}),
    type: String(r["type"]) as ItemType,
    ...(unitPrice ? { unitPrice } : {}),
    ...(optStr(r["income_account_id"]) !== undefined ? { incomeAccountId: optStr(r["income_account_id"])! as AccountId } : {}),
    ...(optStr(r["expense_account_id"]) !== undefined ? { expenseAccountId: optStr(r["expense_account_id"])! as AccountId } : {}),
    ...(optStr(r["asset_account_id"]) !== undefined ? { assetAccountId: optStr(r["asset_account_id"])! as AccountId } : {}),
    ...(optBool(r["taxable"]) !== undefined ? { taxable: optBool(r["taxable"])! } : {}),
    active: optBool(r["active"]) ?? true,
  };
}
