import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  computeTrialBalance,
  type Currency,
  type EntryId,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import {
  payrollRunToPostCommand,
  payrollTotals,
  PayrollError as EnginePayrollError,
  type EmployeePay,
  type PayrollAccounts,
  type PayrollRun,
} from "@rgnr8/subledger";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";

/**
 * Payroll — the part of the books a small business gets wrong most often.
 *
 * The tempting shortcut is to record the money that left the bank and stop
 * there. That understates what the business actually spent (the employer's own
 * taxes never appear) and hides what it still owes (the withholdings, which are
 * the employees' money held in trust until the deposit is made). A business run
 * that way looks solvent right up until the tax deposit is due.
 *
 * So a run here posts the full gross-to-net entry:
 *
 *   Dr  Wages Expense                 total gross
 *   Dr  Employer Payroll Tax Expense  the employer's own share
 *     Cr  Bank                          net pay actually leaving
 *     Cr  Payroll Liabilities           withholdings + employer taxes + deductions
 *
 * The liabilities sit on the balance sheet until a remittance clears them. That
 * is the whole difference between payroll as an accrual and payroll as a cash
 * shortcut, and it's why `remit` exists as a first-class action.
 *
 * Net pay is derived, not typed: `net = gross − employee taxes − deductions`.
 * The engine refuses a run where they disagree.
 */

export class PayrollServiceError extends Error {}

export interface EmployeeRecord {
  readonly id: string;
  readonly name: string;
  readonly active: boolean;
  /**
   * What an hour of this person costs the business, in minor units. Not their
   * wage — the wage plus payroll taxes, insurance and whatever else the burden
   * is, because a job costed at the bare wage is costed at roughly 70% of the
   * truth and every margin computed from it is wrong in the same direction.
   */
  readonly costRateMinor?: string;
  /** What an hour of their time bills at on a time-and-materials job. */
  readonly billRateMinor?: string;
}

export interface PayrollLineRecord {
  readonly employeeId: string;
  readonly grossMinor: string;
  readonly employeeTaxesMinor: string;
  readonly deductionsMinor: string;
  readonly netMinor: string;
}

export type PayrollStatus = "DRAFT" | "POSTED" | "VOID";

export interface PayrollRunRecord {
  readonly id: string;
  readonly date: string;
  readonly status: PayrollStatus;
  readonly employerTaxesMinor: string;
  readonly memo: string;
  readonly entryId: string;
  readonly bankCode: string;
  readonly lines: readonly PayrollLineRecord[];
  /**
   * Bumped every time the run is voided. It is woven into the posting
   * idempotency key so that a corrected re-run after a void is a genuinely
   * distinct event, and does not collide with the original key (which would
   * make the engine hand back the already-reversed entry and post nothing —
   * silently dropping a real payroll expense while the status reads POSTED).
   */
  readonly revision: number;
}

export interface PayrollStore {
  migrate(): Promise<void>;
  listEmployees(tenant: string): Promise<EmployeeRecord[]>;
  saveEmployee(tenant: string, employee: EmployeeRecord): Promise<void>;
  listRuns(tenant: string): Promise<PayrollRunRecord[]>;
  getRun(tenant: string, id: string): Promise<PayrollRunRecord | undefined>;
  saveRun(tenant: string, run: PayrollRunRecord): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

const key = (tenant: string, id: string): string => `${tenant} ${id}`;

export class InMemoryPayrollStore implements PayrollStore {
  private readonly employees = new Map<string, EmployeeRecord>();
  private readonly runs = new Map<string, PayrollRunRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listEmployees(tenant: string): Promise<EmployeeRecord[]> {
    const prefix = `${tenant} `;
    const out: EmployeeRecord[] = [];
    for (const [k, v] of this.employees) if (k.startsWith(prefix)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.name.localeCompare(b.name)));
  }

  saveEmployee(tenant: string, employee: EmployeeRecord): Promise<void> {
    this.employees.set(key(tenant, employee.id), employee);
    return Promise.resolve();
  }

  listRuns(tenant: string): Promise<PayrollRunRecord[]> {
    const prefix = `${tenant} `;
    const out: PayrollRunRecord[] = [];
    for (const [k, v] of this.runs) if (k.startsWith(prefix)) out.push(v);
    return Promise.resolve(
      out.sort((a, b) => (a.date === b.date ? a.id.localeCompare(b.id) : b.date.localeCompare(a.date))),
    );
  }

  getRun(tenant: string, id: string): Promise<PayrollRunRecord | undefined> {
    return Promise.resolve(this.runs.get(key(tenant, id)));
  }

  saveRun(tenant: string, run: PayrollRunRecord): Promise<void> {
    this.runs.set(key(tenant, run.id), run);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const PAYROLL_DDL = `
CREATE TABLE IF NOT EXISTS payroll_employee (
  tenant_id text NOT NULL,
  id        text NOT NULL,
  name      text NOT NULL,
  active    boolean NOT NULL DEFAULT true,
  CONSTRAINT payroll_employee_pk PRIMARY KEY (tenant_id, id)
);

-- Added after the first deploy: CREATE TABLE IF NOT EXISTS would skip them.
ALTER TABLE payroll_employee ADD COLUMN IF NOT EXISTS cost_rate_minor text NOT NULL DEFAULT '0';
ALTER TABLE payroll_employee ADD COLUMN IF NOT EXISTS bill_rate_minor text NOT NULL DEFAULT '0';

CREATE TABLE IF NOT EXISTS payroll_run (
  tenant_id             text NOT NULL,
  id                    text NOT NULL,
  run_date              text NOT NULL,
  status                text NOT NULL DEFAULT 'DRAFT',
  employer_taxes_minor  text NOT NULL DEFAULT '0',
  memo                  text NOT NULL DEFAULT '',
  entry_id              text NOT NULL DEFAULT '',
  bank_code             text NOT NULL DEFAULT '',
  revision              integer NOT NULL DEFAULT 0,
  CONSTRAINT payroll_run_pk PRIMARY KEY (tenant_id, id)
);
ALTER TABLE payroll_run ADD COLUMN IF NOT EXISTS revision integer NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS payroll_run_line (
  tenant_id            text NOT NULL,
  run_id               text NOT NULL,
  line_no              integer NOT NULL,
  employee_id          text NOT NULL,
  gross_minor          text NOT NULL,
  employee_taxes_minor text NOT NULL,
  deductions_minor     text NOT NULL,
  net_minor            text NOT NULL,
  CONSTRAINT payroll_run_line_pk PRIMARY KEY (tenant_id, run_id, line_no)
);
`;

export class PgPayrollStore implements PayrollStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(PAYROLL_DDL);
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

  async listEmployees(tenant: string): Promise<EmployeeRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT id, name, active, cost_rate_minor, bill_rate_minor
         FROM payroll_employee WHERE tenant_id=$1 ORDER BY name`, [tenant],
      );
      return res.rows.map((r) => ({
        id: String(r["id"]),
        name: String(r["name"]),
        active: r["active"] === true || r["active"] === "t",
        costRateMinor: String(r["cost_rate_minor"] ?? "0"),
        billRateMinor: String(r["bill_rate_minor"] ?? "0"),
      }));
    });
  }

  async saveEmployee(tenant: string, employee: EmployeeRecord): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO payroll_employee (tenant_id, id, name, active,
           cost_rate_minor, bill_rate_minor)
         VALUES ($1,$2,$3,$4,$5,$6)
         ON CONFLICT (tenant_id, id) DO UPDATE SET name=EXCLUDED.name,
           active=EXCLUDED.active, cost_rate_minor=EXCLUDED.cost_rate_minor,
           bill_rate_minor=EXCLUDED.bill_rate_minor`,
        [
          tenant, employee.id, employee.name, employee.active,
          employee.costRateMinor ?? "0", employee.billRateMinor ?? "0",
        ],
      ),
    );
  }

  private async linesFor(db: Queryable, tenant: string, runId: string): Promise<PayrollLineRecord[]> {
    const res = await db.query(
      `SELECT employee_id, gross_minor, employee_taxes_minor, deductions_minor, net_minor
       FROM payroll_run_line WHERE tenant_id=$1 AND run_id=$2 ORDER BY line_no`,
      [tenant, runId],
    );
    return res.rows.map((r) => ({
      employeeId: String(r["employee_id"]),
      grossMinor: String(r["gross_minor"]),
      employeeTaxesMinor: String(r["employee_taxes_minor"]),
      deductionsMinor: String(r["deductions_minor"]),
      netMinor: String(r["net_minor"]),
    }));
  }

  private runFromRow(r: Record<string, unknown>, lines: PayrollLineRecord[]): PayrollRunRecord {
    return {
      id: String(r["id"]),
      date: String(r["run_date"]),
      status: String(r["status"]) as PayrollStatus,
      employerTaxesMinor: String(r["employer_taxes_minor"]),
      memo: String(r["memo"] ?? ""),
      entryId: String(r["entry_id"] ?? ""),
      bankCode: String(r["bank_code"] ?? ""),
      revision: Number(r["revision"] ?? 0),
      lines,
    };
  }

  async listRuns(tenant: string): Promise<PayrollRunRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM payroll_run WHERE tenant_id=$1 ORDER BY run_date DESC, id", [tenant],
      );
      const out: PayrollRunRecord[] = [];
      for (const row of res.rows) {
        out.push(this.runFromRow(row, await this.linesFor(db, tenant, String(row["id"]))));
      }
      return out;
    });
  }

  async getRun(tenant: string, id: string): Promise<PayrollRunRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM payroll_run WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      return this.runFromRow(row, await this.linesFor(db, tenant, id));
    });
  }

  async saveRun(tenant: string, run: PayrollRunRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO payroll_run (tenant_id, id, run_date, status, employer_taxes_minor,
           memo, entry_id, bank_code, revision)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           run_date=EXCLUDED.run_date, status=EXCLUDED.status,
           employer_taxes_minor=EXCLUDED.employer_taxes_minor, memo=EXCLUDED.memo,
           entry_id=EXCLUDED.entry_id, bank_code=EXCLUDED.bank_code,
           revision=EXCLUDED.revision`,
        [tenant, run.id, run.date, run.status, run.employerTaxesMinor, run.memo,
         run.entryId, run.bankCode, run.revision],
      );
      await db.query(
        "DELETE FROM payroll_run_line WHERE tenant_id=$1 AND run_id=$2", [tenant, run.id],
      );
      let n = 0;
      for (const l of run.lines) {
        await db.query(
          `INSERT INTO payroll_run_line (tenant_id, run_id, line_no, employee_id,
             gross_minor, employee_taxes_minor, deductions_minor, net_minor)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
          [tenant, run.id, n, l.employeeId, l.grossMinor, l.employeeTaxesMinor,
           l.deductionsMinor, l.netMinor],
        );
        n += 1;
      }
    });
  }
}

// --- the flow ----------------------------------------------------------------

export interface PayrollContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

/**
 * Where payroll lands in the chart. Most small businesses keep a single Payroll
 * Liabilities account rather than splitting withholdings from employer taxes,
 * so all three liability legs default to it — separate lines, one account, which
 * nets to exactly what is owed.
 */
export interface PayrollCodes {
  readonly wagesExpense: string;
  readonly employerTaxExpense: string;
  readonly bank: string;
  readonly employeeTaxPayable: string;
  readonly employerTaxPayable: string;
  readonly deductionsPayable: string;
}

export const DEFAULT_PAYROLL_CODES: PayrollCodes = Object.freeze({
  wagesExpense: "6200",
  employerTaxExpense: "6210",
  bank: "1000",
  employeeTaxPayable: "2300",
  employerTaxPayable: "2300",
  deductionsPayable: "2300",
});

function minorOf(raw: unknown, label: string): bigint {
  const text = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!/^-?\d+$/.test(text)) {
    throw new PayrollServiceError(`${label} must be an integer minor-unit amount`);
  }
  const value = BigInt(text);
  if (value < 0n) throw new PayrollServiceError(`${label} cannot be negative`);
  return value;
}

function requireDate(raw: unknown, label = "date"): string {
  const date = String(raw ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new PayrollServiceError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

async function codeToId(
  ctx: PayrollContext, code: string, role: string,
): Promise<import("@rgnr8/ledger-kernel").AccountId> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const account = chart.getByCode(code);
  if (!account) throw new PayrollServiceError(`${role}: unknown account code ${code}`);
  return account.id;
}

async function resolveAccounts(
  ctx: PayrollContext, overrides: Partial<PayrollCodes> | undefined,
): Promise<{ accounts: PayrollAccounts; codes: PayrollCodes }> {
  const codes: PayrollCodes = { ...DEFAULT_PAYROLL_CODES, ...(overrides ?? {}) };
  const accounts: PayrollAccounts = {
    wagesExpense: await codeToId(ctx, codes.wagesExpense, "wages expense"),
    employerTaxExpense: await codeToId(ctx, codes.employerTaxExpense, "employer tax expense"),
    cash: await codeToId(ctx, codes.bank, "bank"),
    employeeTaxPayable: await codeToId(ctx, codes.employeeTaxPayable, "employee tax payable"),
    employerTaxPayable: await codeToId(ctx, codes.employerTaxPayable, "employer tax payable"),
    deductionsPayable: await codeToId(ctx, codes.deductionsPayable, "deductions payable"),
  };
  return { accounts, codes };
}

export interface EmployeeInput {
  readonly id?: string;
  readonly name?: string;
  readonly active?: boolean;
  readonly cost_rate_minor?: string | number;
  readonly bill_rate_minor?: string | number;
}

function slug(text: string): string {
  return text.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
}

export async function saveEmployee(
  ctx: PayrollContext, input: EmployeeInput,
): Promise<EmployeeRecord> {
  const name = String(input.name ?? "").trim();
  if (!name) throw new PayrollServiceError("an employee needs a name");
  const id = String(input.id ?? "").trim() || slug(name);
  if (!id) throw new PayrollServiceError("an employee needs an id");
  const rate = (raw: unknown, label: string): string => {
    const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
    if (!value) return "0";
    if (!/^\d+$/.test(value)) {
      throw new PayrollServiceError(`${label} must be a whole number of minor units per hour`);
    }
    return value;
  };
  const employee: EmployeeRecord = {
    id,
    name,
    active: input.active !== false,
    costRateMinor: rate(input.cost_rate_minor, "cost rate"),
    billRateMinor: rate(input.bill_rate_minor, "bill rate"),
  };
  await ctx.backend.payroll().saveEmployee(String(ctx.tenant), employee);
  return employee;
}

export interface PayrollLineInput {
  readonly employee_id?: string;
  readonly gross_minor?: string | number;
  readonly employee_taxes_minor?: string | number;
  readonly deductions_minor?: string | number;
}

export interface CreateRunRequest {
  readonly id?: string;
  readonly date?: string;
  readonly memo?: string;
  readonly employer_taxes_minor?: string | number;
  readonly bank_code?: string;
  readonly lines?: readonly PayrollLineInput[];
}

/**
 * Draft a run. Net pay is computed here rather than asked for — an owner types
 * gross, withholdings and deductions, and arithmetic is not their job.
 */
export async function createRun(
  ctx: PayrollContext, req: CreateRunRequest,
): Promise<PayrollRunRecord> {
  const date = requireDate(req.date, "pay date");
  const rawLines = Array.isArray(req.lines) ? req.lines : [];
  if (rawLines.length === 0) throw new PayrollServiceError("a payroll run needs at least one employee");

  const store = ctx.backend.payroll();
  const known = new Set((await store.listEmployees(String(ctx.tenant))).map((e) => e.id));
  const seen = new Set<string>();
  const lines: PayrollLineRecord[] = [];
  for (const l of rawLines) {
    const employeeId = String(l.employee_id ?? "").trim();
    if (!employeeId) throw new PayrollServiceError("each line needs an employee");
    if (!known.has(employeeId)) throw new PayrollServiceError(`unknown employee ${employeeId}`);
    if (seen.has(employeeId)) {
      throw new PayrollServiceError(`${employeeId} appears twice in the same run`);
    }
    seen.add(employeeId);
    const gross = minorOf(l.gross_minor ?? "0", `${employeeId}: gross`);
    const taxes = minorOf(l.employee_taxes_minor ?? "0", `${employeeId}: withholdings`);
    const deductions = minorOf(l.deductions_minor ?? "0", `${employeeId}: deductions`);
    if (gross === 0n) throw new PayrollServiceError(`${employeeId}: gross pay cannot be zero`);
    const net = gross - taxes - deductions;
    if (net < 0n) {
      throw new PayrollServiceError(
        `${employeeId}: withholdings and deductions come to more than gross pay`,
      );
    }
    lines.push({
      employeeId,
      grossMinor: gross.toString(),
      employeeTaxesMinor: taxes.toString(),
      deductionsMinor: deductions.toString(),
      netMinor: net.toString(),
    });
  }

  const id = String(req.id ?? "").trim() || `PR-${date}`;
  const existing = await store.getRun(String(ctx.tenant), id);
  if (existing && existing.status === "POSTED") {
    throw new PayrollServiceError(`payroll run ${id} is already posted`);
  }
  const run: PayrollRunRecord = {
    id,
    date,
    status: "DRAFT",
    employerTaxesMinor: minorOf(req.employer_taxes_minor ?? "0", "employer taxes").toString(),
    memo: String(req.memo ?? "").trim(),
    entryId: "",
    bankCode: String(req.bank_code ?? "").trim() || DEFAULT_PAYROLL_CODES.bank,
    lines,
    // Re-entering a run after it was voided keeps the bumped revision, so its
    // fresh post cannot collide with the reversed original's idempotency key.
    revision: existing?.revision ?? 0,
  };
  await store.saveRun(String(ctx.tenant), run);
  return run;
}

export interface RunTotals {
  readonly gross_minor: string;
  readonly employee_taxes_minor: string;
  readonly deductions_minor: string;
  readonly net_minor: string;
  readonly employer_taxes_minor: string;
  /** What the run costs the business: gross plus the employer's own taxes. */
  readonly total_cost_minor: string;
  /** What is owed to the tax authorities and benefit providers afterwards. */
  readonly liability_minor: string;
}

export function totalsOf(run: PayrollRunRecord): RunTotals {
  let gross = 0n;
  let taxes = 0n;
  let deductions = 0n;
  let net = 0n;
  for (const l of run.lines) {
    gross += BigInt(l.grossMinor);
    taxes += BigInt(l.employeeTaxesMinor);
    deductions += BigInt(l.deductionsMinor);
    net += BigInt(l.netMinor);
  }
  const employer = BigInt(run.employerTaxesMinor);
  return {
    gross_minor: gross.toString(),
    employee_taxes_minor: taxes.toString(),
    deductions_minor: deductions.toString(),
    net_minor: net.toString(),
    employer_taxes_minor: employer.toString(),
    total_cost_minor: (gross + employer).toString(),
    liability_minor: (taxes + deductions + employer).toString(),
  };
}

export function runJson(run: PayrollRunRecord): Record<string, unknown> {
  return {
    id: run.id,
    date: run.date,
    status: run.status,
    memo: run.memo,
    entry_id: run.entryId,
    bank_code: run.bankCode,
    lines: run.lines.map((l) => ({
      employee_id: l.employeeId,
      gross_minor: l.grossMinor,
      employee_taxes_minor: l.employeeTaxesMinor,
      deductions_minor: l.deductionsMinor,
      net_minor: l.netMinor,
    })),
    totals: totalsOf(run),
  };
}

function provenanceFor(date: string, at: string): Provenance {
  return {
    sourceSystem: "payroll",
    sourceObject: "payroll.run",
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/payroll-1",
    mappingVersion: "ledger-service/payroll-1",
  };
}

function toEngineRun(run: PayrollRunRecord, currency: Currency): PayrollRun {
  const employees: EmployeePay[] = run.lines.map((l) => ({
    employeeId: l.employeeId,
    grossPay: Money.fromMinorUnits(BigInt(l.grossMinor), currency),
    employeeTaxes: Money.fromMinorUnits(BigInt(l.employeeTaxesMinor), currency),
    deductions: Money.fromMinorUnits(BigInt(l.deductionsMinor), currency),
    netPay: Money.fromMinorUnits(BigInt(l.netMinor), currency),
  }));
  return {
    // Revision 0 keeps the original `payroll:<id>` key; a re-run after a void
    // (revision ≥ 1) posts under `payroll:<id>~r<n>`, a distinct entry.
    id: run.revision > 0 ? `${run.id}~r${run.revision}` : run.id,
    date: run.date,
    employees,
    employerTaxes: Money.fromMinorUnits(BigInt(run.employerTaxesMinor), currency),
  };
}

export interface PostRunResult {
  readonly run: Record<string, unknown>;
  readonly entry_id: string;
}

/** Post the full gross-to-net journal for a drafted run. */
export async function postRun(
  ctx: PayrollContext, id: string, overrides?: Partial<PayrollCodes>,
): Promise<PostRunResult> {
  const store = ctx.backend.payroll();
  const run = await store.getRun(String(ctx.tenant), id);
  if (!run) throw new PayrollServiceError(`unknown payroll run ${id}`);
  if (run.status === "POSTED") throw new PayrollServiceError(`${id} is already posted`);
  if (run.status === "VOID") throw new PayrollServiceError(`${id} was voided`);

  const { accounts } = await resolveAccounts(ctx, { bank: run.bankCode, ...(overrides ?? {}) });
  const chart = await ctx.backend.chart(ctx.tenant);
  const engineRun = toEngineRun(run, ctx.currency);

  let command;
  try {
    // The engine re-derives the totals and refuses a run where net pay and the
    // gross-less-withholdings arithmetic disagree.
    payrollTotals(engineRun, ctx.currency);
    command = payrollRunToPostCommand(engineRun, accounts, {
      tenantId: String(ctx.tenant),
      currency: ctx.currency,
      provenance: provenanceFor(run.date, ctx.now()),
    });
  } catch (err) {
    if (err instanceof EnginePayrollError) throw new PayrollServiceError(err.message);
    throw err;
  }

  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  const entry = await engine.post(command, { postedAt: ctx.now() });
  const posted: PayrollRunRecord = { ...run, status: "POSTED", entryId: String(entry.id) };
  await store.saveRun(String(ctx.tenant), posted);
  return { run: runJson(posted), entry_id: String(entry.id) };
}

/** Void a posted run by reversing its entry. The original is never deleted. */
export async function voidRun(ctx: PayrollContext, id: string): Promise<Record<string, unknown>> {
  const store = ctx.backend.payroll();
  const run = await store.getRun(String(ctx.tenant), id);
  if (!run) throw new PayrollServiceError(`unknown payroll run ${id}`);
  if (run.status !== "POSTED") throw new PayrollServiceError(`${id} is not posted`);

  const chart = await ctx.backend.chart(ctx.tenant);
  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  await engine.reverse(ctx.tenant, run.entryId as EntryId, {
    // Key the reversal by the revision being reversed, so voiding a re-run
    // (a later revision) does not collide with the first void.
    idempotencyKey: asIdempotencyKey(`payroll-void:${run.id}~r${run.revision}`),
    periodKey: asPeriodKey(run.date.slice(0, 7)),
    entryDate: run.date,
    postedAt: ctx.now(),
    provenance: provenanceFor(run.date, ctx.now()),
    memo: `Void of payroll ${run.id}`,
  });
  const voided: PayrollRunRecord = { ...run, status: "VOID", revision: run.revision + 1 };
  await store.saveRun(String(ctx.tenant), voided);
  return runJson(voided);
}

// --- what is still owed ------------------------------------------------------

export interface LiabilityView {
  readonly account_code: string;
  readonly account_name: string;
  /** Positive = the business still owes this much. */
  readonly owed_minor: string;
  readonly posted_runs: number;
  readonly draft_runs: number;
}

/**
 * What payroll still owes. This is the number that catches people out: the
 * withholdings are not the business's money, and this says so before the
 * deposit is due.
 */
export async function liabilityView(
  ctx: PayrollContext, code = DEFAULT_PAYROLL_CODES.employeeTaxPayable,
): Promise<LiabilityView> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const account = chart.getByCode(code);
  if (!account) throw new PayrollServiceError(`unknown account code ${code}`);

  const tb = await computeTrialBalance(
    ctx.backend.store(ctx.tenant), ctx.tenant, chart, ctx.currency,
  );
  const row = tb.rows.find((r) => r.code === code);
  // A liability is credit-balanced, so what's owed is credits less debits.
  const owed = row ? row.credit.minorUnits - row.debit.minorUnits : 0n;

  const runs = await ctx.backend.payroll().listRuns(String(ctx.tenant));
  return {
    account_code: code,
    account_name: account.name,
    owed_minor: owed.toString(),
    posted_runs: runs.filter((r) => r.status === "POSTED").length,
    draft_runs: runs.filter((r) => r.status === "DRAFT").length,
  };
}

export interface RemitRequest {
  readonly id?: string;
  readonly date?: string;
  readonly amount_minor?: string | number;
  readonly bank_code?: string;
  readonly liability_code?: string;
  readonly memo?: string;
}

/**
 * Pay the tax deposit or benefit remittance: debit the liability, credit the
 * bank. Paying more than is owed is refused — that is a typo, not a payment.
 */
export async function remit(
  ctx: PayrollContext, req: RemitRequest,
): Promise<Record<string, unknown>> {
  const date = requireDate(req.date, "payment date");
  const amount = minorOf(req.amount_minor, "amount");
  if (amount === 0n) throw new PayrollServiceError("a remittance must be positive");

  const liabilityCode = String(req.liability_code ?? "").trim()
    || DEFAULT_PAYROLL_CODES.employeeTaxPayable;
  const bankCode = String(req.bank_code ?? "").trim() || DEFAULT_PAYROLL_CODES.bank;

  const owed = BigInt((await liabilityView(ctx, liabilityCode)).owed_minor);
  if (amount > owed) {
    throw new PayrollServiceError(
      `payroll liabilities are ${(Number(owed) / 100).toFixed(2)} — a payment of `
      + `${(Number(amount) / 100).toFixed(2)} would overpay`,
    );
  }

  const chart = await ctx.backend.chart(ctx.tenant);
  const liability = await codeToId(ctx, liabilityCode, "payroll liability");
  const bank = await codeToId(ctx, bankCode, "bank");
  const money = Money.fromMinorUnits(amount, ctx.currency);
  const memo = String(req.memo ?? "").trim() || "Payroll tax deposit";

  // With an explicit id the remittance is idempotent — a retried request posts
  // once. Without one, two real remittances of the same amount against the same
  // liability on the same day (two separate deposits to the same agency) are
  // distinct events, so the default key carries a per-(date,liability) sequence
  // rather than collapsing the second into the first.
  const explicitId = String(req.id ?? "").trim();
  let remitKey = `payroll-remit:${explicitId}`;
  if (!explicitId) {
    const priorSameDay = (await ctx.backend.store(ctx.tenant).list(ctx.tenant)).filter(
      (e) => e.provenance.sourceSystem === "payroll"
        && e.provenance.sourceObject === "payroll.remit"
        && e.entryDate === date
        && e.lines.some((l) => l.accountId === liability),
    ).length;
    remitKey = `payroll-remit:${date}:${liabilityCode}#${priorSameDay}`;
  }

  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  const entry = await engine.post(
    {
      tenantId: ctx.tenant,
      idempotencyKey: asIdempotencyKey(remitKey),
      periodKey: asPeriodKey(date.slice(0, 7)),
      currency: ctx.currency,
      entryDate: date,
      memo,
      provenance: { ...provenanceFor(date, ctx.now()), sourceObject: "payroll.remit" },
      lines: [
        { accountId: liability, side: "DEBIT", amount: money },
        { accountId: bank, side: "CREDIT", amount: money },
      ],
    },
    { postedAt: ctx.now() },
  );
  return {
    entry_id: String(entry.id),
    date,
    amount_minor: amount.toString(),
    remaining_minor: (owed - amount).toString(),
  };
}
