/**
 * Cash-basis reporting toggle.
 *
 * The ledger is accrual (an invoice books revenue + AR before any cash moves).
 * Cash basis recognizes revenue/expense only when cash changes hands. The exact,
 * standard way to convert an accrual income statement to cash basis is the
 * indirect adjustment by the change in the accrual balance-sheet accounts:
 *
 *   cash-basis revenue  = accrual revenue  − Δ(accounts receivable)
 *   cash-basis expense  = accrual expense  − Δ(accounts payable)
 *
 * An invoice raises AR without cash, so backing out the AR increase removes the
 * revenue that hasn't been collected; collecting it later lowers AR and lets the
 * revenue through — precisely the cash-basis timing. The same logic applies to
 * AP for expenses. (This is what QuickBooks does when you flip a report to cash
 * basis: it re-sources each transaction, which nets to exactly this adjustment.)
 *
 * `Δ` is measured as the change in *natural* balance between the period's start
 * and end as-of trial balances, so an increase in AR/AP reads positive.
 */

import { Money } from "@rgnr8/ledger-kernel";
import type { AccountId, Currency } from "@rgnr8/ledger-kernel";
import { indexById, naturalAmount, type TrialBalance } from "./accounts.js";
import { incomeStatement, type IncomeStatement } from "./statements.js";

/** Sum the natural change (end − start) across a set of accounts. */
function naturalChange(
  start: TrialBalance,
  end: TrialBalance,
  ids: readonly AccountId[],
  currency: Currency,
): Money {
  const s = indexById(start);
  const e = indexById(end);
  let change = Money.zero(currency);
  for (const id of ids) {
    const se = s.get(id);
    const ee = e.get(id);
    const startNat = se ? naturalAmount(se) : Money.zero(currency);
    const endNat = ee ? naturalAmount(ee) : Money.zero(currency);
    change = change.plus(endNat.minus(startNat));
  }
  return change;
}

export interface CashBasisAccounts {
  /** Accounts-receivable accounts (revenue is deferred until these collect). */
  readonly receivableIds: readonly AccountId[];
  /** Accounts-payable accounts (expense is deferred until these are paid). */
  readonly payableIds: readonly AccountId[];
}

export interface CashBasisIncomeStatement {
  readonly currency: Currency;
  readonly accrualRevenue: Money;
  readonly accrualExpenses: Money;
  /** Δ receivables backed out of revenue. */
  readonly receivableChange: Money;
  /** Δ payables backed out of expenses. */
  readonly payableChange: Money;
  readonly revenue: Money;
  readonly expenses: Money;
  readonly netIncome: Money;
}

/**
 * Convert an accrual period income statement to cash basis.
 *
 * @param periodTb  the period's accrual trial balance (P&L activity, `{from,to}`)
 * @param startTb   as-of trial balance at the period start (for opening AR/AP)
 * @param endTb     as-of trial balance at the period end (for closing AR/AP)
 * @param accounts  which accounts are receivable / payable
 */
export function cashBasisIncomeStatement(
  periodTb: TrialBalance,
  startTb: TrialBalance,
  endTb: TrialBalance,
  accounts: CashBasisAccounts,
): CashBasisIncomeStatement {
  const currency = periodTb.currency;
  const accrual: IncomeStatement = incomeStatement(periodTb);
  const receivableChange = naturalChange(startTb, endTb, accounts.receivableIds, currency);
  const payableChange = naturalChange(startTb, endTb, accounts.payableIds, currency);

  const revenue = accrual.revenue.minus(receivableChange);
  const expenses = accrual.expenses.minus(payableChange);
  return {
    currency,
    accrualRevenue: accrual.revenue,
    accrualExpenses: accrual.expenses,
    receivableChange,
    payableChange,
    revenue,
    expenses,
    netIncome: revenue.minus(expenses),
  };
}
