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
import { JOB_DIMENSION, COST_CODE_DIMENSION, type JobRecord } from "./jobs.js";
import { unbilledWork, type WorkOrderContext } from "./workorders.js";

/**
 * The four ways a job gets billed, and the two accounts that make them honest.
 *
 * A contractor does not have one billing method; they have four, often on the
 * same customer. Time and materials for the service call, a schedule of values
 * for the addition, milestones for the fixed-price bathroom, and a deposit
 * taken before any of it starts. Software that supports one and asks the owner
 * to hand-write the others is where the numbers start diverging.
 *
 * All four produce an **ordinary invoice** through the ordinary AR path. There
 * is no second way to bill a customer, because a second way is how you end up
 * with two revenue figures and no idea which the tax return used.
 *
 * ## Retainage
 *
 * On construction work the customer holds back 5–10% until the job is signed
 * off. That money is earned — it is revenue — but it is not collectible yet,
 * and putting it in accounts receivable makes the aging report lie for months.
 *
 * So a progress invoice is raised for what is **collectible now**, and the
 * retained portion posts separately to Retainage Receivable against the same
 * revenue. AR ties to the open invoices, the aging report is true, and the
 * held-back money is a number somebody can actually chase. Releasing it later
 * is an ordinary invoice that credits the retainage account.
 *
 * ## Deposits
 *
 * Money taken before work is not revenue, and calling it revenue is the single
 * most common way a small contractor overstates a good year and gets a tax bill
 * for it. A deposit is a liability until an invoice draws it down, and drawing
 * down more than was taken is refused.
 */

export class BillingError extends Error {}

export const RETAINAGE_RECEIVABLE_CODE = "1260";
export const CUSTOMER_DEPOSITS_CODE = "2400";
const PPM = 1_000_000n;

export type MilestoneStatus = "PENDING" | "BILLED" | "CANCELLED";

export interface ScheduleLineRecord {
  readonly lineNo: number;
  readonly description: string;
  readonly costCode: string;
  readonly scheduledValueMinor: string;
  readonly billedMinor: string;
}

export interface MilestoneRecord {
  readonly id: string;
  readonly jobId: string;
  readonly name: string;
  readonly amountMinor: string;
  readonly dueDate: string;
  readonly status: MilestoneStatus;
  readonly invoiceId: string;
}

export interface BillingStore {
  migrate(): Promise<void>;
  listSchedule(tenant: string, jobId: string): Promise<ScheduleLineRecord[]>;
  saveSchedule(tenant: string, jobId: string, lines: readonly ScheduleLineRecord[]): Promise<void>;
  listMilestones(tenant: string, jobId?: string): Promise<MilestoneRecord[]>;
  saveMilestone(tenant: string, milestone: MilestoneRecord): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryBillingStore implements BillingStore {
  private readonly schedules = new Map<string, ScheduleLineRecord[]>();
  private readonly milestones = new Map<string, MilestoneRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listSchedule(tenant: string, jobId: string): Promise<ScheduleLineRecord[]> {
    return Promise.resolve([...(this.schedules.get(`${tenant}::${jobId}`) ?? [])]);
  }

  saveSchedule(
    tenant: string, jobId: string, lines: readonly ScheduleLineRecord[],
  ): Promise<void> {
    this.schedules.set(`${tenant}::${jobId}`, [...lines]);
    return Promise.resolve();
  }

  listMilestones(tenant: string, jobId?: string): Promise<MilestoneRecord[]> {
    const out: MilestoneRecord[] = [];
    for (const [k, v] of this.milestones) {
      if (!k.startsWith(`${tenant}::`)) continue;
      if (jobId && v.jobId !== jobId) continue;
      out.push(v);
    }
    return Promise.resolve(out.sort((a, b) => (
      a.dueDate === b.dueDate ? a.id.localeCompare(b.id) : a.dueDate.localeCompare(b.dueDate)
    )));
  }

  saveMilestone(tenant: string, milestone: MilestoneRecord): Promise<void> {
    this.milestones.set(`${tenant}::${milestone.id}`, milestone);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const BILLING_DDL = `
CREATE TABLE IF NOT EXISTS schedule_of_values (
  tenant_id             text NOT NULL,
  job_id                text NOT NULL,
  line_no               integer NOT NULL,
  description           text NOT NULL DEFAULT '',
  cost_code             text NOT NULL DEFAULT '',
  scheduled_value_minor text NOT NULL DEFAULT '0',
  billed_minor          text NOT NULL DEFAULT '0',
  CONSTRAINT schedule_of_values_pk PRIMARY KEY (tenant_id, job_id, line_no)
);

CREATE TABLE IF NOT EXISTS billing_milestone (
  tenant_id    text NOT NULL,
  id           text NOT NULL,
  job_id       text NOT NULL,
  name         text NOT NULL DEFAULT '',
  amount_minor text NOT NULL DEFAULT '0',
  due_date     text NOT NULL DEFAULT '',
  status       text NOT NULL DEFAULT 'PENDING',
  invoice_id   text NOT NULL DEFAULT '',
  CONSTRAINT billing_milestone_pk PRIMARY KEY (tenant_id, id)
);
`;

export class PgBillingStore implements BillingStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(BILLING_DDL);
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

  async listSchedule(tenant: string, jobId: string): Promise<ScheduleLineRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT * FROM schedule_of_values WHERE tenant_id=$1 AND job_id=$2 ORDER BY line_no`,
        [tenant, jobId],
      );
      return res.rows.map((r) => ({
        lineNo: Number(r["line_no"]),
        description: String(r["description"] ?? ""),
        costCode: String(r["cost_code"] ?? ""),
        scheduledValueMinor: String(r["scheduled_value_minor"] ?? "0"),
        billedMinor: String(r["billed_minor"] ?? "0"),
      }));
    });
  }

  async saveSchedule(
    tenant: string, jobId: string, lines: readonly ScheduleLineRecord[],
  ): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM schedule_of_values WHERE tenant_id=$1 AND job_id=$2", [tenant, jobId],
      );
      for (const l of lines) {
        await db.query(
          `INSERT INTO schedule_of_values (tenant_id, job_id, line_no, description,
             cost_code, scheduled_value_minor, billed_minor)
           VALUES ($1,$2,$3,$4,$5,$6,$7)`,
          [
            tenant, jobId, l.lineNo, l.description, l.costCode,
            l.scheduledValueMinor, l.billedMinor,
          ],
        );
      }
    });
  }

  async listMilestones(tenant: string, jobId?: string): Promise<MilestoneRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = jobId
        ? await db.query(
          `SELECT * FROM billing_milestone WHERE tenant_id=$1 AND job_id=$2
           ORDER BY due_date, id`, [tenant, jobId],
        )
        : await db.query(
          "SELECT * FROM billing_milestone WHERE tenant_id=$1 ORDER BY due_date, id", [tenant],
        );
      return res.rows.map((r) => ({
        id: String(r["id"]),
        jobId: String(r["job_id"]),
        name: String(r["name"] ?? ""),
        amountMinor: String(r["amount_minor"] ?? "0"),
        dueDate: String(r["due_date"] ?? ""),
        status: String(r["status"]) as MilestoneStatus,
        invoiceId: String(r["invoice_id"] ?? ""),
      }));
    });
  }

  async saveMilestone(tenant: string, milestone: MilestoneRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO billing_milestone (tenant_id, id, job_id, name, amount_minor,
         due_date, status, invoice_id)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         job_id=EXCLUDED.job_id, name=EXCLUDED.name, amount_minor=EXCLUDED.amount_minor,
         due_date=EXCLUDED.due_date, status=EXCLUDED.status, invoice_id=EXCLUDED.invoice_id`,
      [
        tenant, milestone.id, milestone.jobId, milestone.name, milestone.amountMinor,
        milestone.dueDate, milestone.status, milestone.invoiceId,
      ],
    ));
  }
}

// --- the flow ----------------------------------------------------------------

export interface BillingContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

function requireDate(raw: unknown, label: string): string {
  const date = String(raw ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new BillingError(`${label} must be YYYY-MM-DD`);
  return date;
}

function minorOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^\d+$/.test(value)) {
    throw new BillingError(`${label} must be a whole number of minor units`);
  }
  return BigInt(value);
}

async function requireJob(ctx: BillingContext, jobId: string): Promise<JobRecord> {
  const job = await ctx.backend.jobs().getJob(String(ctx.tenant), jobId);
  if (!job) throw new BillingError(`unknown job ${jobId}`);
  return job;
}

function provenanceFor(source: string, id: string, date: string, at: string): Provenance {
  return {
    sourceSystem: source,
    sourceObject: id,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/billing-1",
    mappingVersion: "ledger-service/billing-1",
  };
}

/** What a billing call produces: an invoice to post, and what to do after. */
export interface BillingDraft {
  readonly request: Record<string, unknown>;
  readonly job: JobRecord;
  /** Held back by the customer — posted separately, never in AR. */
  readonly retainageMinor: bigint;
  readonly grossMinor: bigint;
  /** Applied after the invoice posts. */
  readonly onPosted: {
    readonly scheduleLines?: readonly ScheduleLineRecord[];
    readonly milestoneId?: string;
    readonly workEntryIds?: readonly string[];
  };
}

// --- schedule of values ------------------------------------------------------

export interface ScheduleInput {
  readonly lines?: ReadonlyArray<{
    readonly description?: string;
    readonly cost_code?: string;
    readonly scheduled_value_minor?: string | number;
  }>;
}

/**
 * The schedule of values: the contract, cut into the pieces it will be billed
 * in. The pieces have to add up to the contract, because a schedule that does
 * not is how a job gets billed for 104% of itself one line at a time.
 */
export async function saveSchedule(
  ctx: BillingContext, jobId: string, input: ScheduleInput,
): Promise<ScheduleLineRecord[]> {
  const job = await requireJob(ctx, jobId);
  const existing = await ctx.backend.billing().listSchedule(String(ctx.tenant), jobId);
  const codes = new Set(
    (await ctx.backend.jobs().listCostCodes(String(ctx.tenant))).map((c) => c.code),
  );

  const lines: ScheduleLineRecord[] = [];
  let total = 0n;
  let lineNo = 1;
  for (const l of input.lines ?? []) {
    const value = minorOf(l.scheduled_value_minor, `line ${lineNo} scheduled value`);
    if (value === 0n) throw new BillingError(`line ${lineNo}: needs a value`);
    const costCode = String(l.cost_code ?? "").trim().toUpperCase();
    if (costCode && !codes.has(costCode)) {
      throw new BillingError(`unknown cost code ${costCode}`);
    }
    const previous = existing.find((x) => x.lineNo === lineNo);
    if (previous && BigInt(previous.billedMinor) > value) {
      throw new BillingError(
        `line ${lineNo} has already been billed ${previous.billedMinor} — it cannot be cut to ${value}`,
      );
    }
    total += value;
    lines.push({
      lineNo,
      description: String(l.description ?? "").trim(),
      costCode,
      scheduledValueMinor: value.toString(),
      billedMinor: previous?.billedMinor ?? "0",
    });
    lineNo += 1;
  }
  if (lines.length === 0) throw new BillingError("a schedule of values needs at least one line");

  const contract = BigInt(job.contractMinor);
  if (contract > 0n && total !== contract) {
    throw new BillingError(
      `the schedule adds up to ${total} but the contract is ${contract} — they have to agree`,
    );
  }
  await ctx.backend.billing().saveSchedule(String(ctx.tenant), jobId, lines);
  return lines;
}

export interface ProgressBillInput {
  readonly id?: string;
  readonly date?: string;
  readonly due_date?: string;
  readonly memo?: string;
  /** Override the job's retainage rate for this application. */
  readonly retainage_ppm?: number;
  readonly lines?: ReadonlyArray<{
    readonly line_no?: number;
    /** Percent complete for this line, in parts per million. */
    readonly percent_ppm?: number;
    /** …or the amount to bill this period, if it is being typed directly. */
    readonly amount_minor?: string | number;
  }>;
}

/**
 * A progress bill: for each line of the schedule, the value earned to date
 * less what has already been billed.
 *
 * Percent complete is per line and cumulative, which is how the AIA form works
 * and how every general contractor's project manager reads it. Billing a line
 * past 100% is refused — that is not a progress bill, it is a change order, and
 * a change order belongs on the contract.
 */
export async function billProgress(
  ctx: BillingContext, jobId: string, input: ProgressBillInput,
): Promise<BillingDraft> {
  const job = await requireJob(ctx, jobId);
  const schedule = await ctx.backend.billing().listSchedule(String(ctx.tenant), jobId);
  if (schedule.length === 0) {
    throw new BillingError(`job ${jobId} has no schedule of values to bill against`);
  }
  const date = requireDate(input.date, "date");
  const invoiceId = String(input.id ?? "").trim();
  if (!invoiceId) throw new BillingError("the invoice needs an id");

  const wanted = new Map<number, bigint>();
  for (const l of input.lines ?? []) {
    const lineNo = Number(l.line_no ?? 0);
    const line = schedule.find((x) => x.lineNo === lineNo);
    if (!line) throw new BillingError(`the schedule has no line ${lineNo}`);
    const scheduled = BigInt(line.scheduledValueMinor);
    const billed = BigInt(line.billedMinor);
    let amount: bigint;
    if (l.percent_ppm !== undefined) {
      const ppm = Number(l.percent_ppm);
      if (!Number.isInteger(ppm) || ppm < 0 || ppm > 1_000_000) {
        throw new BillingError(`line ${lineNo}: percent complete must be between 0 and 100%`);
      }
      amount = mulDiv(scheduled, BigInt(ppm), PPM) - billed;
    } else {
      amount = minorOf(l.amount_minor, `line ${lineNo} amount`);
    }
    if (amount === 0n) continue;
    if (amount < 0n) {
      throw new BillingError(
        `line ${lineNo}: that is less than has already been billed — issue a credit, don't bill backwards`,
      );
    }
    if (billed + amount > scheduled) {
      throw new BillingError(
        `line ${lineNo}: billing ${billed + amount} against a scheduled ${scheduled} — that is a change order, not a progress bill`,
      );
    }
    wanted.set(lineNo, amount);
  }
  if (wanted.size === 0) throw new BillingError("nothing to bill this period");

  const retainagePpm = input.retainage_ppm === undefined
    ? job.retainagePpm
    : Number(input.retainage_ppm);
  if (!Number.isInteger(retainagePpm) || retainagePpm < 0 || retainagePpm > 1_000_000) {
    throw new BillingError("retainage must be between 0 and 100%");
  }

  let gross = 0n;
  let retainage = 0n;
  const lines: Record<string, unknown>[] = [];
  const updated: ScheduleLineRecord[] = schedule.map((l) => {
    const amount = wanted.get(l.lineNo);
    if (!amount) return l;
    gross += amount;
    const held = mulDiv(amount, BigInt(retainagePpm), PPM);
    retainage += held;
    const net = amount - held;
    if (net > 0n) {
      lines.push({
        description: l.description || `Line ${l.lineNo}`,
        unit_amount_minor: net.toString(),
        account_code: job.revenueAccountCode,
        taxable: false,
        dimensions: {
          [JOB_DIMENSION]: job.id,
          ...(l.costCode ? { [COST_CODE_DIMENSION]: l.costCode } : {}),
        },
      });
    }
    return { ...l, billedMinor: (BigInt(l.billedMinor) + amount).toString() };
  });

  if (lines.length === 0) {
    throw new BillingError("every line of this application is held as retainage — nothing to invoice");
  }

  return {
    request: {
      id: invoiceId,
      party_id: job.customerId,
      date,
      ...(input.due_date ? { due_date: input.due_date } : {}),
      memo: String(input.memo ?? "") || `${job.name} — progress billing`,
      lines,
    },
    job,
    retainageMinor: retainage,
    grossMinor: gross,
    onPosted: { scheduleLines: updated },
  };
}

// --- milestones --------------------------------------------------------------

export interface MilestoneInput {
  readonly milestones?: ReadonlyArray<{
    readonly id?: string;
    readonly name?: string;
    readonly amount_minor?: string | number;
    readonly due_date?: string;
  }>;
}

export async function saveMilestones(
  ctx: BillingContext, jobId: string, input: MilestoneInput,
): Promise<MilestoneRecord[]> {
  const job = await requireJob(ctx, jobId);
  const store = ctx.backend.billing();
  const existing = await store.listMilestones(String(ctx.tenant), jobId);

  const out: MilestoneRecord[] = [];
  let total = 0n;
  let n = existing.length;
  for (const m of input.milestones ?? []) {
    const name = String(m.name ?? "").trim();
    if (!name) throw new BillingError("a milestone needs a name");
    const amount = minorOf(m.amount_minor, `${name} amount`);
    if (amount === 0n) throw new BillingError(`${name}: needs an amount`);
    n += 1;
    const id = String(m.id ?? "").trim() || `${jobId}-M${n}`;
    const previous = existing.find((x) => x.id === id);
    if (previous?.status === "BILLED") {
      throw new BillingError(`milestone ${id} has been billed and cannot be changed`);
    }
    total += amount;
    const record: MilestoneRecord = {
      id,
      jobId,
      name,
      amountMinor: amount.toString(),
      dueDate: String(m.due_date ?? "").trim(),
      status: "PENDING",
      invoiceId: "",
    };
    await store.saveMilestone(String(ctx.tenant), record);
    out.push(record);
  }

  const contract = BigInt(job.contractMinor);
  const alreadyBilled = existing
    .filter((m) => m.status === "BILLED")
    .reduce((acc, m) => acc + BigInt(m.amountMinor), 0n);
  if (contract > 0n && total + alreadyBilled > contract) {
    throw new BillingError(
      `these milestones add up to ${total + alreadyBilled} against a contract of ${contract}`,
    );
  }
  return out;
}

export interface MilestoneBillInput {
  readonly id?: string;
  readonly milestone_id?: string;
  readonly date?: string;
  readonly due_date?: string;
  readonly memo?: string;
  readonly retainage_ppm?: number;
}

export async function billMilestone(
  ctx: BillingContext, jobId: string, input: MilestoneBillInput,
): Promise<BillingDraft> {
  const job = await requireJob(ctx, jobId);
  const milestoneId = String(input.milestone_id ?? "").trim();
  const milestones = await ctx.backend.billing().listMilestones(String(ctx.tenant), jobId);
  const milestone = milestones.find((m) => m.id === milestoneId);
  if (!milestone) throw new BillingError(`unknown milestone ${milestoneId || "(none)"}`);
  if (milestone.status !== "PENDING") {
    throw new BillingError(`milestone ${milestoneId} is ${milestone.status}`);
  }
  const date = requireDate(input.date, "date");
  const invoiceId = String(input.id ?? "").trim();
  if (!invoiceId) throw new BillingError("the invoice needs an id");

  const retainagePpm = input.retainage_ppm === undefined
    ? job.retainagePpm
    : Number(input.retainage_ppm);
  const gross = BigInt(milestone.amountMinor);
  const retainage = mulDiv(gross, BigInt(retainagePpm), PPM);
  const net = gross - retainage;
  if (net <= 0n) throw new BillingError("the whole milestone is retainage — nothing to invoice");

  return {
    request: {
      id: invoiceId,
      party_id: job.customerId,
      date,
      ...(input.due_date ? { due_date: input.due_date } : {}),
      memo: String(input.memo ?? "") || `${job.name} — ${milestone.name}`,
      lines: [{
        description: milestone.name,
        unit_amount_minor: net.toString(),
        account_code: job.revenueAccountCode,
        taxable: false,
        dimensions: { [JOB_DIMENSION]: job.id },
      }],
    },
    job,
    retainageMinor: retainage,
    grossMinor: gross,
    onPosted: { milestoneId: milestone.id },
  };
}

// --- time and materials ------------------------------------------------------

export interface TimeAndMaterialsInput {
  readonly id?: string;
  readonly date?: string;
  readonly due_date?: string;
  readonly memo?: string;
  readonly through?: string;
  /** One line per work order rather than one per entry. */
  readonly summarize?: boolean;
  readonly tax_rate_ppm?: number;
}

/**
 * Bill the time and materials nobody has invoiced yet.
 *
 * The default is a line per entry, because a customer who queries a T&M invoice
 * is asking "what did you do on the 14th", and a single line saying "labor,
 * $4,320" cannot answer them. Summarizing by work order is available for the
 * customer who would rather not see it.
 */
export async function billTimeAndMaterials(
  ctx: BillingContext, jobId: string, input: TimeAndMaterialsInput,
): Promise<BillingDraft> {
  const job = await requireJob(ctx, jobId);
  const date = requireDate(input.date, "date");
  const invoiceId = String(input.id ?? "").trim();
  if (!invoiceId) throw new BillingError("the invoice needs an id");

  const workCtx: WorkOrderContext = {
    backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency, now: ctx.now,
  };
  const through = String(input.through ?? "").trim() || date;
  const unbilled = await unbilledWork(workCtx, jobId, through);
  if (unbilled.length === 0) {
    throw new BillingError(`job ${jobId} has no unbilled billable work up to ${through}`);
  }

  const lines: Record<string, unknown>[] = [];
  const entryIds: string[] = [];
  let gross = 0n;

  if (input.summarize === true) {
    const byOrder = new Map<string, { title: string; amount: bigint }>();
    for (const u of unbilled) {
      const bucket = byOrder.get(u.order.id) ?? { title: u.order.title, amount: 0n };
      bucket.amount += u.billMinor;
      byOrder.set(u.order.id, bucket);
      entryIds.push(u.entry.id);
      gross += u.billMinor;
    }
    for (const [orderId, bucket] of byOrder) {
      lines.push({
        description: `${orderId} — ${bucket.title}`,
        unit_amount_minor: bucket.amount.toString(),
        account_code: job.revenueAccountCode,
        dimensions: { [JOB_DIMENSION]: job.id },
      });
    }
  } else {
    for (const u of unbilled) {
      const hours = BigInt(u.entry.quantityMilli);
      const label = u.entry.description
        || `${u.order.title} — ${u.entry.kind.toLowerCase()}`;
      lines.push({
        description: `${u.entry.date} ${label}`,
        unit_amount_minor: u.billMinor.toString(),
        account_code: job.revenueAccountCode,
        dimensions: {
          [JOB_DIMENSION]: job.id,
          ...(u.entry.costCode ? { [COST_CODE_DIMENSION]: u.entry.costCode } : {}),
        },
      });
      entryIds.push(u.entry.id);
      gross += u.billMinor;
      void hours;
    }
  }

  return {
    request: {
      id: invoiceId,
      party_id: job.customerId,
      date,
      ...(input.due_date ? { due_date: input.due_date } : {}),
      memo: String(input.memo ?? "") || `${job.name} — time and materials`,
      ...(input.tax_rate_ppm ? { tax_rate_ppm: Number(input.tax_rate_ppm) } : {}),
      lines,
    },
    job,
    retainageMinor: 0n,
    grossMinor: gross,
    onPosted: { workEntryIds: entryIds },
  };
}

// --- what happens after the invoice posts ------------------------------------

/**
 * Post the retained portion: earned revenue that the customer is holding back.
 *
 * It goes to Retainage Receivable rather than accounts receivable so that the
 * aging report keeps telling the truth about what is actually collectible. The
 * invoice itself was raised for the net.
 */
export async function postRetainage(
  ctx: BillingContext, draft: BillingDraft, invoiceId: string, date: string,
): Promise<string> {
  if (draft.retainageMinor <= 0n) return "";
  const chart = await ctx.backend.chart(ctx.tenant);
  const retainage = chart.getByCode(RETAINAGE_RECEIVABLE_CODE);
  const revenue = chart.getByCode(draft.job.revenueAccountCode);
  if (!retainage || !revenue) {
    throw new BillingError(
      `this chart has no account ${RETAINAGE_RECEIVABLE_CODE} — create a job to have the job-costing accounts added`,
    );
  }
  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`retainage:${invoiceId}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: `${draft.job.name} — retainage held on ${invoiceId}`,
    provenance: provenanceFor("retainage", invoiceId, date, ctx.now()),
    lines: [
      {
        accountId: retainage.id,
        side: "DEBIT",
        amount: Money.fromMinorUnits(draft.retainageMinor, ctx.currency),
        memo: "Held by the customer",
        dimensions: { [JOB_DIMENSION]: draft.job.id },
      },
      {
        accountId: revenue.id,
        side: "CREDIT",
        amount: Money.fromMinorUnits(draft.retainageMinor, ctx.currency),
        memo: "Earned, not yet collectible",
        dimensions: { [JOB_DIMENSION]: draft.job.id },
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

/** Record what a billing run consumed, once the invoice has actually posted. */
export async function applyDraft(
  ctx: BillingContext, draft: BillingDraft, invoiceId: string,
): Promise<void> {
  const tenant = String(ctx.tenant);
  if (draft.onPosted.scheduleLines) {
    await ctx.backend.billing().saveSchedule(tenant, draft.job.id, draft.onPosted.scheduleLines);
  }
  if (draft.onPosted.milestoneId) {
    const milestones = await ctx.backend.billing().listMilestones(tenant, draft.job.id);
    const milestone = milestones.find((m) => m.id === draft.onPosted.milestoneId);
    if (milestone) {
      await ctx.backend.billing().saveMilestone(tenant, {
        ...milestone, status: "BILLED", invoiceId,
      });
    }
  }
  for (const entryId of draft.onPosted.workEntryIds ?? []) {
    const entry = await ctx.backend.workOrders().getEntry(tenant, entryId);
    if (entry) await ctx.backend.workOrders().saveEntry(tenant, { ...entry, invoiceId });
  }
}

/**
 * Release retainage: an ordinary invoice whose line credits the retainage
 * account, so the money moves from held-back to collectible without inventing
 * revenue a second time.
 */
export async function releaseRetainageRequest(
  ctx: BillingContext, jobId: string, input: {
    readonly id?: string; readonly date?: string; readonly due_date?: string;
    readonly amount_minor?: string | number; readonly memo?: string;
  },
): Promise<Record<string, unknown>> {
  const job = await requireJob(ctx, jobId);
  const date = requireDate(input.date, "date");
  const invoiceId = String(input.id ?? "").trim();
  if (!invoiceId) throw new BillingError("the invoice needs an id");

  const held = await retainageHeld(ctx, jobId);
  const amount = input.amount_minor === undefined || String(input.amount_minor).trim() === ""
    ? held
    : minorOf(input.amount_minor, "amount");
  if (amount <= 0n) throw new BillingError(`job ${jobId} has no retainage to release`);
  if (amount > held) {
    throw new BillingError(`only ${held} of retainage is held on ${jobId}, not ${amount}`);
  }
  return {
    id: invoiceId,
    party_id: job.customerId,
    date,
    ...(input.due_date ? { due_date: input.due_date } : {}),
    memo: String(input.memo ?? "") || `${job.name} — retainage released`,
    lines: [{
      description: "Retainage released",
      unit_amount_minor: amount.toString(),
      account_code: RETAINAGE_RECEIVABLE_CODE,
      taxable: false,
      dimensions: { [JOB_DIMENSION]: job.id },
    }],
  };
}

/** The balance of one account on one job, debit-positive. */
async function jobAccountBalance(
  ctx: BillingContext, jobId: string, code: string,
): Promise<bigint> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const account = chart.getByCode(code);
  if (!account) return 0n;
  let balance = 0n;
  for (const entry of await ctx.backend.store(ctx.tenant).list(ctx.tenant)) {
    for (const line of entry.lines) {
      if (line.accountId !== account.id) continue;
      if (line.dimensions?.[JOB_DIMENSION] !== jobId) continue;
      balance += line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
    }
  }
  return balance;
}

export function retainageHeld(ctx: BillingContext, jobId: string): Promise<bigint> {
  return jobAccountBalance(ctx, jobId, RETAINAGE_RECEIVABLE_CODE);
}

/** Deposits are a credit balance; report them as a positive amount held. */
export async function depositHeld(ctx: BillingContext, jobId: string): Promise<bigint> {
  return -(await jobAccountBalance(ctx, jobId, CUSTOMER_DEPOSITS_CODE));
}

export interface DepositInput {
  readonly id?: string;
  readonly date?: string;
  readonly amount_minor?: string | number;
  readonly bank_code?: string;
  readonly memo?: string;
}

/**
 * Take a deposit. It is a liability, not income: money taken for work not yet
 * done is the most common way a small contractor overstates a good year and
 * gets a tax bill for it.
 */
export async function takeDeposit(
  ctx: BillingContext, jobId: string, input: DepositInput,
): Promise<{ entryId: string; heldMinor: string }> {
  const job = await requireJob(ctx, jobId);
  const date = requireDate(input.date, "date");
  const amount = minorOf(input.amount_minor, "amount");
  if (amount <= 0n) throw new BillingError("a deposit needs an amount");

  const chart = await ctx.backend.chart(ctx.tenant);
  const bankCode = String(input.bank_code ?? "").trim() || "1000";
  const bank = chart.getByCode(bankCode);
  const deposits = chart.getByCode(CUSTOMER_DEPOSITS_CODE);
  if (!bank) throw new BillingError(`unknown account code ${bankCode}`);
  if (!deposits) {
    throw new BillingError(
      `this chart has no account ${CUSTOMER_DEPOSITS_CODE} — create a job to have the job-costing accounts added`,
    );
  }
  // With an explicit id the deposit is idempotent — a retried request with the
  // same id posts once. Without one, two real deposits on the same job the same
  // day (a morning deposit, an afternoon top-up) are distinct events, so the
  // default id carries a per-(job,date) sequence rather than collapsing them
  // into one on a `${job}-${date}` collision.
  const explicitId = String(input.id ?? "").trim();
  let id = explicitId;
  if (!id) {
    const priorSameDay = (await ctx.backend.store(ctx.tenant).list(ctx.tenant)).filter(
      (e) => e.provenance.sourceSystem === "deposit"
        && e.entryDate === date
        && e.lines.some((l) => l.dimensions?.[JOB_DIMENSION] === job.id),
    ).length;
    id = `${jobId}-${date}#${priorSameDay}`;
  }
  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`deposit:${id}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: String(input.memo ?? "") || `${job.name} — deposit received`,
    provenance: provenanceFor("deposit", id, date, ctx.now()),
    lines: [
      {
        accountId: bank.id,
        side: "DEBIT",
        amount: Money.fromMinorUnits(amount, ctx.currency),
        memo: "Deposit received",
      },
      {
        accountId: deposits.id,
        side: "CREDIT",
        amount: Money.fromMinorUnits(amount, ctx.currency),
        memo: "Held against work not yet done",
        dimensions: { [JOB_DIMENSION]: job.id },
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
  return {
    entryId: String(entry.id),
    heldMinor: (await depositHeld(ctx, jobId)).toString(),
  };
}

/**
 * Draw a deposit down against an invoice.
 *
 * This is an ordinary payment whose "bank account" is the deposit liability, so
 * the invoice closes the same way a cash payment closes it and the liability
 * comes off the balance sheet. Applying more than was taken is refused.
 */
export async function depositApplication(
  ctx: BillingContext, jobId: string, input: {
    readonly invoice_id?: string; readonly date?: string;
    readonly amount_minor?: string | number;
  },
): Promise<{ docId: string; date: string; amountMinor: string }> {
  await requireJob(ctx, jobId);
  const date = requireDate(input.date, "date");
  const invoiceId = String(input.invoice_id ?? "").trim();
  if (!invoiceId) throw new BillingError("which invoice is the deposit being applied to?");
  const doc = await ctx.backend.documents().getDoc(String(ctx.tenant), "invoice", invoiceId);
  if (!doc) throw new BillingError(`unknown invoice ${invoiceId}`);

  const held = await depositHeld(ctx, jobId);
  const open = BigInt(doc.openMinor);
  const amount = input.amount_minor === undefined || String(input.amount_minor).trim() === ""
    ? (held < open ? held : open)
    : minorOf(input.amount_minor, "amount");
  if (amount <= 0n) throw new BillingError(`job ${jobId} has no deposit to apply`);
  if (amount > held) {
    throw new BillingError(`only ${held} of deposit is held on ${jobId}, not ${amount}`);
  }
  if (amount > open) {
    throw new BillingError(`invoice ${invoiceId} only has ${open} outstanding`);
  }
  return { docId: invoiceId, date, amountMinor: amount.toString() };
}

// --- reading -----------------------------------------------------------------

export async function billingView(
  ctx: BillingContext, jobId: string,
): Promise<Record<string, unknown>> {
  const job = await requireJob(ctx, jobId);
  const schedule = await ctx.backend.billing().listSchedule(String(ctx.tenant), jobId);
  const milestones = await ctx.backend.billing().listMilestones(String(ctx.tenant), jobId);
  const workCtx: WorkOrderContext = {
    backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency, now: ctx.now,
  };
  const unbilled = await unbilledWork(workCtx, jobId);

  let scheduled = 0n;
  let billed = 0n;
  for (const l of schedule) {
    scheduled += BigInt(l.scheduledValueMinor);
    billed += BigInt(l.billedMinor);
  }
  return {
    contract: "job-billing/1",
    currency: ctx.currency.code,
    job_id: job.id,
    billing_method: job.billingMethod,
    contract_minor: job.contractMinor,
    retainage_ppm: job.retainagePpm,
    schedule: schedule.map((l) => ({
      line_no: l.lineNo,
      description: l.description,
      cost_code: l.costCode,
      scheduled_value_minor: l.scheduledValueMinor,
      billed_minor: l.billedMinor,
      remaining_minor: (BigInt(l.scheduledValueMinor) - BigInt(l.billedMinor)).toString(),
      percent_billed_ppm: BigInt(l.scheduledValueMinor) > 0n
        ? Number(mulDiv(BigInt(l.billedMinor), PPM, BigInt(l.scheduledValueMinor)))
        : 0,
    })),
    milestones: milestones.map((m) => ({
      id: m.id,
      name: m.name,
      amount_minor: m.amountMinor,
      due_date: m.dueDate,
      status: m.status,
      invoice_id: m.invoiceId,
    })),
    unbilled_work: unbilled.map((u) => ({
      entry_id: u.entry.id,
      work_order_id: u.order.id,
      date: u.entry.date,
      description: u.entry.description || u.order.title,
      kind: u.entry.kind,
      quantity_milli: u.entry.quantityMilli,
      bill_minor: u.billMinor.toString(),
    })),
    totals: {
      scheduled_minor: scheduled.toString(),
      billed_minor: billed.toString(),
      remaining_minor: (scheduled - billed).toString(),
      unbilled_work_minor: unbilled.reduce((acc, u) => acc + u.billMinor, 0n).toString(),
      retainage_held_minor: (await retainageHeld(ctx, jobId)).toString(),
      deposit_held_minor: (await depositHeld(ctx, jobId)).toString(),
    },
  };
}
