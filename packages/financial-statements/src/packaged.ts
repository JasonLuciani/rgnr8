/**
 * Adapters that turn the computed statements into the minor-unit-string shapes a
 * sealed financial package stores. Keeping this here (rather than in `close`)
 * lets the close package stay a dependency-light leaf: it defines the packaged
 * shape, and this maps our rich statements onto it so a closed period can seal
 * the *full* published statements — every line — not just the summary totals.
 */

import type { BalanceSheet, IncomeStatement, StatementLine } from "./statements.js";

export interface PackagedStatementLine {
  readonly code: string;
  readonly name: string;
  readonly amountMinor: string;
}

export interface PackagedIncomeStatementFull {
  readonly revenueMinor: string;
  readonly expensesMinor: string;
  readonly netIncomeMinor: string;
  readonly revenueLines: readonly PackagedStatementLine[];
  readonly expenseLines: readonly PackagedStatementLine[];
}

export interface PackagedBalanceSheetFull {
  readonly totalAssetsMinor: string;
  readonly totalLiabilitiesAndEquityMinor: string;
  readonly netIncomeMinor: string;
  readonly balances: boolean;
  readonly assetLines: readonly PackagedStatementLine[];
  readonly liabilityLines: readonly PackagedStatementLine[];
  readonly equityLines: readonly PackagedStatementLine[];
}

function packLine(l: StatementLine): PackagedStatementLine {
  return { code: l.code, name: l.name, amountMinor: l.amount.minorUnits.toString() };
}
function packClass(lines: readonly StatementLine[], cls: StatementLine["accountClass"]): PackagedStatementLine[] {
  return lines.filter((l) => l.accountClass === cls).map(packLine);
}

/** Map an income statement onto the sealed, full-line packaged shape. */
export function packagedIncomeStatement(is: IncomeStatement): PackagedIncomeStatementFull {
  return {
    revenueMinor: is.revenue.minorUnits.toString(),
    expensesMinor: is.expenses.minorUnits.toString(),
    netIncomeMinor: is.netIncome.minorUnits.toString(),
    revenueLines: packClass(is.lines, "revenue"),
    expenseLines: packClass(is.lines, "expense"),
  };
}

/** Map a balance sheet onto the sealed, full-line packaged shape. */
export function packagedBalanceSheet(bs: BalanceSheet): PackagedBalanceSheetFull {
  const niLine = bs.lines.find((l) => l.code === "3999");
  const netIncome = niLine ? niLine.amount : bs.assets.minus(bs.assets);
  return {
    totalAssetsMinor: bs.assets.minorUnits.toString(),
    totalLiabilitiesAndEquityMinor: bs.liabilities.plus(bs.equity).minorUnits.toString(),
    netIncomeMinor: netIncome.minorUnits.toString(),
    balances: bs.balanced,
    assetLines: packClass(bs.lines, "asset"),
    liabilityLines: packClass(bs.lines, "liability"),
    equityLines: packClass(bs.lines, "equity"),
  };
}
