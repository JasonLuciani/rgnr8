import {
  AccountSubtype,
  accountTypeOfSubtype,
  asAccountId,
  type Account,
  type Currency,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";

/**
 * Jobs — the thing a contractor actually manages, and the thing a chart of
 * accounts cannot express.
 *
 * A P&L says the business spent $412,000 on materials this year. That is not a
 * question anybody running work asks. The questions are "is the Harper kitchen
 * making money", "how much of the framing budget is left", and "have we billed
 * ahead of what we've built". Answering them needs three structures the general
 * ledger doesn't have on its own:
 *
 * 1. A **job** — a unit of work under a customer, with a contract value, a
 *    billing method, and a life of its own that spans periods.
 * 2. **Cost codes** — a small, defined breakdown (labor, materials, subs,
 *    equipment, other, and whatever else the trade needs) that every cost lands
 *    in. The whole value of job costing is comparing like with like across jobs,
 *    which free-text categories destroy.
 * 3. A **budget** per job and cost code, with the original estimate kept apart
 *    from the current one. Cost-to-complete is exactly `revised − actual`, and
 *    it is meaningless if revising the estimate quietly overwrites what was
 *    originally promised.
 *
 * Costs reach a job through the journal, not a parallel system: every line can
 * carry `job` and `cost_code` dimensions, which are validated against these
 * tables rather than typed free-hand. That means a bill, a bank feed line, a
 * payroll run, a recurring template, and a work order all cost a job the same
 * way, and the job report and the trial balance can never disagree — they are
 * reading the same rows.
 */

export class JobError extends Error {}

export const JOB_DIMENSION = "job";
export const COST_CODE_DIMENSION = "cost_code";

export type JobStatus = "ESTIMATING" | "ACTIVE" | "ON_HOLD" | "COMPLETE" | "CLOSED";
export type BillingMethod =
  | "TIME_AND_MATERIALS"
  | "PROGRESS"
  | "FIXED_MILESTONES"
  | "COST_PLUS";
/**
 * How cost hits the P&L before the job is billed.
 *
 * `AS_INCURRED` books cost to COGS the day it happens and corrects the timing
 * mismatch with a percent-complete WIP entry — what most CPAs expect to see.
 * `CAPITALIZE` parks cost on the balance sheet and relieves it when the job is
 * billed, which suits shorter jobs where a monthly WIP entry is more ceremony
 * than the numbers deserve.
 */
export type CostMethod = "AS_INCURRED" | "CAPITALIZE";

export type CostCategory = "LABOR" | "MATERIAL" | "SUBCONTRACT" | "EQUIPMENT" | "OTHER";

const STATUSES: readonly JobStatus[] = [
  "ESTIMATING", "ACTIVE", "ON_HOLD", "COMPLETE", "CLOSED",
];
const BILLING_METHODS: readonly BillingMethod[] = [
  "TIME_AND_MATERIALS", "PROGRESS", "FIXED_MILESTONES", "COST_PLUS",
];
const COST_METHODS: readonly CostMethod[] = ["AS_INCURRED", "CAPITALIZE"];
const CATEGORIES: readonly CostCategory[] = [
  "LABOR", "MATERIAL", "SUBCONTRACT", "EQUIPMENT", "OTHER",
];

export interface CostCodeRecord {
  readonly code: string;
  readonly name: string;
  readonly category: CostCategory;
  /** Where cost in this code posts by default. */
  readonly accountCode: string;
  readonly active: boolean;
}

export interface JobRecord {
  readonly id: string;
  readonly customerId: string;
  readonly name: string;
  readonly status: JobStatus;
  readonly billingMethod: BillingMethod;
  readonly costMethod: CostMethod;
  readonly startDate: string;
  readonly endDate: string;
  /** The contract value as currently agreed, including approved change orders. */
  readonly contractMinor: string;
  /** Retainage withheld by the customer, in parts per million (5% = 50000). */
  readonly retainagePpm: number;
  /** Where this job's revenue posts. */
  readonly revenueAccountCode: string;
  readonly memo: string;
}

export interface JobBudgetLine {
  readonly costCode: string;
  /** What was originally estimated. Never overwritten by a revision. */
  readonly budgetCostMinor: string;
  /** The current estimate of total cost for this code. */
  readonly revisedCostMinor: string;
  /** What this code is expected to earn (only meaningful on some jobs). */
  readonly budgetRevenueMinor: string;
}

export interface JobStore {
  migrate(): Promise<void>;
  listCostCodes(tenant: string): Promise<CostCodeRecord[]>;
  saveCostCode(tenant: string, record: CostCodeRecord): Promise<void>;
  listJobs(tenant: string): Promise<JobRecord[]>;
  getJob(tenant: string, id: string): Promise<JobRecord | undefined>;
  saveJob(tenant: string, record: JobRecord): Promise<void>;
  listBudget(tenant: string, jobId: string): Promise<JobBudgetLine[]>;
  saveBudget(tenant: string, jobId: string, lines: readonly JobBudgetLine[]): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryJobStore implements JobStore {
  private readonly costCodes = new Map<string, CostCodeRecord>();
  private readonly jobs = new Map<string, JobRecord>();
  private readonly budgets = new Map<string, JobBudgetLine[]>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listCostCodes(tenant: string): Promise<CostCodeRecord[]> {
    const out: CostCodeRecord[] = [];
    for (const [k, v] of this.costCodes) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.code.localeCompare(b.code)));
  }

  saveCostCode(tenant: string, record: CostCodeRecord): Promise<void> {
    this.costCodes.set(`${tenant}::${record.code}`, record);
    return Promise.resolve();
  }

  listJobs(tenant: string): Promise<JobRecord[]> {
    const out: JobRecord[] = [];
    for (const [k, v] of this.jobs) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.name.localeCompare(b.name)));
  }

  getJob(tenant: string, id: string): Promise<JobRecord | undefined> {
    return Promise.resolve(this.jobs.get(`${tenant}::${id}`));
  }

  saveJob(tenant: string, record: JobRecord): Promise<void> {
    this.jobs.set(`${tenant}::${record.id}`, record);
    return Promise.resolve();
  }

  listBudget(tenant: string, jobId: string): Promise<JobBudgetLine[]> {
    return Promise.resolve([...(this.budgets.get(`${tenant}::${jobId}`) ?? [])]);
  }

  saveBudget(tenant: string, jobId: string, lines: readonly JobBudgetLine[]): Promise<void> {
    this.budgets.set(`${tenant}::${jobId}`, [...lines]);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const JOB_DDL = `
CREATE TABLE IF NOT EXISTS cost_code (
  tenant_id    text NOT NULL,
  code         text NOT NULL,
  name         text NOT NULL,
  category     text NOT NULL,
  account_code text NOT NULL,
  active       boolean NOT NULL DEFAULT true,
  CONSTRAINT cost_code_pk PRIMARY KEY (tenant_id, code)
);

CREATE TABLE IF NOT EXISTS job (
  tenant_id            text NOT NULL,
  id                   text NOT NULL,
  customer_id          text NOT NULL,
  name                 text NOT NULL,
  status               text NOT NULL DEFAULT 'ACTIVE',
  billing_method       text NOT NULL DEFAULT 'TIME_AND_MATERIALS',
  cost_method          text NOT NULL DEFAULT 'AS_INCURRED',
  start_date           text NOT NULL DEFAULT '',
  end_date             text NOT NULL DEFAULT '',
  contract_minor       text NOT NULL DEFAULT '0',
  retainage_ppm        integer NOT NULL DEFAULT 0,
  revenue_account_code text NOT NULL DEFAULT '4100',
  memo                 text NOT NULL DEFAULT '',
  CONSTRAINT job_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS job_budget (
  tenant_id            text NOT NULL,
  job_id               text NOT NULL,
  cost_code            text NOT NULL,
  budget_cost_minor    text NOT NULL DEFAULT '0',
  revised_cost_minor   text NOT NULL DEFAULT '0',
  budget_revenue_minor text NOT NULL DEFAULT '0',
  CONSTRAINT job_budget_pk PRIMARY KEY (tenant_id, job_id, cost_code)
);
`;

export class PgJobStore implements JobStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(JOB_DDL);
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

  async listCostCodes(tenant: string): Promise<CostCodeRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM cost_code WHERE tenant_id=$1 ORDER BY code", [tenant],
      );
      return res.rows.map((r) => ({
        code: String(r["code"]),
        name: String(r["name"]),
        category: String(r["category"]) as CostCategory,
        accountCode: String(r["account_code"]),
        active: r["active"] === true || r["active"] === "t",
      }));
    });
  }

  async saveCostCode(tenant: string, record: CostCodeRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO cost_code (tenant_id, code, name, category, account_code, active)
         VALUES ($1,$2,$3,$4,$5,$6)
         ON CONFLICT (tenant_id, code) DO UPDATE SET
           name=EXCLUDED.name, category=EXCLUDED.category,
           account_code=EXCLUDED.account_code, active=EXCLUDED.active`,
        [tenant, record.code, record.name, record.category, record.accountCode, record.active],
      );
    });
  }

  private jobFromRow(r: Record<string, unknown>): JobRecord {
    return {
      id: String(r["id"]),
      customerId: String(r["customer_id"]),
      name: String(r["name"]),
      status: String(r["status"]) as JobStatus,
      billingMethod: String(r["billing_method"]) as BillingMethod,
      costMethod: String(r["cost_method"]) as CostMethod,
      startDate: String(r["start_date"] ?? ""),
      endDate: String(r["end_date"] ?? ""),
      contractMinor: String(r["contract_minor"] ?? "0"),
      retainagePpm: Number(r["retainage_ppm"] ?? 0),
      revenueAccountCode: String(r["revenue_account_code"] ?? "4100"),
      memo: String(r["memo"] ?? ""),
    };
  }

  async listJobs(tenant: string): Promise<JobRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM job WHERE tenant_id=$1 ORDER BY name", [tenant]);
      return res.rows.map((r) => this.jobFromRow(r));
    });
  }

  async getJob(tenant: string, id: string): Promise<JobRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM job WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      return row ? this.jobFromRow(row) : undefined;
    });
  }

  async saveJob(tenant: string, record: JobRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO job (tenant_id, id, customer_id, name, status, billing_method,
           cost_method, start_date, end_date, contract_minor, retainage_ppm,
           revenue_account_code, memo)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           customer_id=EXCLUDED.customer_id, name=EXCLUDED.name, status=EXCLUDED.status,
           billing_method=EXCLUDED.billing_method, cost_method=EXCLUDED.cost_method,
           start_date=EXCLUDED.start_date, end_date=EXCLUDED.end_date,
           contract_minor=EXCLUDED.contract_minor, retainage_ppm=EXCLUDED.retainage_ppm,
           revenue_account_code=EXCLUDED.revenue_account_code, memo=EXCLUDED.memo`,
        [
          tenant, record.id, record.customerId, record.name, record.status,
          record.billingMethod, record.costMethod, record.startDate, record.endDate,
          record.contractMinor, record.retainagePpm, record.revenueAccountCode, record.memo,
        ],
      );
    });
  }

  async listBudget(tenant: string, jobId: string): Promise<JobBudgetLine[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM job_budget WHERE tenant_id=$1 AND job_id=$2 ORDER BY cost_code",
        [tenant, jobId],
      );
      return res.rows.map((r) => ({
        costCode: String(r["cost_code"]),
        budgetCostMinor: String(r["budget_cost_minor"] ?? "0"),
        revisedCostMinor: String(r["revised_cost_minor"] ?? "0"),
        budgetRevenueMinor: String(r["budget_revenue_minor"] ?? "0"),
      }));
    });
  }

  async saveBudget(
    tenant: string, jobId: string, lines: readonly JobBudgetLine[],
  ): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query("DELETE FROM job_budget WHERE tenant_id=$1 AND job_id=$2", [tenant, jobId]);
      for (const l of lines) {
        await db.query(
          `INSERT INTO job_budget (tenant_id, job_id, cost_code, budget_cost_minor,
             revised_cost_minor, budget_revenue_minor)
           VALUES ($1,$2,$3,$4,$5,$6)`,
          [
            tenant, jobId, l.costCode, l.budgetCostMinor,
            l.revisedCostMinor, l.budgetRevenueMinor,
          ],
        );
      }
    });
  }
}

// --- the flow ----------------------------------------------------------------

export interface JobContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
}

/**
 * The accounts job costing needs, created on demand.
 *
 * A tenant seeded from the contractor or professional-services template already
 * has these. One seeded from the general chart — or migrated from QuickBooks —
 * does not, and refusing to run job costing until somebody hand-builds five
 * accounts is a bad first experience. Adding an account is additive and
 * reversible; the accounts are only ever used if a job actually posts to them.
 */
export const JOB_ACCOUNTS: ReadonlyArray<{
  code: string; name: string; subtype: AccountSubtype;
}> = [
  { code: "1250", name: "Costs in Excess of Billings", subtype: AccountSubtype.OTHER_CURRENT_ASSET },
  { code: "1260", name: "Retainage Receivable", subtype: AccountSubtype.OTHER_CURRENT_ASSET },
  { code: "1270", name: "Work in Progress", subtype: AccountSubtype.OTHER_CURRENT_ASSET },
  { code: "2400", name: "Customer Deposits", subtype: AccountSubtype.OTHER_CURRENT_LIABILITY },
  { code: "2450", name: "Billings in Excess of Costs", subtype: AccountSubtype.OTHER_CURRENT_LIABILITY },
  { code: "5500", name: "Job Labor", subtype: AccountSubtype.COST_OF_GOODS_SOLD },
];

export async function ensureJobAccounts(ctx: JobContext): Promise<string[]> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const added: string[] = [];
  for (const spec of JOB_ACCOUNTS) {
    if (chart.getByCode(spec.code)) continue;
    const account: Account = {
      id: asAccountId(`acct:${spec.code}`),
      code: spec.code,
      name: spec.name,
      type: accountTypeOfSubtype(spec.subtype),
      currency: ctx.currency,
      subtype: spec.subtype,
      active: true,
    };
    await ctx.backend.saveAccount(ctx.tenant, account);
    added.push(spec.code);
  }
  return added;
}

/**
 * The starter cost-code structure. Five codes, because a contractor who has
 * never job-costed will not invent a breakdown and a system that demands one
 * before the first job gets abandoned. They're editable, and a trade that wants
 * CSI divisions or phase codes just adds them.
 */
export const DEFAULT_COST_CODES: readonly CostCodeRecord[] = [
  { code: "LAB", name: "Labor", category: "LABOR", accountCode: "5500", active: true },
  { code: "MAT", name: "Materials", category: "MATERIAL", accountCode: "5100", active: true },
  { code: "SUB", name: "Subcontractors", category: "SUBCONTRACT", accountCode: "5200", active: true },
  { code: "EQP", name: "Equipment", category: "EQUIPMENT", accountCode: "5300", active: true },
  { code: "OTH", name: "Other job costs", category: "OTHER", accountCode: "5000", active: true },
];

/** Seed the default cost codes, skipping any the tenant already defined. */
export async function seedCostCodes(ctx: JobContext): Promise<CostCodeRecord[]> {
  const store = ctx.backend.jobs();
  const chart = await ctx.backend.chart(ctx.tenant);
  const existing = new Set((await store.listCostCodes(String(ctx.tenant))).map((c) => c.code));
  const seeded: CostCodeRecord[] = [];
  for (const spec of DEFAULT_COST_CODES) {
    if (existing.has(spec.code)) continue;
    // A chart without 5300 shouldn't block the other four; fall back to the
    // generic cost-of-services account rather than pointing at nothing.
    const accountCode = chart.getByCode(spec.accountCode) ? spec.accountCode : "5000";
    if (!chart.getByCode(accountCode)) continue;
    const record = { ...spec, accountCode };
    await store.saveCostCode(String(ctx.tenant), record);
    seeded.push(record);
  }
  return seeded;
}

function requireDate(raw: unknown, label: string): string {
  const date = String(raw ?? "").trim();
  if (date && !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new JobError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

function requireMinor(raw: unknown, label: string, allowZero = true): string {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return "0";
  if (!/^-?\d+$/.test(value)) {
    throw new JobError(`${label} must be a whole number of minor units`);
  }
  if (value.startsWith("-")) throw new JobError(`${label} cannot be negative`);
  if (!allowZero && value === "0") throw new JobError(`${label} must be more than zero`);
  return value;
}

export function slug(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48);
}

export interface CostCodeInput {
  readonly code?: string;
  readonly name?: string;
  readonly category?: string;
  readonly account_code?: string;
  readonly active?: boolean;
}

export async function saveCostCode(
  ctx: JobContext, input: CostCodeInput,
): Promise<CostCodeRecord> {
  const code = String(input.code ?? "").trim().toUpperCase();
  if (!code) throw new JobError("a cost code needs a code");
  if (!/^[A-Z0-9][A-Z0-9._-]*$/.test(code)) {
    throw new JobError("a cost code may only contain letters, digits, dot, dash and underscore");
  }
  const name = String(input.name ?? "").trim();
  if (!name) throw new JobError("a cost code needs a name");
  const category = String(input.category ?? "OTHER").trim().toUpperCase() as CostCategory;
  if (!CATEGORIES.includes(category)) {
    throw new JobError(`category must be one of ${CATEGORIES.join(", ")}`);
  }
  const accountCode = String(input.account_code ?? "").trim();
  const chart = await ctx.backend.chart(ctx.tenant);
  const account = chart.getByCode(accountCode);
  if (!account) throw new JobError(`unknown account code ${accountCode || "(none)"}`);
  if (account.type !== "EXPENSE") {
    throw new JobError(
      `${accountCode} is a ${account.type.toLowerCase()} account — job cost has to land in an expense or cost-of-sales account`,
    );
  }
  const record: CostCodeRecord = {
    code, name, category, accountCode, active: input.active !== false,
  };
  await ctx.backend.jobs().saveCostCode(String(ctx.tenant), record);
  return record;
}

export interface JobInput {
  readonly id?: string;
  readonly customer_id?: string;
  readonly name?: string;
  readonly status?: string;
  readonly billing_method?: string;
  readonly cost_method?: string;
  readonly start_date?: string;
  readonly end_date?: string;
  readonly contract_minor?: string | number;
  readonly retainage_ppm?: number | string;
  readonly revenue_account_code?: string;
  readonly memo?: string;
}

export async function saveJob(ctx: JobContext, input: JobInput): Promise<JobRecord> {
  const name = String(input.name ?? "").trim();
  if (!name) throw new JobError("a job needs a name");

  const customerId = String(input.customer_id ?? "").trim();
  if (!customerId) throw new JobError("a job belongs to a customer");
  const customer = await ctx.backend.documents()
    .getParty(String(ctx.tenant), "customer", customerId);
  if (!customer) throw new JobError(`unknown customer ${customerId}`);

  const status = String(input.status ?? "ACTIVE").trim().toUpperCase() as JobStatus;
  if (!STATUSES.includes(status)) {
    throw new JobError(`status must be one of ${STATUSES.join(", ")}`);
  }
  const billingMethod = String(input.billing_method ?? "TIME_AND_MATERIALS")
    .trim().toUpperCase() as BillingMethod;
  if (!BILLING_METHODS.includes(billingMethod)) {
    throw new JobError(`billing method must be one of ${BILLING_METHODS.join(", ")}`);
  }
  const costMethod = String(input.cost_method ?? "AS_INCURRED")
    .trim().toUpperCase() as CostMethod;
  if (!COST_METHODS.includes(costMethod)) {
    throw new JobError(`cost method must be one of ${COST_METHODS.join(", ")}`);
  }

  const startDate = requireDate(input.start_date, "start date");
  const endDate = requireDate(input.end_date, "end date");
  if (startDate && endDate && endDate < startDate) {
    throw new JobError("the end date is before the start date");
  }

  const retainagePpm = Number(input.retainage_ppm ?? 0);
  if (!Number.isInteger(retainagePpm) || retainagePpm < 0 || retainagePpm > 1_000_000) {
    throw new JobError("retainage must be between 0 and 1,000,000 parts per million");
  }

  const chart = await ctx.backend.chart(ctx.tenant);
  const revenueAccountCode = String(input.revenue_account_code ?? "").trim()
    || (chart.getByCode("4100") ? "4100" : "4000");
  const revenue = chart.getByCode(revenueAccountCode);
  if (!revenue) throw new JobError(`unknown revenue account ${revenueAccountCode}`);
  if (revenue.type !== "REVENUE") {
    throw new JobError(`${revenueAccountCode} is not a revenue account`);
  }

  // A fixed-price job with no contract value has nothing to bill against and no
  // percent complete to compute; T&M legitimately has none until the work is done.
  const contractMinor = requireMinor(input.contract_minor, "contract value");
  if (contractMinor === "0" && (billingMethod === "PROGRESS" || billingMethod === "FIXED_MILESTONES")) {
    throw new JobError(
      `a ${billingMethod === "PROGRESS" ? "progress-billed" : "fixed-price"} job needs a contract value to bill against`,
    );
  }

  const id = String(input.id ?? "").trim() || slug(name);
  if (!id) throw new JobError("a job needs a name");

  const record: JobRecord = {
    id, customerId, name, status, billingMethod, costMethod,
    startDate, endDate, contractMinor, retainagePpm, revenueAccountCode,
    memo: String(input.memo ?? "").trim(),
  };
  await ensureJobAccounts(ctx);
  await ctx.backend.jobs().saveJob(String(ctx.tenant), record);
  return record;
}

export function jobJson(j: JobRecord): Record<string, unknown> {
  return {
    id: j.id,
    customer_id: j.customerId,
    name: j.name,
    status: j.status,
    billing_method: j.billingMethod,
    cost_method: j.costMethod,
    start_date: j.startDate,
    end_date: j.endDate,
    contract_minor: j.contractMinor,
    retainage_ppm: j.retainagePpm,
    revenue_account_code: j.revenueAccountCode,
    memo: j.memo,
  };
}

export function costCodeJson(c: CostCodeRecord): Record<string, unknown> {
  return {
    code: c.code,
    name: c.name,
    category: c.category,
    account_code: c.accountCode,
    active: c.active,
  };
}

export interface BudgetInput {
  readonly lines?: ReadonlyArray<{
    readonly cost_code?: string;
    readonly budget_cost_minor?: string | number;
    readonly revised_cost_minor?: string | number;
    readonly budget_revenue_minor?: string | number;
  }>;
}

/**
 * Write a job's budget.
 *
 * `revised_cost_minor` defaults to the original when it isn't supplied, so a
 * first budget needs one number per line. Revising later changes only the
 * revised figure — the original stays put, because "we are $40k over what we
 * bid" is the sentence the whole structure exists to make sayable.
 */
export async function saveJobBudget(
  ctx: JobContext, jobId: string, input: BudgetInput,
): Promise<JobBudgetLine[]> {
  const store = ctx.backend.jobs();
  const job = await store.getJob(String(ctx.tenant), jobId);
  if (!job) throw new JobError(`unknown job ${jobId}`);

  const codes = new Set((await store.listCostCodes(String(ctx.tenant))).map((c) => c.code));
  const existing = new Map(
    (await store.listBudget(String(ctx.tenant), jobId)).map((l) => [l.costCode, l]),
  );

  const seen = new Set<string>();
  const lines: JobBudgetLine[] = [];
  for (const raw of input.lines ?? []) {
    const costCode = String(raw.cost_code ?? "").trim().toUpperCase();
    if (!codes.has(costCode)) throw new JobError(`unknown cost code ${costCode || "(none)"}`);
    if (seen.has(costCode)) throw new JobError(`cost code ${costCode} appears twice`);
    seen.add(costCode);
    const budgetCostMinor = requireMinor(raw.budget_cost_minor, `${costCode} budget`);
    // An unsupplied revision keeps whatever the current estimate is — unless
    // it was never actually revised (it is still equal to the old bid), in
    // which case it follows the new bid. Correcting a typo in the budget
    // shouldn't leave a stale estimate behind that nobody typed.
    const prior = existing.get(costCode);
    const everRevised = prior !== undefined && prior.revisedCostMinor !== prior.budgetCostMinor;
    const revised = raw.revised_cost_minor === undefined || raw.revised_cost_minor === ""
      ? (everRevised ? prior.revisedCostMinor : budgetCostMinor)
      : requireMinor(raw.revised_cost_minor, `${costCode} revised estimate`);
    lines.push({
      costCode,
      budgetCostMinor,
      revisedCostMinor: revised,
      budgetRevenueMinor: requireMinor(raw.budget_revenue_minor, `${costCode} expected revenue`),
    });
  }
  lines.sort((a, b) => a.costCode.localeCompare(b.costCode));
  await store.saveBudget(String(ctx.tenant), jobId, lines);
  return lines;
}

// --- what the job actually cost ---------------------------------------------

export interface JobActuals {
  /** Cost by cost code (expense-type lines carrying this job). */
  readonly costByCode: ReadonlyMap<string, bigint>;
  /** Revenue recognized against the job. */
  readonly revenueMinor: bigint;
  /** Cost carried on the job but with no cost code — visible, never hidden. */
  readonly uncodedMinor: bigint;
}

/**
 * Read what a job has actually cost, from the journal.
 *
 * This walks posted entries rather than a maintained total, deliberately. A
 * cached job-cost column drifts the first time an entry is reversed, and a job
 * report that disagrees with the trial balance is worse than no job report: it
 * gets believed. The trade-off is a full scan, which is fine at small-business
 * volumes and is the same scan the general-ledger report already does.
 */
export async function jobActuals(
  ctx: JobContext, jobId: string, through?: string,
): Promise<JobActuals> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const entries = await ctx.backend.store(ctx.tenant).list(ctx.tenant);
  const costByCode = new Map<string, bigint>();
  let revenue = 0n;
  let uncoded = 0n;

  for (const entry of entries) {
    // Reversals count. A reversed cost is a cost that came back off the job,
    // and skipping them would leave the job report claiming money was spent
    // that the trial balance says was not.
    if (through && entry.entryDate > through) continue;
    for (const line of entry.lines) {
      if (line.dimensions?.[JOB_DIMENSION] !== jobId) continue;
      const account = chart.get(line.accountId);
      if (!account) continue;
      const signed = line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
      if (account.type === "EXPENSE") {
        const code = line.dimensions?.[COST_CODE_DIMENSION] ?? "";
        if (!code) uncoded += signed;
        else costByCode.set(code, (costByCode.get(code) ?? 0n) + signed);
      } else if (account.type === "REVENUE") {
        revenue += -signed;   // revenue is a credit balance
      }
    }
  }
  return { costByCode, revenueMinor: revenue, uncodedMinor: uncoded };
}

export interface JobCostRow {
  readonly cost_code: string;
  readonly name: string;
  readonly category: string;
  readonly budget_cost_minor: string;
  readonly revised_cost_minor: string;
  readonly actual_cost_minor: string;
  readonly committed_minor: string;
  /** Revised estimate less what has been spent and committed. */
  readonly remaining_minor: string;
  /** Actual against the revised estimate, in parts per million. */
  readonly percent_spent_ppm: number;
  readonly over_budget: boolean;
}

export interface JobCostReport {
  readonly contract: "job-cost/1";
  readonly currency: string;
  readonly job: Record<string, unknown>;
  readonly through: string;
  readonly rows: readonly JobCostRow[];
  readonly totals: Record<string, unknown>;
  /** Cost codes with spend and no budget line — usually the interesting ones. */
  readonly unbudgeted: readonly string[];
  /** Cost carried on the job with no cost code at all. */
  readonly uncoded_minor: string;
}

function ppm(part: bigint, whole: bigint): number {
  if (whole === 0n) return 0;
  return Number((part * 1_000_000n) / whole);
}

/**
 * Budget vs actual for one job, by cost code.
 *
 * `committed` — ordered on a purchase order but not yet billed — is supplied by
 * the caller rather than looked up here, so the report has no opinion about
 * whether purchasing exists yet. Committed cost is the number that stops a job
 * going over budget invisibly: the framing budget is not fine because only half
 * of it has been billed, if the rest is already on a signed PO.
 */
export async function jobCostReport(
  ctx: JobContext,
  jobId: string,
  options: { readonly through?: string; readonly committed?: ReadonlyMap<string, bigint> } = {},
): Promise<JobCostReport> {
  const store = ctx.backend.jobs();
  const job = await store.getJob(String(ctx.tenant), jobId);
  if (!job) throw new JobError(`unknown job ${jobId}`);

  const codes = new Map(
    (await store.listCostCodes(String(ctx.tenant))).map((c) => [c.code, c]),
  );
  const budget = await store.listBudget(String(ctx.tenant), jobId);
  const actuals = await jobActuals(ctx, jobId, options.through);
  const committed = options.committed ?? new Map<string, bigint>();

  const allCodes = new Set<string>([
    ...budget.map((b) => b.costCode),
    ...actuals.costByCode.keys(),
    ...committed.keys(),
  ]);

  const rows: JobCostRow[] = [];
  const unbudgeted: string[] = [];
  let totalBudget = 0n;
  let totalRevised = 0n;
  let totalActual = 0n;
  let totalCommitted = 0n;

  for (const code of [...allCodes].sort()) {
    const line = budget.find((b) => b.costCode === code);
    const actual = actuals.costByCode.get(code) ?? 0n;
    const promised = committed.get(code) ?? 0n;
    const budgetCost = BigInt(line?.budgetCostMinor ?? "0");
    const revised = BigInt(line?.revisedCostMinor ?? line?.budgetCostMinor ?? "0");
    if (!line && (actual !== 0n || promised !== 0n)) unbudgeted.push(code);
    totalBudget += budgetCost;
    totalRevised += revised;
    totalActual += actual;
    totalCommitted += promised;
    rows.push({
      cost_code: code,
      name: codes.get(code)?.name ?? code,
      category: codes.get(code)?.category ?? "OTHER",
      budget_cost_minor: budgetCost.toString(),
      revised_cost_minor: revised.toString(),
      actual_cost_minor: actual.toString(),
      committed_minor: promised.toString(),
      remaining_minor: (revised - actual - promised).toString(),
      percent_spent_ppm: ppm(actual, revised),
      over_budget: revised > 0n && actual + promised > revised,
    });
  }

  const contract = BigInt(job.contractMinor);
  return {
    contract: "job-cost/1",
    currency: ctx.currency.code,
    job: jobJson(job),
    through: options.through ?? "",
    rows,
    totals: {
      budget_cost_minor: totalBudget.toString(),
      revised_cost_minor: totalRevised.toString(),
      actual_cost_minor: (totalActual + actuals.uncodedMinor).toString(),
      committed_minor: totalCommitted.toString(),
      remaining_minor: (totalRevised - totalActual - actuals.uncodedMinor - totalCommitted).toString(),
      percent_spent_ppm: ppm(totalActual + actuals.uncodedMinor, totalRevised),
      contract_minor: job.contractMinor,
      revenue_minor: actuals.revenueMinor.toString(),
      // Gross margin as the books currently see it. On a percent-complete job
      // this is only meaningful after the WIP entry has run.
      margin_minor: (actuals.revenueMinor - totalActual - actuals.uncodedMinor).toString(),
      // What the job is expected to make if the current estimate holds.
      projected_margin_minor: (contract - totalRevised).toString(),
    },
    unbudgeted,
    uncoded_minor: actuals.uncodedMinor.toString(),
  };
}

/** Every job with the handful of figures that fit on one line. */
export async function jobList(ctx: JobContext): Promise<Record<string, unknown>> {
  const store = ctx.backend.jobs();
  const jobs = await store.listJobs(String(ctx.tenant));
  const rows: Record<string, unknown>[] = [];
  for (const job of jobs) {
    const actuals = await jobActuals(ctx, job.id);
    let cost = actuals.uncodedMinor;
    for (const v of actuals.costByCode.values()) cost += v;
    const budget = await store.listBudget(String(ctx.tenant), job.id);
    const revised = budget.reduce((acc, b) => acc + BigInt(b.revisedCostMinor), 0n);
    rows.push({
      ...jobJson(job),
      cost_to_date_minor: cost.toString(),
      revenue_to_date_minor: actuals.revenueMinor.toString(),
      revised_cost_minor: revised.toString(),
      percent_spent_ppm: ppm(cost, revised),
    });
  }
  return { contract: "job-list/1", currency: ctx.currency.code, jobs: rows };
}
