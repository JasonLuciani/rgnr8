import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  type Currency,
  type Provenance,
  type PostCommand,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";

/**
 * Debt — loans, lines of credit and financed liabilities as first-class things
 * the software manages, not dumb balances the owner maintains by hand.
 *
 * The wedge: QuickBooks Online has no loan manager (the desktop-only "Loan
 * Manager" never came to the cloud) and Xero has declined the request for years,
 * so in both, every loan payment is a manual journal split into principal and
 * interest — a monthly chore most owners get wrong. Here a payment is split
 * automatically from the *actual outstanding balance*, which is both the correct
 * way a servicer computes interest and the thing that makes extra principal and
 * early payoff behave without special cases.
 *
 * Two numbers are kept apart deliberately:
 *  - The **amortization schedule** is a projection — what the payments *will* be
 *    if nothing changes. It is derived, never posted.
 *  - The **loan balance** is the ledger's own liability, moved only by a posted
 *    payment or draw. Interest each period is `balance × periodic rate`, so a
 *    payment that includes extra principal simply lowers next period's interest;
 *    no schedule row has to be "caught up".
 *
 * All money is integer minor units; the rate is carried in micro-units (×1e6, so
 * 6.5% is 65000) to match the FX convention already used in consolidation. The
 * amortization arithmetic is done in scaled bigint — there is no floating point
 * anywhere in this module.
 */

export class DebtError extends Error {}

/**
 * Amortizing kinds carry a term and a level payment; revolving kinds (a line of
 * credit, a card) have no fixed schedule — they are drawn and repaid, and
 * interest accrues on whatever is outstanding.
 */
export type LoanKind =
  | "TERM"
  | "SBA"
  | "MORTGAGE"
  | "EQUIPMENT"
  | "AUTO"
  | "OWNER_NOTE"
  | "MCA"
  | "INTERCOMPANY"
  | "LINE_OF_CREDIT"
  | "CREDIT_CARD";

const LOAN_KINDS: readonly LoanKind[] = [
  "TERM", "SBA", "MORTGAGE", "EQUIPMENT", "AUTO", "OWNER_NOTE",
  "MCA", "INTERCOMPANY", "LINE_OF_CREDIT", "CREDIT_CARD",
];

const REVOLVING: readonly LoanKind[] = ["LINE_OF_CREDIT", "CREDIT_CARD"];

export function isRevolving(kind: LoanKind): boolean {
  return REVOLVING.includes(kind);
}

export type PaymentFrequency = "MONTHLY" | "BIWEEKLY" | "WEEKLY" | "QUARTERLY" | "ANNUAL";

const PERIODS_PER_YEAR: Record<PaymentFrequency, number> = {
  MONTHLY: 12, BIWEEKLY: 26, WEEKLY: 52, QUARTERLY: 4, ANNUAL: 1,
};

const FREQUENCIES: readonly PaymentFrequency[] = [
  "MONTHLY", "BIWEEKLY", "WEEKLY", "QUARTERLY", "ANNUAL",
];

export interface LoanRecord {
  readonly id: string;
  readonly lender: string;
  readonly kind: LoanKind;
  /** Original amount borrowed, minor units. */
  readonly originalPrincipalMinor: string;
  /** What is still owed today, minor units — the ledger liability. */
  readonly currentPrincipalMinor: string;
  /** Annual interest rate × 1e6 (6.5% → 65000). */
  readonly annualRateMicro: string;
  readonly startDate: string;
  /** Number of scheduled payments; 0 for a revolving line. */
  readonly termPeriods: number;
  readonly frequency: PaymentFrequency;
  /** The level payment, minor units. 0 = derive it from principal/rate/term. */
  readonly paymentMinor: string;
  /** The balance-sheet liability account this loan lives in. */
  readonly liabilityAccountCode: string;
  /** Where interest expense is booked. */
  readonly interestAccountCode: string;
  /** Optional lender covenant: minimum debt-service-coverage ratio × 1e6. 0 = none. */
  readonly minDscrMicro: string;
  readonly active: boolean;
  readonly memo: string;
}

export interface LoanPaymentRecord {
  readonly id: string;
  readonly loanId: string;
  readonly date: string;
  readonly principalMinor: string;
  readonly interestMinor: string;
  /** A draw on a revolving line increases the balance; recorded as a negative principal move. */
  readonly kind: "PAYMENT" | "DRAW";
  readonly entryId: string;
  readonly memo: string;
}

export interface ScheduleRow {
  readonly period: number;
  readonly dueDate: string;
  readonly paymentMinor: string;
  readonly principalMinor: string;
  readonly interestMinor: string;
  readonly balanceMinor: string;
}

// --- the amortization arithmetic (pure, scaled-integer) ----------------------

const SCALE = 1_000_000_000_000n; // 1e12 — the fixed-point scale for rate factors

/** (base/SCALE)^n, returned still scaled by SCALE. base is a value already ×SCALE. */
function powScaled(base: bigint, n: number): bigint {
  let acc = SCALE; // 1.0
  for (let i = 0; i < n; i++) acc = (acc * base) / SCALE;
  return acc;
}

function roundDiv(num: bigint, den: bigint): bigint {
  if (den === 0n) return 0n;
  return (num + den / 2n) / den;
}

/** The periodic rate, scaled by SCALE, from an annual micro-rate and a frequency. */
export function periodicRateScaled(annualRateMicro: bigint, freq: PaymentFrequency): bigint {
  // r = (annualRateMicro / 1e6) / periodsPerYear, then ×SCALE.
  return (annualRateMicro * SCALE) / (1_000_000n * BigInt(PERIODS_PER_YEAR[freq]));
}

/**
 * The level payment that amortizes `principal` over `n` periods at periodic rate
 * `rScaled` (scaled by SCALE). Standard annuity formula, done in bigint:
 *   payment = P·r / (1 − (1+r)^−n)
 * Zero interest degrades to straight principal / n (rounded up so the schedule
 * cannot end owing a cent).
 */
export function levelPayment(principalMinor: bigint, rScaled: bigint, n: number): bigint {
  if (n <= 0) return principalMinor;
  if (rScaled === 0n) return (principalMinor + BigInt(n) - 1n) / BigInt(n); // ceil
  const onePlusR = SCALE + rScaled;
  const pow = powScaled(onePlusR, n);          // (1+r)^n × SCALE
  const denom = SCALE - (SCALE * SCALE) / pow; // (1 − (1+r)^−n) × SCALE
  return roundDiv(principalMinor * rScaled, denom);
}

/**
 * Build the full amortization schedule. Interest each period is the outstanding
 * balance times the periodic rate (rounded to the cent); principal is the rest
 * of the payment; the final period trues up so the balance lands on exactly zero
 * — the payment cannot leave rounding dust behind.
 */
export function amortizationSchedule(loan: LoanRecord): ScheduleRow[] {
  const n = loan.termPeriods;
  if (n <= 0) return []; // revolving — no fixed schedule
  const principal = BigInt(loan.originalPrincipalMinor);
  const rScaled = periodicRateScaled(BigInt(loan.annualRateMicro), loan.frequency);
  const payment = BigInt(loan.paymentMinor) > 0n
    ? BigInt(loan.paymentMinor)
    : levelPayment(principal, rScaled, n);

  const rows: ScheduleRow[] = [];
  let balance = principal;
  for (let i = 1; i <= n; i++) {
    const interest = roundDiv(balance * rScaled, SCALE);
    let principalPart = payment - interest;
    let thisPayment = payment;
    if (principalPart >= balance || i === n) {
      // final (or over-large) payment: retire the balance exactly
      principalPart = balance;
      thisPayment = balance + interest;
    }
    balance -= principalPart;
    rows.push({
      period: i,
      dueDate: addPeriods(loan.startDate, loan.frequency, i),
      paymentMinor: thisPayment.toString(),
      principalMinor: principalPart.toString(),
      interestMinor: interest.toString(),
      balanceMinor: balance.toString(),
    });
    if (balance <= 0n) break;
  }
  return rows;
}

// --- date arithmetic (UTC, pure) ---------------------------------------------

function addPeriods(startDate: string, freq: PaymentFrequency, i: number): string {
  const [y, m, d] = startDate.split("-").map((p) => Number.parseInt(p, 10));
  if (!y || !m || !d) throw new DebtError(`start date must be YYYY-MM-DD, got ${startDate}`);
  if (freq === "WEEKLY") return addDays(y, m, d, 7 * i);
  if (freq === "BIWEEKLY") return addDays(y, m, d, 14 * i);
  const months = freq === "QUARTERLY" ? 3 * i : freq === "ANNUAL" ? 12 * i : i;
  return addMonths(y, m, d, months);
}

function addDays(y: number, m: number, d: number, days: number): string {
  const base = Date.UTC(y, m - 1, d) + days * 86_400_000;
  return new Date(base).toISOString().slice(0, 10);
}

function addMonths(y: number, m: number, d: number, months: number): string {
  const total = (y * 12 + (m - 1)) + months;
  const ny = Math.floor(total / 12);
  const nm = total % 12;
  // clamp the day to the target month's length (Jan 31 + 1mo → Feb 28/29)
  const lastDay = new Date(Date.UTC(ny, nm + 1, 0)).getUTCDate();
  const nd = Math.min(d, lastDay);
  return new Date(Date.UTC(ny, nm, nd)).toISOString().slice(0, 10);
}

// --- store seam --------------------------------------------------------------

export interface DebtStore {
  migrate(): Promise<void>;
  listLoans(tenant: string): Promise<LoanRecord[]>;
  getLoan(tenant: string, id: string): Promise<LoanRecord | undefined>;
  saveLoan(tenant: string, loan: LoanRecord): Promise<void>;
  listPayments(tenant: string, loanId?: string): Promise<LoanPaymentRecord[]>;
  getPayment(tenant: string, id: string): Promise<LoanPaymentRecord | undefined>;
  savePayment(tenant: string, payment: LoanPaymentRecord): Promise<void>;
}

export class InMemoryDebtStore implements DebtStore {
  private readonly loans = new Map<string, LoanRecord>();
  private readonly payments = new Map<string, LoanPaymentRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listLoans(tenant: string): Promise<LoanRecord[]> {
    const out: LoanRecord[] = [];
    for (const [k, v] of this.loans) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.id.localeCompare(b.id)));
  }

  getLoan(tenant: string, id: string): Promise<LoanRecord | undefined> {
    return Promise.resolve(this.loans.get(`${tenant}::${id}`));
  }

  saveLoan(tenant: string, loan: LoanRecord): Promise<void> {
    this.loans.set(`${tenant}::${loan.id}`, loan);
    return Promise.resolve();
  }

  listPayments(tenant: string, loanId?: string): Promise<LoanPaymentRecord[]> {
    const out: LoanPaymentRecord[] = [];
    for (const [k, v] of this.payments) {
      if (!k.startsWith(`${tenant}::`)) continue;
      if (loanId && v.loanId !== loanId) continue;
      out.push(v);
    }
    return Promise.resolve(out.sort((a, b) => (
      a.date === b.date ? a.id.localeCompare(b.id) : a.date.localeCompare(b.date)
    )));
  }

  getPayment(tenant: string, id: string): Promise<LoanPaymentRecord | undefined> {
    return Promise.resolve(this.payments.get(`${tenant}::${id}`));
  }

  savePayment(tenant: string, payment: LoanPaymentRecord): Promise<void> {
    this.payments.set(`${tenant}::${payment.id}`, payment);
    return Promise.resolve();
  }
}

export const DEBT_DDL = `
CREATE TABLE IF NOT EXISTS loan (
  tenant_id                text NOT NULL,
  id                       text NOT NULL,
  lender                   text NOT NULL,
  kind                     text NOT NULL DEFAULT 'TERM',
  original_principal_minor text NOT NULL DEFAULT '0',
  current_principal_minor  text NOT NULL DEFAULT '0',
  annual_rate_micro        text NOT NULL DEFAULT '0',
  start_date               text NOT NULL,
  term_periods             integer NOT NULL DEFAULT 0,
  frequency                text NOT NULL DEFAULT 'MONTHLY',
  payment_minor            text NOT NULL DEFAULT '0',
  liability_account_code   text NOT NULL DEFAULT '2700',
  interest_account_code    text NOT NULL DEFAULT '6950',
  min_dscr_micro           text NOT NULL DEFAULT '0',
  active                   boolean NOT NULL DEFAULT true,
  memo                     text NOT NULL DEFAULT '',
  CONSTRAINT loan_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS loan_payment (
  tenant_id       text NOT NULL,
  id              text NOT NULL,
  loan_id         text NOT NULL,
  pay_date        text NOT NULL,
  principal_minor text NOT NULL DEFAULT '0',
  interest_minor  text NOT NULL DEFAULT '0',
  kind            text NOT NULL DEFAULT 'PAYMENT',
  entry_id        text NOT NULL DEFAULT '',
  memo            text NOT NULL DEFAULT '',
  CONSTRAINT loan_payment_pk PRIMARY KEY (tenant_id, id)
);
`;

export class PgDebtStore implements DebtStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(DEBT_DDL);
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

  private loanFrom(r: Record<string, unknown>): LoanRecord {
    return {
      id: String(r["id"]),
      lender: String(r["lender"] ?? ""),
      kind: String(r["kind"] ?? "TERM") as LoanKind,
      originalPrincipalMinor: String(r["original_principal_minor"] ?? "0"),
      currentPrincipalMinor: String(r["current_principal_minor"] ?? "0"),
      annualRateMicro: String(r["annual_rate_micro"] ?? "0"),
      startDate: String(r["start_date"] ?? ""),
      termPeriods: Number(r["term_periods"] ?? 0),
      frequency: String(r["frequency"] ?? "MONTHLY") as PaymentFrequency,
      paymentMinor: String(r["payment_minor"] ?? "0"),
      liabilityAccountCode: String(r["liability_account_code"] ?? "2700"),
      interestAccountCode: String(r["interest_account_code"] ?? "6950"),
      minDscrMicro: String(r["min_dscr_micro"] ?? "0"),
      active: r["active"] !== false && r["active"] !== "f",
      memo: String(r["memo"] ?? ""),
    };
  }

  async listLoans(tenant: string): Promise<LoanRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM loan WHERE tenant_id=$1 ORDER BY id", [tenant]);
      return res.rows.map((r) => this.loanFrom(r));
    });
  }

  async getLoan(tenant: string, id: string): Promise<LoanRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM loan WHERE tenant_id=$1 AND id=$2", [tenant, id]);
      const r = res.rows[0];
      return r ? this.loanFrom(r) : undefined;
    });
  }

  async saveLoan(tenant: string, loan: LoanRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO loan (tenant_id, id, lender, kind, original_principal_minor,
         current_principal_minor, annual_rate_micro, start_date, term_periods,
         frequency, payment_minor, liability_account_code, interest_account_code,
         min_dscr_micro, active, memo)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         lender=EXCLUDED.lender, kind=EXCLUDED.kind,
         original_principal_minor=EXCLUDED.original_principal_minor,
         current_principal_minor=EXCLUDED.current_principal_minor,
         annual_rate_micro=EXCLUDED.annual_rate_micro, start_date=EXCLUDED.start_date,
         term_periods=EXCLUDED.term_periods, frequency=EXCLUDED.frequency,
         payment_minor=EXCLUDED.payment_minor,
         liability_account_code=EXCLUDED.liability_account_code,
         interest_account_code=EXCLUDED.interest_account_code,
         min_dscr_micro=EXCLUDED.min_dscr_micro, active=EXCLUDED.active, memo=EXCLUDED.memo`,
      [
        tenant, loan.id, loan.lender, loan.kind, loan.originalPrincipalMinor,
        loan.currentPrincipalMinor, loan.annualRateMicro, loan.startDate, loan.termPeriods,
        loan.frequency, loan.paymentMinor, loan.liabilityAccountCode, loan.interestAccountCode,
        loan.minDscrMicro, loan.active, loan.memo,
      ],
    ));
  }

  async listPayments(tenant: string, loanId?: string): Promise<LoanPaymentRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = loanId
        ? await db.query(
          "SELECT * FROM loan_payment WHERE tenant_id=$1 AND loan_id=$2 ORDER BY pay_date, id",
          [tenant, loanId])
        : await db.query(
          "SELECT * FROM loan_payment WHERE tenant_id=$1 ORDER BY pay_date, id", [tenant]);
      return res.rows.map((r) => this.paymentFrom(r));
    });
  }

  async getPayment(tenant: string, id: string): Promise<LoanPaymentRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM loan_payment WHERE tenant_id=$1 AND id=$2", [tenant, id]);
      const r = res.rows[0];
      return r ? this.paymentFrom(r) : undefined;
    });
  }

  private paymentFrom(r: Record<string, unknown>): LoanPaymentRecord {
    return {
      id: String(r["id"]),
      loanId: String(r["loan_id"]),
      date: String(r["pay_date"]),
      principalMinor: String(r["principal_minor"] ?? "0"),
      interestMinor: String(r["interest_minor"] ?? "0"),
      kind: String(r["kind"] ?? "PAYMENT") as "PAYMENT" | "DRAW",
      entryId: String(r["entry_id"] ?? ""),
      memo: String(r["memo"] ?? ""),
    };
  }

  async savePayment(tenant: string, payment: LoanPaymentRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO loan_payment (tenant_id, id, loan_id, pay_date, principal_minor,
         interest_minor, kind, entry_id, memo)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         loan_id=EXCLUDED.loan_id, pay_date=EXCLUDED.pay_date,
         principal_minor=EXCLUDED.principal_minor, interest_minor=EXCLUDED.interest_minor,
         kind=EXCLUDED.kind, entry_id=EXCLUDED.entry_id, memo=EXCLUDED.memo`,
      [
        tenant, payment.id, payment.loanId, payment.date, payment.principalMinor,
        payment.interestMinor, payment.kind, payment.entryId, payment.memo,
      ],
    ));
  }
}

// --- operations --------------------------------------------------------------

export interface DebtContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

function requireDate(raw: unknown, label: string): string {
  const date = String(raw ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new DebtError(`${label} must be YYYY-MM-DD`);
  return date;
}

function minorOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^-?\d+$/.test(value)) throw new DebtError(`${label} must be a whole number of minor units`);
  return BigInt(value);
}

function provenanceFor(id: string, date: string, at: string): Provenance {
  return {
    sourceSystem: "debt",
    sourceObject: id,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/debt-1",
    mappingVersion: "ledger-service/debt-1",
  };
}

export interface LoanInput {
  readonly id?: string;
  readonly lender?: string;
  readonly kind?: string;
  readonly original_principal_minor?: string | number;
  readonly annual_rate_micro?: string | number;
  readonly start_date?: string;
  readonly term_periods?: string | number;
  readonly frequency?: string;
  readonly payment_minor?: string | number;
  readonly liability_account_code?: string;
  readonly interest_account_code?: string;
  readonly min_dscr_micro?: string | number;
  readonly memo?: string;
  /** When set, post the loan proceeds: debit this account, credit the liability. */
  readonly proceeds_to_code?: string;
  readonly active?: boolean;
}

/**
 * Open (or edit) a loan. On first creation, optionally posts the proceeds — cash
 * (or the financed asset) in, the liability up — so the balance sheet reflects
 * the loan the moment it is taken. Editing an existing loan never re-posts.
 */
export async function saveLoan(
  ctx: DebtContext, input: LoanInput,
): Promise<{ loan: LoanRecord; entryId: string }> {
  const id = String(input.id ?? "").trim();
  if (!id) throw new DebtError("a loan needs an id");
  const kind = String(input.kind ?? "TERM").trim().toUpperCase() as LoanKind;
  if (!LOAN_KINDS.includes(kind)) throw new DebtError(`kind must be one of ${LOAN_KINDS.join(", ")}`);
  const frequency = String(input.frequency ?? "MONTHLY").trim().toUpperCase() as PaymentFrequency;
  if (!FREQUENCIES.includes(frequency)) {
    throw new DebtError(`frequency must be one of ${FREQUENCIES.join(", ")}`);
  }
  const principal = minorOf(input.original_principal_minor, "original principal");
  if (principal < 0n) throw new DebtError("principal cannot be negative");
  const rate = minorOf(input.annual_rate_micro, "annual rate");
  if (rate < 0n) throw new DebtError("interest rate cannot be negative");
  const term = Number(input.term_periods ?? 0);
  if (!Number.isInteger(term) || term < 0) throw new DebtError("term must be a whole number of periods, 0 or more");
  if (!isRevolving(kind) && term === 0 && principal > 0n) {
    throw new DebtError("an amortizing loan needs a term (number of payments)");
  }
  const startDate = requireDate(input.start_date, "start date");

  const chart = await ctx.backend.chart(ctx.tenant);
  const need = (code: string, role: string, type: string) => {
    const account = chart.getByCode(code);
    if (!account) throw new DebtError(`unknown account code ${code}`);
    if (account.type !== type) throw new DebtError(`${code} is not a ${role} account`);
    return code;
  };
  const liabilityAccountCode = need(
    String(input.liability_account_code ?? "").trim() || "2700", "liability", "LIABILITY");
  const interestAccountCode = need(
    String(input.interest_account_code ?? "").trim() || "6950", "expense", "EXPENSE");

  const existing = await ctx.backend.debt().getLoan(String(ctx.tenant), id);
  const loan: LoanRecord = {
    id,
    lender: String(input.lender ?? "").trim() || id,
    kind,
    originalPrincipalMinor: principal.toString(),
    currentPrincipalMinor: existing?.currentPrincipalMinor ?? principal.toString(),
    annualRateMicro: rate.toString(),
    startDate,
    termPeriods: term,
    frequency,
    paymentMinor: minorOf(input.payment_minor, "payment").toString(),
    liabilityAccountCode,
    interestAccountCode,
    minDscrMicro: minorOf(input.min_dscr_micro, "minimum DSCR").toString(),
    active: input.active !== false,
    memo: String(input.memo ?? ""),
  };

  let entryId = "";
  const proceedsCode = String(input.proceeds_to_code ?? "").trim();
  if (!existing && proceedsCode && principal > 0n) {
    // Post the proceeds: debit where the money (or asset) landed, credit the loan.
    const proceeds = chart.getByCode(proceedsCode);
    const liability = chart.getByCode(liabilityAccountCode);
    if (!proceeds) throw new DebtError(`unknown account code ${proceedsCode}`);
    if (!liability) throw new DebtError(`unknown account code ${liabilityAccountCode}`);
    const command: PostCommand = {
      tenantId: ctx.tenant,
      idempotencyKey: asIdempotencyKey(`loan-open:${id}`),
      periodKey: asPeriodKey(startDate.slice(0, 7)),
      currency: ctx.currency,
      entryDate: startDate,
      memo: `Loan drawn — ${loan.lender}`,
      provenance: provenanceFor(id, startDate, ctx.now()),
      lines: [
        { accountId: proceeds.id, side: "DEBIT", amount: Money.fromMinorUnits(principal, ctx.currency), memo: loan.lender },
        { accountId: liability.id, side: "CREDIT", amount: Money.fromMinorUnits(principal, ctx.currency), memo: loan.lender },
      ],
    };
    const engine = new PostingEngine(chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant));
    entryId = String((await engine.post(command, { postedAt: ctx.now() })).id);
  }
  await ctx.backend.debt().saveLoan(String(ctx.tenant), loan);
  return { loan, entryId };
}

export interface PaymentInput {
  readonly id?: string;
  readonly loan_id?: string;
  readonly date?: string;
  readonly amount_minor?: string | number;
  /** Where the cash came from (a bank account); defaults to 1000. */
  readonly paid_from_code?: string;
  readonly memo?: string;
}

/**
 * Record a loan payment. Interest is `outstanding balance × periodic rate`,
 * rounded to the cent; the rest of the payment reduces principal (capped at the
 * balance, so an overpayment simply pays the loan off). Posts a balanced entry —
 * liability down, interest expense up, cash out — and lowers the loan balance.
 *
 * Because interest is taken from the live balance rather than a schedule row,
 * paying extra principal, paying early, or paying off entirely all just work.
 */
export async function recordLoanPayment(
  ctx: DebtContext, input: PaymentInput,
): Promise<{ loan: LoanRecord; entryId: string; principalMinor: string; interestMinor: string }> {
  const loanId = String(input.loan_id ?? "").trim();
  const loan = await ctx.backend.debt().getLoan(String(ctx.tenant), loanId);
  if (!loan) throw new DebtError(`unknown loan ${loanId}`);
  const date = requireDate(input.date, "date");
  const amount = minorOf(input.amount_minor, "amount");
  if (amount <= 0n) throw new DebtError("a payment needs an amount");

  const balance = BigInt(loan.currentPrincipalMinor);
  const rScaled = periodicRateScaled(BigInt(loan.annualRateMicro), loan.frequency);
  const interestDue = roundDiv(balance * rScaled, SCALE);
  // Interest never exceeds the payment; principal is the remainder, capped at what is owed.
  const interest = amount < interestDue ? amount : interestDue;
  let principalPart = amount - interest;
  if (principalPart > balance) principalPart = balance; // overpayment pays it off, no more

  // Idempotency: an explicit id is idempotent; otherwise two genuine same-day
  // payments on one loan are distinct events disambiguated by a sequence.
  const explicitId = String(input.id ?? "").trim();
  let id = explicitId;
  if (!id) {
    const priorSameDay = (await ctx.backend.debt().listPayments(String(ctx.tenant), loanId))
      .filter((p) => p.date === date && p.kind === "PAYMENT").length;
    id = `${loanId}-${date}#${priorSameDay}`;
  }
  const seen = await ctx.backend.debt().getPayment(String(ctx.tenant), id);
  if (seen) {
    return { loan, entryId: seen.entryId, principalMinor: seen.principalMinor, interestMinor: seen.interestMinor };
  }

  const chart = await ctx.backend.chart(ctx.tenant);
  const paidFromCode = String(input.paid_from_code ?? "").trim() || "1000";
  const paidFrom = chart.getByCode(paidFromCode);
  const liability = chart.getByCode(loan.liabilityAccountCode);
  const interestAcct = chart.getByCode(loan.interestAccountCode);
  if (!paidFrom) throw new DebtError(`unknown account code ${paidFromCode}`);
  if (!liability) throw new DebtError(`unknown account code ${loan.liabilityAccountCode}`);
  if (!interestAcct) throw new DebtError(`unknown account code ${loan.interestAccountCode}`);

  const total = principalPart + interest;
  const lines = [];
  if (principalPart > 0n) {
    lines.push({ accountId: liability.id, side: "DEBIT" as const, amount: Money.fromMinorUnits(principalPart, ctx.currency), memo: "Principal" });
  }
  if (interest > 0n) {
    lines.push({ accountId: interestAcct.id, side: "DEBIT" as const, amount: Money.fromMinorUnits(interest, ctx.currency), memo: "Interest" });
  }
  lines.push({ accountId: paidFrom.id, side: "CREDIT" as const, amount: Money.fromMinorUnits(total, ctx.currency), memo: `Payment — ${loan.lender}` });

  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`loan-pay:${id}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: String(input.memo ?? "") || `Loan payment — ${loan.lender}`,
    provenance: provenanceFor(id, date, ctx.now()),
    lines,
  };
  const engine = new PostingEngine(chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant));
  const entry = await engine.post(command, { postedAt: ctx.now() });

  const updated: LoanRecord = { ...loan, currentPrincipalMinor: (balance - principalPart).toString() };
  await ctx.backend.debt().saveLoan(String(ctx.tenant), updated);
  await ctx.backend.debt().savePayment(String(ctx.tenant), {
    id, loanId, date, principalMinor: principalPart.toString(), interestMinor: interest.toString(),
    kind: "PAYMENT", entryId: String(entry.id), memo: String(input.memo ?? ""),
  });
  return {
    loan: updated, entryId: String(entry.id),
    principalMinor: principalPart.toString(), interestMinor: interest.toString(),
  };
}

export interface DrawInput {
  readonly id?: string;
  readonly loan_id?: string;
  readonly date?: string;
  readonly amount_minor?: string | number;
  readonly deposit_to_code?: string;
  readonly memo?: string;
}

/** Draw on a revolving line: cash in, liability up. Only for revolving kinds. */
export async function recordDraw(
  ctx: DebtContext, input: DrawInput,
): Promise<{ loan: LoanRecord; entryId: string }> {
  const loanId = String(input.loan_id ?? "").trim();
  const loan = await ctx.backend.debt().getLoan(String(ctx.tenant), loanId);
  if (!loan) throw new DebtError(`unknown loan ${loanId}`);
  if (!isRevolving(loan.kind)) {
    throw new DebtError(`${loanId} is a ${loan.kind} — draws are only for a line of credit or card`);
  }
  const date = requireDate(input.date, "date");
  const amount = minorOf(input.amount_minor, "amount");
  if (amount <= 0n) throw new DebtError("a draw needs an amount");

  const explicitId = String(input.id ?? "").trim();
  let id = explicitId;
  if (!id) {
    const prior = (await ctx.backend.debt().listPayments(String(ctx.tenant), loanId))
      .filter((p) => p.date === date && p.kind === "DRAW").length;
    id = `${loanId}-draw-${date}#${prior}`;
  }
  const seen = await ctx.backend.debt().getPayment(String(ctx.tenant), id);
  if (seen) return { loan, entryId: seen.entryId };

  const chart = await ctx.backend.chart(ctx.tenant);
  const depositCode = String(input.deposit_to_code ?? "").trim() || "1000";
  const deposit = chart.getByCode(depositCode);
  const liability = chart.getByCode(loan.liabilityAccountCode);
  if (!deposit) throw new DebtError(`unknown account code ${depositCode}`);
  if (!liability) throw new DebtError(`unknown account code ${loan.liabilityAccountCode}`);

  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`loan-draw:${id}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: String(input.memo ?? "") || `Draw — ${loan.lender}`,
    provenance: provenanceFor(id, date, ctx.now()),
    lines: [
      { accountId: deposit.id, side: "DEBIT", amount: Money.fromMinorUnits(amount, ctx.currency), memo: loan.lender },
      { accountId: liability.id, side: "CREDIT", amount: Money.fromMinorUnits(amount, ctx.currency), memo: loan.lender },
    ],
  };
  const engine = new PostingEngine(chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant));
  const entry = await engine.post(command, { postedAt: ctx.now() });

  const updated: LoanRecord = {
    ...loan, currentPrincipalMinor: (BigInt(loan.currentPrincipalMinor) + amount).toString(),
  };
  await ctx.backend.debt().saveLoan(String(ctx.tenant), updated);
  await ctx.backend.debt().savePayment(String(ctx.tenant), {
    id, loanId, date, principalMinor: (-amount).toString(), interestMinor: "0",
    kind: "DRAW", entryId: String(entry.id), memo: String(input.memo ?? ""),
  });
  return { loan: updated, entryId: String(entry.id) };
}

// --- the dashboard the owner logs in for -------------------------------------

/** Monthly-equivalent of a per-period payment, so loans on different cadences add up. */
function monthlyEquivalent(paymentMinor: bigint, freq: PaymentFrequency): bigint {
  return roundDiv(paymentMinor * BigInt(PERIODS_PER_YEAR[freq]), 12n);
}

export function scheduleRowJson(r: ScheduleRow): Record<string, unknown> {
  return {
    period: r.period,
    due_date: r.dueDate,
    payment_minor: r.paymentMinor,
    principal_minor: r.principalMinor,
    interest_minor: r.interestMinor,
    balance_minor: r.balanceMinor,
  };
}

export function loanJson(loan: LoanRecord): Record<string, unknown> {
  return {
    id: loan.id,
    lender: loan.lender,
    kind: loan.kind,
    revolving: isRevolving(loan.kind),
    original_principal_minor: loan.originalPrincipalMinor,
    current_principal_minor: loan.currentPrincipalMinor,
    annual_rate_micro: loan.annualRateMicro,
    annual_rate_pct: (Number(loan.annualRateMicro) / 10_000).toFixed(4),
    start_date: loan.startDate,
    term_periods: loan.termPeriods,
    frequency: loan.frequency,
    payment_minor: loan.paymentMinor,
    liability_account_code: loan.liabilityAccountCode,
    interest_account_code: loan.interestAccountCode,
    min_dscr_micro: loan.minDscrMicro,
    active: loan.active,
    memo: loan.memo,
  };
}

/**
 * The debt dashboard: what the owner owes, at what rate, what it costs each
 * month, when they are free, and what interest they have paid this year. This is
 * the surface the incumbents don't build — because they don't hold debt as a
 * managed object, only as a balance-sheet line the owner keeps by hand.
 */
export async function debtDashboard(
  ctx: DebtContext, asOf?: string,
): Promise<Record<string, unknown>> {
  const asOfDate = asOf && /^\d{4}-\d{2}-\d{2}$/.test(asOf) ? asOf : ctx.now().slice(0, 10);
  const year = asOfDate.slice(0, 4);
  const loans = (await ctx.backend.debt().listLoans(String(ctx.tenant))).filter((l) => l.active);
  const payments = await ctx.backend.debt().listPayments(String(ctx.tenant));

  let totalPrincipal = 0n;
  let weightedRateNum = 0n; // Σ balance × rate
  let monthlyDebtService = 0n;
  const rows: Record<string, unknown>[] = [];

  for (const loan of loans) {
    const balance = BigInt(loan.currentPrincipalMinor);
    totalPrincipal += balance;
    weightedRateNum += balance * BigInt(loan.annualRateMicro);

    const schedule = amortizationSchedule(loan);
    const payment = BigInt(loan.paymentMinor) > 0n
      ? BigInt(loan.paymentMinor)
      : (schedule[0] ? BigInt(schedule[0].paymentMinor) : 0n);
    monthlyDebtService += monthlyEquivalent(payment, loan.frequency);

    const nextDue = schedule.find((r) => r.dueDate >= asOfDate && BigInt(r.balanceMinor) >= 0n);
    const payoffDate = schedule.length ? schedule[schedule.length - 1]!.dueDate : "";
    // maturity within 90 days of the as-of date
    const maturitySoon = payoffDate !== ""
      && payoffDate >= asOfDate
      && daysBetween(asOfDate, payoffDate) <= 90;

    const ytd = payments.filter((p) => p.loanId === loan.id && p.date.slice(0, 4) === year);
    const interestYtd = ytd.reduce((s, p) => s + BigInt(p.interestMinor), 0n);
    const principalYtd = ytd.reduce((s, p) => s + (BigInt(p.principalMinor) > 0n ? BigInt(p.principalMinor) : 0n), 0n);

    rows.push({
      ...loanJson(loan),
      next_payment_date: nextDue?.dueDate ?? "",
      next_payment_minor: nextDue?.paymentMinor ?? "0",
      payoff_date: payoffDate,
      maturity_within_90_days: maturitySoon,
      interest_paid_ytd_minor: interestYtd.toString(),
      principal_paid_ytd_minor: principalYtd.toString(),
    });
  }

  const weightedRateMicro = totalPrincipal > 0n ? (weightedRateNum / totalPrincipal) : 0n;

  return {
    contract: "debt-dashboard/1",
    as_of: asOfDate,
    currency: ctx.currency.code,
    totals: {
      total_principal_minor: totalPrincipal.toString(),
      weighted_avg_rate_micro: weightedRateMicro.toString(),
      weighted_avg_rate_pct: (Number(weightedRateMicro) / 10_000).toFixed(4),
      monthly_debt_service_minor: monthlyDebtService.toString(),
      annual_debt_service_minor: (monthlyDebtService * 12n).toString(),
      loan_count: loans.length,
    },
    loans: rows,
  };
}

function daysBetween(a: string, b: string): number {
  const [ay, am, ad] = a.split("-").map(Number);
  const [by, bm, bd] = b.split("-").map(Number);
  return Math.round((Date.UTC(by!, bm! - 1, bd!) - Date.UTC(ay!, am! - 1, ad!)) / 86_400_000);
}

/**
 * Payoff planning: what an extra amount per period does to the finish date and
 * the total interest — the "if I pay $X more, I'm free N months sooner and save
 * $Y" answer no incumbent gives. Pure projection over the live balance.
 */
export function payoffPlan(loan: LoanRecord, extraPerPeriodMinor: bigint): Record<string, unknown> {
  const rScaled = periodicRateScaled(BigInt(loan.annualRateMicro), loan.frequency);
  const basePayment = BigInt(loan.paymentMinor) > 0n
    ? BigInt(loan.paymentMinor)
    : levelPayment(BigInt(loan.originalPrincipalMinor), rScaled, loan.termPeriods);

  const run = (extra: bigint): { periods: number; interest: bigint } => {
    let balance = BigInt(loan.currentPrincipalMinor);
    const payment = basePayment + extra;
    let interest = 0n;
    let periods = 0;
    // cap iterations so a payment below the interest accrual can't loop forever
    while (balance > 0n && periods < 10_000) {
      const i = roundDiv(balance * rScaled, SCALE);
      let principalPart = payment - i;
      if (principalPart <= 0n) return { periods: -1, interest: -1n }; // never amortizes
      if (principalPart > balance) principalPart = balance;
      interest += i;
      balance -= principalPart;
      periods += 1;
    }
    return { periods, interest };
  };

  const base = run(0n);
  const accelerated = run(extraPerPeriodMinor);
  const periodsSaved = base.periods >= 0 && accelerated.periods >= 0
    ? base.periods - accelerated.periods : 0;
  const interestSaved = base.interest >= 0n && accelerated.interest >= 0n
    ? base.interest - accelerated.interest : 0n;

  return {
    contract: "payoff-plan/1",
    loan_id: loan.id,
    extra_per_period_minor: extraPerPeriodMinor.toString(),
    base_periods: base.periods,
    accelerated_periods: accelerated.periods,
    periods_saved: periodsSaved,
    interest_saved_minor: interestSaved.toString(),
  };
}
