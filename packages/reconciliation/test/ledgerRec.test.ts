import { test } from "node:test";
import assert from "node:assert/strict";
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
  type Account,
  type PostCommand,
  type Provenance,
} from "@rgnr8/ledger-kernel";
import {
  ClearedRegister,
  bankRegister,
  finishBankReconciliation,
  reconcileBankAccount,
  type Statement,
  type StatementLine,
} from "../src/index.js";

const usd = (s: string) => Money.fromDecimal(s, USD);
const TENANT = asTenantId("acme");
const POST_AT = "2026-08-31T00:00:00Z";
const CASH = asAccountId("gl.cash");
const INCOME = asAccountId("gl.income");
const EXPENSE = asAccountId("gl.expense");

const PROV: Provenance = Object.freeze({
  sourceSystem: "test", sourceObject: "je", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01T00:00:00Z",
  normalizationVersion: "n1", mappingVersion: "m1",
});

function acct(id: string, code: string, type: AccountType): Account {
  return { id: asAccountId(id), code, name: code, type, currency: USD };
}
function coa(): ChartOfAccounts {
  return new ChartOfAccounts([
    acct("gl.cash", "cash", AccountType.ASSET),
    acct("gl.income", "income", AccountType.REVENUE),
    acct("gl.expense", "expense", AccountType.EXPENSE),
  ]);
}

/** A one-sided-to-cash journal: +amount debits cash (deposit), −amount credits cash (payment). */
function cashEntry(id: string, date: string, amount: string): PostCommand {
  const m = usd(amount);
  const into = m.minorUnits >= 0n;
  const abs = into ? m : m.negate();
  return {
    tenantId: TENANT,
    idempotencyKey: asIdempotencyKey(`je:${id}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: USD,
    entryDate: date,
    memo: id,
    provenance: PROV,
    lines: into
      ? [
          { accountId: CASH, side: "DEBIT", amount: abs },
          { accountId: INCOME, side: "CREDIT", amount: abs },
        ]
      : [
          { accountId: EXPENSE, side: "DEBIT", amount: abs },
          { accountId: CASH, side: "CREDIT", amount: abs },
        ],
  };
}

function line(id: string, date: string, amount: string): StatementLine {
  return { id, date, amount: usd(amount), description: id };
}
function statement(lines: StatementLine[], opening: string, closing: string): Statement {
  return {
    accountId: "gl.cash",
    periodStart: "2026-08-01",
    periodEnd: "2026-08-31",
    openingBalance: usd(opening),
    closingBalance: usd(closing),
    lines,
  };
}

async function seed(cmds: PostCommand[]): Promise<InMemoryLedgerStore> {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(coa(), store, new PeriodRegistry());
  for (const c of cmds) await engine.post(c, { postedAt: POST_AT });
  return store;
}

test("bankRegister derives one signed line per entry touching the account", async () => {
  const store = await seed([
    cashEntry("d1", "2026-08-03", "500.00"),
    cashEntry("p1", "2026-08-05", "-200.00"),
  ]);
  const reg = new ClearedRegister();
  const lines = bankRegister(await store.list(TENANT), CASH, USD, reg);
  assert.equal(lines.length, 2);
  assert.equal(lines[0]?.amount.toDecimalString(), "500.00");
  assert.equal(lines[1]?.amount.toDecimalString(), "-200.00");
  assert.equal(lines[0]?.status, "UNCLEARED");
});

test("a tie-out reconciliation clears the matched entries and can finish", async () => {
  const store = await seed([
    cashEntry("d1", "2026-08-03", "500.00"),
    cashEntry("p1", "2026-08-05", "-200.00"),
    cashEntry("p2", "2026-08-28", "-50.00"), // in-transit: not on this statement
  ]);
  const reg = new ClearedRegister();
  const stmt = statement(
    [line("s1", "2026-08-03", "500.00"), line("s2", "2026-08-06", "-200.00")],
    "1000.00",
    "1300.00",
  );
  const recon = reconcileBankAccount(await store.list(TENANT), CASH, stmt, reg);

  assert.equal(recon.status, "BALANCED");
  assert.ok(recon.difference.isZero());
  assert.equal(recon.clearedBalance.toDecimalString(), "1300.00");
  assert.equal(recon.newlyClearedEntryIds.length, 2);
  assert.equal(recon.unclearedLines.length, 1); // the in-transit p2
  assert.equal(recon.unclearedLines[0]?.memo, "p2");
  assert.ok(recon.canFinish);

  finishBankReconciliation(recon, reg);
  const after = bankRegister(await store.list(TENANT), CASH, USD, reg);
  assert.equal(after.filter((l) => l.status === "RECONCILED").length, 2);
  assert.equal(reg.reconciledThrough("gl.cash"), "2026-08-31");
});

test("a statement line missing from the books is out of balance and cannot finish", async () => {
  const store = await seed([cashEntry("d1", "2026-08-03", "500.00")]);
  const reg = new ClearedRegister();
  const stmt = statement(
    [line("s1", "2026-08-03", "500.00"), line("s2", "2026-08-10", "-75.00")],
    "1000.00",
    "1425.00",
  );
  const recon = reconcileBankAccount(await store.list(TENANT), CASH, stmt, reg);
  assert.equal(recon.status, "OUT_OF_BALANCE");
  assert.equal(recon.unmatchedStatement.length, 1);
  assert.equal(recon.difference.toDecimalString(), "-75.00");
  assert.throws(() => finishBankReconciliation(recon, reg), /out-of-balance/);
});

test("already-reconciled lines are not re-matched in a later reconciliation", async () => {
  const store = await seed([
    cashEntry("d1", "2026-08-03", "500.00"),
    cashEntry("d2", "2026-09-04", "300.00"),
  ]);
  const reg = new ClearedRegister();
  // August: clear d1.
  const aug = statement([line("s1", "2026-08-03", "500.00")], "1000.00", "1500.00");
  const r1 = reconcileBankAccount(await store.list(TENANT), CASH, aug, reg);
  finishBankReconciliation(r1, reg);

  // September: only d2 is open; opening already folds in d1.
  const sep: Statement = {
    accountId: "gl.cash", periodStart: "2026-09-01", periodEnd: "2026-09-30",
    openingBalance: usd("1500.00"), closingBalance: usd("1800.00"),
    lines: [line("s2", "2026-09-04", "300.00")],
  };
  const r2 = reconcileBankAccount(await store.list(TENANT), CASH, sep, reg);
  assert.equal(r2.status, "BALANCED");
  assert.equal(r2.newlyClearedEntryIds.length, 1);
  assert.equal(r2.matched[0]?.book.id, r2.newlyClearedEntryIds[0]);
});
