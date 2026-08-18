import {
  Money,
  computeTrialBalance,
  asPeriodKey,
  type Currency,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import {
  Budget,
  budgetVsActual,
  budgetVsActualJson,
  fromKernelTrialBalance,
  glDetail,
} from "@rgnr8/financial-statements";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";

/**
 * The two reports an accountant asks for first, and an owner asks for when
 * something looks wrong.
 *
 * **General ledger detail** is every posting, in order, per account, with a
 * running balance. It is the report that answers "why is this number what it
 * is" — the register already does that for one account; this does it across the
 * whole chart, which is what you need when the trial balance surprises you.
 *
 * **Budget vs actual** is the plan against what happened. The subtlety is
 * *favourable*: spending less than budgeted is good, earning less is not, so
 * the sign of a variance means opposite things on opposite sides of the P&L.
 * The engine knows that; this layer just has to not lose it.
 */

export class ReportingError extends Error {}

// --- budget storage ----------------------------------------------------------

export interface BudgetLineRecord {
  readonly period: string;
  readonly accountCode: string;
  readonly amountMinor: string;
}

export interface BudgetStore {
  migrate(): Promise<void>;
  listBudget(tenant: string, period?: string): Promise<BudgetLineRecord[]>;
  saveBudget(tenant: string, lines: readonly BudgetLineRecord[]): Promise<void>;
  deleteBudget(tenant: string, period: string): Promise<void>;
}

export class InMemoryBudgetStore implements BudgetStore {
  private readonly lines = new Map<string, BudgetLineRecord>();

  private key(tenant: string, l: { period: string; accountCode: string }): string {
    return `${tenant}::${l.period}::${l.accountCode}`;
  }

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listBudget(tenant: string, period?: string): Promise<BudgetLineRecord[]> {
    const prefix = `${tenant}::`;
    const out: BudgetLineRecord[] = [];
    for (const [k, v] of this.lines) {
      if (!k.startsWith(prefix)) continue;
      if (period && v.period !== period) continue;
      out.push(v);
    }
    return Promise.resolve(
      out.sort((a, b) =>
        a.period === b.period ? a.accountCode.localeCompare(b.accountCode)
          : a.period.localeCompare(b.period)),
    );
  }

  saveBudget(tenant: string, lines: readonly BudgetLineRecord[]): Promise<void> {
    for (const l of lines) {
      // A zero budget is a statement ("we plan to spend nothing here"), but a
      // blank one is an absence — the caller clears by sending no line at all.
      this.lines.set(this.key(tenant, l), l);
    }
    return Promise.resolve();
  }

  deleteBudget(tenant: string, period: string): Promise<void> {
    for (const [k, v] of [...this.lines]) {
      if (k.startsWith(`${tenant}::`) && v.period === period) this.lines.delete(k);
    }
    return Promise.resolve();
  }
}

export const BUDGET_DDL = `
CREATE TABLE IF NOT EXISTS budget_line (
  tenant_id    text NOT NULL,
  period_key   text NOT NULL,
  account_code text NOT NULL,
  amount_minor numeric(38,0) NOT NULL,
  CONSTRAINT budget_line_pk PRIMARY KEY (tenant_id, period_key, account_code)
);
`;

export class PgBudgetStore implements BudgetStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(BUDGET_DDL);
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

  async listBudget(tenant: string, period?: string): Promise<BudgetLineRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = period
        ? await db.query(
            `SELECT period_key, account_code, amount_minor FROM budget_line
             WHERE tenant_id=$1 AND period_key=$2 ORDER BY account_code`,
            [tenant, period],
          )
        : await db.query(
            `SELECT period_key, account_code, amount_minor FROM budget_line
             WHERE tenant_id=$1 ORDER BY period_key, account_code`,
            [tenant],
          );
      return res.rows.map((r) => ({
        period: String(r["period_key"]),
        accountCode: String(r["account_code"]),
        amountMinor: String(r["amount_minor"]),
      }));
    });
  }

  async saveBudget(tenant: string, lines: readonly BudgetLineRecord[]): Promise<void> {
    await this.tx(tenant, async (db) => {
      for (const l of lines) {
        await db.query(
          `INSERT INTO budget_line (tenant_id, period_key, account_code, amount_minor)
           VALUES ($1,$2,$3,$4)
           ON CONFLICT (tenant_id, period_key, account_code)
           DO UPDATE SET amount_minor = EXCLUDED.amount_minor`,
          [tenant, l.period, l.accountCode, l.amountMinor],
        );
      }
    });
  }

  async deleteBudget(tenant: string, period: string): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query("DELETE FROM budget_line WHERE tenant_id=$1 AND period_key=$2", [tenant, period]),
    );
  }
}

// --- the reports -------------------------------------------------------------

export interface ReportingContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
}

function requirePeriod(raw: unknown): string {
  const period = String(raw ?? "").trim();
  if (!/^\d{4}-\d{2}$/.test(period)) {
    throw new ReportingError("period must be YYYY-MM");
  }
  return period;
}

function optionalDate(raw: unknown, label: string): string | undefined {
  const date = String(raw ?? "").trim();
  if (!date) return undefined;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new ReportingError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

export interface GlDetailJson {
  readonly contract: "gl-detail/1";
  readonly currency: string;
  readonly from: string;
  readonly to: string;
  readonly accounts: ReadonlyArray<Record<string, unknown>>;
  readonly total_debit_minor: string;
  readonly total_credit_minor: string;
}

/**
 * Every posting across the chart, per account, with a running balance.
 *
 * Accounts with no activity in the window are omitted rather than listed as
 * rows of zeroes: a general ledger is long enough without them, and their
 * balances are already on the trial balance.
 */
export async function generalLedger(
  ctx: ReportingContext,
  query: Readonly<Record<string, string>>,
): Promise<GlDetailJson> {
  const from = optionalDate(query["from"], "from");
  const to = optionalDate(query["to"], "to");
  const chart = await ctx.backend.chart(ctx.tenant);
  const entries = await ctx.backend.store(ctx.tenant).list(ctx.tenant);

  const codes = String(query["codes"] ?? "")
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean);
  const accountIds = codes.length
    ? codes.map((code) => {
        const account = chart.getByCode(code);
        if (!account) throw new ReportingError(`unknown account code ${code}`);
        return account.id;
      })
    : undefined;

  const window = from || to
    ? { ...(from ? { from } : {}), ...(to ? { to } : {}) }
    : undefined;

  const detail = glDetail(entries, chart, ctx.currency, {
    ...(window ? { window } : {}),
    ...(accountIds ? { accountIds } : {}),
  });

  let totalDebit = 0n;
  let totalCredit = 0n;
  const accounts = detail
    .filter((a) => a.rows.length > 0)
    .map((a) => {
      totalDebit += a.totalDebit.minorUnits;
      totalCredit += a.totalCredit.minorUnits;
      return {
        code: a.code,
        name: a.name,
        type: a.type,
        opening_minor: a.opening.minorUnits.toString(),
        closing_minor: a.closing.minorUnits.toString(),
        total_debit_minor: a.totalDebit.minorUnits.toString(),
        total_credit_minor: a.totalCredit.minorUnits.toString(),
        rows: a.rows.map((r) => ({
          entry_id: r.entryId,
          sequence: r.sequence,
          date: r.date,
          memo: r.memo,
          debit_minor: r.debit.minorUnits.toString(),
          credit_minor: r.credit.minorUnits.toString(),
          balance_minor: r.balance.minorUnits.toString(),
        })),
      };
    });

  return {
    contract: "gl-detail/1",
    currency: ctx.currency.code,
    from: from ?? "",
    to: to ?? "",
    accounts,
    total_debit_minor: totalDebit.toString(),
    total_credit_minor: totalCredit.toString(),
  };
}

export interface BudgetInput {
  readonly period?: string;
  readonly lines?: ReadonlyArray<{
    readonly account_code?: string;
    readonly amount_minor?: string | number;
  }>;
}

/** Store a period's budget. Amounts are the account's natural sign. */
export async function saveBudgetLines(
  ctx: ReportingContext, input: BudgetInput,
): Promise<{ period: string; saved: number }> {
  const period = requirePeriod(input.period);
  const rawLines = Array.isArray(input.lines) ? input.lines : [];
  if (rawLines.length === 0) throw new ReportingError("a budget needs at least one line");

  const chart = await ctx.backend.chart(ctx.tenant);
  const lines: BudgetLineRecord[] = [];
  for (const l of rawLines) {
    const code = String(l.account_code ?? "").trim();
    if (!chart.getByCode(code)) throw new ReportingError(`unknown account code ${code}`);
    const raw = typeof l.amount_minor === "number"
      ? String(l.amount_minor)
      : String(l.amount_minor ?? "").trim();
    if (!/^-?\d+$/.test(raw)) {
      throw new ReportingError(`${code}: amount must be an integer minor-unit value`);
    }
    lines.push({ period, accountCode: code, amountMinor: raw });
  }
  await ctx.backend.budgets().saveBudget(String(ctx.tenant), lines);
  return { period, saved: lines.length };
}

/**
 * Budget vs actual for a period.
 *
 * "Favourable" is the whole point and is easy to get backwards: under-spending
 * is good, under-earning is not. The engine decides it from the account's class
 * so the report reads correctly on both sides of the P&L.
 */
export async function budgetReport(
  ctx: ReportingContext, periodRaw: unknown,
): Promise<Record<string, unknown>> {
  const period = requirePeriod(periodRaw);
  const chart = await ctx.backend.chart(ctx.tenant);
  const stored = await ctx.backend.budgets().listBudget(String(ctx.tenant), period);

  const budget = new Budget();
  const missing: string[] = [];
  for (const l of stored) {
    const account = chart.getByCode(l.accountCode);
    if (!account) {
      // An account deleted after the budget was set. Say so rather than
      // silently dropping the line and quietly changing the totals.
      missing.push(l.accountCode);
      continue;
    }
    budget.set(
      account.id,
      asPeriodKey(period),
      Money.fromMinorUnits(BigInt(l.amountMinor), ctx.currency),
    );
  }

  // The period's activity only — a budget is about what happened in the month,
  // not the balance carried into it.
  const tb = await computeTrialBalance(
    ctx.backend.store(ctx.tenant),
    ctx.tenant,
    chart,
    ctx.currency,
    { from: `${period}-01`, to: `${period}-31` },
  );
  // A budget is about what you earn and what you spend. Comparing a bank
  // balance or a tax liability against a budget of zero produces a row that
  // says "worse than planned" about a number nobody planned — noise that
  // makes the real misses harder to see.
  const actuals = fromKernelTrialBalance(tb);
  const pAndL = {
    ...actuals,
    entries: actuals.entries.filter(
      (e) => e.accountClass === "revenue" || e.accountClass === "expense",
    ),
  };
  const report = budgetVsActual(budget, pAndL, asPeriodKey(period));
  return {
    ...budgetVsActualJson(report),
    budgeted_accounts: stored.length,
    ...(missing.length ? { unknown_accounts: missing } : {}),
  };
}
