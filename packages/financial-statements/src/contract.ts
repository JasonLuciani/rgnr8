/**
 * Cross-language JSON contract emitter: `financial-statements/1`.
 *
 * The TS `@rgnr8/financial-statements` package OWNS the numbers; the Python
 * `rgnr8-reports` package RENDERS them. This module serialises the three
 * statements into a plain, language-neutral object with integer minor units so
 * either side agrees on the byte-shape. Amounts are cents at SMB scale, well
 * within `Number.MAX_SAFE_INTEGER`, so minor units are emitted as JS numbers
 * (not strings) — consistently throughout.
 *
 * The producer is a pure function: given the statement structures this package
 * already computes, it returns a fresh plain object with no `Money`/`bigint`
 * leaking across the boundary.
 */

import type { Money } from "@rgnr8/ledger-kernel";
import type {
  BalanceSheet,
  CashFlowStatement,
  IncomeStatement,
  StatementLine,
} from "./statements.js";

/** The contract identifier this module emits. */
export const FINANCIAL_STATEMENTS_CONTRACT = "financial-statements/1" as const;

/** A single serialised statement line: a human label and integer minor units. */
export interface ContractLine {
  readonly label: string;
  readonly amount_minor: number;
}

export interface ContractIncomeStatement {
  readonly revenue: readonly ContractLine[];
  readonly expenses: readonly ContractLine[];
  readonly total_revenue: number;
  readonly total_expenses: number;
  readonly net_income: number;
}

export interface ContractBalanceSheet {
  readonly assets: readonly ContractLine[];
  readonly liabilities: readonly ContractLine[];
  readonly equity: readonly ContractLine[];
  readonly total_assets: number;
  readonly total_liabilities: number;
  readonly total_equity: number;
  readonly balanced: boolean;
}

export interface ContractCashFlow {
  readonly operating: readonly ContractLine[];
  readonly investing: readonly ContractLine[];
  readonly financing: readonly ContractLine[];
  readonly net_change: number;
  readonly ending_cash: number;
}

export interface FinancialStatementsContract {
  readonly contract: typeof FINANCIAL_STATEMENTS_CONTRACT;
  readonly period: string;
  readonly currency: string;
  readonly income_statement: ContractIncomeStatement;
  readonly balance_sheet: ContractBalanceSheet;
  readonly cash_flow: ContractCashFlow;
}

export interface FinancialStatementsInput {
  readonly period: string;
  readonly currency: string;
  readonly income: IncomeStatement;
  readonly balanceSheet: BalanceSheet;
  readonly cashFlow: CashFlowStatement;
}

/**
 * Convert `Money` (bigint minor units) to a JS number, asserting it is exactly
 * representable. SMB-scale cent amounts are far below the limit; this guard
 * catches any pathological value before it silently loses precision.
 */
export function minorToNumber(m: Money): number {
  const minor = m.minorUnits;
  if (minor > BigInt(Number.MAX_SAFE_INTEGER) || minor < BigInt(Number.MIN_SAFE_INTEGER)) {
    throw new RangeError(
      `Money ${minor.toString()} exceeds Number.MAX_SAFE_INTEGER; cannot emit as a JS number`,
    );
  }
  return Number(minor);
}

function toContractLine(l: StatementLine): ContractLine {
  return { label: l.name, amount_minor: minorToNumber(l.amount) };
}

function linesOfClass(
  lines: readonly StatementLine[],
  cls: StatementLine["accountClass"],
): ContractLine[] {
  return lines.filter((l) => l.accountClass === cls).map(toContractLine);
}

/**
 * Produce the `financial-statements/1` contract object from this package's
 * computed statements. Pure: no mutation of the inputs, no shared state.
 *
 * The cash-flow statement carries section aggregates (operating/investing/
 * financing) rather than per-line detail, so each section is emitted as a
 * single summary line — still an array of `{label, amount_minor}`, matching the
 * contract's line shape.
 */
export function financialStatementsJson(
  input: FinancialStatementsInput,
): FinancialStatementsContract {
  const { period, currency, income, balanceSheet: bs, cashFlow: cf } = input;

  return {
    contract: FINANCIAL_STATEMENTS_CONTRACT,
    period,
    currency,
    income_statement: {
      revenue: linesOfClass(income.lines, "revenue"),
      expenses: linesOfClass(income.lines, "expense"),
      total_revenue: minorToNumber(income.revenue),
      total_expenses: minorToNumber(income.expenses),
      net_income: minorToNumber(income.netIncome),
    },
    balance_sheet: {
      assets: linesOfClass(bs.lines, "asset"),
      liabilities: linesOfClass(bs.lines, "liability"),
      equity: linesOfClass(bs.lines, "equity"),
      total_assets: minorToNumber(bs.assets),
      total_liabilities: minorToNumber(bs.liabilities),
      total_equity: minorToNumber(bs.equity),
      balanced: bs.balanced,
    },
    cash_flow: {
      operating: [{ label: "Operating activities", amount_minor: minorToNumber(cf.operating) }],
      investing: [{ label: "Investing activities", amount_minor: minorToNumber(cf.investing) }],
      financing: [{ label: "Financing activities", amount_minor: minorToNumber(cf.financing) }],
      net_change: minorToNumber(cf.netChange),
      ending_cash: minorToNumber(cf.endingCash),
    },
  };
}

/** Recursively sort object keys so the serialisation is stable/deterministic. */
function sortKeys(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortKeys);
  if (value !== null && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value as Record<string, unknown>).sort()) {
      out[key] = sortKeys((value as Record<string, unknown>)[key]);
    }
    return out;
  }
  return value;
}

/**
 * `JSON.stringify` of the contract with keys sorted recursively, for stable
 * byte output (e.g. golden files, cross-language fixture agreement).
 */
export function financialStatementsJsonString(input: FinancialStatementsInput): string {
  return JSON.stringify(sortKeys(financialStatementsJson(input)));
}
