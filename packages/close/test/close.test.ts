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
} from "@rgnr8/ledger-kernel";
import type { Account, Provenance } from "@rgnr8/ledger-kernel";
import { PeriodClosedError } from "@rgnr8/ledger-kernel";
import { buildCloseChecklist, closePeriod, reopenPeriod, type CloseGateInputs } from "../src/index.js";

const AUG = asPeriodKey("2026-08");
const TENANT = asTenantId("acme");
const usd = (s: string) => Money.fromDecimal(s, USD);
const PROV: Provenance = {
  sourceSystem: "t", sourceObject: "je", sourceVersion: "1", effectiveDate: "2026-08-01",
  postedDate: "2026-08-01", ingestedAt: "2026-08-01", normalizationVersion: "1", mappingVersion: "1",
};

const cleanInputs: CloseGateInputs = {
  reconciliations: [{ accountId: "chk", status: "BALANCED", signOff: { reviewer: "c" } }],
  controls: [{ name: "AR", balanced: true }, { name: "AP", balanced: true }],
  trialBalanceBalanced: true,
  requireSignOff: true,
};

test("a clean period closes and locks against further posting", async () => {
  const periods = new PeriodRegistry();
  const pkg = closePeriod(periods, AUG, cleanInputs, { closedBy: "controller", at: "2026-09-01T00:00:00Z" });
  assert.ok(pkg.ready);
  assert.ok(pkg.closed);
  assert.equal(pkg.closedBy, "controller");
  assert.ok(pkg.tasks.every((t) => t.status === "PASSED"));

  // The kernel now rejects postings into the closed period.
  const chart = new ChartOfAccounts([
    { id: asAccountId("cash"), code: "1000", name: "Cash", type: AccountType.ASSET, currency: USD } as Account,
    { id: asAccountId("rev"), code: "4000", name: "Rev", type: AccountType.REVENUE, currency: USD } as Account,
  ]);
  const engine = new PostingEngine(chart, new InMemoryLedgerStore(), periods);
  await assert.rejects(
    () =>
      engine.post(
        {
          tenantId: TENANT, idempotencyKey: asIdempotencyKey("x"), periodKey: AUG, currency: USD,
          entryDate: "2026-08-15", provenance: PROV,
          lines: [
            { accountId: asAccountId("cash"), side: "DEBIT", amount: usd("100.00") },
            { accountId: asAccountId("rev"), side: "CREDIT", amount: usd("100.00") },
          ],
        },
        { postedAt: "2026-09-01T00:00:00Z" },
      ),
    PeriodClosedError,
  );
});

test("an out-of-balance account blocks the close and locks nothing", () => {
  const periods = new PeriodRegistry();
  const dirty: CloseGateInputs = {
    ...cleanInputs,
    reconciliations: [{ accountId: "chk", status: "OUT_OF_BALANCE" }],
  };
  const pkg = closePeriod(periods, AUG, dirty, { closedBy: "c", at: "2026-09-01T00:00:00Z" });
  assert.ok(!pkg.closed);
  assert.ok(pkg.blockers.length >= 1);
  assert.ok(periods.isOpen(AUG)); // nothing was locked
});

test("missing reviewer sign-off blocks the close when required", () => {
  const periods = new PeriodRegistry();
  const noSign: CloseGateInputs = {
    ...cleanInputs,
    reconciliations: [{ accountId: "chk", status: "BALANCED" }], // no signOff
  };
  const pkg = closePeriod(periods, AUG, noSign, { closedBy: "c", at: "2026-09-01T00:00:00Z" });
  assert.ok(!pkg.closed);
  assert.ok(pkg.tasks.some((t) => t.id === "reconciliation_signoff" && t.status === "FAILED"));
});

test("control-account drift blocks the close", () => {
  const periods = new PeriodRegistry();
  const drift: CloseGateInputs = { ...cleanInputs, controls: [{ name: "AR", balanced: false }] };
  const pkg = closePeriod(periods, AUG, drift, { closedBy: "c", at: "2026-09-01T00:00:00Z" });
  assert.ok(!pkg.closed);
  assert.ok(pkg.tasks.some((t) => t.id === "control_accounts" && t.status === "FAILED"));
});

test("reopen unlocks the period for a prior-period adjustment", () => {
  const periods = new PeriodRegistry();
  closePeriod(periods, AUG, cleanInputs, { closedBy: "c", at: "2026-09-01T00:00:00Z" });
  assert.ok(!periods.isOpen(AUG));
  reopenPeriod(periods, AUG);
  assert.ok(periods.isOpen(AUG));
});

test("checklist reports each gate's detail", () => {
  const tasks = buildCloseChecklist(cleanInputs);
  assert.equal(tasks.find((t) => t.id === "trial_balance")?.status, "PASSED");
  assert.equal(tasks.find((t) => t.id === "control_accounts")?.detail, "AR/AP controls tied");
});
