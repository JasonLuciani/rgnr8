import type { BalanceSheet, IncomeStatement } from "./statements.js";

function pad(s: string, n: number): string {
  return s.length >= n ? s : s + " ".repeat(n - s.length);
}
function money(m: { toDecimalString(): string }, width = 14): string {
  const s = m.toDecimalString();
  return " ".repeat(Math.max(0, width - s.length)) + s;
}

export function renderIncomeStatement(is: IncomeStatement): string {
  const out = [`Income Statement  ${is.periodStart} → ${is.asOf}  (${is.currency})`, "".padEnd(48, "-")];
  for (const sec of [is.revenue, is.expenses]) {
    out.push(sec.title);
    for (const l of sec.lines) out.push(`  ${pad(l.code + " " + l.name, 30)}${money(l.amount)}`);
    out.push(`  ${pad("Total " + sec.title, 30)}${money(sec.total)}`);
  }
  out.push("".padEnd(48, "-"));
  out.push(`${pad("Net income", 32)}${money(is.netIncome)}`);
  return out.join("\n");
}

export function renderBalanceSheet(bs: BalanceSheet): string {
  const out = [`Balance Sheet  as of ${bs.asOf}  (${bs.currency})`, "".padEnd(48, "-")];
  const sec = (s: typeof bs.assets) => {
    out.push(s.title);
    for (const l of s.lines) out.push(`  ${pad(l.code + " " + l.name, 30)}${money(l.amount)}`);
    out.push(`  ${pad("Total " + s.title, 30)}${money(s.total)}`);
  };
  sec(bs.assets);
  sec(bs.liabilities);
  sec(bs.equity);
  out.push(`  ${pad("Current earnings", 30)}${money(bs.netIncome)}`);
  out.push("".padEnd(48, "-"));
  out.push(`${pad("Total assets", 32)}${money(bs.totalAssets)}`);
  out.push(`${pad("Total liabilities + equity", 32)}${money(bs.totalLiabilitiesAndEquity)}`);
  out.push(`Balances: ${bs.balances ? "YES" : "NO"}`);
  return out.join("\n");
}
