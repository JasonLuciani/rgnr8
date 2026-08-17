/**
 * A small, self-contained HTML renderer for the three statements. It reuses the
 * kernel's brand theme (fonts, colours, table styles) — no external assets — and
 * emits a single branded document. This is a simple view, not a typesetting
 * engine: totals and lines in accessible tables.
 */

import { Money, RG_THEME_CSS, brandBar } from "@rgnr8/ledger-kernel";
import type {
  BalanceSheet,
  CashFlowStatement,
  IncomeStatement,
  PeriodComparison,
  StatementLine,
} from "./statements.js";
import type { TrialBalance } from "./accounts.js";
import type { GlDetailAccount } from "./glDetail.js";
import type { RetainedEarnings } from "./retainedEarnings.js";

function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Format money as "USD 1,234.56", flagging negatives for styling. */
function fmt(m: Money): string {
  const dec = m.toDecimalString();
  const cls = m.isNegative() ? ' class="num neg"' : ' class="num"';
  return `<td${cls}>${esc(m.currency.code)}&nbsp;${esc(dec)}</td>`;
}

function pctCell(p: number | null): string {
  if (p === null) return '<td class="num muted">—</td>';
  const cls = p < 0 ? ' class="num neg"' : ' class="num"';
  return `<td${cls}>${p.toFixed(2)}%</td>`;
}

function lineRows(lines: readonly StatementLine[]): string {
  return lines
    .map(
      (l) =>
        `<tr><td>${esc(l.code)}</td><td>${esc(l.name)}</td>${fmt(l.amount)}</tr>`,
    )
    .join("");
}

function card(title: string, body: string): string {
  return `<section class="card"><h2 style="margin:0 0 10px;font:600 16px/1.2 var(--rg-sans)">${esc(
    title,
  )}</h2>${body}</section>`;
}

function totalRow(label: string, m: Money, strong = true): string {
  const w = strong ? "700" : "400";
  return `<tr><td colspan="2" style="font-weight:${w}">${esc(label)}</td>${fmt(m)}</tr>`;
}

/** Render an income statement fragment. */
export function renderIncomeStatement(is: IncomeStatement): string {
  const rev = is.lines.filter((l) => l.accountClass === "revenue");
  const exp = is.lines.filter((l) => l.accountClass === "expense");
  const body =
    '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th><th class="num">Amount</th></tr></thead><tbody>' +
    '<tr><td colspan="3" class="rg-eyebrow">Revenue</td></tr>' +
    lineRows(rev) +
    totalRow("Total revenue", is.revenue) +
    '<tr><td colspan="3" class="rg-eyebrow">Expenses</td></tr>' +
    lineRows(exp) +
    totalRow("Total expenses", is.expenses) +
    totalRow("Net income", is.netIncome) +
    "</tbody></table></div>";
  return card("Income Statement", body);
}

/** Render a balance sheet fragment. */
export function renderBalanceSheet(bs: BalanceSheet): string {
  const assets = bs.lines.filter((l) => l.accountClass === "asset");
  const liabs = bs.lines.filter((l) => l.accountClass === "liability");
  const equity = bs.lines.filter((l) => l.accountClass === "equity");
  const banner = bs.balanced
    ? '<div class="banner good">Balanced — assets = liabilities + equity</div>'
    : `<div class="banner warn">Out of balance — residual ${esc(bs.residual.toString())}</div>`;
  const body =
    banner +
    '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th><th class="num">Amount</th></tr></thead><tbody>' +
    '<tr><td colspan="3" class="rg-eyebrow">Assets</td></tr>' +
    lineRows(assets) +
    totalRow("Total assets", bs.assets) +
    '<tr><td colspan="3" class="rg-eyebrow">Liabilities</td></tr>' +
    lineRows(liabs) +
    totalRow("Total liabilities", bs.liabilities) +
    '<tr><td colspan="3" class="rg-eyebrow">Equity</td></tr>' +
    lineRows(equity) +
    totalRow("Total equity", bs.equity) +
    totalRow("Liabilities + equity", bs.liabilities.plus(bs.equity)) +
    "</tbody></table></div>";
  return card("Balance Sheet", body);
}

/** Render an indirect cash-flow fragment. */
export function renderCashFlow(cf: CashFlowStatement): string {
  const banner = cf.reconciled
    ? '<div class="banner good">Reconciled to cash movement</div>'
    : '<div class="banner warn">Sections do not reconcile to cash movement</div>';
  const body =
    banner +
    '<div class="table-scroll"><table><thead><tr><th>Section</th><th></th><th class="num">Amount</th></tr></thead><tbody>' +
    totalRow("Operating (net income + working capital)", cf.operating) +
    totalRow("Investing", cf.investing) +
    totalRow("Financing", cf.financing) +
    totalRow("Net change in cash", cf.netChange) +
    totalRow("Beginning cash", cf.beginningCash, false) +
    totalRow("Ending cash", cf.endingCash) +
    "</tbody></table></div>";
  return card("Cash Flow (Indirect)", body);
}

/** Render a period-over-period income-statement comparison fragment. */
export function renderPeriodComparison(pc: PeriodComparison): string {
  const rows = pc.lines
    .map(
      (l) =>
        `<tr><td>${esc(l.code)}</td><td>${esc(l.name)}</td>${fmt(l.current)}${fmt(
          l.prior,
        )}${fmt(l.variance)}${pctCell(l.variancePct)}</tr>`,
    )
    .join("");
  const totalRowV = (label: string, v: PeriodComparison["revenue"]): string =>
    `<tr><td colspan="2" style="font-weight:700">${esc(label)}</td>${fmt(v.current)}${fmt(
      v.prior,
    )}${fmt(v.variance)}${pctCell(v.variancePct)}</tr>`;
  const body =
    '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th><th class="num">Current</th><th class="num">Prior</th><th class="num">Variance</th><th class="num">%</th></tr></thead><tbody>' +
    rows +
    totalRowV("Total revenue", pc.revenue) +
    totalRowV("Total expenses", pc.expenses) +
    totalRowV("Net income", pc.netIncome) +
    "</tbody></table></div>";
  return card("Period Comparison", body);
}

/** Render a trial balance (debit/credit columns) fragment. */
export function renderTrialBalance(tb: TrialBalance): string {
  const zero = Money.zero(tb.currency);
  let dr = zero;
  let cr = zero;
  const rows = tb.entries
    .map((e) => {
      const debit = e.signed.isNegative() ? zero : e.signed;
      const credit = e.signed.isNegative() ? e.signed.negate() : zero;
      dr = dr.plus(debit);
      cr = cr.plus(credit);
      return `<tr><td>${esc(e.code)}</td><td>${esc(e.name)}</td>${fmt(debit)}${fmt(credit)}</tr>`;
    })
    .join("");
  const banner = dr.equals(cr)
    ? '<div class="banner good">In balance — total debits = total credits</div>'
    : '<div class="banner warn">Out of balance</div>';
  const body =
    banner +
    '<div class="table-scroll"><table><thead><tr><th>Code</th><th>Account</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead><tbody>' +
    rows +
    `<tr><td colspan="2" style="font-weight:700">Totals</td>${fmt(dr)}${fmt(cr)}</tr>` +
    "</tbody></table></div>";
  return card("Trial Balance", body);
}

/** Render a single account's GL detail (drill-down) fragment. */
export function renderGlDetailAccount(acct: GlDetailAccount): string {
  const rows = acct.rows
    .map(
      (r) =>
        `<tr><td>${esc(r.date)}</td><td>${esc(r.memo)}</td>${fmt(r.debit)}${fmt(r.credit)}${fmt(
          r.balance,
        )}</tr>`,
    )
    .join("");
  const body =
    '<div class="table-scroll"><table><thead><tr><th>Date</th><th>Memo</th><th class="num">Debit</th><th class="num">Credit</th><th class="num">Balance</th></tr></thead><tbody>' +
    `<tr><td colspan="4" style="font-weight:600">Opening balance</td>${fmt(acct.opening)}</tr>` +
    rows +
    `<tr><td colspan="2" style="font-weight:700">Closing balance</td>${fmt(acct.totalDebit)}${fmt(
      acct.totalCredit,
    )}${fmt(acct.closing)}</tr>` +
    "</tbody></table></div>";
  return card(`GL Detail — ${esc(acct.code)} ${esc(acct.name)}`, body);
}

/** Render a statement of retained earnings fragment. */
export function renderRetainedEarnings(re: RetainedEarnings): string {
  const body =
    '<div class="table-scroll"><table><tbody>' +
    totalRow("Beginning retained earnings", re.beginning, false) +
    totalRow("Net income", re.netIncome, false) +
    totalRow("Distributions", re.distributions.negate(), false) +
    totalRow("Ending retained earnings", re.ending) +
    "</tbody></table></div>";
  return card("Statement of Retained Earnings", body);
}

export interface StatementReport {
  readonly title?: string;
  readonly context?: string;
  readonly incomeStatement: IncomeStatement;
  readonly balanceSheet: BalanceSheet;
  readonly cashFlow: CashFlowStatement;
  readonly periodComparison?: PeriodComparison;
}

/** Render a complete, standalone branded HTML document for a set of statements. */
export function renderStatements(report: StatementReport): string {
  const title = report.title ?? "Financial Statements";
  const context = report.context ?? "Financial Statements";
  const parts = [
    renderIncomeStatement(report.incomeStatement),
    renderBalanceSheet(report.balanceSheet),
    renderCashFlow(report.cashFlow),
  ];
  if (report.periodComparison) parts.push(renderPeriodComparison(report.periodComparison));
  return (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">" +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    `<title>${esc(title)}</title><style>${RG_THEME_CSS}</style></head><body>` +
    brandBar(context) +
    `<main class="rg-shell"><span class="rg-eyebrow">Statements</span><h1 style="margin:4px 0 18px">${esc(
      title,
    )}</h1>` +
    parts.join("") +
    "</main></body></html>"
  );
}
