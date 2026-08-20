import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  InMemoryPeriodStore,
  Money,
  PeriodRegistry,
  PostingEngine,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  computeTrialBalance,
  type Account,
  type PostCommand,
  type Provenance,
} from "@rgnr8/ledger-kernel";
import { computeIncomeStatement, computeBalanceSheet } from "@rgnr8/statements";
import {
  CloseGateError,
  InMemoryCloseStateStore,
  InMemoryFinancialPackageStore,
  SeparationOfDutiesError,
  CloseStateError,
  approveReopen,
  buildFinancialPackage,
  publishClose,
  requestReopen,
  type CloseEngineDeps,
  type CloseGateInputs,
  type FinancialPackage,
  type FinancialPackageInput,
} from "../src/index.js";

const TENANT = asTenantId("acme");
const AUG = asPeriodKey("2026-08");
const AS_OF = "2026-08-31";

function acct(id: string, code: string, name: string, type: AccountType): Account {
  return { id: asAccountId(id), code, name, type, currency: USD };
}
const COA = new ChartOfAccounts([
  acct("cash", "1000", "Cash", AccountType.ASSET),
  acct("ar", "1200", "Accounts Receivable", AccountType.ASSET),
  acct("equity", "3000", "Owner Equity", AccountType.EQUITY),
  acct("rev", "4000", "Sales", AccountType.REVENUE),
  acct("rent", "6000", "Rent", AccountType.EXPENSE),
]);
const PROV: Provenance = {
  sourceSystem: "test", sourceObject: "je", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01T00:00:00Z",
  normalizationVersion: "n/1", mappingVersion: "m/1",
};
function cmd(key: string, date: string, lines: PostCommand["lines"]): PostCommand {
  return { tenantId: TENANT, idempotencyKey: asIdempotencyKey(key), periodKey: AUG, currency: USD, entryDate: date, lines, provenance: PROV };
}

async function books(): Promise<{ input: FinancialPackageInput; store: InMemoryLedgerStore }> {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(COA, store, new PeriodRegistry());
  const d = asAccountId;
  await engine.post(cmd("open", "2026-08-01", [
    { accountId: d("cash"), side: "DEBIT", amount: Money.fromDecimal("50000.00", USD) },
    { accountId: d("equity"), side: "CREDIT", amount: Money.fromDecimal("50000.00", USD) },
  ]), { postedAt: "2026-08-01T00:00:00Z" });
  await engine.post(cmd("sale", "2026-08-05", [
    { accountId: d("ar"), side: "DEBIT", amount: Money.fromDecimal("12000.00", USD) },
    { accountId: d("rev"), side: "CREDIT", amount: Money.fromDecimal("12000.00", USD) },
  ]), { postedAt: "2026-08-05T00:00:00Z" });
  const tb = await computeTrialBalance(store, TENANT, COA, USD);
  const is = await computeIncomeStatement(store, TENANT, COA, "2026-08-01", AS_OF, USD);
  const bs = await computeBalanceSheet(store, TENANT, COA, AS_OF, USD);
  const input: FinancialPackageInput = {
    periodKey: "2026-08", currency: "USD",
    trialBalance: {
      rows: tb.rows.map((r) => ({ code: r.code, name: r.name, debitMinor: r.debit.minorUnits.toString(), creditMinor: r.credit.minorUnits.toString() })),
      totalDebitMinor: tb.totalDebit.minorUnits.toString(), totalCreditMinor: tb.totalCredit.minorUnits.toString(), inBalance: tb.inBalance,
    },
    incomeStatement: { revenueMinor: is.revenue.total.minorUnits.toString(), expensesMinor: is.expenses.total.minorUnits.toString(), netIncomeMinor: is.netIncome.minorUnits.toString() },
    balanceSheet: { totalAssetsMinor: bs.totalAssets.minorUnits.toString(), totalLiabilitiesAndEquityMinor: bs.totalLiabilitiesAndEquity.minorUnits.toString(), netIncomeMinor: bs.netIncome.minorUnits.toString(), balances: bs.balances },
  };
  return { input, store };
}

const META = { closedBy: "controller", closedAt: "2026-09-01T17:00:00Z", packagedAt: "2026-09-01T17:00:05Z" };

async function pkg(): Promise<FinancialPackage> {
  const { input } = await books();
  return buildFinancialPackage(input, META);
}

/** A gate where everything passes. */
const cleanGate: CloseGateInputs = {
  reconciliations: [{ accountId: "1000", status: "BALANCED" }],
  controls: [{ name: "AR", balanced: true }],
  trialBalanceBalanced: true,
};

function deps(): CloseEngineDeps & { periods: InMemoryPeriodStore; states: InMemoryCloseStateStore } {
  return {
    periods: new InMemoryPeriodStore(),
    packages: new InMemoryFinancialPackageStore(),
    states: new InMemoryCloseStateStore(),
  };
}

test("publish locks the period durably, persists the package, and records PUBLISHED", async () => {
  const d = deps();
  const p = await pkg();
  const state = await publishClose(d, {
    tenantId: "acme", inputs: cleanGate, pkg: p,
    preparedBy: "alex", publishedBy: "sam", at: "2026-09-01T17:00:00Z",
  });

  assert.equal(state.status, "PUBLISHED");
  assert.equal(state.publishedBy, "sam");
  assert.equal(state.packageFingerprint, p.fingerprint);
  // Period is durably locked in the kernel period store.
  assert.equal(await d.periods.status(TENANT, AUG), "LOCKED");
  // The immutable package is persisted and retrievable.
  assert.ok(await d.packages.get("acme", "2026-08"));
  // The gate evidence is captured on the state.
  assert.ok(state.checklist.every((t) => t.status === "PASSED"));
});

test("publish is refused when a control fails — nothing is locked or persisted", async () => {
  const d = deps();
  const p = await pkg();
  const failing: CloseGateInputs = { ...cleanGate, controls: [{ name: "AP", balanced: false }] };
  await assert.rejects(
    () => publishClose(d, { tenantId: "acme", inputs: failing, pkg: p, publishedBy: "sam", at: META.closedAt }),
    CloseGateError,
  );
  assert.equal(await d.periods.status(TENANT, AUG), "OPEN");
  assert.equal(await d.packages.get("acme", "2026-08"), null);
  assert.equal(await d.states.get("acme", "2026-08"), null);
});

test("separation of duties: the preparer cannot also publish", async () => {
  const d = deps();
  const p = await pkg();
  await assert.rejects(
    () => publishClose(d, { tenantId: "acme", inputs: cleanGate, pkg: p, preparedBy: "sam", publishedBy: "sam", at: META.closedAt }),
    SeparationOfDutiesError,
  );
  assert.equal(await d.periods.status(TENANT, AUG), "OPEN");
});

test("publish is idempotent for the same package, but rejects a different one", async () => {
  const d = deps();
  const p = await pkg();
  const first = await publishClose(d, { tenantId: "acme", inputs: cleanGate, pkg: p, publishedBy: "sam", at: META.closedAt });
  const again = await publishClose(d, { tenantId: "acme", inputs: cleanGate, pkg: p, publishedBy: "sam", at: META.closedAt });
  assert.equal(again.publishedAt, first.publishedAt); // same record

  // A different package for an already-published period is refused.
  const other = buildFinancialPackage((await books()).input, { ...META, closedBy: "someone-else" });
  if (other.fingerprint !== p.fingerprint) {
    await assert.rejects(
      () => publishClose(d, { tenantId: "acme", inputs: cleanGate, pkg: other, publishedBy: "sam", at: META.closedAt }),
      CloseStateError,
    );
  }
});

test("reopen is a two-step approved workflow; approver must differ from requester", async () => {
  const d = deps();
  const p = await pkg();
  await publishClose(d, { tenantId: "acme", inputs: cleanGate, pkg: p, publishedBy: "sam", at: META.closedAt });

  // A request records intent but does NOT unlock the period.
  const requested = await requestReopen(d, {
    tenantId: "acme", periodKey: "2026-08", requestedBy: "alex", reason: "found a missing bill", at: "2026-09-02T09:00:00Z",
  });
  assert.equal(requested.status, "REOPEN_REQUESTED");
  assert.equal(await d.periods.status(TENANT, AUG), "LOCKED", "still locked pending approval");

  // The requester cannot approve their own reopen.
  await assert.rejects(
    () => approveReopen(d, { tenantId: "acme", periodKey: "2026-08", approvedBy: "alex", at: "2026-09-02T09:05:00Z" }),
    SeparationOfDutiesError,
  );

  // A different person approves → the period unlocks.
  const reopened = await approveReopen(d, { tenantId: "acme", periodKey: "2026-08", approvedBy: "sam", at: "2026-09-02T10:00:00Z" });
  assert.equal(reopened.status, "REOPENED");
  assert.equal(await d.periods.status(TENANT, AUG), "OPEN");

  // Evidence preserved: the published package is still in the store after reopen,
  // and the full transition history is retained.
  assert.ok(await d.packages.get("acme", "2026-08"), "the sealed package is preserved as evidence");
  assert.deepEqual(reopened.history.map((h) => h.action), ["PUBLISH", "REQUEST_REOPEN", "APPROVE_REOPEN"]);
});

test("cannot reopen a period that was never published, or approve with no pending request", async () => {
  const d = deps();
  await assert.rejects(
    () => requestReopen(d, { tenantId: "acme", periodKey: "2026-08", requestedBy: "alex", reason: "x", at: META.closedAt }),
    CloseStateError,
  );
  const p = await pkg();
  await publishClose(d, { tenantId: "acme", inputs: cleanGate, pkg: p, publishedBy: "sam", at: META.closedAt });
  await assert.rejects(
    () => approveReopen(d, { tenantId: "acme", periodKey: "2026-08", approvedBy: "sam", at: META.closedAt }),
    CloseStateError,
  );
});
