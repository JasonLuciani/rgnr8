import {
  Money,
  RG,
  RG_BASE_CSS,
  RG_TOKENS_CSS,
  brandBar,
  type Currency,
  type TrialBalance,
} from "@rgnr8/ledger-kernel";
import type { QboTrialBalanceRow } from "./types.js";

/**
 * The parallel-close diff harness: hold RGNR8's ledger-derived trial balance
 * against QuickBooks' reported trial balance, account by account. This is the
 * proof that migrating into RGNR8 preserves the books — and, run each period,
 * the "does our number match QBO" gate on the road to system-of-record.
 *
 * Accounts are matched by name (RGNR8's chart is built from QBO names). Balances
 * are compared as a signed net-debit position in integer minor units, so there
 * is no float drift; the tolerance is expressed in minor units and defaults to 0
 * (exact).
 */

export type DiffStatus = "match" | "mismatch" | "only_rgnr8" | "only_qbo";

export interface DiffRow {
  readonly account: string;
  readonly rgnr8: Money; // net debit position (positive = debit balance)
  readonly qbo: Money;
  readonly delta: Money; // rgnr8 − qbo
  readonly status: DiffStatus;
}

export interface CompareOptions {
  readonly currency: Currency;
  /** Allowed absolute delta in minor units before a row is a "mismatch". */
  readonly toleranceMinor?: bigint;
}

export interface DiffReport {
  readonly currency: string;
  readonly rows: readonly DiffRow[];
  readonly inAgreement: boolean;
  readonly totalAbsDelta: Money;
  readonly matched: number;
  readonly mismatches: readonly DiffRow[];
  readonly onlyInRgnr8: readonly DiffRow[];
  readonly onlyInQbo: readonly DiffRow[];
}

function parseNetMinor(row: QboTrialBalanceRow, currency: Currency): bigint {
  const d = row.debit && row.debit !== "" ? Money.fromDecimal(row.debit, currency).minorUnits : 0n;
  const c = row.credit && row.credit !== "" ? Money.fromDecimal(row.credit, currency).minorUnits : 0n;
  return d - c;
}

export function compareTrialBalances(
  rgnr8: TrialBalance,
  qbo: readonly QboTrialBalanceRow[],
  opts: CompareOptions,
): DiffReport {
  const tol = opts.toleranceMinor ?? 0n;
  const cur = opts.currency;

  const rgnr8Net = new Map<string, bigint>();
  for (const r of rgnr8.rows) {
    rgnr8Net.set(r.name, r.debit.minorUnits - r.credit.minorUnits);
  }
  const qboNet = new Map<string, bigint>();
  for (const r of qbo) {
    qboNet.set(r.account, (qboNet.get(r.account) ?? 0n) + parseNetMinor(r, cur));
  }

  const names = [...new Set([...rgnr8Net.keys(), ...qboNet.keys()])].sort();
  const rows: DiffRow[] = [];
  let totalAbs = 0n;

  for (const name of names) {
    const inR = rgnr8Net.has(name);
    const inQ = qboNet.has(name);
    const rMinor = rgnr8Net.get(name) ?? 0n;
    const qMinor = qboNet.get(name) ?? 0n;
    const deltaMinor = rMinor - qMinor;
    totalAbs += deltaMinor < 0n ? -deltaMinor : deltaMinor;

    let status: DiffStatus;
    if (!inQ) status = "only_rgnr8";
    else if (!inR) status = "only_qbo";
    else status = (deltaMinor < 0n ? -deltaMinor : deltaMinor) <= tol ? "match" : "mismatch";

    rows.push({
      account: name,
      rgnr8: Money.fromMinorUnits(rMinor, cur),
      qbo: Money.fromMinorUnits(qMinor, cur),
      delta: Money.fromMinorUnits(deltaMinor, cur),
      status,
    });
  }

  const mismatches = rows.filter((r) => r.status === "mismatch");
  const onlyInRgnr8 = rows.filter((r) => r.status === "only_rgnr8");
  const onlyInQbo = rows.filter((r) => r.status === "only_qbo");

  return {
    currency: cur.code,
    rows,
    inAgreement: mismatches.length === 0 && onlyInRgnr8.length === 0 && onlyInQbo.length === 0,
    totalAbsDelta: Money.fromMinorUnits(totalAbs, cur),
    matched: rows.filter((r) => r.status === "match").length,
    mismatches,
    onlyInRgnr8,
    onlyInQbo,
  };
}

// --- HTML render -------------------------------------------------------------

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

const STATUS_COLOR: Readonly<Record<DiffStatus, string>> = {
  match: RG.positive,
  mismatch: RG.risk,
  only_rgnr8: RG.watch,
  only_qbo: RG.watch,
};

export function renderDiffHtml(report: DiffReport): string {
  const rows = report.rows
    .map(
      (r) => `<tr>
      <td>${esc(r.account)}</td>
      <td class="num">${esc(r.rgnr8.toDecimalString())}</td>
      <td class="num">${esc(r.qbo.toDecimalString())}</td>
      <td class="num">${esc(r.delta.toDecimalString())}</td>
      <td><span class="dot" style="background:${STATUS_COLOR[r.status]}"></span>${r.status}</td>
    </tr>`,
    )
    .join("\n");
  const banner = report.inAgreement
    ? `<div class="banner good">Ledgers agree — RGNR8 ties to QuickBooks to the penny.</div>`
    : `<div class="banner warn">${report.mismatches.length} mismatch(es), ${report.onlyInRgnr8.length} only in RGNR8, ${report.onlyInQbo.length} only in QBO · total abs delta ${esc(report.totalAbsDelta.toDecimalString())} ${report.currency}.</div>`;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 vs QuickBooks — trial balance</title>
<style>
${RG_TOKENS_CSS}
${RG_BASE_CSS}
  .wrap{max-width:840px;margin:0 auto;padding:24px 20px 56px}
  h1{font-size:22px;margin:0 0 12px;font-weight:800;letter-spacing:-.01em}
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
${brandBar("Parallel close · trial balance")}
<div class="wrap">
  <h1>RGNR8 vs QuickBooks — trial balance</h1>
  ${banner}
  <table>
    <thead><tr><th>Account</th><th class="num">RGNR8</th><th class="num">QuickBooks</th><th class="num">Δ</th><th>Status</th></tr></thead>
    <tbody>
${rows}
    </tbody>
  </table>
</div>
</body></html>`;
}
