import { Money, type Currency } from "@rgnr8/ledger-kernel";
import type { AccountId, ChartOfAccounts, LedgerStore, TenantId } from "@rgnr8/ledger-kernel";
import { AccountType } from "@rgnr8/ledger-kernel";

export interface LineItem {
  readonly accountId: AccountId;
  readonly code: string;
  readonly name: string;
  readonly amount: Money; // oriented to the section (positive = normal)
}

export interface Section {
  readonly title: string;
  readonly lines: readonly LineItem[];
  readonly total: Money;
}

export interface IncomeStatement {
  readonly periodStart: string;
  readonly asOf: string;
  readonly currency: string;
  readonly revenue: Section;
  readonly expenses: Section;
  readonly netIncome: Money; // revenue − expenses
}

export interface BalanceSheet {
  readonly asOf: string;
  readonly currency: string;
  readonly assets: Section;
  readonly liabilities: Section;
  readonly equity: Section;
  readonly netIncome: Money; // current-earnings, folded into the equity side
  readonly totalAssets: Money;
  readonly totalLiabilitiesAndEquity: Money;
  readonly balances: boolean; // assets == liabilities + equity + net income
}

/** Net debit position per account (debit positive) from entries in a date window. */
function netByAccount(
  entries: Awaited<ReturnType<LedgerStore["list"]>>,
  from: string | null,
  to: string,
): Map<AccountId, bigint> {
  const net = new Map<AccountId, bigint>();
  for (const e of entries) {
    if (e.entryDate > to) continue;
    if (from !== null && e.entryDate < from) continue;
    for (const line of e.lines) {
      const delta = line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
      net.set(line.accountId, (net.get(line.accountId) ?? 0n) + delta);
    }
  }
  return net;
}

function section(
  title: string,
  net: Map<AccountId, bigint>,
  coa: ChartOfAccounts,
  types: readonly AccountType[],
  creditNormal: boolean,
  currency: Currency,
): Section {
  const lines: LineItem[] = [];
  let totalMinor = 0n;
  const accounts = coa
    .list()
    .filter((a) => types.includes(a.type))
    .sort((a, b) => a.code.localeCompare(b.code));
  for (const a of accounts) {
    const n = net.get(a.id) ?? 0n;
    const oriented = creditNormal ? -n : n; // credit-normal accounts read positive on the credit side
    if (oriented === 0n) continue;
    totalMinor += oriented;
    lines.push({ accountId: a.id, code: a.code, name: a.name, amount: Money.fromMinorUnits(oriented, currency) });
  }
  return { title, lines, total: Money.fromMinorUnits(totalMinor, currency) };
}

export async function computeIncomeStatement(
  store: LedgerStore,
  tenant: TenantId,
  coa: ChartOfAccounts,
  periodStart: string,
  asOf: string,
  currency: Currency,
): Promise<IncomeStatement> {
  const entries = await store.list(tenant);
  const net = netByAccount(entries, periodStart, asOf);
  const revenue = section("Revenue", net, coa, [AccountType.REVENUE], true, currency);
  const expenses = section("Expenses", net, coa, [AccountType.EXPENSE], false, currency);
  return {
    periodStart,
    asOf,
    currency: currency.code,
    revenue,
    expenses,
    netIncome: revenue.total.minus(expenses.total),
  };
}

export async function computeBalanceSheet(
  store: LedgerStore,
  tenant: TenantId,
  coa: ChartOfAccounts,
  asOf: string,
  currency: Currency,
): Promise<BalanceSheet> {
  const entries = await store.list(tenant);
  const net = netByAccount(entries, null, asOf);
  const assets = section("Assets", net, coa, [AccountType.ASSET], false, currency);
  const liabilities = section("Liabilities", net, coa, [AccountType.LIABILITY], true, currency);
  const equity = section("Equity", net, coa, [AccountType.EQUITY], true, currency);

  // Current earnings = revenue − expenses to date, folded onto the equity side.
  const revenue = section("Revenue", net, coa, [AccountType.REVENUE], true, currency);
  const expenses = section("Expenses", net, coa, [AccountType.EXPENSE], false, currency);
  const netIncome = revenue.total.minus(expenses.total);

  const totalAssets = assets.total;
  const totalLiabEquity = liabilities.total.plus(equity.total).plus(netIncome);
  return {
    asOf,
    currency: currency.code,
    assets,
    liabilities,
    equity,
    netIncome,
    totalAssets,
    totalLiabilitiesAndEquity: totalLiabEquity,
    balances: totalAssets.minorUnits === totalLiabEquity.minorUnits,
  };
}
