import { type Currency, type TenantId } from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import { JobError, saveJob, saveJobBudget, slug, type JobContext } from "./jobs.js";

/**
 * Estimates — the document a job starts life as, and the only place a
 * contractor's margin is actually decided.
 *
 * Most software treats an estimate as an invoice that hasn't happened yet: a
 * list of prices. That throws away the half of the document that matters. An
 * estimate line has a **cost** and a **price**, and the gap between them is the
 * business. Storing only the price means that when the job runs, there is
 * nothing to compare the actual cost against — so "we bid this at 22% and
 * finished at 9%" is a sentence the system cannot say.
 *
 * So every line here carries quantity, unit cost, a markup, and the resulting
 * price, and accepting an estimate seeds the job's budget from the cost side
 * automatically. The estimate stops being a quote and becomes the plan the job
 * is measured against.
 *
 * Two other decisions worth naming:
 *
 * **Markup and margin are both shown, always.** They are not the same number
 * and confusing them is the most expensive arithmetic error in the trades:
 * cost 10,000 marked up 20% prices at 12,000, which is a 16.7% margin, not 20%.
 * A system that reports one and lets the owner assume the other is helping them
 * lose money politely.
 *
 * **Revising creates a revision.** An estimate that has been sent is a thing
 * the customer has seen; editing it in place destroys the record of what was
 * agreed. Revision 2 supersedes revision 1, and both stay.
 */

export class EstimateError extends Error {}

export type EstimateStatus =
  | "DRAFT" | "SENT" | "ACCEPTED" | "DECLINED" | "EXPIRED" | "SUPERSEDED";

const OPEN_STATUSES: readonly EstimateStatus[] = ["DRAFT", "SENT"];

export interface EstimateLineRecord {
  readonly lineNo: number;
  readonly description: string;
  readonly costCode: string;
  /** Quantity in thousandths — 2.5 hours is 2500. Integers, never a float. */
  readonly quantityMilli: string;
  readonly unitCostMinor: string;
  readonly markupPpm: number;
  readonly unitPriceMinor: string;
  readonly extendedCostMinor: string;
  readonly extendedPriceMinor: string;
  /** Income account this line will bill to. */
  readonly accountCode: string;
  readonly taxable: boolean;
}

export interface EstimateRecord {
  readonly id: string;
  /** The estimate this is a revision of — revision 1 is its own root. */
  readonly rootId: string;
  readonly revision: number;
  readonly customerId: string;
  /** Set once the estimate is accepted onto a job. */
  readonly jobId: string;
  readonly date: string;
  readonly expiryDate: string;
  readonly status: EstimateStatus;
  readonly taxRatePpm: number;
  readonly memo: string;
  readonly lines: readonly EstimateLineRecord[];
}

export interface EstimateStore {
  migrate(): Promise<void>;
  list(tenant: string): Promise<EstimateRecord[]>;
  get(tenant: string, id: string): Promise<EstimateRecord | undefined>;
  save(tenant: string, record: EstimateRecord): Promise<void>;
  remove(tenant: string, id: string): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryEstimateStore implements EstimateStore {
  private readonly items = new Map<string, EstimateRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  list(tenant: string): Promise<EstimateRecord[]> {
    const out: EstimateRecord[] = [];
    for (const [k, v] of this.items) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => (
      a.rootId === b.rootId ? a.revision - b.revision : a.rootId.localeCompare(b.rootId)
    )));
  }

  get(tenant: string, id: string): Promise<EstimateRecord | undefined> {
    return Promise.resolve(this.items.get(`${tenant}::${id}`));
  }

  save(tenant: string, record: EstimateRecord): Promise<void> {
    this.items.set(`${tenant}::${record.id}`, record);
    return Promise.resolve();
  }

  remove(tenant: string, id: string): Promise<void> {
    this.items.delete(`${tenant}::${id}`);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const ESTIMATE_DDL = `
CREATE TABLE IF NOT EXISTS estimate (
  tenant_id     text NOT NULL,
  id            text NOT NULL,
  root_id       text NOT NULL,
  revision      integer NOT NULL DEFAULT 1,
  customer_id   text NOT NULL,
  job_id        text NOT NULL DEFAULT '',
  est_date      text NOT NULL,
  expiry_date   text NOT NULL DEFAULT '',
  status        text NOT NULL DEFAULT 'DRAFT',
  tax_rate_ppm  integer NOT NULL DEFAULT 0,
  memo          text NOT NULL DEFAULT '',
  CONSTRAINT estimate_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS estimate_line (
  tenant_id            text NOT NULL,
  estimate_id          text NOT NULL,
  line_no              integer NOT NULL,
  description          text NOT NULL DEFAULT '',
  cost_code            text NOT NULL DEFAULT '',
  quantity_milli       text NOT NULL DEFAULT '1000',
  unit_cost_minor      text NOT NULL DEFAULT '0',
  markup_ppm           integer NOT NULL DEFAULT 0,
  unit_price_minor     text NOT NULL DEFAULT '0',
  extended_cost_minor  text NOT NULL DEFAULT '0',
  extended_price_minor text NOT NULL DEFAULT '0',
  account_code         text NOT NULL DEFAULT '',
  taxable              boolean NOT NULL DEFAULT true,
  CONSTRAINT estimate_line_pk PRIMARY KEY (tenant_id, estimate_id, line_no)
);
`;

export class PgEstimateStore implements EstimateStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(ESTIMATE_DDL);
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
  ): Promise<EstimateLineRecord[]> {
    const res = await db.query(
      "SELECT * FROM estimate_line WHERE tenant_id=$1 AND estimate_id=$2 ORDER BY line_no",
      [tenant, id],
    );
    return res.rows.map((r) => ({
      lineNo: Number(r["line_no"]),
      description: String(r["description"] ?? ""),
      costCode: String(r["cost_code"] ?? ""),
      quantityMilli: String(r["quantity_milli"] ?? "1000"),
      unitCostMinor: String(r["unit_cost_minor"] ?? "0"),
      markupPpm: Number(r["markup_ppm"] ?? 0),
      unitPriceMinor: String(r["unit_price_minor"] ?? "0"),
      extendedCostMinor: String(r["extended_cost_minor"] ?? "0"),
      extendedPriceMinor: String(r["extended_price_minor"] ?? "0"),
      accountCode: String(r["account_code"] ?? ""),
      taxable: r["taxable"] !== false && r["taxable"] !== "f",
    }));
  }

  private fromRow(
    r: Record<string, unknown>, lines: EstimateLineRecord[],
  ): EstimateRecord {
    return {
      id: String(r["id"]),
      rootId: String(r["root_id"]),
      revision: Number(r["revision"] ?? 1),
      customerId: String(r["customer_id"]),
      jobId: String(r["job_id"] ?? ""),
      date: String(r["est_date"]),
      expiryDate: String(r["expiry_date"] ?? ""),
      status: String(r["status"]) as EstimateStatus,
      taxRatePpm: Number(r["tax_rate_ppm"] ?? 0),
      memo: String(r["memo"] ?? ""),
      lines,
    };
  }

  async list(tenant: string): Promise<EstimateRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM estimate WHERE tenant_id=$1 ORDER BY root_id, revision", [tenant],
      );
      const out: EstimateRecord[] = [];
      for (const row of res.rows) {
        out.push(this.fromRow(row, await this.linesFor(db, tenant, String(row["id"]))));
      }
      return out;
    });
  }

  async get(tenant: string, id: string): Promise<EstimateRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM estimate WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      return this.fromRow(row, await this.linesFor(db, tenant, id));
    });
  }

  async save(tenant: string, record: EstimateRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO estimate (tenant_id, id, root_id, revision, customer_id, job_id,
           est_date, expiry_date, status, tax_rate_ppm, memo)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           root_id=EXCLUDED.root_id, revision=EXCLUDED.revision,
           customer_id=EXCLUDED.customer_id, job_id=EXCLUDED.job_id,
           est_date=EXCLUDED.est_date, expiry_date=EXCLUDED.expiry_date,
           status=EXCLUDED.status, tax_rate_ppm=EXCLUDED.tax_rate_ppm, memo=EXCLUDED.memo`,
        [
          tenant, record.id, record.rootId, record.revision, record.customerId,
          record.jobId, record.date, record.expiryDate, record.status,
          record.taxRatePpm, record.memo,
        ],
      );
      await db.query(
        "DELETE FROM estimate_line WHERE tenant_id=$1 AND estimate_id=$2", [tenant, record.id],
      );
      for (const l of record.lines) {
        await db.query(
          `INSERT INTO estimate_line (tenant_id, estimate_id, line_no, description,
             cost_code, quantity_milli, unit_cost_minor, markup_ppm, unit_price_minor,
             extended_cost_minor, extended_price_minor, account_code, taxable)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)`,
          [
            tenant, record.id, l.lineNo, l.description, l.costCode, l.quantityMilli,
            l.unitCostMinor, l.markupPpm, l.unitPriceMinor, l.extendedCostMinor,
            l.extendedPriceMinor, l.accountCode, l.taxable,
          ],
        );
      }
    });
  }

  async remove(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM estimate_line WHERE tenant_id=$1 AND estimate_id=$2", [tenant, id],
      );
      await db.query("DELETE FROM estimate WHERE tenant_id=$1 AND id=$2", [tenant, id]);
    });
  }
}

// --- the arithmetic ----------------------------------------------------------

const MILLI = 1000n;
const PPM = 1_000_000n;

/** a × b ÷ d, rounded half-up. Integers throughout — never a float. */
export function mulDiv(a: bigint, b: bigint, d: bigint): bigint {
  const product = a * b;
  const negative = product < 0n;
  const abs = negative ? -product : product;
  const rounded = (abs + d / 2n) / d;
  return negative ? -rounded : rounded;
}

/** Markup — what you add to cost. 10,000 marked up 20% prices at 12,000. */
export function priceFromMarkup(costMinor: bigint, markupPpm: number): bigint {
  return mulDiv(costMinor, PPM + BigInt(markupPpm), PPM);
}

/** Markup implied by a price someone typed directly. */
export function markupFromPrice(costMinor: bigint, priceMinor: bigint): number {
  if (costMinor === 0n) return 0;
  return Number(mulDiv(priceMinor - costMinor, PPM, costMinor));
}

/**
 * Margin — the share of the *price* that isn't cost. The number that matters,
 * and never the same as markup: 20% markup is 16.7% margin.
 */
export function marginPpm(costMinor: bigint, priceMinor: bigint): number {
  if (priceMinor === 0n) return 0;
  return Number(mulDiv(priceMinor - costMinor, PPM, priceMinor));
}

// --- the flow ----------------------------------------------------------------

export interface EstimateContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
}

function requireDate(raw: unknown, label: string, required = true): string {
  const date = String(raw ?? "").trim();
  if (!date) {
    if (required) throw new EstimateError(`${label} is required`);
    return "";
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new EstimateError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

function minorOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^-?\d+$/.test(value)) {
    throw new EstimateError(`${label} must be a whole number of minor units`);
  }
  const parsed = BigInt(value);
  if (parsed < 0n) throw new EstimateError(`${label} cannot be negative`);
  return parsed;
}

export interface EstimateLineInput {
  readonly description?: string;
  readonly cost_code?: string;
  readonly quantity_milli?: string | number;
  readonly unit_cost_minor?: string | number;
  readonly markup_ppm?: number | string;
  readonly unit_price_minor?: string | number;
  readonly account_code?: string;
  readonly taxable?: boolean;
}

export interface EstimateInput {
  readonly id?: string;
  readonly customer_id?: string;
  readonly job_id?: string;
  readonly date?: string;
  readonly expiry_date?: string;
  readonly tax_rate_ppm?: number | string;
  readonly memo?: string;
  readonly lines?: readonly EstimateLineInput[];
}

async function buildLines(
  ctx: EstimateContext, input: EstimateInput,
): Promise<EstimateLineRecord[]> {
  const raw = input.lines ?? [];
  if (raw.length === 0) throw new EstimateError("an estimate needs at least one line");

  const chart = await ctx.backend.chart(ctx.tenant);
  const costCodes = new Set(
    (await ctx.backend.jobs().listCostCodes(String(ctx.tenant))).map((c) => c.code),
  );

  const lines: EstimateLineRecord[] = [];
  let lineNo = 1;
  for (const l of raw) {
    const description = String(l.description ?? "").trim();
    const costCode = String(l.cost_code ?? "").trim().toUpperCase();
    if (costCode && !costCodes.has(costCode)) {
      throw new EstimateError(`unknown cost code ${costCode}`);
    }
    const quantityMilli = minorOf(l.quantity_milli ?? 1000, `line ${lineNo} quantity`);
    if (quantityMilli === 0n) throw new EstimateError(`line ${lineNo}: quantity must be more than zero`);
    const unitCost = minorOf(l.unit_cost_minor, `line ${lineNo} unit cost`);

    // A price typed directly wins, and the markup is derived from it — that is
    // how estimating actually goes ("this one's 4,500, don't ask").
    let unitPrice: bigint;
    let markupPpm: number;
    if (l.unit_price_minor !== undefined && String(l.unit_price_minor).trim() !== "") {
      unitPrice = minorOf(l.unit_price_minor, `line ${lineNo} unit price`);
      markupPpm = markupFromPrice(unitCost, unitPrice);
    } else {
      markupPpm = Number(l.markup_ppm ?? 0);
      if (!Number.isInteger(markupPpm) || markupPpm < -1_000_000) {
        throw new EstimateError(`line ${lineNo}: markup must be a whole number of parts per million`);
      }
      unitPrice = priceFromMarkup(unitCost, markupPpm);
    }
    if (unitPrice === 0n && unitCost === 0n) {
      throw new EstimateError(`line ${lineNo}: needs a cost or a price`);
    }

    const accountCode = String(l.account_code ?? "").trim()
      || (chart.getByCode("4100") ? "4100" : "4000");
    const account = chart.getByCode(accountCode);
    if (!account) throw new EstimateError(`unknown account code ${accountCode}`);
    if (account.type !== "REVENUE") {
      throw new EstimateError(`${accountCode} is not a revenue account — an estimate line bills to income`);
    }

    lines.push({
      lineNo,
      description,
      costCode,
      quantityMilli: quantityMilli.toString(),
      unitCostMinor: unitCost.toString(),
      markupPpm,
      unitPriceMinor: unitPrice.toString(),
      extendedCostMinor: mulDiv(quantityMilli, unitCost, MILLI).toString(),
      extendedPriceMinor: mulDiv(quantityMilli, unitPrice, MILLI).toString(),
      accountCode,
      taxable: l.taxable !== false,
    });
    lineNo += 1;
  }
  return lines;
}

export async function saveEstimate(
  ctx: EstimateContext, input: EstimateInput,
): Promise<EstimateRecord> {
  const customerId = String(input.customer_id ?? "").trim();
  if (!customerId) throw new EstimateError("an estimate needs a customer");
  const customer = await ctx.backend.documents()
    .getParty(String(ctx.tenant), "customer", customerId);
  if (!customer) throw new EstimateError(`unknown customer ${customerId}`);

  const date = requireDate(input.date, "date");
  const expiryDate = requireDate(input.expiry_date, "expiry date", false);
  if (expiryDate && expiryDate < date) {
    throw new EstimateError("the expiry date is before the estimate date");
  }

  const taxRatePpm = Number(input.tax_rate_ppm ?? 0);
  if (!Number.isInteger(taxRatePpm) || taxRatePpm < 0 || taxRatePpm > 1_000_000) {
    throw new EstimateError("tax rate must be between 0 and 1,000,000 parts per million");
  }

  const jobId = String(input.job_id ?? "").trim();
  if (jobId && !await ctx.backend.jobs().getJob(String(ctx.tenant), jobId)) {
    throw new EstimateError(`unknown job ${jobId}`);
  }

  const id = String(input.id ?? "").trim() || `EST-${date.replace(/-/g, "")}-${slug(customerId)}`;
  const existing = await ctx.backend.estimates().get(String(ctx.tenant), id);
  if (existing && !OPEN_STATUSES.includes(existing.status)) {
    throw new EstimateError(
      `estimate ${id} is ${existing.status} — revise it instead of editing it`,
    );
  }

  const record: EstimateRecord = {
    id,
    rootId: existing?.rootId ?? id,
    revision: existing?.revision ?? 1,
    customerId,
    jobId,
    date,
    expiryDate,
    status: existing?.status ?? "DRAFT",
    taxRatePpm,
    memo: String(input.memo ?? "").trim(),
    lines: await buildLines(ctx, input),
  };
  await ctx.backend.estimates().save(String(ctx.tenant), record);
  return record;
}

/**
 * Create the next revision of an estimate.
 *
 * The previous revision is marked SUPERSEDED rather than changed, so what the
 * customer was originally shown is still readable. An accepted estimate can be
 * revised too — that is a change order, and the fact that revision 1 was the
 * one accepted is exactly what you want on the record.
 */
export async function reviseEstimate(
  ctx: EstimateContext, id: string, input: EstimateInput,
): Promise<EstimateRecord> {
  const store = ctx.backend.estimates();
  const previous = await store.get(String(ctx.tenant), id);
  if (!previous) throw new EstimateError(`unknown estimate ${id}`);

  const siblings = (await store.list(String(ctx.tenant)))
    .filter((e) => e.rootId === previous.rootId);
  const revision = Math.max(...siblings.map((e) => e.revision)) + 1;
  const newId = `${previous.rootId}-r${revision}`;

  const merged: EstimateInput = {
    customer_id: input.customer_id ?? previous.customerId,
    job_id: input.job_id ?? previous.jobId,
    date: input.date ?? previous.date,
    expiry_date: input.expiry_date ?? previous.expiryDate,
    tax_rate_ppm: input.tax_rate_ppm ?? previous.taxRatePpm,
    memo: input.memo ?? previous.memo,
    lines: input.lines ?? previous.lines.map((l) => ({
      description: l.description,
      cost_code: l.costCode,
      quantity_milli: l.quantityMilli,
      unit_cost_minor: l.unitCostMinor,
      unit_price_minor: l.unitPriceMinor,
      account_code: l.accountCode,
      taxable: l.taxable,
    })),
    id: newId,
  };

  const lines = await buildLines(ctx, merged);
  const record: EstimateRecord = {
    id: newId,
    rootId: previous.rootId,
    revision,
    customerId: String(merged.customer_id),
    jobId: String(merged.job_id ?? ""),
    date: requireDate(merged.date, "date"),
    expiryDate: requireDate(merged.expiry_date, "expiry date", false),
    status: "DRAFT",
    taxRatePpm: Number(merged.tax_rate_ppm ?? 0),
    memo: String(merged.memo ?? ""),
    lines,
  };
  await store.save(String(ctx.tenant), record);
  await store.save(String(ctx.tenant), { ...previous, status: "SUPERSEDED" });
  return record;
}

export async function setEstimateStatus(
  ctx: EstimateContext, id: string, status: EstimateStatus,
): Promise<EstimateRecord> {
  const store = ctx.backend.estimates();
  const estimate = await store.get(String(ctx.tenant), id);
  if (!estimate) throw new EstimateError(`unknown estimate ${id}`);
  const updated = { ...estimate, status };
  await store.save(String(ctx.tenant), updated);
  return updated;
}

export interface EstimateTotals {
  readonly cost_minor: string;
  readonly price_minor: string;
  readonly tax_minor: string;
  readonly total_minor: string;
  readonly margin_minor: string;
  readonly margin_ppm: number;
  readonly markup_ppm: number;
}

export function estimateTotals(estimate: EstimateRecord): EstimateTotals {
  let cost = 0n;
  let price = 0n;
  let taxable = 0n;
  for (const l of estimate.lines) {
    cost += BigInt(l.extendedCostMinor);
    price += BigInt(l.extendedPriceMinor);
    if (l.taxable) taxable += BigInt(l.extendedPriceMinor);
  }
  const tax = mulDiv(taxable, BigInt(estimate.taxRatePpm), PPM);
  return {
    cost_minor: cost.toString(),
    price_minor: price.toString(),
    tax_minor: tax.toString(),
    total_minor: (price + tax).toString(),
    margin_minor: (price - cost).toString(),
    margin_ppm: marginPpm(cost, price),
    markup_ppm: markupFromPrice(cost, price),
  };
}

export function estimateJson(e: EstimateRecord): Record<string, unknown> {
  return {
    id: e.id,
    root_id: e.rootId,
    revision: e.revision,
    customer_id: e.customerId,
    job_id: e.jobId,
    date: e.date,
    expiry_date: e.expiryDate,
    status: e.status,
    tax_rate_ppm: e.taxRatePpm,
    memo: e.memo,
    lines: e.lines.map((l) => ({
      line_no: l.lineNo,
      description: l.description,
      cost_code: l.costCode,
      quantity_milli: l.quantityMilli,
      unit_cost_minor: l.unitCostMinor,
      markup_ppm: l.markupPpm,
      unit_price_minor: l.unitPriceMinor,
      extended_cost_minor: l.extendedCostMinor,
      extended_price_minor: l.extendedPriceMinor,
      margin_ppm: marginPpm(BigInt(l.extendedCostMinor), BigInt(l.extendedPriceMinor)),
      account_code: l.accountCode,
      taxable: l.taxable,
    })),
    totals: estimateTotals(e),
  };
}

export interface AcceptInput {
  /** Attach to an existing job instead of creating one. */
  readonly job_id?: string;
  readonly job_name?: string;
  readonly billing_method?: string;
  readonly cost_method?: string;
  readonly start_date?: string;
  readonly end_date?: string;
  readonly retainage_ppm?: number;
  /** Accept an estimate whose expiry has passed, with eyes open. */
  readonly ignore_expiry?: boolean;
}

export interface AcceptResult {
  readonly estimate: EstimateRecord;
  readonly jobId: string;
  readonly created: boolean;
  readonly budgetSeeded: number;
}

/**
 * Accept an estimate onto a job.
 *
 * This is the moment the estimate stops being a sales document and becomes the
 * plan: the job's contract value is the estimate's total, and the job's budget
 * is seeded from the estimate's **cost** lines, grouped by cost code. Without
 * that seeding, every contractor who has ever bought job-costing software does
 * the same thing — types the budget in a second time, gets a digit wrong, and
 * quietly stops trusting the variance column.
 */
export async function acceptEstimate(
  ctx: EstimateContext, id: string, input: AcceptInput = {},
): Promise<AcceptResult> {
  const store = ctx.backend.estimates();
  const estimate = await store.get(String(ctx.tenant), id);
  if (!estimate) throw new EstimateError(`unknown estimate ${id}`);
  if (!OPEN_STATUSES.includes(estimate.status)) {
    throw new EstimateError(`estimate ${id} is ${estimate.status} and cannot be accepted`);
  }
  if (estimate.expiryDate && !input.ignore_expiry) {
    // The estimate's own date is the only clock available here — the service
    // never reads a wall clock, and an accept is dated by the caller.
    const today = String(input.start_date ?? "").trim();
    if (today && today > estimate.expiryDate) {
      throw new EstimateError(
        `estimate ${id} expired on ${estimate.expiryDate} — revise it, or accept it explicitly`,
      );
    }
  }

  const totals = estimateTotals(estimate);
  const jobCtx: JobContext = {
    backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency,
  };

  let jobId = String(input.job_id ?? "").trim() || estimate.jobId;
  let created = false;
  if (jobId) {
    const job = await ctx.backend.jobs().getJob(String(ctx.tenant), jobId);
    if (!job) throw new EstimateError(`unknown job ${jobId}`);
  } else {
    const name = String(input.job_name ?? "").trim() || estimate.memo || `Job from ${estimate.id}`;
    const job = await saveJob(jobCtx, {
      customer_id: estimate.customerId,
      name,
      billing_method: input.billing_method ?? "PROGRESS",
      ...(input.cost_method ? { cost_method: input.cost_method } : {}),
      contract_minor: totals.price_minor,
      ...(input.start_date ? { start_date: input.start_date } : {}),
      ...(input.end_date ? { end_date: input.end_date } : {}),
      ...(input.retainage_ppm !== undefined ? { retainage_ppm: input.retainage_ppm } : {}),
      revenue_account_code: estimate.lines[0]?.accountCode ?? "",
      memo: `Accepted from ${estimate.id}`,
    });
    jobId = job.id;
    created = true;
  }

  // What an estimate contributes to a job budget, grouped by cost code.
  const costsByCode = (est: EstimateRecord): Map<string, { cost: bigint; revenue: bigint }> => {
    const m = new Map<string, { cost: bigint; revenue: bigint }>();
    for (const l of est.lines) {
      if (!l.costCode) continue;
      const bucket = m.get(l.costCode) ?? { cost: 0n, revenue: 0n };
      bucket.cost += BigInt(l.extendedCostMinor);
      bucket.revenue += BigInt(l.extendedPriceMinor);
      m.set(l.costCode, bucket);
    }
    return m;
  };

  // If a PRIOR revision of this same estimate root already landed on this job,
  // the job already carries its contract and budget. Re-accepting a newer
  // revision (e.g. a full-restatement change order made with reviseEstimate,
  // which copies every line forward) must apply only the DELTA versus that
  // prior revision — otherwise the original scope is counted twice, inflating
  // the contract that multiplies percent-complete revenue and corrupting the
  // budget baseline. A brand-new job has no prior; a change order raised as a
  // *separate* estimate (a different root) is correctly additive and finds no
  // prior revision here. reviseEstimate carries the job link forward, so the
  // prior revision is discoverable by (same root, already on this job).
  let priorRev: EstimateRecord | undefined;
  if (!created) {
    for (const sib of await store.list(String(ctx.tenant))) {
      if (sib.rootId !== estimate.rootId || sib.id === estimate.id || sib.jobId !== jobId) continue;
      if (!priorRev || sib.revision > priorRev.revision) priorRev = sib;
    }
  }

  const addByCode = costsByCode(estimate);
  const priorByCode = priorRev ? costsByCode(priorRev) : new Map<string, { cost: bigint; revenue: bigint }>();
  if (addByCode.size > 0 || priorByCode.size > 0) {
    // MERGE, never replace, and apply the delta of this revision against any
    // prior revision of the same root — so re-accepting a restated change order
    // nets to the new scope instead of doubling the original. Every other cost
    // code's baseline (from other roots) is left exactly where it was.
    const prior = new Map(
      (await ctx.backend.jobs().listBudget(String(ctx.tenant), jobId))
        .map((b) => [b.costCode, b]),
    );
    const codes = new Set([...prior.keys(), ...addByCode.keys(), ...priorByCode.keys()]);
    const lines = [...codes].map((code) => {
      const add = addByCode.get(code) ?? { cost: 0n, revenue: 0n };
      const prev = priorByCode.get(code) ?? { cost: 0n, revenue: 0n };
      const deltaCost = add.cost - prev.cost;
      const deltaRevenue = add.revenue - prev.revenue;
      const base = prior.get(code);
      const budget = BigInt(base?.budgetCostMinor ?? "0") + deltaCost;
      const revised = BigInt(base?.revisedCostMinor ?? base?.budgetCostMinor ?? "0") + deltaCost;
      const revenue = BigInt(base?.budgetRevenueMinor ?? "0") + deltaRevenue;
      return {
        cost_code: code,
        budget_cost_minor: budget.toString(),
        revised_cost_minor: revised.toString(),
        budget_revenue_minor: revenue.toString(),
      };
    });
    try {
      await saveJobBudget(jobCtx, jobId, { lines });
    } catch (err) {
      if (err instanceof JobError) throw new EstimateError(err.message);
      throw err;
    }
  }

  // A change order raises the contract. On a new job the contract was already
  // set to the estimate total; on an existing one, add this revision's price and
  // back out any prior revision's price so percent-complete and projected margin
  // reflect the current scope, not the sum of every restatement.
  if (!created) {
    const job = await ctx.backend.jobs().getJob(String(ctx.tenant), jobId);
    if (job) {
      const priorPrice = priorRev ? BigInt(estimateTotals(priorRev).price_minor) : 0n;
      const raised = (
        BigInt(job.contractMinor) + BigInt(totals.price_minor) - priorPrice
      ).toString();
      await ctx.backend.jobs().saveJob(String(ctx.tenant), { ...job, contractMinor: raised });
    }
  }

  const accepted: EstimateRecord = { ...estimate, status: "ACCEPTED", jobId };
  await store.save(String(ctx.tenant), accepted);
  return { estimate: accepted, jobId, created, budgetSeeded: addByCode.size };
}

/**
 * The invoice an accepted estimate becomes, as a document request.
 *
 * Returned rather than posted, so the caller runs it through the same AR path
 * as every other invoice — there is no second way to raise an invoice, which is
 * how you end up with two revenue figures.
 */
export function estimateToInvoiceRequest(
  estimate: EstimateRecord,
  invoice: { readonly id: string; readonly date: string; readonly due_date?: string },
): Record<string, unknown> {
  if (estimate.status !== "ACCEPTED") {
    throw new EstimateError(`estimate ${estimate.id} is ${estimate.status}, not ACCEPTED`);
  }
  return {
    id: invoice.id,
    party_id: estimate.customerId,
    date: invoice.date,
    ...(invoice.due_date ? { due_date: invoice.due_date } : {}),
    memo: estimate.memo || `From estimate ${estimate.id}`,
    tax_rate_ppm: estimate.taxRatePpm,
    lines: estimate.lines.map((l) => ({
      description: l.description,
      unit_amount_minor: l.extendedPriceMinor,
      account_code: l.accountCode,
      taxable: l.taxable,
      ...(estimate.jobId
        ? {
          dimensions: {
            job: estimate.jobId,
            ...(l.costCode ? { cost_code: l.costCode } : {}),
          },
        }
        : {}),
    })),
  };
}
