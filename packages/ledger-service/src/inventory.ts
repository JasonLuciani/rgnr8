import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  type Currency,
  type PostCommand,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import { validateDimensions } from "./dimensions.js";
import { mulDiv } from "./estimates.js";
import { COST_CODE_DIMENSION, JOB_DIMENSION } from "./jobs.js";

/**
 * Inventory — what is on the shelf, what it cost, and whether the shelf agrees
 * with the balance sheet.
 *
 * Most small-business inventory features track quantities in one place and
 * value in another, and the two drift within a month. The design here is that
 * **quantity and value move together, and both move when the cost enters the
 * books**: receiving stock updates units and the moving average at the same
 * moment the journal entry posts, and issuing stock to a job relieves both.
 *
 * The valuation report ends with the number that matters: the difference
 * between what the item records say inventory is worth and what the inventory
 * account on the balance sheet says. In a system where those can disagree, they
 * eventually will, and the only useful thing to do about it is show the gap
 * rather than pick a side.
 *
 * ## Moving average, not FIFO
 *
 * Cost is a moving weighted average, recomputed on every receipt. FIFO is more
 * precise and needs layer tracking that a business with 200 SKUs and no
 * warehouse staff will not maintain correctly; an average that is always right
 * beats a layer scheme that is right until somebody backdates a receipt.
 */

export class InventoryError extends Error {}

export type ItemKind = "STOCK" | "NON_STOCK" | "SERVICE";
export type MovementKind = "RECEIPT" | "ISSUE" | "COUNT" | "WRITE_OFF";

const KINDS: readonly ItemKind[] = ["STOCK", "NON_STOCK", "SERVICE"];
const MILLI = 1000n;

export interface ItemRecord {
  readonly sku: string;
  readonly name: string;
  readonly kind: ItemKind;
  readonly unit: string;
  /** Units on hand, in thousandths. */
  readonly quantityMilli: string;
  /** Moving weighted average cost per unit. */
  readonly unitCostMinor: string;
  /** Total value on hand — kept alongside the average so rounding cannot drift. */
  readonly valueMinor: string;
  readonly inventoryAccountCode: string;
  readonly costAccountCode: string;
  readonly incomeAccountCode: string;
  readonly reorderPointMilli: string;
  readonly active: boolean;
}

export interface MovementRecord {
  readonly id: string;
  readonly sku: string;
  readonly date: string;
  readonly kind: MovementKind;
  /** Signed: positive in, negative out. */
  readonly quantityMilli: string;
  readonly unitCostMinor: string;
  readonly valueMinor: string;
  readonly reference: string;
  readonly entryId: string;
  readonly memo: string;
}

export interface InventoryStore {
  migrate(): Promise<void>;
  listItems(tenant: string): Promise<ItemRecord[]>;
  getItem(tenant: string, sku: string): Promise<ItemRecord | undefined>;
  saveItem(tenant: string, item: ItemRecord): Promise<void>;
  listMovements(tenant: string, sku?: string): Promise<MovementRecord[]>;
  saveMovement(tenant: string, movement: MovementRecord): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryInventoryStore implements InventoryStore {
  private readonly items = new Map<string, ItemRecord>();
  private readonly movements = new Map<string, MovementRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listItems(tenant: string): Promise<ItemRecord[]> {
    const out: ItemRecord[] = [];
    for (const [k, v] of this.items) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.sku.localeCompare(b.sku)));
  }

  getItem(tenant: string, sku: string): Promise<ItemRecord | undefined> {
    return Promise.resolve(this.items.get(`${tenant}::${sku}`));
  }

  saveItem(tenant: string, item: ItemRecord): Promise<void> {
    this.items.set(`${tenant}::${item.sku}`, item);
    return Promise.resolve();
  }

  listMovements(tenant: string, sku?: string): Promise<MovementRecord[]> {
    const out: MovementRecord[] = [];
    for (const [k, v] of this.movements) {
      if (!k.startsWith(`${tenant}::`)) continue;
      if (sku && v.sku !== sku) continue;
      out.push(v);
    }
    return Promise.resolve(out.sort((a, b) => (
      a.date === b.date ? a.id.localeCompare(b.id) : a.date.localeCompare(b.date)
    )));
  }

  saveMovement(tenant: string, movement: MovementRecord): Promise<void> {
    this.movements.set(`${tenant}::${movement.id}`, movement);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const INVENTORY_DDL = `
CREATE TABLE IF NOT EXISTS inventory_item (
  tenant_id              text NOT NULL,
  sku                    text NOT NULL,
  name                   text NOT NULL,
  kind                   text NOT NULL DEFAULT 'STOCK',
  unit                   text NOT NULL DEFAULT 'ea',
  quantity_milli         text NOT NULL DEFAULT '0',
  unit_cost_minor        text NOT NULL DEFAULT '0',
  value_minor            text NOT NULL DEFAULT '0',
  inventory_account_code text NOT NULL DEFAULT '1300',
  cost_account_code      text NOT NULL DEFAULT '5100',
  income_account_code    text NOT NULL DEFAULT '4100',
  reorder_point_milli    text NOT NULL DEFAULT '0',
  active                 boolean NOT NULL DEFAULT true,
  CONSTRAINT inventory_item_pk PRIMARY KEY (tenant_id, sku)
);

CREATE TABLE IF NOT EXISTS inventory_movement (
  tenant_id       text NOT NULL,
  id              text NOT NULL,
  sku             text NOT NULL,
  move_date       text NOT NULL,
  kind            text NOT NULL,
  quantity_milli  text NOT NULL DEFAULT '0',
  unit_cost_minor text NOT NULL DEFAULT '0',
  value_minor     text NOT NULL DEFAULT '0',
  reference       text NOT NULL DEFAULT '',
  entry_id        text NOT NULL DEFAULT '',
  memo            text NOT NULL DEFAULT '',
  CONSTRAINT inventory_movement_pk PRIMARY KEY (tenant_id, id)
);
`;

export class PgInventoryStore implements InventoryStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(INVENTORY_DDL);
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

  private itemFrom(r: Record<string, unknown>): ItemRecord {
    return {
      sku: String(r["sku"]),
      name: String(r["name"]),
      kind: String(r["kind"]) as ItemKind,
      unit: String(r["unit"] ?? "ea"),
      quantityMilli: String(r["quantity_milli"] ?? "0"),
      unitCostMinor: String(r["unit_cost_minor"] ?? "0"),
      valueMinor: String(r["value_minor"] ?? "0"),
      inventoryAccountCode: String(r["inventory_account_code"] ?? "1300"),
      costAccountCode: String(r["cost_account_code"] ?? "5100"),
      incomeAccountCode: String(r["income_account_code"] ?? "4100"),
      reorderPointMilli: String(r["reorder_point_milli"] ?? "0"),
      active: r["active"] !== false && r["active"] !== "f",
    };
  }

  async listItems(tenant: string): Promise<ItemRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM inventory_item WHERE tenant_id=$1 ORDER BY sku", [tenant],
      );
      return res.rows.map((r) => this.itemFrom(r));
    });
  }

  async getItem(tenant: string, sku: string): Promise<ItemRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM inventory_item WHERE tenant_id=$1 AND sku=$2", [tenant, sku],
      );
      const row = res.rows[0];
      return row ? this.itemFrom(row) : undefined;
    });
  }

  async saveItem(tenant: string, item: ItemRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO inventory_item (tenant_id, sku, name, kind, unit, quantity_milli,
         unit_cost_minor, value_minor, inventory_account_code, cost_account_code,
         income_account_code, reorder_point_milli, active)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
       ON CONFLICT (tenant_id, sku) DO UPDATE SET
         name=EXCLUDED.name, kind=EXCLUDED.kind, unit=EXCLUDED.unit,
         quantity_milli=EXCLUDED.quantity_milli, unit_cost_minor=EXCLUDED.unit_cost_minor,
         value_minor=EXCLUDED.value_minor,
         inventory_account_code=EXCLUDED.inventory_account_code,
         cost_account_code=EXCLUDED.cost_account_code,
         income_account_code=EXCLUDED.income_account_code,
         reorder_point_milli=EXCLUDED.reorder_point_milli, active=EXCLUDED.active`,
      [
        tenant, item.sku, item.name, item.kind, item.unit, item.quantityMilli,
        item.unitCostMinor, item.valueMinor, item.inventoryAccountCode,
        item.costAccountCode, item.incomeAccountCode, item.reorderPointMilli, item.active,
      ],
    ));
  }

  async listMovements(tenant: string, sku?: string): Promise<MovementRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = sku
        ? await db.query(
          `SELECT * FROM inventory_movement WHERE tenant_id=$1 AND sku=$2
           ORDER BY move_date, id`, [tenant, sku],
        )
        : await db.query(
          "SELECT * FROM inventory_movement WHERE tenant_id=$1 ORDER BY move_date, id", [tenant],
        );
      return res.rows.map((r) => ({
        id: String(r["id"]),
        sku: String(r["sku"]),
        date: String(r["move_date"]),
        kind: String(r["kind"]) as MovementKind,
        quantityMilli: String(r["quantity_milli"] ?? "0"),
        unitCostMinor: String(r["unit_cost_minor"] ?? "0"),
        valueMinor: String(r["value_minor"] ?? "0"),
        reference: String(r["reference"] ?? ""),
        entryId: String(r["entry_id"] ?? ""),
        memo: String(r["memo"] ?? ""),
      }));
    });
  }

  async saveMovement(tenant: string, movement: MovementRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO inventory_movement (tenant_id, id, sku, move_date, kind,
         quantity_milli, unit_cost_minor, value_minor, reference, entry_id, memo)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         sku=EXCLUDED.sku, move_date=EXCLUDED.move_date, kind=EXCLUDED.kind,
         quantity_milli=EXCLUDED.quantity_milli, unit_cost_minor=EXCLUDED.unit_cost_minor,
         value_minor=EXCLUDED.value_minor, reference=EXCLUDED.reference,
         entry_id=EXCLUDED.entry_id, memo=EXCLUDED.memo`,
      [
        tenant, movement.id, movement.sku, movement.date, movement.kind,
        movement.quantityMilli, movement.unitCostMinor, movement.valueMinor,
        movement.reference, movement.entryId, movement.memo,
      ],
    ));
  }
}

// --- the flow ----------------------------------------------------------------

export interface InventoryContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

function requireDate(raw: unknown, label: string): string {
  const date = String(raw ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new InventoryError(`${label} must be YYYY-MM-DD`);
  return date;
}

function wholeOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^-?\d+$/.test(value)) throw new InventoryError(`${label} must be a whole number`);
  return BigInt(value);
}

function provenanceFor(id: string, date: string, at: string): Provenance {
  return {
    sourceSystem: "inventory",
    sourceObject: id,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/inventory-1",
    mappingVersion: "ledger-service/inventory-1",
  };
}

export interface ItemInput {
  readonly sku?: string;
  readonly name?: string;
  readonly kind?: string;
  readonly unit?: string;
  readonly inventory_account_code?: string;
  readonly cost_account_code?: string;
  readonly income_account_code?: string;
  readonly reorder_point_milli?: string | number;
  readonly active?: boolean;
}

export async function saveItem(ctx: InventoryContext, input: ItemInput): Promise<ItemRecord> {
  const sku = String(input.sku ?? "").trim().toUpperCase();
  if (!sku) throw new InventoryError("an item needs a SKU");
  const name = String(input.name ?? "").trim();
  if (!name) throw new InventoryError("an item needs a name");
  const kind = String(input.kind ?? "STOCK").trim().toUpperCase() as ItemKind;
  if (!KINDS.includes(kind)) throw new InventoryError(`kind must be one of ${KINDS.join(", ")}`);

  const chart = await ctx.backend.chart(ctx.tenant);
  const need = (code: string, role: string, type: string) => {
    const account = chart.getByCode(code);
    if (!account) throw new InventoryError(`unknown account code ${code}`);
    if (account.type !== type) {
      throw new InventoryError(`${code} is not a ${role} account`);
    }
    return code;
  };

  const existing = await ctx.backend.inventory().getItem(String(ctx.tenant), sku);
  const inventoryAccountCode = kind === "STOCK"
    ? need(String(input.inventory_account_code ?? "").trim() || "1300", "balance-sheet", "ASSET")
    : "";
  const costAccountCode = need(
    String(input.cost_account_code ?? "").trim() || "5100", "cost", "EXPENSE",
  );
  const incomeAccountCode = need(
    String(input.income_account_code ?? "").trim() || "4100", "revenue", "REVENUE",
  );

  const item: ItemRecord = {
    sku,
    name,
    kind,
    unit: String(input.unit ?? "").trim() || "ea",
    quantityMilli: existing?.quantityMilli ?? "0",
    unitCostMinor: existing?.unitCostMinor ?? "0",
    valueMinor: existing?.valueMinor ?? "0",
    inventoryAccountCode,
    costAccountCode,
    incomeAccountCode,
    reorderPointMilli: wholeOf(input.reorder_point_milli, "reorder point").toString(),
    active: input.active !== false,
  };
  await ctx.backend.inventory().saveItem(String(ctx.tenant), item);
  return item;
}

async function requireStockItem(ctx: InventoryContext, sku: string): Promise<ItemRecord> {
  const item = await ctx.backend.inventory().getItem(String(ctx.tenant), sku.toUpperCase());
  if (!item) throw new InventoryError(`unknown item ${sku}`);
  if (item.kind !== "STOCK") {
    throw new InventoryError(`${sku} is a ${item.kind.toLowerCase()} item — it has no stock to move`);
  }
  return item;
}

/**
 * Add stock at a known cost and recompute the moving average.
 *
 * Non-posting: it is called from wherever the *cost* was posted — a purchase
 * bill, an accrued goods receipt, a direct receipt — so that units and value
 * only ever move when the money did.
 */
export async function applyReceipt(
  ctx: InventoryContext,
  input: {
    readonly sku: string; readonly date: string;
    readonly quantityMilli: bigint; readonly unitCostMinor: bigint;
    readonly reference?: string; readonly entryId?: string; readonly memo?: string;
    readonly id?: string;
  },
): Promise<ItemRecord> {
  const item = await requireStockItem(ctx, input.sku);
  if (input.quantityMilli <= 0n) {
    throw new InventoryError("a receipt needs a quantity");
  }
  const addedValue = mulDiv(input.quantityMilli, input.unitCostMinor, MILLI);
  const quantity = BigInt(item.quantityMilli) + input.quantityMilli;
  const value = BigInt(item.valueMinor) + addedValue;
  const updated: ItemRecord = {
    ...item,
    quantityMilli: quantity.toString(),
    valueMinor: value.toString(),
    // The average is derived from the totals, never accumulated, so rounding
    // cannot compound over a year of deliveries.
    unitCostMinor: quantity > 0n ? mulDiv(value, MILLI, quantity).toString() : "0",
  };
  await ctx.backend.inventory().saveItem(String(ctx.tenant), updated);
  await ctx.backend.inventory().saveMovement(String(ctx.tenant), {
    id: input.id ?? `${item.sku}-${input.date}-${(await ctx.backend.inventory()
      .listMovements(String(ctx.tenant), item.sku)).length + 1}`,
    sku: item.sku,
    date: input.date,
    kind: "RECEIPT",
    quantityMilli: input.quantityMilli.toString(),
    unitCostMinor: input.unitCostMinor.toString(),
    valueMinor: addedValue.toString(),
    reference: input.reference ?? "",
    entryId: input.entryId ?? "",
    memo: input.memo ?? "",
  });
  return updated;
}

/** Take stock out at the moving average. Non-posting, for the same reason. */
export async function applyIssue(
  ctx: InventoryContext,
  input: {
    readonly sku: string; readonly date: string; readonly quantityMilli: bigint;
    readonly reference?: string; readonly entryId?: string; readonly memo?: string;
    readonly kind?: MovementKind; readonly id?: string;
  },
): Promise<{ item: ItemRecord; valueMinor: bigint; unitCostMinor: bigint }> {
  const item = await requireStockItem(ctx, input.sku);
  if (input.quantityMilli <= 0n) throw new InventoryError("an issue needs a quantity");
  const onHand = BigInt(item.quantityMilli);
  if (input.quantityMilli > onHand) {
    throw new InventoryError(
      `${item.sku}: ${input.quantityMilli} thousandths issued against ${onHand} on hand — count it before you cost it`,
    );
  }
  const unitCost = BigInt(item.unitCostMinor);
  // The last issue takes the remaining value exactly, so a fully issued item is
  // worth nothing rather than a few cents of rounding.
  const value = input.quantityMilli === onHand
    ? BigInt(item.valueMinor)
    : mulDiv(input.quantityMilli, unitCost, MILLI);
  const quantity = onHand - input.quantityMilli;
  const remaining = BigInt(item.valueMinor) - value;
  const updated: ItemRecord = {
    ...item,
    quantityMilli: quantity.toString(),
    valueMinor: remaining.toString(),
    unitCostMinor: quantity > 0n ? mulDiv(remaining, MILLI, quantity).toString() : "0",
  };
  await ctx.backend.inventory().saveItem(String(ctx.tenant), updated);
  await ctx.backend.inventory().saveMovement(String(ctx.tenant), {
    id: input.id ?? `${item.sku}-${input.date}-${(await ctx.backend.inventory()
      .listMovements(String(ctx.tenant), item.sku)).length + 1}`,
    sku: item.sku,
    date: input.date,
    kind: input.kind ?? "ISSUE",
    quantityMilli: (-input.quantityMilli).toString(),
    unitCostMinor: unitCost.toString(),
    valueMinor: (-value).toString(),
    reference: input.reference ?? "",
    entryId: input.entryId ?? "",
    memo: input.memo ?? "",
  });
  return { item: updated, valueMinor: value, unitCostMinor: unitCost };
}

export interface ReceiveInput {
  readonly id?: string;
  readonly sku?: string;
  readonly date?: string;
  readonly quantity_milli?: string | number;
  readonly unit_cost_minor?: string | number;
  /** Where the money came from — a bank account, or AP if it's on terms. */
  readonly paid_from_code?: string;
  readonly reference?: string;
  readonly memo?: string;
}

/**
 * Buy stock directly — the trip to the supply house, with no purchase order.
 * Debits inventory and credits wherever the money came from.
 */
export async function receiveStock(
  ctx: InventoryContext, input: ReceiveInput,
): Promise<{ item: ItemRecord; entryId: string }> {
  const sku = String(input.sku ?? "").trim().toUpperCase();
  const item = await requireStockItem(ctx, sku);
  const date = requireDate(input.date, "date");
  const quantity = wholeOf(input.quantity_milli, "quantity");
  const unitCost = wholeOf(input.unit_cost_minor, "unit cost");
  if (quantity <= 0n) throw new InventoryError("a receipt needs a quantity");
  if (unitCost <= 0n) throw new InventoryError("a receipt needs a unit cost");

  const chart = await ctx.backend.chart(ctx.tenant);
  const inventory = chart.getByCode(item.inventoryAccountCode);
  const paidFromCode = String(input.paid_from_code ?? "").trim() || "1000";
  const paidFrom = chart.getByCode(paidFromCode);
  if (!inventory) throw new InventoryError(`unknown account code ${item.inventoryAccountCode}`);
  if (!paidFrom) throw new InventoryError(`unknown account code ${paidFromCode}`);

  const value = mulDiv(quantity, unitCost, MILLI);
  const id = String(input.id ?? "").trim() || `${sku}-${date}`;
  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`stock-in:${id}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: String(input.memo ?? "") || `Stock received — ${item.name}`,
    provenance: provenanceFor(id, date, ctx.now()),
    lines: [
      {
        accountId: inventory.id,
        side: "DEBIT",
        amount: Money.fromMinorUnits(value, ctx.currency),
        memo: item.name,
      },
      {
        accountId: paidFrom.id,
        side: "CREDIT",
        amount: Money.fromMinorUnits(value, ctx.currency),
        memo: item.name,
      },
    ],
  };
  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  const entry = await engine.post(command, { postedAt: ctx.now() });
  const updated = await applyReceipt(ctx, {
    sku,
    date,
    quantityMilli: quantity,
    unitCostMinor: unitCost,
    reference: String(input.reference ?? ""),
    entryId: String(entry.id),
    id: `mv:${id}`,
    memo: String(input.memo ?? ""),
  });
  return { item: updated, entryId: String(entry.id) };
}

export interface IssueInput {
  readonly id?: string;
  readonly sku?: string;
  readonly date?: string;
  readonly quantity_milli?: string | number;
  readonly job_id?: string;
  readonly cost_code?: string;
  readonly work_order_id?: string;
  readonly cost_account_code?: string;
  readonly memo?: string;
}

/**
 * Issue stock to a job. Debits the job's cost at the moving average and takes
 * the value off the shelf, which is the whole point: material bought in bulk in
 * March lands on the job that used it in June.
 */
export async function issueStock(
  ctx: InventoryContext, input: IssueInput,
): Promise<{ item: ItemRecord; entryId: string; valueMinor: string }> {
  const sku = String(input.sku ?? "").trim().toUpperCase();
  const item = await requireStockItem(ctx, sku);
  const date = requireDate(input.date, "date");
  const quantity = wholeOf(input.quantity_milli, "quantity");
  if (quantity <= 0n) throw new InventoryError("an issue needs a quantity");

  const jobId = String(input.job_id ?? "").trim();
  if (jobId && !await ctx.backend.jobs().getJob(String(ctx.tenant), jobId)) {
    throw new InventoryError(`unknown job ${jobId}`);
  }
  const costCode = String(input.cost_code ?? "").trim().toUpperCase();

  const chart = await ctx.backend.chart(ctx.tenant);
  const costCodeAccount = costCode
    ? (await ctx.backend.jobs().listCostCodes(String(ctx.tenant)))
      .find((c) => c.code === costCode)?.accountCode
    : undefined;
  const costAccountCode = String(input.cost_account_code ?? "").trim()
    || costCodeAccount
    || item.costAccountCode;
  const cost = chart.getByCode(costAccountCode);
  const inventory = chart.getByCode(item.inventoryAccountCode);
  if (!cost) throw new InventoryError(`unknown account code ${costAccountCode}`);
  if (!inventory) throw new InventoryError(`unknown account code ${item.inventoryAccountCode}`);

  const id = String(input.id ?? "").trim() || `${sku}-${date}-out`;
  // Value the issue first so the entry and the shelf agree exactly.
  const preview = await requireStockItem(ctx, sku);
  const onHand = BigInt(preview.quantityMilli);
  if (quantity > onHand) {
    throw new InventoryError(
      `${sku}: ${quantity} thousandths issued against ${onHand} on hand — count it before you cost it`,
    );
  }
  const value = quantity === onHand
    ? BigInt(preview.valueMinor)
    : mulDiv(quantity, BigInt(preview.unitCostMinor), MILLI);
  if (value === 0n) throw new InventoryError(`${sku} has no value on hand to issue`);

  const dimensions = jobId
    ? { [JOB_DIMENSION]: jobId, ...(costCode ? { [COST_CODE_DIMENSION]: costCode } : {}) }
    : undefined;
  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`stock-out:${id}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: String(input.memo ?? "") || `Issued to ${jobId || "the job"} — ${item.name}`,
    provenance: provenanceFor(id, date, ctx.now()),
    lines: [
      {
        accountId: cost.id,
        side: "DEBIT",
        amount: Money.fromMinorUnits(value, ctx.currency),
        memo: item.name,
        ...(dimensions ? { dimensions } : {}),
      },
      {
        accountId: inventory.id,
        side: "CREDIT",
        amount: Money.fromMinorUnits(value, ctx.currency),
        memo: item.name,
      },
    ],
  };
  await validateDimensions(
    { backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency }, command,
  );
  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  const entry = await engine.post(command, { postedAt: ctx.now() });
  const result = await applyIssue(ctx, {
    sku,
    date,
    quantityMilli: quantity,
    reference: String(input.work_order_id ?? jobId ?? ""),
    entryId: String(entry.id),
    id: `mv:${id}`,
    memo: String(input.memo ?? ""),
  });
  return {
    item: result.item,
    entryId: String(entry.id),
    valueMinor: result.valueMinor.toString(),
  };
}

export interface CountInput {
  readonly id?: string;
  readonly sku?: string;
  readonly date?: string;
  /** What is actually on the shelf. */
  readonly counted_milli?: string | number;
  readonly adjustment_account_code?: string;
  readonly memo?: string;
}

/**
 * A physical count. The difference between the shelf and the record is posted
 * to shrinkage at the moving average — the point of counting is that the books
 * follow the shelf, not the other way round.
 */
export async function recordCount(
  ctx: InventoryContext, input: CountInput,
): Promise<{ item: ItemRecord; entryId: string; differenceMilli: string }> {
  const sku = String(input.sku ?? "").trim().toUpperCase();
  const item = await requireStockItem(ctx, sku);
  const date = requireDate(input.date, "date");
  const counted = wholeOf(input.counted_milli, "counted quantity");
  if (counted < 0n) throw new InventoryError("a count cannot be negative");

  const onHand = BigInt(item.quantityMilli);
  const difference = counted - onHand;
  if (difference === 0n) {
    return { item, entryId: "", differenceMilli: "0" };
  }

  const chart = await ctx.backend.chart(ctx.tenant);
  const adjustmentCode = String(input.adjustment_account_code ?? "").trim()
    || (chart.getByCode("5300") ? "5300" : item.costAccountCode);
  const adjustment = chart.getByCode(adjustmentCode);
  const inventory = chart.getByCode(item.inventoryAccountCode);
  if (!adjustment) throw new InventoryError(`unknown account code ${adjustmentCode}`);
  if (!inventory) throw new InventoryError(`unknown account code ${item.inventoryAccountCode}`);

  const unitCost = BigInt(item.unitCostMinor);
  const value = difference > 0n
    ? mulDiv(difference, unitCost, MILLI)
    : (counted === 0n ? BigInt(item.valueMinor) : mulDiv(-difference, unitCost, MILLI));
  if (value === 0n) {
    // A quantity change on an item with no value is a record correction, not a
    // journal entry; the ledger is in money, not units.
    const updated: ItemRecord = { ...item, quantityMilli: counted.toString() };
    await ctx.backend.inventory().saveItem(String(ctx.tenant), updated);
    return { item: updated, entryId: "", differenceMilli: difference.toString() };
  }

  const id = String(input.id ?? "").trim() || `${sku}-count-${date}`;
  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`stock-count:${id}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: String(input.memo ?? "") || `Stock count — ${item.name}`,
    provenance: provenanceFor(id, date, ctx.now()),
    lines: difference > 0n
      ? [
        {
          accountId: inventory.id, side: "DEBIT" as const,
          amount: Money.fromMinorUnits(value, ctx.currency), memo: "Found on count",
        },
        {
          accountId: adjustment.id, side: "CREDIT" as const,
          amount: Money.fromMinorUnits(value, ctx.currency), memo: "Found on count",
        },
      ]
      : [
        {
          accountId: adjustment.id, side: "DEBIT" as const,
          amount: Money.fromMinorUnits(value, ctx.currency), memo: "Shrinkage",
        },
        {
          accountId: inventory.id, side: "CREDIT" as const,
          amount: Money.fromMinorUnits(value, ctx.currency), memo: "Shrinkage",
        },
      ],
  };
  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  const entry = await engine.post(command, { postedAt: ctx.now() });

  let updated: ItemRecord;
  if (difference > 0n) {
    updated = await applyReceipt(ctx, {
      sku, date, quantityMilli: difference, unitCostMinor: unitCost,
      entryId: String(entry.id), id: `mv:${id}`, memo: "Found on count",
      reference: "count",
    });
  } else {
    updated = (await applyIssue(ctx, {
      sku, date, quantityMilli: -difference, kind: "COUNT",
      entryId: String(entry.id), id: `mv:${id}`, memo: "Shrinkage",
      reference: "count",
    })).item;
  }
  return { item: updated, entryId: String(entry.id), differenceMilli: difference.toString() };
}

export function itemJson(i: ItemRecord): Record<string, unknown> {
  return {
    sku: i.sku,
    name: i.name,
    kind: i.kind,
    unit: i.unit,
    quantity_milli: i.quantityMilli,
    unit_cost_minor: i.unitCostMinor,
    value_minor: i.valueMinor,
    inventory_account_code: i.inventoryAccountCode,
    cost_account_code: i.costAccountCode,
    income_account_code: i.incomeAccountCode,
    reorder_point_milli: i.reorderPointMilli,
    active: i.active,
    below_reorder_point: BigInt(i.reorderPointMilli) > 0n
      && BigInt(i.quantityMilli) <= BigInt(i.reorderPointMilli),
  };
}

export function movementJson(m: MovementRecord): Record<string, unknown> {
  return {
    id: m.id,
    sku: m.sku,
    date: m.date,
    kind: m.kind,
    quantity_milli: m.quantityMilli,
    unit_cost_minor: m.unitCostMinor,
    value_minor: m.valueMinor,
    reference: m.reference,
    entry_id: m.entryId,
    memo: m.memo,
  };
}

/**
 * What stock is worth, and whether the balance sheet agrees.
 *
 * The tie-out is the reason this report exists. Two systems that can disagree
 * eventually will, and the only useful response is to show the gap rather than
 * quietly pick a side.
 */
export async function valuation(
  ctx: InventoryContext,
): Promise<Record<string, unknown>> {
  const items = (await ctx.backend.inventory().listItems(String(ctx.tenant)))
    .filter((i) => i.kind === "STOCK");
  const chart = await ctx.backend.chart(ctx.tenant);

  const byAccount = new Map<string, bigint>();
  let total = 0n;
  for (const item of items) {
    const value = BigInt(item.valueMinor);
    total += value;
    byAccount.set(
      item.inventoryAccountCode,
      (byAccount.get(item.inventoryAccountCode) ?? 0n) + value,
    );
  }

  // What the ledger says those accounts hold.
  const ledgerByAccount = new Map<string, bigint>();
  for (const entry of await ctx.backend.store(ctx.tenant).list(ctx.tenant)) {
    for (const line of entry.lines) {
      const account = chart.get(line.accountId);
      if (!account || !byAccount.has(account.code)) continue;
      const signed = line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
      ledgerByAccount.set(account.code, (ledgerByAccount.get(account.code) ?? 0n) + signed);
    }
  }
  let ledgerTotal = 0n;
  for (const v of ledgerByAccount.values()) ledgerTotal += v;

  return {
    contract: "inventory-valuation/1",
    currency: ctx.currency.code,
    items: items.map(itemJson),
    accounts: [...byAccount.entries()].map(([code, value]) => ({
      account_code: code,
      items_value_minor: value.toString(),
      ledger_balance_minor: (ledgerByAccount.get(code) ?? 0n).toString(),
      difference_minor: (value - (ledgerByAccount.get(code) ?? 0n)).toString(),
    })),
    totals: {
      items_value_minor: total.toString(),
      ledger_balance_minor: ledgerTotal.toString(),
      difference_minor: (total - ledgerTotal).toString(),
      ties_out: total === ledgerTotal,
    },
    reorder: items
      .filter((i) => BigInt(i.reorderPointMilli) > 0n
        && BigInt(i.quantityMilli) <= BigInt(i.reorderPointMilli))
      .map((i) => ({
        sku: i.sku,
        name: i.name,
        quantity_milli: i.quantityMilli,
        reorder_point_milli: i.reorderPointMilli,
      })),
  };
}
