/**
 * Budgets and budget-vs-actual reporting.
 *
 * A budget is a target amount per (account, period) — e.g. "5410 Marketing,
 * 2026-08 → $4,000". Budget vs actual compares those targets to the ledger's
 * actual natural amounts for the same accounts and period, producing a variance
 * per line and in total. Amounts are exact integer `Money`; a "favorable"
 * variance depends on the account class (more revenue is good, more expense is
 * bad), which the report flags so the presentation can color it correctly.
 */

import { Money } from "@rgnr8/ledger-kernel";
import type { AccountId, Currency, PeriodKey } from "@rgnr8/ledger-kernel";
import {
  naturalAmount,
  type AccountClass,
  type TrialBalance,
  type TrialBalanceEntry,
} from "./accounts.js";

export interface BudgetLine {
  readonly accountId: AccountId;
  readonly period: PeriodKey;
  readonly amount: Money; // natural amount for the account's class
}

/** A per-(account, period) budget store. In-memory reference implementation. */
export class Budget {
  private readonly byKey = new Map<string, Money>();

  private key(accountId: AccountId, period: PeriodKey): string {
    return `${period}::${accountId}`;
  }

  set(accountId: AccountId, period: PeriodKey, amount: Money): this {
    this.byKey.set(this.key(accountId, period), amount);
    return this;
  }

  get(accountId: AccountId, period: PeriodKey): Money | undefined {
    return this.byKey.get(this.key(accountId, period));
  }

  /** All budget lines for a period. */
  linesForPeriod(period: PeriodKey): BudgetLine[] {
    const out: BudgetLine[] = [];
    for (const [k, amount] of this.byKey) {
      const [p, accountId] = k.split("::") as [string, string];
      if (p === period) out.push({ accountId: accountId as AccountId, period, amount });
    }
    return out;
  }
}

export interface BudgetVarianceLine {
  readonly accountId: AccountId;
  readonly code: string;
  readonly name: string;
  readonly accountClass: AccountClass;
  readonly budget: Money;
  readonly actual: Money;
  /** actual − budget (natural amounts). */
  readonly variance: Money;
  /** True when the variance helps the bottom line (revenue over / expense under). */
  readonly favorable: boolean;
  /** Percent of budget used, two decimals; null when budget is zero. */
  readonly pctOfBudget: number | null;
}

export interface BudgetVarianceReport {
  readonly currency: Currency;
  readonly period: PeriodKey;
  readonly lines: readonly BudgetVarianceLine[];
  readonly totalBudget: Money;
  readonly totalActual: Money;
  readonly totalVariance: Money;
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function isFavorable(cls: AccountClass, variance: Money): boolean {
  if (variance.isZero()) return true;
  const over = !variance.isNegative(); // actual > budget
  // Revenue/asset: over budget is favorable. Expense/liability: under is favorable.
  if (cls === "revenue" || cls === "asset") return over;
  return !over;
}

/**
 * Budget vs actual for one period. `actuals` is the period's trial balance
 * (natural amounts derived per line). Every account that appears in either the
 * budget or the actuals is reported; the missing side reads zero.
 */
export function budgetVsActual(
  budget: Budget,
  actuals: TrialBalance,
  period: PeriodKey,
): BudgetVarianceReport {
  const currency = actuals.currency;
  const zero = Money.zero(currency);

  const actualByAccount = new Map<AccountId, TrialBalanceEntry>();
  for (const e of actuals.entries) actualByAccount.set(e.accountId, e);

  const budgetLines = budget.linesForPeriod(period);
  const budgetByAccount = new Map<AccountId, Money>();
  for (const b of budgetLines) budgetByAccount.set(b.accountId, b.amount);

  const ids = new Set<AccountId>([...actualByAccount.keys(), ...budgetByAccount.keys()]);
  const lines: BudgetVarianceLine[] = [];
  let totalBudget = zero;
  let totalActual = zero;

  for (const id of ids) {
    const entry = actualByAccount.get(id);
    const budgetAmt = budgetByAccount.get(id) ?? zero;
    const actualAmt = entry ? naturalAmount(entry) : zero;
    // Only meaningful for P&L accounts; a budget line on a non-P&L account is
    // still reported (some shops budget cash), classed by whatever it is.
    const cls: AccountClass = entry?.accountClass ?? "expense";
    const variance = actualAmt.minus(budgetAmt);
    lines.push({
      accountId: id,
      code: entry?.code ?? String(id),
      name: entry?.name ?? String(id),
      accountClass: cls,
      budget: budgetAmt,
      actual: actualAmt,
      variance,
      favorable: isFavorable(cls, variance),
      pctOfBudget: budgetAmt.isZero()
        ? null
        : round2((Number(actualAmt.minorUnits) / Number(budgetAmt.minorUnits)) * 100),
    });
    totalBudget = totalBudget.plus(budgetAmt);
    totalActual = totalActual.plus(actualAmt);
  }

  lines.sort((a, b) => a.code.localeCompare(b.code));
  return {
    currency,
    period,
    lines,
    totalBudget,
    totalActual,
    totalVariance: totalActual.minus(totalBudget),
  };
}

/** The serializable `budget-vs-actual/1` contract for owner-facing rendering. */
export interface BudgetVsActualJson {
  readonly contract: "budget-vs-actual/1";
  readonly period: string;
  readonly currency: string;
  readonly lines: ReadonlyArray<{
    readonly code: string;
    readonly name: string;
    readonly account_class: string;
    readonly budget_minor: string;
    readonly actual_minor: string;
    readonly variance_minor: string;
    readonly favorable: boolean;
    readonly pct_of_budget: number | null;
  }>;
  readonly total_budget_minor: string;
  readonly total_actual_minor: string;
  readonly total_variance_minor: string;
}

export function budgetVsActualJson(report: BudgetVarianceReport): BudgetVsActualJson {
  return {
    contract: "budget-vs-actual/1",
    period: String(report.period),
    currency: report.currency.code,
    lines: report.lines.map((l) => ({
      code: l.code,
      name: l.name,
      account_class: l.accountClass,
      budget_minor: l.budget.minorUnits.toString(),
      actual_minor: l.actual.minorUnits.toString(),
      variance_minor: l.variance.minorUnits.toString(),
      favorable: l.favorable,
      pct_of_budget: l.pctOfBudget,
    })),
    total_budget_minor: report.totalBudget.minorUnits.toString(),
    total_actual_minor: report.totalActual.minorUnits.toString(),
    total_variance_minor: report.totalVariance.minorUnits.toString(),
  };
}
