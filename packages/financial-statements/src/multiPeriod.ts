/**
 * Multi-period (comparative) statement columns.
 *
 * `periodCompare` handles the common two-period income-statement case; this
 * generalizes to N labeled periods presented side by side — the "this month /
 * last month / same month last year" and "12-month trend" views. Every account
 * is aligned across all columns (an account absent from a period reads zero
 * there), and each column carries its own totals so the presentation is exact.
 */

import { Money } from "@rgnr8/ledger-kernel";
import type { AccountId, Currency } from "@rgnr8/ledger-kernel";
import {
  entriesOfClass,
  indexById,
  naturalAmount,
  sumNatural,
  type AccountClass,
  type TrialBalance,
} from "./accounts.js";
import { balanceSheet, incomeStatement } from "./statements.js";

export interface PeriodColumn {
  readonly label: string;
  readonly tb: TrialBalance;
}

export interface MultiPeriodRow {
  readonly accountId: AccountId;
  readonly code: string;
  readonly name: string;
  readonly accountClass: AccountClass;
  /** Natural amount per column, index-aligned to `columnLabels`. */
  readonly amounts: readonly Money[];
}

export interface MultiPeriodIncomeStatement {
  readonly currency: Currency;
  readonly columnLabels: readonly string[];
  readonly lines: readonly MultiPeriodRow[];
  readonly revenue: readonly Money[];
  readonly expenses: readonly Money[];
  readonly netIncome: readonly Money[];
}

function alignRows(
  columns: readonly PeriodColumn[],
  classes: readonly AccountClass[],
  currency: Currency,
): MultiPeriodRow[] {
  const zero = Money.zero(currency);
  const indexes = columns.map((c) => indexById(c.tb));
  // Union of accounts across all columns, restricted to the wanted classes.
  const meta = new Map<AccountId, { code: string; name: string; accountClass: AccountClass }>();
  for (const c of columns) {
    for (const cls of classes) {
      for (const e of entriesOfClass(c.tb, cls)) {
        if (!meta.has(e.accountId)) {
          meta.set(e.accountId, { code: e.code, name: e.name, accountClass: e.accountClass });
        }
      }
    }
  }
  const rows: MultiPeriodRow[] = [];
  for (const [id, m] of meta) {
    const amounts = indexes.map((idx) => {
      const e = idx.get(id);
      return e ? naturalAmount(e) : zero;
    });
    rows.push({ accountId: id, ...m, amounts });
  }
  rows.sort((a, b) => a.code.localeCompare(b.code));
  return rows;
}

/** Comparative income statement across N labeled periods. */
export function multiPeriodIncomeStatement(
  columns: readonly PeriodColumn[],
): MultiPeriodIncomeStatement {
  if (columns.length === 0) throw new Error("multiPeriodIncomeStatement needs at least one column");
  const currency = columns[0]!.tb.currency;
  const lines = alignRows(columns, ["revenue", "expense"], currency);
  const statements = columns.map((c) => incomeStatement(c.tb));
  return {
    currency,
    columnLabels: columns.map((c) => c.label),
    lines,
    revenue: statements.map((s) => s.revenue),
    expenses: statements.map((s) => s.expenses),
    netIncome: statements.map((s) => s.netIncome),
  };
}

export interface BalanceSheetColumn {
  readonly label: string;
  readonly tb: TrialBalance;
  /** Net income to fold into equity for this column (period-to-date). */
  readonly netIncome: Money;
}

export interface ComparativeBalanceSheet {
  readonly currency: Currency;
  readonly columnLabels: readonly string[];
  readonly lines: readonly MultiPeriodRow[];
  readonly assets: readonly Money[];
  readonly liabilities: readonly Money[];
  readonly equity: readonly Money[];
  /** Per-column balanced flag (assets = liabilities + equity). */
  readonly balanced: readonly boolean[];
}

/** Comparative balance sheet across N labeled as-of dates. */
export function comparativeBalanceSheet(
  columns: readonly BalanceSheetColumn[],
): ComparativeBalanceSheet {
  if (columns.length === 0) throw new Error("comparativeBalanceSheet needs at least one column");
  const currency = columns[0]!.tb.currency;
  const lines = alignRows(
    columns.map((c) => ({ label: c.label, tb: c.tb })),
    ["asset", "liability", "equity"],
    currency,
  );
  const sheets = columns.map((c) => balanceSheet(c.tb, c.netIncome));
  return {
    currency,
    columnLabels: columns.map((c) => c.label),
    lines,
    assets: sheets.map((s) => s.assets),
    liabilities: sheets.map((s) => s.liabilities),
    equity: sheets.map((s) => s.equity),
    balanced: sheets.map((s) => s.balanced),
  };
}

/** Sum a class's natural amount for a single trial balance (helper for callers). */
export function classTotal(tb: TrialBalance, cls: AccountClass): Money {
  return sumNatural(entriesOfClass(tb, cls), tb.currency);
}
