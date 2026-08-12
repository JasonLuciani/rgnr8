import {
  Money,
  RG_BASE_CSS,
  RG_TOKENS_CSS,
  brandBar,
  type Currency,
} from "@rgnr8/ledger-kernel";
import type { BalanceSheet, IncomeStatement } from "@rgnr8/statements";

/**
 * Statement-level parallel-close compare: hold RGNR8's ledger-derived income
 * statement and balance sheet against the totals QuickBooks reports. The
 * trial-balance diff (compare.ts) proves account-by-account tie-out; this proves
 * the *statements an owner actually reads* match — net income, revenue, expenses,
 * total assets, total liabilities + equity — which is what a controller signs.
 * All comparisons are in integer minor units; tolerance is in minor units.
 */

export type LineStatus = "match" | "mismatch";

export interface StatementLine {
  readonly label: string;
  readonly rgnr8: Money;
  readonly qbo: Money;
  readonly delta: Money; // rgnr8 − qbo
  readonly status: LineStatus;
}

export interface StatementDiff {
  readonly title: string;
  readonly currency: string;
  readonly lines: readonly StatementLine[];
  readonly inAgreement: boolean;
  readonly totalAbsDelta: Money;
}

export interface StatementCompareOptions {
  readonly currency: Currency;
  readonly toleranceMinor?: bigint;
}

export interface QboIncomeTotals {
  readonly revenue: string; // decimal strings, as QBO reports them
  readonly expenses: string;
  readonly netIncome: string;
}

export interface QboBalanceTotals {
  readonly totalAssets: string;
  readonly totalLiabilitiesAndEquity: string;
  readonly netIncome?: string;
}

function line(label: string, rgnr8: Money, qbo: Money, tol: bigint): StatementLine {
  const delta = rgnr8.minus(qbo);
  const abs = delta.minorUnits < 0n ? -delta.minorUnits : delta.minorUnits;
  return { label, rgnr8, qbo, delta, status: abs <= tol ? "match" : "mismatch" };
}

function assemble(title: string, lines: StatementLine[], currency: Currency): StatementDiff {
  let totalAbs = 0n;
  for (const l of lines) {
    const abs = l.delta.minorUnits < 0n ? -l.delta.minorUnits : l.delta.minorUnits;
    totalAbs += abs;
  }
  return {
    title,
    currency: currency.code,
    lines,
    inAgreement: lines.every((l) => l.status === "match"),
    totalAbsDelta: Money.fromMinorUnits(totalAbs, currency),
  };
}

export function compareIncomeStatement(
  rgnr8: IncomeStatement,
  qbo: QboIncomeTotals,
  opts: StatementCompareOptions,
): StatementDiff {
  const cur = opts.currency;
  const tol = opts.toleranceMinor ?? 0n;
  const lines = [
    line("Revenue", rgnr8.revenue.total, Money.fromDecimal(qbo.revenue, cur), tol),
    line("Expenses", rgnr8.expenses.total, Money.fromDecimal(qbo.expenses, cur), tol),
    line("Net income", rgnr8.netIncome, Money.fromDecimal(qbo.netIncome, cur), tol),
  ];
  return assemble("Income statement", lines, cur);
}

export function compareBalanceSheet(
  rgnr8: BalanceSheet,
  qbo: QboBalanceTotals,
  opts: StatementCompareOptions,
): StatementDiff {
  const cur = opts.currency;
  const tol = opts.toleranceMinor ?? 0n;
  const lines = [
    line("Total assets", rgnr8.totalAssets, Money.fromDecimal(qbo.totalAssets, cur), tol),
    line(
      "Total liabilities + equity",
      rgnr8.totalLiabilitiesAndEquity,
      Money.fromDecimal(qbo.totalLiabilitiesAndEquity, cur),
      tol,
    ),
  ];
  if (qbo.netIncome !== undefined) {
    lines.push(line("Net income", rgnr8.netIncome, Money.fromDecimal(qbo.netIncome, cur), tol));
  }
  return assemble("Balance sheet", lines, cur);
}

export interface StatementsDiff {
  readonly income: StatementDiff;
  readonly balance: StatementDiff;
  readonly inAgreement: boolean;
}

export function compareStatements(
  income: IncomeStatement,
  balance: BalanceSheet,
  qbo: { readonly income: QboIncomeTotals; readonly balance: QboBalanceTotals },
  opts: StatementCompareOptions,
): StatementsDiff {
  const incomeDiff = compareIncomeStatement(income, qbo.income, opts);
  const balanceDiff = compareBalanceSheet(balance, qbo.balance, opts);
  return {
    income: incomeDiff,
    balance: balanceDiff,
    inAgreement: incomeDiff.inAgreement && balanceDiff.inAgreement,
  };
}

// --- HTML render -------------------------------------------------------------

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function tableFor(diff: StatementDiff): string {
  const rows = diff.lines
    .map(
      (l) => `<tr>
      <td>${esc(l.label)}</td>
      <td class="num">${esc(l.rgnr8.toDecimalString())}</td>
      <td class="num">${esc(l.qbo.toDecimalString())}</td>
      <td class="num">${esc(l.delta.toDecimalString())}</td>
      <td><span class="dot" style="background:${l.status === "match" ? "#0ca30c" : "#d03b3b"}"></span>${l.status}</td>
    </tr>`,
    )
    .join("\n");
  return `<h2>${esc(diff.title)} ${diff.inAgreement ? "✓" : "✗"}</h2>
  <table>
    <thead><tr><th>Line</th><th class="num">RGNR8</th><th class="num">QuickBooks</th><th class="num">Δ</th><th>Status</th></tr></thead>
    <tbody>
${rows}
    </tbody>
  </table>`;
}

export function renderStatementsDiffHtml(diff: StatementsDiff): string {
  const banner = diff.inAgreement
    ? `<div class="banner good">Statements agree — RGNR8's P&amp;L and balance sheet tie to QuickBooks.</div>`
    : `<div class="banner warn">Statement totals diverge from QuickBooks — see the highlighted lines.</div>`;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 vs QuickBooks — statements</title>
<style>
${RG_TOKENS_CSS}
${RG_BASE_CSS}
  .wrap{max-width:840px;margin:0 auto;padding:24px 20px 56px}
  h1{font-size:22px;margin:0 0 12px;font-weight:800;letter-spacing:-.01em}
  h2{font-size:11px;margin:24px 0 8px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--rg-muted)}
  .banner{padding:12px 16px;border-radius:var(--rg-radius);margin-bottom:14px;font-weight:700;font-size:13px;border:1px solid transparent}
  .warn{background:#FDF3E4;color:#8A5A00;border-color:#F3DCA6}
  .good{background:#E7F6EF;color:#0A6E4B;border-color:#BFE7D6}
  table{border-collapse:separate;border-spacing:0;width:100%;background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:var(--rg-radius);overflow:hidden;box-shadow:var(--rg-shadow)}
  th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--rg-line);font-size:13.5px}
  thead th{background:var(--rg-surface);font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-muted)}
  tbody tr:last-child td{border-bottom:none}
  .num{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
</style></head>
<body>
${brandBar("Parallel close · statements")}
<div class="wrap">
  <h1>RGNR8 vs QuickBooks — statements</h1>
  ${banner}
  ${tableFor(diff.income)}
  ${tableFor(diff.balance)}
</div>
</body></html>`;
}
