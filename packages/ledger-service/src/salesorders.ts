import { type Currency, type TenantId } from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import { mulDiv, type EstimateRecord } from "./estimates.js";

/**
 * Sales orders — the step between "they said yes" and "we billed them".
 *
 * A small business with one-visit jobs does not need one. A business that
 * agrees work in March, does it across April and May, and bills it in pieces
 * needs somewhere for the *commitment* to live that is neither a quote (which
 * may never happen) nor an invoice (which is money owed today). Without it,
 * either the whole job is invoiced up front — putting revenue and a receivable
 * in the books for work not yet done — or the backlog exists only in somebody's
 * head.
 *
 * The order therefore does not post. It records what was agreed, what has been
 * invoiced against it, and what is left. Invoicing draws down the order and
 * produces an ordinary invoice; over-invoicing is refused, because an order
 * that can be billed twice is not a control, it is a suggestion.
 *
 * Customer → Job → Sales order → Invoice. The job is optional (not every order
 * is a project) but when it is set, everything the order bills lands on the job.
 */

export class SalesOrderError extends Error {}

export type SalesOrderStatus = "OPEN" | "PARTIAL" | "FULFILLED" | "CLOSED" | "CANCELLED";

const MILLI = 1000n;
const PPM = 1_000_000n;

export interface SalesOrderLineRecord {
  readonly lineNo: number;
  readonly description: string;
  readonly costCode: string;
  /** Ordered quantity, in thousandths. */
  readonly quantityMilli: string;
  /** How much of it has been invoiced, in thousandths. */
  readonly invoicedMilli: string;
  readonly unitPriceMinor: string;
  readonly accountCode: string;
  readonly taxable: boolean;
}

export interface SalesOrderRecord {
  readonly id: string;
  readonly customerId: string;
  readonly jobId: string;
  readonly estimateId: string;
  readonly date: string;
  /** When the customer expects it. Drives the backlog view, nothing else. */
  readonly requestedDate: string;
  readonly status: SalesOrderStatus;
  readonly taxRatePpm: number;
  readonly memo: string;
  readonly lines: readonly SalesOrderLineRecord[];
}

export interface SalesOrderStore {
  migrate(): Promise<void>;
  list(tenant: string): Promise<SalesOrderRecord[]>;
  get(tenant: string, id: string): Promise<SalesOrderRecord | undefined>;
  save(tenant: string, record: SalesOrderRecord): Promise<void>;
  remove(tenant: string, id: string): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemorySalesOrderStore implements SalesOrderStore {
  private readonly items = new Map<string, SalesOrderRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  list(tenant: string): Promise<SalesOrderRecord[]> {
    const out: SalesOrderRecord[] = [];
    for (const [k, v] of this.items) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.id.localeCompare(b.id)));
  }

  get(tenant: string, id: string): Promise<SalesOrderRecord | undefined> {
    return Promise.resolve(this.items.get(`${tenant}::${id}`));
  }

  save(tenant: string, record: SalesOrderRecord): Promise<void> {
    this.items.set(`${tenant}::${record.id}`, record);
    return Promise.resolve();
  }

  remove(tenant: string, id: string): Promise<void> {
    this.items.delete(`${tenant}::${id}`);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const SALES_ORDER_DDL = `
CREATE TABLE IF NOT EXISTS sales_order (
  tenant_id      text NOT NULL,
  id             text NOT NULL,
  customer_id    text NOT NULL,
  job_id         text NOT NULL DEFAULT '',
  estimate_id    text NOT NULL DEFAULT '',
  so_date        text NOT NULL,
  requested_date text NOT NULL DEFAULT '',
  status         text NOT NULL DEFAULT 'OPEN',
  tax_rate_ppm   integer NOT NULL DEFAULT 0,
  memo           text NOT NULL DEFAULT '',
  CONSTRAINT sales_order_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS sales_order_line (
  tenant_id        text NOT NULL,
  order_id         text NOT NULL,
  line_no          integer NOT NULL,
  description      text NOT NULL DEFAULT '',
  cost_code        text NOT NULL DEFAULT '',
  quantity_milli   text NOT NULL DEFAULT '1000',
  invoiced_milli   text NOT NULL DEFAULT '0',
  unit_price_minor text NOT NULL DEFAULT '0',
  account_code     text NOT NULL DEFAULT '',
  taxable          boolean NOT NULL DEFAULT true,
  CONSTRAINT sales_order_line_pk PRIMARY KEY (tenant_id, order_id, line_no)
);
`;

export class PgSalesOrderStore implements SalesOrderStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(SALES_ORDER_DDL);
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
  ): Promise<SalesOrderLineRecord[]> {
    const res = await db.query(
      "SELECT * FROM sales_order_line WHERE tenant_id=$1 AND order_id=$2 ORDER BY line_no",
      [tenant, id],
    );
    return res.rows.map((r) => ({
      lineNo: Number(r["line_no"]),
      description: String(r["description"] ?? ""),
      costCode: String(r["cost_code"] ?? ""),
      quantityMilli: String(r["quantity_milli"] ?? "0"),
      invoicedMilli: String(r["invoiced_milli"] ?? "0"),
      unitPriceMinor: String(r["unit_price_minor"] ?? "0"),
      accountCode: String(r["account_code"] ?? ""),
      taxable: r["taxable"] !== false && r["taxable"] !== "f",
    }));
  }

  private fromRow(
    r: Record<string, unknown>, lines: SalesOrderLineRecord[],
  ): SalesOrderRecord {
    return {
      id: String(r["id"]),
      customerId: String(r["customer_id"]),
      jobId: String(r["job_id"] ?? ""),
      estimateId: String(r["estimate_id"] ?? ""),
      date: String(r["so_date"]),
      requestedDate: String(r["requested_date"] ?? ""),
      status: String(r["status"]) as SalesOrderStatus,
      taxRatePpm: Number(r["tax_rate_ppm"] ?? 0),
      memo: String(r["memo"] ?? ""),
      lines,
    };
  }

  async list(tenant: string): Promise<SalesOrderRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM sales_order WHERE tenant_id=$1 ORDER BY id", [tenant],
      );
      const out: SalesOrderRecord[] = [];
      for (const row of res.rows) {
        out.push(this.fromRow(row, await this.linesFor(db, tenant, String(row["id"]))));
      }
      return out;
    });
  }

  async get(tenant: string, id: string): Promise<SalesOrderRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM sales_order WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      return this.fromRow(row, await this.linesFor(db, tenant, id));
    });
  }

  async save(tenant: string, record: SalesOrderRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO sales_order (tenant_id, id, customer_id, job_id, estimate_id,
           so_date, requested_date, status, tax_rate_ppm, memo)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           customer_id=EXCLUDED.customer_id, job_id=EXCLUDED.job_id,
           estimate_id=EXCLUDED.estimate_id, so_date=EXCLUDED.so_date,
           requested_date=EXCLUDED.requested_date, status=EXCLUDED.status,
           tax_rate_ppm=EXCLUDED.tax_rate_ppm, memo=EXCLUDED.memo`,
        [
          tenant, record.id, record.customerId, record.jobId, record.estimateId,
          record.date, record.requestedDate, record.status, record.taxRatePpm, record.memo,
        ],
      );
      await db.query(
        "DELETE FROM sales_order_line WHERE tenant_id=$1 AND order_id=$2", [tenant, record.id],
      );
      for (const l of record.lines) {
        await db.query(
          `INSERT INTO sales_order_line (tenant_id, order_id, line_no, description,
             cost_code, quantity_milli, invoiced_milli, unit_price_minor,
             account_code, taxable)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
          [
            tenant, record.id, l.lineNo, l.description, l.costCode, l.quantityMilli,
            l.invoicedMilli, l.unitPriceMinor, l.accountCode, l.taxable,
          ],
        );
      }
    });
  }

  async remove(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM sales_order_line WHERE tenant_id=$1 AND order_id=$2", [tenant, id],
      );
      await db.query("DELETE FROM sales_order WHERE tenant_id=$1 AND id=$2", [tenant, id]);
    });
  }
}

// --- the flow ----------------------------------------------------------------

export interface SalesOrderContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
}

function requireDate(raw: unknown, label: string, required = true): string {
  const date = String(raw ?? "").trim();
  if (!date) {
    if (required) throw new SalesOrderError(`${label} is required`);
    return "";
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new SalesOrderError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

function milliOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^\d+$/.test(value)) {
    throw new SalesOrderError(`${label} must be a whole number of thousandths`);
  }
  return BigInt(value);
}

export interface SalesOrderInput {
  readonly id?: string;
  readonly customer_id?: string;
  readonly job_id?: string;
  readonly from_estimate?: string;
  readonly date?: string;
  readonly requested_date?: string;
  readonly tax_rate_ppm?: number;
  readonly memo?: string;
  readonly lines?: ReadonlyArray<{
    readonly description?: string;
    readonly cost_code?: string;
    readonly quantity_milli?: string | number;
    readonly unit_price_minor?: string | number;
    readonly account_code?: string;
    readonly taxable?: boolean;
  }>;
}

function linesFromEstimate(estimate: EstimateRecord): SalesOrderLineRecord[] {
  return estimate.lines.map((l, i) => ({
    lineNo: i + 1,
    description: l.description,
    costCode: l.costCode,
    quantityMilli: l.quantityMilli,
    invoicedMilli: "0",
    unitPriceMinor: l.unitPriceMinor,
    accountCode: l.accountCode,
    taxable: l.taxable,
  }));
}

export async function saveSalesOrder(
  ctx: SalesOrderContext, input: SalesOrderInput,
): Promise<SalesOrderRecord> {
  const store = ctx.backend.salesOrders();

  let estimate: EstimateRecord | undefined;
  const fromEstimate = String(input.from_estimate ?? "").trim();
  if (fromEstimate) {
    estimate = await ctx.backend.estimates().get(String(ctx.tenant), fromEstimate);
    if (!estimate) throw new SalesOrderError(`unknown estimate ${fromEstimate}`);
    if (estimate.status !== "ACCEPTED") {
      throw new SalesOrderError(
        `estimate ${fromEstimate} is ${estimate.status} — only an accepted estimate becomes an order`,
      );
    }
  }

  const customerId = String(input.customer_id ?? estimate?.customerId ?? "").trim();
  if (!customerId) throw new SalesOrderError("an order needs a customer");
  if (!await ctx.backend.documents().getParty(String(ctx.tenant), "customer", customerId)) {
    throw new SalesOrderError(`unknown customer ${customerId}`);
  }

  const jobId = String(input.job_id ?? estimate?.jobId ?? "").trim();
  if (jobId && !await ctx.backend.jobs().getJob(String(ctx.tenant), jobId)) {
    throw new SalesOrderError(`unknown job ${jobId}`);
  }

  const date = requireDate(input.date ?? estimate?.date, "date");
  const requestedDate = requireDate(input.requested_date, "requested date", false);
  if (requestedDate && requestedDate < date) {
    throw new SalesOrderError("the requested date is before the order date");
  }

  const id = String(input.id ?? "").trim()
    || (estimate ? `SO-${estimate.rootId}` : `SO-${date.replace(/-/g, "")}`);
  const existing = await store.get(String(ctx.tenant), id);
  if (existing && existing.lines.some((l) => BigInt(l.invoicedMilli) > 0n)) {
    throw new SalesOrderError(
      `order ${id} has already been invoiced against — change what is left, not the order`,
    );
  }

  const chart = await ctx.backend.chart(ctx.tenant);
  const costCodes = new Set(
    (await ctx.backend.jobs().listCostCodes(String(ctx.tenant))).map((c) => c.code),
  );

  let lines: SalesOrderLineRecord[];
  if (input.lines && input.lines.length > 0) {
    lines = [];
    let lineNo = 1;
    for (const l of input.lines) {
      const quantityMilli = milliOf(l.quantity_milli ?? 1000, `line ${lineNo} quantity`);
      if (quantityMilli === 0n) {
        throw new SalesOrderError(`line ${lineNo}: quantity must be more than zero`);
      }
      const unitPrice = milliOf(l.unit_price_minor, `line ${lineNo} unit price`);
      if (unitPrice === 0n) throw new SalesOrderError(`line ${lineNo}: needs a price`);
      const accountCode = String(l.account_code ?? "").trim()
        || (chart.getByCode("4100") ? "4100" : "4000");
      const account = chart.getByCode(accountCode);
      if (!account) throw new SalesOrderError(`unknown account code ${accountCode}`);
      if (account.type !== "REVENUE") {
        throw new SalesOrderError(`${accountCode} is not a revenue account`);
      }
      const costCode = String(l.cost_code ?? "").trim().toUpperCase();
      if (costCode && !costCodes.has(costCode)) {
        throw new SalesOrderError(`unknown cost code ${costCode}`);
      }
      lines.push({
        lineNo,
        description: String(l.description ?? "").trim(),
        costCode,
        quantityMilli: quantityMilli.toString(),
        invoicedMilli: existing?.lines.find((x) => x.lineNo === lineNo)?.invoicedMilli ?? "0",
        unitPriceMinor: unitPrice.toString(),
        accountCode,
        taxable: l.taxable !== false,
      });
      lineNo += 1;
    }
  } else if (estimate) {
    lines = linesFromEstimate(estimate);
  } else {
    throw new SalesOrderError("an order needs at least one line");
  }

  const record: SalesOrderRecord = {
    id,
    customerId,
    jobId,
    estimateId: fromEstimate || existing?.estimateId || "",
    date,
    requestedDate,
    status: existing?.status ?? "OPEN",
    taxRatePpm: Number(input.tax_rate_ppm ?? estimate?.taxRatePpm ?? 0),
    memo: String(input.memo ?? estimate?.memo ?? "").trim(),
    lines,
  };
  await store.save(String(ctx.tenant), record);
  return record;
}

function statusFor(record: SalesOrderRecord): SalesOrderStatus {
  if (record.status === "CANCELLED" || record.status === "CLOSED") return record.status;
  let anyInvoiced = false;
  let allInvoiced = true;
  for (const l of record.lines) {
    const invoiced = BigInt(l.invoicedMilli);
    if (invoiced > 0n) anyInvoiced = true;
    if (invoiced < BigInt(l.quantityMilli)) allInvoiced = false;
  }
  if (allInvoiced) return "FULFILLED";
  return anyInvoiced ? "PARTIAL" : "OPEN";
}

export interface SalesOrderTotals {
  readonly ordered_minor: string;
  readonly invoiced_minor: string;
  readonly remaining_minor: string;
  readonly tax_minor: string;
  readonly total_minor: string;
}

export function salesOrderTotals(record: SalesOrderRecord): SalesOrderTotals {
  let ordered = 0n;
  let invoiced = 0n;
  let taxable = 0n;
  for (const l of record.lines) {
    const price = BigInt(l.unitPriceMinor);
    const lineOrdered = mulDiv(BigInt(l.quantityMilli), price, MILLI);
    ordered += lineOrdered;
    invoiced += mulDiv(BigInt(l.invoicedMilli), price, MILLI);
    if (l.taxable) taxable += lineOrdered;
  }
  const tax = mulDiv(taxable, BigInt(record.taxRatePpm), PPM);
  return {
    ordered_minor: ordered.toString(),
    invoiced_minor: invoiced.toString(),
    remaining_minor: (ordered - invoiced).toString(),
    tax_minor: tax.toString(),
    total_minor: (ordered + tax).toString(),
  };
}

export function salesOrderJson(o: SalesOrderRecord): Record<string, unknown> {
  return {
    id: o.id,
    customer_id: o.customerId,
    job_id: o.jobId,
    estimate_id: o.estimateId,
    date: o.date,
    requested_date: o.requestedDate,
    status: o.status,
    tax_rate_ppm: o.taxRatePpm,
    memo: o.memo,
    lines: o.lines.map((l) => ({
      line_no: l.lineNo,
      description: l.description,
      cost_code: l.costCode,
      quantity_milli: l.quantityMilli,
      invoiced_milli: l.invoicedMilli,
      remaining_milli: (BigInt(l.quantityMilli) - BigInt(l.invoicedMilli)).toString(),
      unit_price_minor: l.unitPriceMinor,
      extended_price_minor: mulDiv(
        BigInt(l.quantityMilli), BigInt(l.unitPriceMinor), MILLI,
      ).toString(),
      account_code: l.accountCode,
      taxable: l.taxable,
    })),
    totals: salesOrderTotals(o),
  };
}

export interface InvoiceFromOrderInput {
  readonly id?: string;
  readonly date?: string;
  readonly due_date?: string;
  readonly memo?: string;
  /** Which lines, and how much of each. Omitted means everything remaining. */
  readonly lines?: ReadonlyArray<{
    readonly line_no?: number;
    readonly quantity_milli?: string | number;
  }>;
}

export interface InvoiceDraft {
  readonly request: Record<string, unknown>;
  readonly order: SalesOrderRecord;
}

/**
 * Draw down an order into an invoice request.
 *
 * The order is updated with what has now been invoiced and the caller posts the
 * invoice through the ordinary AR path. Over-invoicing is refused per line, not
 * on the total: an order for 10 doors and 4 windows that has been fully billed
 * for doors should not accept another door because the windows are outstanding.
 */
export async function invoiceFromOrder(
  ctx: SalesOrderContext, orderId: string, input: InvoiceFromOrderInput,
): Promise<InvoiceDraft> {
  const store = ctx.backend.salesOrders();
  const order = await store.get(String(ctx.tenant), orderId);
  if (!order) throw new SalesOrderError(`unknown order ${orderId}`);
  if (order.status === "CANCELLED") {
    throw new SalesOrderError(`order ${orderId} was cancelled`);
  }

  const wanted = new Map<number, bigint>();
  if (input.lines && input.lines.length > 0) {
    for (const l of input.lines) {
      const lineNo = Number(l.line_no ?? 0);
      const line = order.lines.find((x) => x.lineNo === lineNo);
      if (!line) throw new SalesOrderError(`order ${orderId} has no line ${lineNo}`);
      const remaining = BigInt(line.quantityMilli) - BigInt(line.invoicedMilli);
      const quantity = l.quantity_milli === undefined || String(l.quantity_milli).trim() === ""
        ? remaining
        : milliOf(l.quantity_milli, `line ${lineNo} quantity`);
      if (quantity <= 0n) continue;
      if (quantity > remaining) {
        throw new SalesOrderError(
          `line ${lineNo}: only ${remaining} thousandths are left to invoice, not ${quantity}`,
        );
      }
      wanted.set(lineNo, quantity);
    }
  } else {
    for (const line of order.lines) {
      const remaining = BigInt(line.quantityMilli) - BigInt(line.invoicedMilli);
      if (remaining > 0n) wanted.set(line.lineNo, remaining);
    }
  }
  if (wanted.size === 0) {
    throw new SalesOrderError(`order ${orderId} has nothing left to invoice`);
  }

  const date = requireDate(input.date ?? order.date, "date");
  const invoiceId = String(input.id ?? "").trim();
  if (!invoiceId) throw new SalesOrderError("the invoice needs an id");

  const lines: Record<string, unknown>[] = [];
  for (const line of order.lines) {
    const quantity = wanted.get(line.lineNo);
    if (!quantity) continue;
    lines.push({
      description: line.description,
      unit_amount_minor: mulDiv(quantity, BigInt(line.unitPriceMinor), MILLI).toString(),
      account_code: line.accountCode,
      taxable: line.taxable,
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

  const updated: SalesOrderRecord = {
    ...order,
    lines: order.lines.map((l) => {
      const quantity = wanted.get(l.lineNo);
      return quantity
        ? { ...l, invoicedMilli: (BigInt(l.invoicedMilli) + quantity).toString() }
        : l;
    }),
  };

  return {
    request: {
      id: invoiceId,
      party_id: order.customerId,
      date,
      ...(input.due_date ? { due_date: input.due_date } : {}),
      memo: String(input.memo ?? order.memo ?? "") || `From order ${order.id}`,
      tax_rate_ppm: order.taxRatePpm,
      lines,
    },
    order: { ...updated, status: statusFor(updated) },
  };
}

export async function setOrderStatus(
  ctx: SalesOrderContext, orderId: string, status: SalesOrderStatus,
): Promise<SalesOrderRecord> {
  const store = ctx.backend.salesOrders();
  const order = await store.get(String(ctx.tenant), orderId);
  if (!order) throw new SalesOrderError(`unknown order ${orderId}`);
  if (status === "CANCELLED" && order.lines.some((l) => BigInt(l.invoicedMilli) > 0n)) {
    throw new SalesOrderError(
      `order ${orderId} has been invoiced against — close it instead of cancelling it`,
    );
  }
  const updated = { ...order, status };
  await store.save(String(ctx.tenant), updated);
  return updated;
}

/**
 * The backlog: what has been agreed and not yet billed.
 *
 * This is the number a business uses to decide whether to hire, and it exists
 * nowhere in a general ledger — work agreed is not revenue, and a system that
 * cannot say "we have $180,000 sold and unbilled" leaves that decision to
 * memory.
 */
export async function backlog(
  ctx: SalesOrderContext, filter: { readonly job_id?: string; readonly customer_id?: string } = {},
): Promise<Record<string, unknown>> {
  const orders = await ctx.backend.salesOrders().list(String(ctx.tenant));
  const rows: Record<string, unknown>[] = [];
  let total = 0n;
  for (const order of orders) {
    if (order.status === "CANCELLED" || order.status === "CLOSED") continue;
    if (filter.job_id && order.jobId !== filter.job_id) continue;
    if (filter.customer_id && order.customerId !== filter.customer_id) continue;
    const totals = salesOrderTotals(order);
    const remaining = BigInt(totals.remaining_minor);
    if (remaining === 0n) continue;
    total += remaining;
    rows.push({
      id: order.id,
      customer_id: order.customerId,
      job_id: order.jobId,
      requested_date: order.requestedDate,
      status: order.status,
      ordered_minor: totals.ordered_minor,
      invoiced_minor: totals.invoiced_minor,
      remaining_minor: totals.remaining_minor,
    });
  }
  rows.sort((a, b) => String(a["requested_date"]).localeCompare(String(b["requested_date"])));
  return {
    contract: "sales-backlog/1",
    currency: ctx.currency.code,
    orders: rows,
    remaining_minor: total.toString(),
  };
}
