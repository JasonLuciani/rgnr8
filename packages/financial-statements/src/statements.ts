/**
 * The three financial statements, built from signed trial balances.
 *
 * Every total is exact integer `Money` (bigint minor units). The statements are
 * balanced by construction: on a sound ledger the balance sheet nets to zero and
 * the cash-flow sections reconcile to the cash account's movement. Each shape
 * carries an explicit residual / reconciled flag so callers can assert the
 * invariant, and `assert*` helpers throw when it is violated.
 */

import { Money, asAccountId } from "@rgnr8/ledger-kernel";
import type { AccountId, Currency } from "@rgnr8/ledger-kernel";
import {
  entriesOfClass,
  indexById,
  naturalAmount,
  sumNatural,
  type AccountClass,
  type TrialBalance,
  type TrialBalanceEntry,
} from "./accounts.js";

/** A single presentation line on a statement. */
export interface StatementLine {
  readonly accountId: AccountId;
  readonly code: string;
  readonly name: string;
  readonly accountClass: AccountClass;
  /** Positive-natural amount for the account's class. */
  readonly amount: Money;
}

function toLine(e: TrialBalanceEntry): StatementLine {
  return {
    accountId: e.accountId,
    code: e.code,
    name: e.name,
    accountClass: e.accountClass,
    amount: naturalAmount(e),
  };
}

function byLineCode(a: StatementLine, b: StatementLine): number {
  return a.code.localeCompare(b.code);
}

// --- Income statement (P&L) --------------------------------------------------

export interface IncomeStatement {
  readonly currency: Currency;
  readonly revenue: Money;
  readonly expenses: Money;
  readonly lines: readonly StatementLine[];
  readonly netIncome: Money;
}

/** Income statement for a period from its trial balance. */
export function incomeStatement(tb: TrialBalance): IncomeStatement {
  const revLines = entriesOfClass(tb, "revenue");
  const expLines = entriesOfClass(tb, "expense");
  const revenue = sumNatural(revLines, tb.currency);
  const expenses = sumNatural(expLines, tb.currency);
  const lines = [...revLines, ...expLines].map(toLine).sort(byLineCode);
  return {
    currency: tb.currency,
    revenue,
    expenses,
    lines,
    netIncome: revenue.minus(expenses),
  };
}

// --- Balance sheet -----------------------------------------------------------

/** Synthetic account id for the current-period earnings line on the BS. */
export const NET_INCOME_ACCOUNT_ID: AccountId = asAccountId("__CURRENT_PERIOD_NET_INCOME__");
export const RETAINED_EARNINGS_ACCOUNT_ID: AccountId = asAccountId("__RETAINED_EARNINGS_PRIOR__");

export interface BalanceSheet {
  readonly currency: Currency;
  readonly assets: Money;
  readonly liabilities: Money;
  /** Booked equity plus the current-period net income folded into equity. */
  readonly equity: Money;
  readonly lines: readonly StatementLine[];
  /** assets - (liabilities + equity); zero on a balanced sheet. */
  readonly residual: Money;
  readonly balanced: boolean;
}

/**
 * Balance sheet from a trial balance, folding retained earnings into equity.
 *
 * A balance sheet is drawn as-of a date, so its equity has to carry **all**
 * earnings the business has retained through that date — not just the current
 * period's. RGNR8 posts no automatic period-close entry (a period *lock* moves
 * nothing), so revenue and expense earned before the reporting window are never
 * swept into a retained-earnings account and would otherwise vanish from the
 * sheet, leaving it out of balance by exactly the accumulated prior net income.
 *
 * `netIncome` is the reporting period's result; `beginningRetained` is the
 * cumulative net income earned through the day before the window opened. Both
 * fold into equity. Callers reporting from inception (no prior activity) can omit
 * `beginningRetained` — it defaults to zero and the behaviour is unchanged. On a
 * sound ledger this balances to exactly zero: assets = liabilities + equity.
 */
export function balanceSheet(
  tb: TrialBalance, netIncome: Money, beginningRetained?: Money,
): BalanceSheet {
  const assetLines = entriesOfClass(tb, "asset");
  const liabLines = entriesOfClass(tb, "liability");
  const equityLines = entriesOfClass(tb, "equity");

  const priorRetained = beginningRetained ?? Money.fromMinorUnits(0n, tb.currency);
  const assets = sumNatural(assetLines, tb.currency);
  const liabilities = sumNatural(liabLines, tb.currency);
  const bookedEquity = sumNatural(equityLines, tb.currency);
  const equity = bookedEquity.plus(priorRetained).plus(netIncome);

  const residual = assets.minus(liabilities.plus(equity));

  const niLine: StatementLine = {
    accountId: NET_INCOME_ACCOUNT_ID,
    code: "3999",
    name: "Current-period net income",
    accountClass: "equity",
    amount: netIncome,
  };
  // Only present a prior-earnings line when there is prior activity, so a
  // from-inception sheet reads exactly as it did before.
  const retainedLine: StatementLine = {
    accountId: RETAINED_EARNINGS_ACCOUNT_ID,
    code: "3998",
    name: "Retained earnings (prior periods)",
    accountClass: "equity",
    amount: priorRetained,
  };
  const lines = [
    ...assetLines.map(toLine),
    ...liabLines.map(toLine),
    ...equityLines.map(toLine),
  ]
    .sort(byLineCode)
    .concat(priorRetained.isZero() ? [niLine] : [retainedLine, niLine]);

  return {
    currency: tb.currency,
    assets,
    liabilities,
    equity,
    lines,
    residual,
    balanced: residual.isZero(),
  };
}

/** Throw unless the balance sheet balances to exactly zero (integer money). */
export function assertBalanceSheetBalances(bs: BalanceSheet): void {
  if (!bs.balanced) {
    throw new Error(
      `Balance sheet does not balance: assets ${bs.assets.toString()} vs ` +
        `liabilities+equity ${bs.liabilities.plus(bs.equity).toString()} ` +
        `(residual ${bs.residual.toString()})`,
    );
  }
}

// --- Cash flow (indirect) ----------------------------------------------------

export type CashFlowSection = "operating" | "investing" | "financing";

/**
 * Decides which accounts are cash, and which section every non-cash
 * balance-sheet account's movement belongs to. Revenue/expense accounts are
 * never asked about — they are represented by net income in the operating
 * section (folding them in again would double-count).
 */
export interface CashFlowClassifier {
  isCash(entry: TrialBalanceEntry): boolean;
  section(entry: TrialBalanceEntry): CashFlowSection;
}

/**
 * Sensible defaults over a standard chart:
 *  - cash: asset accounts coded 10xx,
 *  - operating: accumulated depreciation (a contra-asset whose movement is the
 *    non-cash depreciation add-back) — detected by name so it isn't swept into
 *    investing with the gross PP&E accounts,
 *  - investing: long-lived asset accounts coded 15xx–19xx (PP&E),
 *  - financing: long-term liabilities coded 25xx–29xx (debt) and all equity,
 *  - everything else (AR, AP, other working capital): operating.
 */
const ACCUMULATED_DEPRECIATION_NAME = /accumulated deprec|accum\.?\s*deprec/i;

export const defaultCashFlowClassifier: CashFlowClassifier = {
  isCash: (e) => e.accountClass === "asset" && /^10/.test(e.code),
  section: (e) => {
    if (e.accountClass === "equity") return "financing";
    // Accumulated depreciation is a contra-asset: keep its depreciation add-back
    // in operating even though it sits in the PP&E code range.
    if (e.accountClass === "asset" && ACCUMULATED_DEPRECIATION_NAME.test(e.name)) return "operating";
    if (e.accountClass === "asset" && /^1[5-9]/.test(e.code)) return "investing";
    if (e.accountClass === "liability" && /^2[5-9]/.test(e.code)) return "financing";
    return "operating";
  },
};

export interface CashFlowStatement {
  readonly currency: Currency;
  /** Net income + non-cash working-capital changes. */
  readonly operating: Money;
  readonly investing: Money;
  readonly financing: Money;
  /** operating + investing + financing; equals the cash account movement. */
  readonly netChange: Money;
  readonly beginningCash: Money;
  readonly endingCash: Money;
  /** True when the three sections sum to the actual cash movement. */
  readonly reconciled: boolean;
}

/**
 * Indirect cash-flow statement between two trial balances.
 *
 * Method: for every non-cash account, its contribution to cash over the period
 * is -(Δ signed balance). Summed across all non-cash accounts this equals the
 * cash account's movement (the ledger nets to zero in both periods). Revenue and
 * expense accounts' contributions collapse to the period net income, so we seed
 * the operating section with `netIncome` and add only the non-cash
 * working-capital changes — the classic indirect presentation. investing and
 * financing collect their accounts' contributions. `netChange` is measured
 * directly from the cash accounts, and `reconciled` confirms the sections tie to
 * it (which holds exactly when the supplied `netIncome` is the period's P&L
 * result).
 */
export function cashFlow(
  tbStart: TrialBalance,
  tbEnd: TrialBalance,
  netIncome: Money,
  classifier: CashFlowClassifier = defaultCashFlowClassifier,
): CashFlowStatement {
  const currency = tbEnd.currency;
  const zero = Money.zero(currency);
  const startById = indexById(tbStart);
  const endById = indexById(tbEnd);

  const ids = new Set<AccountId>([...startById.keys(), ...endById.keys()]);

  let beginningCash = zero;
  let endingCash = zero;
  let operatingWorkingCapital = zero;
  let investing = zero;
  let financing = zero;

  for (const id of ids) {
    const start = startById.get(id);
    const end = endById.get(id);
    const rep = end ?? start;
    if (!rep) continue;

    const signedStart = start ? start.signed : zero;
    const signedEnd = end ? end.signed : zero;

    if (classifier.isCash(rep)) {
      // Cash asset: natural amount equals the signed balance.
      beginningCash = beginningCash.plus(signedStart);
      endingCash = endingCash.plus(signedEnd);
      continue;
    }

    // Revenue/expense are represented by net income; skip to avoid double count.
    if (rep.accountClass === "revenue" || rep.accountClass === "expense") continue;

    // Contribution to cash of a non-cash balance-sheet account = -(Δ signed).
    const contribution = signedStart.minus(signedEnd);
    switch (classifier.section(rep)) {
      case "investing":
        investing = investing.plus(contribution);
        break;
      case "financing":
        financing = financing.plus(contribution);
        break;
      case "operating":
        operatingWorkingCapital = operatingWorkingCapital.plus(contribution);
        break;
    }
  }

  const operating = netIncome.plus(operatingWorkingCapital);
  const netChange = endingCash.minus(beginningCash);
  const sectionsSum = operating.plus(investing).plus(financing);

  return {
    currency,
    operating,
    investing,
    financing,
    netChange,
    beginningCash,
    endingCash,
    reconciled: sectionsSum.equals(netChange),
  };
}

/** Throw unless the cash-flow sections reconcile to the cash account movement. */
export function assertCashFlowReconciles(cf: CashFlowStatement): void {
  if (!cf.reconciled) {
    const sections = cf.operating.plus(cf.investing).plus(cf.financing);
    throw new Error(
      `Cash flow does not reconcile: sections ${sections.toString()} vs ` +
        `cash movement ${cf.netChange.toString()}`,
    );
  }
}

// --- Period-over-period comparison ------------------------------------------

export interface Variance {
  readonly current: Money;
  readonly prior: Money;
  /** current - prior. */
  readonly variance: Money;
  /** Percent change vs prior, to two decimals; null when prior is zero. */
  readonly variancePct: number | null;
}

export interface VarianceLine extends Variance {
  readonly accountId: AccountId;
  readonly code: string;
  readonly name: string;
  readonly accountClass: AccountClass;
}

export interface PeriodComparison {
  readonly currency: Currency;
  readonly lines: readonly VarianceLine[];
  readonly revenue: Variance;
  readonly expenses: Variance;
  readonly netIncome: Variance;
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}

function varianceOf(current: Money, prior: Money): Variance {
  const variance = current.minus(prior);
  const variancePct = prior.isZero()
    ? null
    : round2((Number(variance.minorUnits) / Number(prior.minorUnits)) * 100);
  return { current, prior, variance, variancePct };
}

/**
 * Line-level income-statement variance between a current and a prior period.
 * Accounts present in only one period are compared against zero.
 */
export function periodCompare(
  current: TrialBalance,
  prior: TrialBalance,
): PeriodComparison {
  const currency = current.currency;
  const zero = Money.zero(currency);
  const cur = incomeStatement(current);
  const pri = incomeStatement(prior);

  const curLines = new Map<AccountId, StatementLine>();
  for (const l of cur.lines) curLines.set(l.accountId, l);
  const priLines = new Map<AccountId, StatementLine>();
  for (const l of pri.lines) priLines.set(l.accountId, l);

  const ids = new Set<AccountId>([...curLines.keys(), ...priLines.keys()]);
  const lines: VarianceLine[] = [];
  for (const id of ids) {
    const c = curLines.get(id);
    const p = priLines.get(id);
    const rep = c ?? p;
    if (!rep) continue;
    const currentAmt = c ? c.amount : zero;
    const priorAmt = p ? p.amount : zero;
    lines.push({
      accountId: rep.accountId,
      code: rep.code,
      name: rep.name,
      accountClass: rep.accountClass,
      ...varianceOf(currentAmt, priorAmt),
    });
  }
  lines.sort((a, b) => a.code.localeCompare(b.code));

  return {
    currency,
    lines,
    revenue: varianceOf(cur.revenue, pri.revenue),
    expenses: varianceOf(cur.expenses, pri.expenses),
    netIncome: varianceOf(cur.netIncome, pri.netIncome),
  };
}
