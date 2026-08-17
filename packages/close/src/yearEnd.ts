/**
 * Fiscal-year configuration and the year-end close-to-Retained-Earnings roll.
 *
 * At year end, every revenue and expense account is zeroed and the net (the
 * year's net income or loss) is rolled into Retained Earnings — so the next year
 * starts with a clean P&L and the equity section carries the accumulated result.
 * This builds the single balanced closing journal that does it, straight from a
 * trial balance:
 *
 *   Dr  each revenue account   (by its credit balance)
 *     Cr  each expense account   (by its debit balance)
 *     Cr  Retained Earnings      (net income)      — or Dr on a net loss
 *
 * Contra balances are handled by sign, so a revenue account that happens to
 * carry a debit balance still zeroes correctly. The entry is balanced by
 * construction and idempotent (keyed by fiscal year), so re-running the close
 * never double-posts.
 */

import {
  Money,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  AccountType,
  type AccountId,
  type Currency,
  type JournalLineInput,
  type PostCommand,
  type Provenance,
  type TenantId,
  type TrialBalance,
} from "@rgnr8/ledger-kernel";

/** Fiscal-year configuration: the calendar month (1–12) the fiscal year starts. */
export interface FiscalYearConfig {
  /** 1 = January (calendar year). 4 = fiscal year starting April 1, etc. */
  readonly startMonth: number;
}

export const CALENDAR_YEAR: FiscalYearConfig = { startMonth: 1 };

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}

/**
 * Inclusive ISO date bounds of the fiscal year *labeled* `fiscalYear` (the
 * calendar year in which it ends). For a calendar-year config this is
 * `YYYY-01-01 .. YYYY-12-31`; for a July-start fiscal year, FY2026 runs
 * `2025-07-01 .. 2026-06-30`.
 */
export function fiscalYearBounds(fiscalYear: number, config: FiscalYearConfig = CALENDAR_YEAR): {
  from: string;
  to: string;
} {
  const startMonth = config.startMonth;
  if (startMonth === 1) {
    return { from: `${fiscalYear}-01-01`, to: `${fiscalYear}-12-31` };
  }
  // Non-calendar FY ends in `fiscalYear`; it started startMonth of the prior year.
  const startYear = fiscalYear - 1;
  const from = `${startYear}-${pad2(startMonth)}-01`;
  // End = day before the start month, one year later.
  const endMonth = startMonth - 1; // 1..12 (startMonth>=2 here)
  const lastDay = lastDayOfMonth(fiscalYear, endMonth);
  const to = `${fiscalYear}-${pad2(endMonth)}-${pad2(lastDay)}`;
  return { from, to };
}

function lastDayOfMonth(year: number, month1to12: number): number {
  // month1to12 in [1,12]; day 0 of next month = last day of this month.
  const days = [31, isLeap(year) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return days[month1to12 - 1]!;
}
function isLeap(y: number): boolean {
  return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
}

export interface YearEndCloseOptions {
  readonly tenantId: string;
  readonly currency: Currency;
  /** The Retained Earnings equity account net income rolls into. */
  readonly retainedEarningsId: AccountId;
  /** Entry date for the closing journal (usually the fiscal year-end date). */
  readonly entryDate: string;
  /** Fiscal year label (used for the period key + idempotency). */
  readonly fiscalYear: number;
  readonly provenance: Provenance;
  readonly memo?: string;
}

export interface YearEndClose {
  readonly command: PostCommand;
  readonly netIncome: Money;
  /** Revenue + expense accounts that were zeroed. */
  readonly closedAccounts: readonly AccountId[];
}

/**
 * Build the year-end closing journal from a fiscal-year trial balance. The trial
 * balance should be the *full-year* P&L position (a `{from,to}` window over the
 * fiscal year). Only revenue and expense rows participate; balance-sheet
 * accounts are untouched.
 */
export function buildYearEndClose(tb: TrialBalance, opts: YearEndCloseOptions): YearEndClose {
  const currency = opts.currency;
  const zero = Money.zero(currency);
  const lines: JournalLineInput[] = [];
  const closedAccounts: AccountId[] = [];

  let debitTotal = zero;
  let creditTotal = zero;

  for (const row of tb.rows) {
    if (row.type !== AccountType.REVENUE && row.type !== AccountType.EXPENSE) continue;
    // Signed debit-positive balance for this P&L account.
    const signed = row.debit.minus(row.credit);
    if (signed.isZero()) continue;
    closedAccounts.push(row.accountId);
    if (signed.isNegative()) {
      // Credit balance (normal for revenue): zero it with a DEBIT.
      const amt = signed.negate();
      lines.push({ accountId: row.accountId, side: "DEBIT", amount: amt, memo: "Close to retained earnings" });
      debitTotal = debitTotal.plus(amt);
    } else {
      // Debit balance (normal for expense): zero it with a CREDIT.
      lines.push({ accountId: row.accountId, side: "CREDIT", amount: signed, memo: "Close to retained earnings" });
      creditTotal = creditTotal.plus(signed);
    }
  }

  // Net income = total revenue (debits here) − total expense (credits here).
  const netIncome = debitTotal.minus(creditTotal);
  // Balance the entry with Retained Earnings.
  if (!netIncome.isZero()) {
    if (!netIncome.isNegative()) {
      // Net income → credit Retained Earnings.
      lines.push({ accountId: opts.retainedEarningsId, side: "CREDIT", amount: netIncome, memo: "Net income to retained earnings" });
    } else {
      // Net loss → debit Retained Earnings.
      lines.push({ accountId: opts.retainedEarningsId, side: "DEBIT", amount: netIncome.negate(), memo: "Net loss to retained earnings" });
    }
  }

  const tenantId: TenantId = asTenantId(opts.tenantId);
  const command: PostCommand = {
    tenantId,
    idempotencyKey: asIdempotencyKey(`yearend:${opts.fiscalYear}`),
    periodKey: asPeriodKey(opts.entryDate.slice(0, 7)),
    currency,
    entryDate: opts.entryDate,
    memo: opts.memo ?? `Year-end close FY${opts.fiscalYear}`,
    provenance: opts.provenance,
    lines,
  };

  return { command, netIncome, closedAccounts };
}
