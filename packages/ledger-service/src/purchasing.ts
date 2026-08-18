import {
  AccountSubtype,
  Money,
  PostingEngine,
  accountTypeOfSubtype,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  type Account,
  type Currency,
  type PostCommand,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import { validateDimensions } from "./dimensions.js";
import { mulDiv } from "./estimates.js";

/**
 * Purchase orders, receiving, and the three-way match.
 *
 * A purchase order that nobody matches is a PDF. The control only exists when
 * three documents have to agree: what was **ordered**, what was **received**,
 * and what the vendor **billed**. Any two of them agreeing is normal; all three
 * disagreeing is how a business pays twice for the same lumber, or pays for a
 * delivery that never came.
 *
 * So this refuses to bill more than was received, refuses to receive more than
 * was ordered, and treats a price that differs from the order as a variance to
 * be seen and accepted rather than a number to be silently believed.
 *
 * ## Committed cost — the number that stops a job going over quietly
 *
 * A job is not fine because only half its framing budget has been billed, if
 * the rest is already on a signed purchase order. Committed cost — ordered and
 * not yet in the books — is what makes that visible, and it is the single most
 * useful thing a purchase order gives a contractor. It flows straight into the
 * job cost report beside budget and actual.
 *
 * ## Receiving before the bill arrives
 *
 * Material can sit on a job for six weeks before the invoice turns up. Without
 * an accrual, the job looks cheap for six weeks and then loses money in a
 * single afternoon.
 *
 * Receiving can therefore **accrue**: debit the job's cost, credit Goods
 * Received Not Invoiced. When the bill lands it relieves the accrual instead of
 * booking the cost a second time, and any difference between the accrued price
 * and the billed price is posted as an explicit **purchase price variance** on
 * the job — visible, dated, and attributable, rather than buried in a balance
 * that never quite clears.
 *
 * Accruing is optional, because it is a real accrual promise: a business that
 * would rather keep its books on what it has actually been billed can receive
 * without it and still get the match and the committed-cost figure.
 */

export class PurchasingError extends Error {}

export type PurchaseOrderStatus =
  | "DRAFT" | "OPEN" | "PARTIAL" | "RECEIVED" | "CLOSED" | "CANCELLED";

const MILLI = 1000n;

/** Where cost sits between the delivery and the invoice. */
export const GRNI_CODE = "2150";

export interface PurchaseOrderLineRecord {
  readonly lineNo: number;
  readonly description: string;
  readonly costCode: string;
  readonly accountCode: string;
  readonly quantityMilli: string;
  readonly receivedMilli: string;
  /** How much of what was received has been accrued into the books. */
  readonly accruedMilli: string;
  readonly billedMilli: string;
  readonly unitPriceMinor: string;
}

export interface PurchaseOrderRecord {
  readonly id: string;
  readonly vendorId: string;
  readonly jobId: string;
  readonly date: string;
  readonly expectedDate: string;
  readonly status: PurchaseOrderStatus;
  readonly memo: string;
  readonly lines: readonly PurchaseOrderLineRecord[];
}

export interface ReceiptLineRecord {
  readonly lineNo: number;
  readonly quantityMilli: string;
}

export interface ReceiptRecord {
  readonly id: string;
  readonly purchaseOrderId: string;
  readonly date: string;
  readonly memo: string;
  readonly accrued: boolean;
  readonly entryId: string;
  readonly lines: readonly ReceiptLineRecord[];
}

export interface PurchasingStore {
  migrate(): Promise<void>;
  list(tenant: string): Promise<PurchaseOrderRecord[]>;
  get(tenant: string, id: string): Promise<PurchaseOrderRecord | undefined>;
  save(tenant: string, record: PurchaseOrderRecord): Promise<void>;
  remove(tenant: string, id: string): Promise<void>;
  listReceipts(tenant: string, purchaseOrderId?: string): Promise<ReceiptRecord[]>;
  saveReceipt(tenant: string, receipt: ReceiptRecord): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryPurchasingStore implements PurchasingStore {
  private readonly orders = new Map<string, PurchaseOrderRecord>();
  private readonly receipts = new Map<string, ReceiptRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  list(tenant: string): Promise<PurchaseOrderRecord[]> {
    const out: PurchaseOrderRecord[] = [];
    for (const [k, v] of this.orders) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.id.localeCompare(b.id)));
  }

  get(tenant: string, id: string): Promise<PurchaseOrderRecord | undefined> {
    return Promise.resolve(this.orders.get(`${tenant}::${id}`));
  }

  save(tenant: string, record: PurchaseOrderRecord): Promise<void> {
    this.orders.set(`${tenant}::${record.id}`, record);
    return Promise.resolve();
  }

  remove(tenant: string, id: string): Promise<void> {
    this.orders.delete(`${tenant}::${id}`);
    return Promise.resolve();
  }

  listReceipts(tenant: string, purchaseOrderId?: string): Promise<ReceiptRecord[]> {
    const out: ReceiptRecord[] = [];
    for (const [k, v] of this.receipts) {
      if (!k.startsWith(`${tenant}::`)) continue;
      if (purchaseOrderId && v.purchaseOrderId !== purchaseOrderId) continue;
      out.push(v);
    }
    return Promise.resolve(out.sort((a, b) => (
      a.date === b.date ? a.id.localeCompare(b.id) : a.date.localeCompare(b.date)
    )));
  }

  saveReceipt(tenant: string, receipt: ReceiptRecord): Promise<void> {
    this.receipts.set(`${tenant}::${receipt.id}`, receipt);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const PURCHASING_DDL = `
CREATE TABLE IF NOT EXISTS purchase_order (
  tenant_id     text NOT NULL,
  id            text NOT NULL,
  vendor_id     text NOT NULL,
  job_id        text NOT NULL DEFAULT '',
  po_date       text NOT NULL,
  expected_date text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'OPEN',
  memo          text NOT NULL DEFAULT '',
  CONSTRAINT purchase_order_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS purchase_order_line (
  tenant_id        text NOT NULL,
  order_id         text NOT NULL,
  line_no          integer NOT NULL,
  description      text NOT NULL DEFAULT '',
  cost_code        text NOT NULL DEFAULT '',
  account_code     text NOT NULL DEFAULT '',
  quantity_milli   text NOT NULL DEFAULT '0',
  received_milli   text NOT NULL DEFAULT '0',
  accrued_milli    text NOT NULL DEFAULT '0',
  billed_milli     text NOT NULL DEFAULT '0',
  unit_price_minor text NOT NULL DEFAULT '0',
  CONSTRAINT purchase_order_line_pk PRIMARY KEY (tenant_id, order_id, line_no)
);

CREATE TABLE IF NOT EXISTS goods_receipt (
  tenant_id text NOT NULL,
  id        text NOT NULL,
  order_id  text NOT NULL,
  gr_date   text NOT NULL,
  memo      text NOT NULL DEFAULT '',
  accrued   boolean NOT NULL DEFAULT false,
  entry_id  text NOT NULL DEFAULT '',
  CONSTRAINT goods_receipt_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS goods_receipt_line (
  tenant_id      text NOT NULL,
  receipt_id     text NOT NULL,
  line_no        integer NOT NULL,
  quantity_milli text NOT NULL DEFAULT '0',
  CONSTRAINT goods_receipt_line_pk PRIMARY KEY (tenant_id, receipt_id, line_no)
);
`;

export class PgPurchasingStore implements PurchasingStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(PURCHASING_DDL);
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

  private async linesFor(
    db: Queryable, tenant: string, id: string,
  ): Promise<PurchaseOrderLineRecord[]> {
    const res = await db.query(
      "SELECT * FROM purchase_order_line WHERE tenant_id=$1 AND order_id=$2 ORDER BY line_no",
      [tenant, id],
    );
    return res.rows.map((r) => ({
      lineNo: Number(r["line_no"]),
      description: String(r["description"] ?? ""),
      costCode: String(r["cost_code"] ?? ""),
      accountCode: String(r["account_code"] ?? ""),
      quantityMilli: String(r["quantity_milli"] ?? "0"),
      receivedMilli: String(r["received_milli"] ?? "0"),
      accruedMilli: String(r["accrued_milli"] ?? "0"),
      billedMilli: String(r["billed_milli"] ?? "0"),
      unitPriceMinor: String(r["unit_price_minor"] ?? "0"),
    }));
  }

  private fromRow(
    r: Record<string, unknown>, lines: PurchaseOrderLineRecord[],
  ): PurchaseOrderRecord {
    return {
      id: String(r["id"]),
      vendorId: String(r["vendor_id"]),
      jobId: String(r["job_id"] ?? ""),
      date: String(r["po_date"]),
      expectedDate: String(r["expected_date"] ?? ""),
      status: String(r["status"]) as PurchaseOrderStatus,
      memo: String(r["memo"] ?? ""),
      lines,
    };
  }

  async list(tenant: string): Promise<PurchaseOrderRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM purchase_order WHERE tenant_id=$1 ORDER BY id", [tenant],
      );
      const out: PurchaseOrderRecord[] = [];
      for (const row of res.rows) {
        out.push(this.fromRow(row, await this.linesFor(db, tenant, String(row["id"]))));
      }
      return out;
    });
  }

  async get(tenant: string, id: string): Promise<PurchaseOrderRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM purchase_order WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      return this.fromRow(row, await this.linesFor(db, tenant, id));
    });
  }

  async save(tenant: string, record: PurchaseOrderRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO purchase_order (tenant_id, id, vendor_id, job_id, po_date,
           expected_date, status, memo)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           vendor_id=EXCLUDED.vendor_id, job_id=EXCLUDED.job_id, po_date=EXCLUDED.po_date,
           expected_date=EXCLUDED.expected_date, status=EXCLUDED.status, memo=EXCLUDED.memo`,
        [
          tenant, record.id, record.vendorId, record.jobId, record.date,
          record.expectedDate, record.status, record.memo,
        ],
      );
      await db.query(
        "DELETE FROM purchase_order_line WHERE tenant_id=$1 AND order_id=$2", [tenant, record.id],
      );
      for (const l of record.lines) {
        await db.query(
          `INSERT INTO purchase_order_line (tenant_id, order_id, line_no, description,
             cost_code, account_code, quantity_milli, received_milli, accrued_milli,
             billed_milli, unit_price_minor)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)`,
          [
            tenant, record.id, l.lineNo, l.description, l.costCode, l.accountCode,
            l.quantityMilli, l.receivedMilli, l.accruedMilli, l.billedMilli,
            l.unitPriceMinor,
          ],
        );
      }
    });
  }

  async remove(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM purchase_order_line WHERE tenant_id=$1 AND order_id=$2", [tenant, id],
      );
      await db.query("DELETE FROM purchase_order WHERE tenant_id=$1 AND id=$2", [tenant, id]);
    });
  }

  async listReceipts(tenant: string, purchaseOrderId?: string): Promise<ReceiptRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = purchaseOrderId
        ? await db.query(
          "SELECT * FROM goods_receipt WHERE tenant_id=$1 AND order_id=$2 ORDER BY gr_date, id",
          [tenant, purchaseOrderId],
        )
        : await db.query(
          "SELECT * FROM goods_receipt WHERE tenant_id=$1 ORDER BY gr_date, id", [tenant],
        );
      const out: ReceiptRecord[] = [];
      for (const row of res.rows) {
        const lines = await db.query(
          `SELECT line_no, quantity_milli FROM goods_receipt_line
           WHERE tenant_id=$1 AND receipt_id=$2 ORDER BY line_no`,
          [tenant, String(row["id"])],
        );
        out.push({
          id: String(row["id"]),
          purchaseOrderId: String(row["order_id"]),
          date: String(row["gr_date"]),
          memo: String(row["memo"] ?? ""),
          accrued: row["accrued"] === true || row["accrued"] === "t",
          entryId: String(row["entry_id"] ?? ""),
          lines: lines.rows.map((l) => ({
            lineNo: Number(l["line_no"]),
            quantityMilli: String(l["quantity_milli"] ?? "0"),
          })),
        });
      }
      return out;
    });
  }

  async saveReceipt(tenant: string, receipt: ReceiptRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO goods_receipt (tenant_id, id, order_id, gr_date, memo, accrued, entry_id)
         VALUES ($1,$2,$3,$4,$5,$6,$7)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           order_id=EXCLUDED.order_id, gr_date=EXCLUDED.gr_date, memo=EXCLUDED.memo,
           accrued=EXCLUDED.accrued, entry_id=EXCLUDED.entry_id`,
        [
          tenant, receipt.id, receipt.purchaseOrderId, receipt.date, receipt.memo,
          receipt.accrued, receipt.entryId,
        ],
      );
      await db.query(
        "DELETE FROM goods_receipt_line WHERE tenant_id=$1 AND receipt_id=$2",
        [tenant, receipt.id],
      );
      for (const l of receipt.lines) {
        await db.query(
          `INSERT INTO goods_receipt_line (tenant_id, receipt_id, line_no, quantity_milli)
           VALUES ($1,$2,$3,$4)`,
          [tenant, receipt.id, l.lineNo, l.quantityMilli],
        );
      }
    });
  }
}

// --- the flow ----------------------------------------------------------------

export interface PurchasingContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

function requireDate(raw: unknown, label: string, required = true): string {
  const date = String(raw ?? "").trim();
  if (!date) {
    if (required) throw new PurchasingError(`${label} is required`);
    return "";
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new PurchasingError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

function wholeOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^\d+$/.test(value)) throw new PurchasingError(`${label} must be a whole number`);
  return BigInt(value);
}

async function ensureGrni(ctx: PurchasingContext): Promise<Account> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const existing = chart.getByCode(GRNI_CODE);
  if (existing) return existing;
  const account: Account = {
    id: asAccountId(`acct:${GRNI_CODE}`),
    code: GRNI_CODE,
    name: "Goods Received Not Invoiced",
    type: accountTypeOfSubtype(AccountSubtype.OTHER_CURRENT_LIABILITY),
    currency: ctx.currency,
    subtype: AccountSubtype.OTHER_CURRENT_LIABILITY,
    active: true,
  };
  await ctx.backend.saveAccount(ctx.tenant, account);
  return account;
}

function provenanceFor(source: string, id: string, date: string, at: string): Provenance {
  return {
    sourceSystem: source,
    sourceObject: id,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/purchasing-1",
    mappingVersion: "ledger-service/purchasing-1",
  };
}

export interface PurchaseOrderInput {
  readonly id?: string;
  readonly vendor_id?: string;
  readonly job_id?: string;
  readonly date?: string;
  readonly expected_date?: string;
  readonly memo?: string;
  readonly lines?: ReadonlyArray<{
    readonly description?: string;
    readonly cost_code?: string;
    readonly account_code?: string;
    readonly quantity_milli?: string | number;
    readonly unit_price_minor?: string | number;
  }>;
}

export async function savePurchaseOrder(
  ctx: PurchasingContext, input: PurchaseOrderInput,
): Promise<PurchaseOrderRecord> {
  const store = ctx.backend.purchasing();

  const vendorId = String(input.vendor_id ?? "").trim();
  if (!vendorId) throw new PurchasingError("a purchase order needs a vendor");
  if (!await ctx.backend.documents().getParty(String(ctx.tenant), "vendor", vendorId)) {
    throw new PurchasingError(`unknown vendor ${vendorId}`);
  }

  const jobId = String(input.job_id ?? "").trim();
  if (jobId && !await ctx.backend.jobs().getJob(String(ctx.tenant), jobId)) {
    throw new PurchasingError(`unknown job ${jobId}`);
  }

  const date = requireDate(input.date, "date");
  const expectedDate = requireDate(input.expected_date, "expected date", false);

  const id = String(input.id ?? "").trim() || `PO-${date.replace(/-/g, "")}`;
  const existing = await store.get(String(ctx.tenant), id);
  if (existing && existing.lines.some((l) => BigInt(l.receivedMilli) > 0n)) {
    throw new PurchasingError(
      `purchase order ${id} has receipts against it — change what is outstanding, not the order`,
    );
  }

  const chart = await ctx.backend.chart(ctx.tenant);
  const costCodes = new Map(
    (await ctx.backend.jobs().listCostCodes(String(ctx.tenant))).map((c) => [c.code, c]),
  );

  const raw = input.lines ?? [];
  if (raw.length === 0) throw new PurchasingError("a purchase order needs at least one line");
  const lines: PurchaseOrderLineRecord[] = [];
  let lineNo = 1;
  for (const l of raw) {
    const quantityMilli = wholeOf(l.quantity_milli ?? 1000, `line ${lineNo} quantity`);
    if (quantityMilli === 0n) {
      throw new PurchasingError(`line ${lineNo}: quantity must be more than zero`);
    }
    const unitPrice = wholeOf(l.unit_price_minor, `line ${lineNo} unit price`);
    if (unitPrice === 0n) throw new PurchasingError(`line ${lineNo}: needs a price`);

    const costCode = String(l.cost_code ?? "").trim().toUpperCase();
    if (costCode && !costCodes.has(costCode)) {
      throw new PurchasingError(`unknown cost code ${costCode}`);
    }
    const accountCode = String(l.account_code ?? "").trim()
      || costCodes.get(costCode)?.accountCode
      || "";
    const account = chart.getByCode(accountCode);
    if (!account) throw new PurchasingError(`line ${lineNo}: unknown account code ${accountCode || "(none)"}`);
    if (account.type !== "EXPENSE" && account.subtype !== AccountSubtype.INVENTORY) {
      throw new PurchasingError(
        `${accountCode} is neither a cost account nor inventory — a purchase has to land in one of them`,
      );
    }

    lines.push({
      lineNo,
      description: String(l.description ?? "").trim(),
      costCode,
      accountCode,
      quantityMilli: quantityMilli.toString(),
      receivedMilli: "0",
      accruedMilli: "0",
      billedMilli: "0",
      unitPriceMinor: unitPrice.toString(),
    });
    lineNo += 1;
  }

  const record: PurchaseOrderRecord = {
    id,
    vendorId,
    jobId,
    date,
    expectedDate,
    status: existing?.status ?? "OPEN",
    memo: String(input.memo ?? "").trim(),
    lines,
  };
  await store.save(String(ctx.tenant), record);
  return record;
}

function statusOf(record: PurchaseOrderRecord): PurchaseOrderStatus {
  if (record.status === "CANCELLED" || record.status === "CLOSED") return record.status;
  let anyReceived = false;
  let allReceived = true;
  for (const l of record.lines) {
    const received = BigInt(l.receivedMilli);
    if (received > 0n) anyReceived = true;
    if (received < BigInt(l.quantityMilli)) allReceived = false;
  }
  if (allReceived) return "RECEIVED";
  return anyReceived ? "PARTIAL" : "OPEN";
}

export interface ReceiveInput {
  readonly id?: string;
  readonly date?: string;
  readonly memo?: string;
  /** Accrue the cost into the books now, against Goods Received Not Invoiced. */
  readonly accrue?: boolean;
  readonly lines?: ReadonlyArray<{
    readonly line_no?: number;
    readonly quantity_milli?: string | number;
  }>;
}

export interface ReceiveResult {
  readonly receipt: ReceiptRecord;
  readonly order: PurchaseOrderRecord;
  readonly accruedMinor: string;
}

/**
 * Record what turned up. Over-receiving is refused: a delivery bigger than the
 * order is either a mistake or a change to the order, and both want a person.
 */
export async function receive(
  ctx: PurchasingContext, orderId: string, input: ReceiveInput,
): Promise<ReceiveResult> {
  const store = ctx.backend.purchasing();
  const order = await store.get(String(ctx.tenant), orderId);
  if (!order) throw new PurchasingError(`unknown purchase order ${orderId}`);
  if (order.status === "CANCELLED") {
    throw new PurchasingError(`purchase order ${orderId} was cancelled`);
  }
  const date = requireDate(input.date, "date");

  const wanted = new Map<number, bigint>();
  if (input.lines && input.lines.length > 0) {
    for (const l of input.lines) {
      const lineNo = Number(l.line_no ?? 0);
      const line = order.lines.find((x) => x.lineNo === lineNo);
      if (!line) throw new PurchasingError(`purchase order ${orderId} has no line ${lineNo}`);
      const outstanding = BigInt(line.quantityMilli) - BigInt(line.receivedMilli);
      const quantity = l.quantity_milli === undefined || String(l.quantity_milli).trim() === ""
        ? outstanding
        : wholeOf(l.quantity_milli, `line ${lineNo} quantity`);
      if (quantity === 0n) continue;
      if (quantity > outstanding) {
        throw new PurchasingError(
          `line ${lineNo}: ${quantity} thousandths received against ${outstanding} outstanding — change the order, or send it back`,
        );
      }
      wanted.set(lineNo, quantity);
    }
  } else {
    for (const line of order.lines) {
      const outstanding = BigInt(line.quantityMilli) - BigInt(line.receivedMilli);
      if (outstanding > 0n) wanted.set(line.lineNo, outstanding);
    }
  }
  if (wanted.size === 0) throw new PurchasingError(`purchase order ${orderId} is fully received`);

  const receiptId = String(input.id ?? "").trim()
    || `${orderId}-R${(await store.listReceipts(String(ctx.tenant), orderId)).length + 1}`;

  let entryId = "";
  let accruedMinor = 0n;
  const accrue = input.accrue === true;
  if (accrue) {
    const chart = await ctx.backend.chart(ctx.tenant);
    const grni = await ensureGrni(ctx);
    const lines = [];
    for (const line of order.lines) {
      const quantity = wanted.get(line.lineNo);
      if (!quantity) continue;
      const amount = mulDiv(quantity, BigInt(line.unitPriceMinor), MILLI);
      if (amount === 0n) continue;
      accruedMinor += amount;
      const account = chart.getByCode(line.accountCode);
      if (!account) throw new PurchasingError(`unknown account code ${line.accountCode}`);
      lines.push({
        accountId: account.id,
        side: "DEBIT" as const,
        amount: Money.fromMinorUnits(amount, ctx.currency),
        memo: line.description || `Received on ${orderId}`,
        ...(order.jobId
          ? {
            dimensions: {
              job: order.jobId,
              ...(line.costCode ? { cost_code: line.costCode } : {}),
            },
          }
          : {}),
      });
    }
    if (accruedMinor > 0n) {
      const command: PostCommand = {
        tenantId: ctx.tenant,
        idempotencyKey: asIdempotencyKey(`receipt:${receiptId}`),
        periodKey: asPeriodKey(date.slice(0, 7)),
        currency: ctx.currency,
        entryDate: date,
        memo: String(input.memo ?? "") || `Goods received on ${orderId}`,
        provenance: provenanceFor("goods-receipt", receiptId, date, ctx.now()),
        lines: [
          ...lines,
          {
            accountId: grni.id,
            side: "CREDIT" as const,
            amount: Money.fromMinorUnits(accruedMinor, ctx.currency),
            memo: `Awaiting ${order.vendorId}'s invoice`,
          },
        ],
      };
      await validateDimensions(
        { backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency }, command,
      );
      const engine = new PostingEngine(
        await ctx.backend.chart(ctx.tenant),
        ctx.backend.store(ctx.tenant),
        ctx.backend.periods(ctx.tenant),
      );
      entryId = String((await engine.post(command, { postedAt: ctx.now() })).id);
    }
  }

  const updated: PurchaseOrderRecord = {
    ...order,
    lines: order.lines.map((l) => {
      const quantity = wanted.get(l.lineNo);
      if (!quantity) return l;
      return {
        ...l,
        receivedMilli: (BigInt(l.receivedMilli) + quantity).toString(),
        accruedMilli: accrue
          ? (BigInt(l.accruedMilli) + quantity).toString()
          : l.accruedMilli,
      };
    }),
  };
  const withStatus = { ...updated, status: statusOf(updated) };
  await store.save(String(ctx.tenant), withStatus);

  const receipt: ReceiptRecord = {
    id: receiptId,
    purchaseOrderId: orderId,
    date,
    memo: String(input.memo ?? "").trim(),
    accrued: accrue && accruedMinor > 0n,
    entryId,
    lines: [...wanted.entries()].map(([lineNo, quantityMilli]) => ({
      lineNo, quantityMilli: quantityMilli.toString(),
    })),
  };
  await store.saveReceipt(String(ctx.tenant), receipt);
  return { receipt, order: withStatus, accruedMinor: accruedMinor.toString() };
}

export interface MatchLineInput {
  readonly line_no?: number;
  readonly quantity_milli?: string | number;
  /** What the vendor actually charged, if it isn't what was ordered. */
  readonly unit_price_minor?: string | number;
}

export interface MatchInput {
  readonly id?: string;
  readonly date?: string;
  readonly due_date?: string;
  readonly memo?: string;
  /** Bill a price that differs from the order. Refused unless this is set. */
  readonly accept_variance?: boolean;
  readonly lines?: readonly MatchLineInput[];
}

export interface PriceVariance {
  readonly line_no: number;
  readonly ordered_minor: string;
  readonly billed_minor: string;
  readonly variance_minor: string;
}

export interface MatchDraft {
  readonly request: Record<string, unknown>;
  readonly order: PurchaseOrderRecord;
  readonly variances: readonly PriceVariance[];
  /** Correction to Goods Received Not Invoiced, when the price moved. */
  readonly grniAdjustment: {
    readonly minor: bigint;
    readonly lines: ReadonlyArray<{
      readonly accountCode: string;
      readonly costCode: string;
      readonly minor: bigint;
    }>;
  };
}

/**
 * The third leg of the match: the vendor's bill against what was ordered and
 * what arrived.
 *
 * Billing more than was received is refused outright — that is the control the
 * whole structure exists for. A price that differs from the order is allowed,
 * but only deliberately, and it is reported as a variance rather than absorbed.
 *
 * Where a receipt was accrued, the bill relieves the accrual instead of booking
 * the cost twice, and the difference between the accrued price and the billed
 * price is corrected on the job with an explicit entry.
 */
export async function matchToBill(
  ctx: PurchasingContext, orderId: string, input: MatchInput,
): Promise<MatchDraft> {
  const store = ctx.backend.purchasing();
  const order = await store.get(String(ctx.tenant), orderId);
  if (!order) throw new PurchasingError(`unknown purchase order ${orderId}`);
  if (order.status === "CANCELLED") {
    throw new PurchasingError(`purchase order ${orderId} was cancelled`);
  }
  const date = requireDate(input.date, "date");
  const billId = String(input.id ?? "").trim();
  if (!billId) throw new PurchasingError("the bill needs an id");

  const wanted = new Map<number, { quantity: bigint; price: bigint }>();
  const chosen: readonly MatchLineInput[] = input.lines && input.lines.length > 0
    ? input.lines
    : order.lines
      .filter((l) => BigInt(l.receivedMilli) > BigInt(l.billedMilli))
      .map((l): MatchLineInput => ({ line_no: l.lineNo }));

  for (const l of chosen) {
    const lineNo = Number(l.line_no ?? 0);
    const line = order.lines.find((x) => x.lineNo === lineNo);
    if (!line) throw new PurchasingError(`purchase order ${orderId} has no line ${lineNo}`);
    const billable = BigInt(line.receivedMilli) - BigInt(line.billedMilli);
    const quantity = l.quantity_milli === undefined || String(l.quantity_milli).trim() === ""
      ? billable
      : wholeOf(l.quantity_milli, `line ${lineNo} quantity`);
    if (quantity === 0n) continue;
    if (quantity > billable) {
      const received = BigInt(line.receivedMilli);
      throw new PurchasingError(
        `line ${lineNo}: billed for ${quantity} but only ${billable} of the ${received} received are unbilled — pay for what arrived`,
      );
    }
    const price = l.unit_price_minor === undefined || String(l.unit_price_minor).trim() === ""
      ? BigInt(line.unitPriceMinor)
      : wholeOf(l.unit_price_minor, `line ${lineNo} unit price`);
    wanted.set(lineNo, { quantity, price });
  }
  if (wanted.size === 0) {
    throw new PurchasingError(
      `purchase order ${orderId} has nothing received and unbilled — receive it first`,
    );
  }

  const variances: PriceVariance[] = [];
  for (const [lineNo, { quantity, price }] of wanted) {
    const line = order.lines.find((x) => x.lineNo === lineNo)!;
    const ordered = BigInt(line.unitPriceMinor);
    if (price === ordered) continue;
    variances.push({
      line_no: lineNo,
      ordered_minor: mulDiv(quantity, ordered, MILLI).toString(),
      billed_minor: mulDiv(quantity, price, MILLI).toString(),
      variance_minor: mulDiv(quantity, price - ordered, MILLI).toString(),
    });
  }
  if (variances.length > 0 && input.accept_variance !== true) {
    const total = variances.reduce((acc, v) => acc + BigInt(v.variance_minor), 0n);
    throw new PurchasingError(
      `the bill differs from the order by ${total} minor units on ${variances.length} line(s) — accept the variance deliberately, or query it with ${order.vendorId}`,
    );
  }

  // Where a receipt was accrued, the cost is already in the books. Bill against
  // the accrual, then correct it for the price difference.
  const billLines: Record<string, unknown>[] = [];
  const grniLines: { accountCode: string; costCode: string; minor: bigint }[] = [];
  let grniAdjustment = 0n;

  for (const line of order.lines) {
    const match = wanted.get(line.lineNo);
    if (!match) continue;
    const billed = BigInt(line.billedMilli);
    const accrued = BigInt(line.accruedMilli);
    const againstAccrual = accrued > billed
      ? (match.quantity < accrued - billed ? match.quantity : accrued - billed)
      : 0n;
    const fresh = match.quantity - againstAccrual;

    if (againstAccrual > 0n) {
      const atBillPrice = mulDiv(againstAccrual, match.price, MILLI);
      const atAccrualPrice = mulDiv(againstAccrual, BigInt(line.unitPriceMinor), MILLI);
      billLines.push({
        description: line.description || `${orderId} line ${line.lineNo}`,
        unit_amount_minor: atBillPrice.toString(),
        account_code: GRNI_CODE,
      });
      const difference = atAccrualPrice - atBillPrice;
      if (difference !== 0n) {
        grniAdjustment += difference;
        grniLines.push({
          accountCode: line.accountCode,
          costCode: line.costCode,
          minor: difference,
        });
      }
    }
    if (fresh > 0n) {
      billLines.push({
        description: line.description || `${orderId} line ${line.lineNo}`,
        unit_amount_minor: mulDiv(fresh, match.price, MILLI).toString(),
        account_code: line.accountCode,
        ...(order.jobId
          ? {
            dimensions: {
              job: order.jobId,
              ...(line.costCode ? { cost_code: line.costCode } : {}),
            },
          }
          : {}),
      });
    }
  }

  const updated: PurchaseOrderRecord = {
    ...order,
    lines: order.lines.map((l) => {
      const match = wanted.get(l.lineNo);
      return match
        ? { ...l, billedMilli: (BigInt(l.billedMilli) + match.quantity).toString() }
        : l;
    }),
  };

  return {
    request: {
      id: billId,
      party_id: order.vendorId,
      date,
      ...(input.due_date ? { due_date: input.due_date } : {}),
      memo: String(input.memo ?? order.memo ?? "") || `From purchase order ${orderId}`,
      lines: billLines,
    },
    order: { ...updated, status: statusOf(updated) },
    variances,
    grniAdjustment: { minor: grniAdjustment, lines: grniLines },
  };
}

/**
 * Post the correction that clears Goods Received Not Invoiced when the bill's
 * price differed from what was accrued. The difference lands on the job as a
 * purchase price variance, which is exactly where it belongs.
 */
export async function postGrniAdjustment(
  ctx: PurchasingContext,
  order: PurchaseOrderRecord,
  billId: string,
  date: string,
  adjustment: MatchDraft["grniAdjustment"],
): Promise<string> {
  if (adjustment.minor === 0n || adjustment.lines.length === 0) return "";
  const chart = await ctx.backend.chart(ctx.tenant);
  const grni = await ensureGrni(ctx);
  const lines = [];
  for (const l of adjustment.lines) {
    if (l.minor === 0n) continue;
    const account = chart.getByCode(l.accountCode);
    if (!account) throw new PurchasingError(`unknown account code ${l.accountCode}`);
    lines.push({
      accountId: account.id,
      // A positive difference means we accrued more than we were billed, so the
      // job's cost comes down.
      side: (l.minor > 0n ? "CREDIT" : "DEBIT") as "CREDIT" | "DEBIT",
      amount: Money.fromMinorUnits(l.minor > 0n ? l.minor : -l.minor, ctx.currency),
      memo: "Purchase price variance",
      ...(order.jobId
        ? {
          dimensions: {
            job: order.jobId,
            ...(l.costCode ? { cost_code: l.costCode } : {}),
          },
        }
        : {}),
    });
  }
  const total = adjustment.minor;
  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`ppv:${billId}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: `Purchase price variance on ${billId}`,
    provenance: provenanceFor("purchase-variance", billId, date, ctx.now()),
    lines: [
      ...lines,
      {
        accountId: grni.id,
        side: (total > 0n ? "DEBIT" : "CREDIT") as "DEBIT" | "CREDIT",
        amount: Money.fromMinorUnits(total > 0n ? total : -total, ctx.currency),
        memo: "Clearing goods received not invoiced",
      },
    ],
  };
  await validateDimensions(
    { backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency }, command,
  );
  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  return String((await engine.post(command, { postedAt: ctx.now() })).id);
}

export async function setPurchaseOrderStatus(
  ctx: PurchasingContext, orderId: string, status: PurchaseOrderStatus,
): Promise<PurchaseOrderRecord> {
  const store = ctx.backend.purchasing();
  const order = await store.get(String(ctx.tenant), orderId);
  if (!order) throw new PurchasingError(`unknown purchase order ${orderId}`);
  if (status === "CANCELLED" && order.lines.some((l) => BigInt(l.receivedMilli) > 0n)) {
    throw new PurchasingError(
      `purchase order ${orderId} has been received against — close it instead of cancelling it`,
    );
  }
  const updated = { ...order, status };
  await store.save(String(ctx.tenant), updated);
  return updated;
}

export function purchaseOrderJson(o: PurchaseOrderRecord): Record<string, unknown> {
  let ordered = 0n;
  let received = 0n;
  let billed = 0n;
  const lines = o.lines.map((l) => {
    const price = BigInt(l.unitPriceMinor);
    const orderedMinor = mulDiv(BigInt(l.quantityMilli), price, MILLI);
    const receivedMinor = mulDiv(BigInt(l.receivedMilli), price, MILLI);
    const billedMinor = mulDiv(BigInt(l.billedMilli), price, MILLI);
    ordered += orderedMinor;
    received += receivedMinor;
    billed += billedMinor;
    return {
      line_no: l.lineNo,
      description: l.description,
      cost_code: l.costCode,
      account_code: l.accountCode,
      quantity_milli: l.quantityMilli,
      received_milli: l.receivedMilli,
      accrued_milli: l.accruedMilli,
      billed_milli: l.billedMilli,
      unit_price_minor: l.unitPriceMinor,
      ordered_minor: orderedMinor.toString(),
      received_minor: receivedMinor.toString(),
      billed_minor: billedMinor.toString(),
    };
  });
  return {
    id: o.id,
    vendor_id: o.vendorId,
    job_id: o.jobId,
    date: o.date,
    expected_date: o.expectedDate,
    status: o.status,
    memo: o.memo,
    lines,
    totals: {
      ordered_minor: ordered.toString(),
      received_minor: received.toString(),
      billed_minor: billed.toString(),
      committed_minor: (ordered - billed).toString(),
    },
  };
}

export function receiptJson(r: ReceiptRecord): Record<string, unknown> {
  return {
    id: r.id,
    purchase_order_id: r.purchaseOrderId,
    date: r.date,
    memo: r.memo,
    accrued: r.accrued,
    entry_id: r.entryId,
    lines: r.lines.map((l) => ({
      line_no: l.lineNo, quantity_milli: l.quantityMilli,
    })),
  };
}

/**
 * What a job has promised to spend and not yet put in its books, by cost code.
 *
 * Quantity already accrued is *in* the books, so it is not counted twice: the
 * committed quantity is what has been ordered less whichever of billed or
 * accrued has gone further.
 */
export async function committedByCostCode(
  ctx: PurchasingContext, jobId: string,
): Promise<Map<string, bigint>> {
  const out = new Map<string, bigint>();
  for (const order of await ctx.backend.purchasing().list(String(ctx.tenant))) {
    if (order.jobId !== jobId) continue;
    if (order.status === "CANCELLED" || order.status === "CLOSED") continue;
    for (const line of order.lines) {
      const inBooks = BigInt(line.billedMilli) > BigInt(line.accruedMilli)
        ? BigInt(line.billedMilli)
        : BigInt(line.accruedMilli);
      const outstanding = BigInt(line.quantityMilli) - inBooks;
      if (outstanding <= 0n) continue;
      const amount = mulDiv(outstanding, BigInt(line.unitPriceMinor), MILLI);
      const code = line.costCode || "";
      out.set(code, (out.get(code) ?? 0n) + amount);
    }
  }
  return out;
}

/** Everything ordered and not yet fully received — the "where is it" list. */
export async function outstandingOrders(
  ctx: PurchasingContext, filter: { readonly job_id?: string; readonly vendor_id?: string } = {},
): Promise<Record<string, unknown>> {
  const orders = await ctx.backend.purchasing().list(String(ctx.tenant));
  const rows: Record<string, unknown>[] = [];
  let committed = 0n;
  for (const order of orders) {
    if (order.status === "CANCELLED" || order.status === "CLOSED") continue;
    if (filter.job_id && order.jobId !== filter.job_id) continue;
    if (filter.vendor_id && order.vendorId !== filter.vendor_id) continue;
    const json = purchaseOrderJson(order);
    const totals = json["totals"] as Record<string, string>;
    if (totals["committed_minor"] === "0") continue;
    committed += BigInt(totals["committed_minor"]!);
    rows.push({
      id: order.id,
      vendor_id: order.vendorId,
      job_id: order.jobId,
      expected_date: order.expectedDate,
      status: order.status,
      ...totals,
    });
  }
  rows.sort((a, b) => String(a["expected_date"]).localeCompare(String(b["expected_date"])));
  return {
    contract: "purchase-commitments/1",
    currency: ctx.currency.code,
    orders: rows,
    committed_minor: committed.toString(),
  };
}
