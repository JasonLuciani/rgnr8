import { newDb } from "pg-mem";
import {
  AccountType,
  ChartOfAccounts,
  PeriodRegistry,
  PostingEngine,
  Money,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
} from "@rgnr8/ledger-kernel";
import type { Account, PeriodStore, PostCommand, Provenance } from "@rgnr8/ledger-kernel";
import { PgLedgerStore, type Pool } from "../src/index.js";

export function makePool(): Pool {
  const db = newDb();
  const pg = db.adapters.createPg();
  return new pg.Pool() as unknown as Pool;
}

export async function freshStore(): Promise<PgLedgerStore> {
  const store = new PgLedgerStore(makePool());
  await store.migrate();
  return store;
}

export const AUG = asPeriodKey("2026-08");
export const POST_AT = "2026-08-15T12:00:00Z";

export const ACCT = {
  cash: asAccountId("cash"),
  ar: asAccountId("ar"),
  revenue: asAccountId("revenue"),
  payroll: asAccountId("payroll"),
};

function acct(id: (typeof ACCT)[keyof typeof ACCT], code: string, name: string, type: AccountType): Account {
  return { id, code, name, type, currency: USD };
}

export function standardCoa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct(ACCT.cash, "1000", "Cash", AccountType.ASSET),
    acct(ACCT.ar, "1100", "Accounts Receivable", AccountType.ASSET),
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

export const usd = (d: string): Money => Money.fromDecimal(d, USD);

export function engineWith(store: PgLedgerStore, periods: PeriodStore = new PeriodRegistry()): PostingEngine {
  return new PostingEngine(standardCoa(), store, periods);
}

export function saleCommand(tenant: string, amount: string, k?: string): PostCommand {
  return {
    tenantId: asTenantId(tenant),
    idempotencyKey: asIdempotencyKey(k ?? `sale-${tenant}-${amount}`),
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
