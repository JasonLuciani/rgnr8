import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  Money,
  PeriodRegistry,
  PostingEngine,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
} from "../src/index.js";
import type { Account, AccountId, PostCommand, Provenance } from "../src/index.js";

export const TENANT = asTenantId("acme");
export const AUG = asPeriodKey("2026-08");

export const ACCT = {
  cash: asAccountId("cash"),
  ar: asAccountId("ar"),
  revenue: asAccountId("revenue"),
  payroll: asAccountId("payroll"),
  loan: asAccountId("loan"),
  equity: asAccountId("equity"),
};

function acct(id: AccountId, code: string, name: string, type: AccountType): Account {
  return { id, code, name, type, currency: USD };
}

export function standardCoa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct(ACCT.cash, "1000", "Cash", AccountType.ASSET),
    acct(ACCT.ar, "1100", "Accounts Receivable", AccountType.ASSET),
    acct(ACCT.loan, "2000", "Loan Payable", AccountType.LIABILITY),
    acct(ACCT.equity, "3000", "Owner's Equity", AccountType.EQUITY),
    acct(ACCT.revenue, "4000", "Revenue", AccountType.REVENUE),
    acct(ACCT.payroll, "6000", "Payroll Expense", AccountType.EXPENSE),
  ]);
}

export const PROV: Provenance = Object.freeze({
  sourceSystem: "test",
  sourceObject: "manual",
  sourceVersion: "1",
  effectiveDate: "2026-08-15",
  postedDate: "2026-08-15",
  ingestedAt: "2026-08-15T00:00:00Z",
  normalizationVersion: "norm-1",
  mappingVersion: "map-1",
});

export interface Fixture {
  coa: ChartOfAccounts;
  store: InMemoryLedgerStore;
  periods: PeriodRegistry;
  engine: PostingEngine;
}

export function fixture(): Fixture {
  const coa = standardCoa();
  const store = new InMemoryLedgerStore();
  const periods = new PeriodRegistry();
  const engine = new PostingEngine(coa, store, periods);
  return { coa, store, periods, engine };
}

export const usd = (decimal: string): Money => Money.fromDecimal(decimal, USD);

let keyN = 0;
export function key(name?: string): ReturnType<typeof asIdempotencyKey> {
  return asIdempotencyKey(name ?? `k-${++keyN}`);
}

/** A simple sale: debit Cash, credit Revenue. */
export function saleCommand(amount: string, k?: string): PostCommand {
  return {
    tenantId: TENANT,
    idempotencyKey: asIdempotencyKey(k ?? `sale-${amount}`),
    periodKey: AUG,
    currency: USD,
    entryDate: "2026-08-15",
    memo: "Cash sale",
    provenance: PROV,
    lines: [
      { accountId: ACCT.cash, side: "DEBIT", amount: usd(amount) },
      { accountId: ACCT.revenue, side: "CREDIT", amount: usd(amount) },
    ],
  };
}

export const POST_AT = "2026-08-15T12:00:00Z";
