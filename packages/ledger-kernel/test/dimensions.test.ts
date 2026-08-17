import { test } from "node:test";
import assert from "node:assert/strict";
import {
  AccountType,
  ChartOfAccounts,
  DimensionError,
  DimensionRegistry,
  InMemoryLedgerStore,
  Money,
  PostingEngine,
  UNASSIGNED,
  USD,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  trialBalanceByDimension,
  type Account,
  type JournalLineInput,
  type PostCommand,
  type Provenance,
} from "../src/index.js";

const tenant = asTenantId("acme");
const prov: Provenance = {
  sourceSystem: "test", sourceObject: "je", sourceVersion: "1",
  effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: "2026-08-01",
  normalizationVersion: "1", mappingVersion: "1",
};
function acct(id: string, code: string, type: AccountType): Account {
  return { id: asAccountId(id), code, name: code, type, currency: USD };
}
const coa = new ChartOfAccounts([
  acct("cash", "1000", AccountType.ASSET),
  acct("rev", "4000", AccountType.REVENUE),
  acct("exp", "5000", AccountType.EXPENSE),
]);
const m = (minor: bigint) => Money.fromMinorUnits(minor, USD);

test("dimension registry rejects unknown keys and disallowed values", () => {
  const reg = new DimensionRegistry([
    { key: "class", label: "Class", values: ["Retail", "Wholesale"] },
    { key: "location", label: "Location", values: [] }, // open dimension
  ]);
  assert.throws(() => reg.validateLineDimensions({ clas: "Retail" }), DimensionError); // typo key
  assert.throws(() => reg.validateLineDimensions({ class: "Nope" }), DimensionError); // bad value
  assert.doesNotThrow(() => reg.validateLineDimensions({ class: "Retail", location: "Austin" }));
});

test("required dimensions must be present on every line", () => {
  const reg = new DimensionRegistry([{ key: "class", label: "Class", values: ["A"], required: true }]);
  assert.throws(() => reg.validateLineDimensions({}), DimensionError);
  assert.doesNotThrow(() => reg.validateLineDimensions({ class: "A" }));
});

test("trial balance splits revenue by class dimension", async () => {
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(coa, store);
  const post = (key: string, lines: JournalLineInput[]) => {
    const cmd: PostCommand = {
      tenantId: tenant, idempotencyKey: asIdempotencyKey(key), periodKey: asPeriodKey("2026-08"),
      currency: USD, entryDate: "2026-08-15", lines, provenance: prov,
    };
    return engine.post(cmd, { postedAt: "2026-08-15T00:00:00Z" });
  };
  await post("s1", [
    { accountId: asAccountId("cash"), side: "DEBIT", amount: m(10000n) },
    { accountId: asAccountId("rev"), side: "CREDIT", amount: m(10000n), dimensions: { class: "Retail" } },
  ]);
  await post("s2", [
    { accountId: asAccountId("cash"), side: "DEBIT", amount: m(4000n) },
    { accountId: asAccountId("rev"), side: "CREDIT", amount: m(4000n), dimensions: { class: "Wholesale" } },
  ]);

  const byClass = await trialBalanceByDimension(store, tenant, coa, USD, "class");
  // Retail revenue 100, Wholesale 40; cash (no class) in UNASSIGNED bucket.
  const retail = byClass.get("Retail")!;
  assert.equal(retail.rows.find((r) => r.code === "4000")!.credit.toDecimalString(), "100.00");
  assert.equal(byClass.get("Wholesale")!.rows.find((r) => r.code === "4000")!.credit.toDecimalString(), "40.00");
  assert.ok(byClass.get(UNASSIGNED)!.rows.some((r) => r.code === "1000"));
});
