import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import {
  InMemoryBackend,
  LedgerService,
  PostgresBackend,
  type LedgerBackend,
  type ServiceResponse,
} from "../src/index.js";

/**
 * Cutover from QuickBooks to RGNR8 (issues 39-41): month-end only, checked by a
 * plan that writes nothing, run only with the signer's name and the plan's
 * fingerprint, the open items becoming real documents without being posted
 * twice, and a way back until RGNR8 has recorded anything of its own.
 */

const NOW = "2026-11-05T12:00:00Z";

const call = (s: LedgerService, method: string, path: string, body: unknown = ""): Promise<ServiceResponse> =>
  s.handle({ method, path, query: {}, body: typeof body === "string" ? body : JSON.stringify(body), headers: {} });
const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

/** A parallel-run account: QuickBooks go-live, a catch-up after Oct 31, the open items copied. */
async function mirrored(backend: LedgerBackend = new InMemoryBackend(), snapshotAsOf = "2026-11-02") {
  await backend.migrate();
  const s = new LedgerService(backend, { now: () => NOW });
  const live = await call(s, "POST", "/t/acme/go-live", {
    contract: "go-live/1", tenant_id: "acme", source_system: "quickbooks", cutover_date: "2026-09-30",
    currency: "USD", coa_category: "PROFESSIONAL_SERVICES", opening_balance_equity_code: "3010",
    source_accounts: [
      { code: "1000", name: "Checking", balance_minor: "500000", subtype: "BANK" },
      { code: "1200", name: "A/R", balance_minor: "38995", subtype: "ACCOUNTS_RECEIVABLE" },
      { code: "2000", name: "A/P", balance_minor: "-4500", subtype: "ACCOUNTS_PAYABLE" },
      { code: "3900", name: "Retained Earnings", balance_minor: "-534495", subtype: "RETAINED_EARNINGS" },
    ],
  });
  assert.equal(live.status, 200, JSON.stringify(live.body));
  await call(s, "POST", "/t/acme/settings", { book_of_record: "quickbooks", mirror_realm_id: "9341" });
  const catchUp = await call(s, "POST", "/t/acme/entries", {
    date: "2026-11-01", source: "quickbooks", idempotency_key: "qbo-mirror:2026-11-01:x",
    lines: [{ code: "1000", side: "DEBIT", amount_minor: "100" }, { code: "4000", side: "CREDIT", amount_minor: "100" }],
  });
  assert.equal(catchUp.status, 201, JSON.stringify(catchUp.body));
  await call(s, "POST", "/t/acme/mirror/open-items", {
    as_of: snapshotAsOf,
    invoices: [
      { qbo_id: "130", number: "1037", party: "Northwind", date: "2026-10-01", due_date: "2026-10-31", open_minor: "28995" },
      { qbo_id: "131", number: "1038", party: "Fabrikam", date: "2026-10-15", due_date: "2026-11-14", open_minor: "10000" },
    ],
    bills: [{ qbo_id: "77", party: "Copyshop", date: "2026-10-10", due_date: "2026-11-09", open_minor: "4500" }],
  });
  return s;
}

async function plan(s: LedgerService, date = "2026-10-31"): Promise<Record<string, unknown>> {
  return obj(await call(s, "POST", "/t/acme/cutover/plan", { cutover_date: date }))["plan"] as Record<string, unknown>;
}

async function journalSize(s: LedgerService): Promise<number> {
  return (obj(await call(s, "GET", "/t/acme/entries"))["entries"] as unknown[]).length;
}

test("the plan ties the open items to A/R and A/P and writes nothing", async () => {
  const s = await mirrored();
  const before = await journalSize(s);
  const p = await plan(s);
  assert.deepEqual(p["blockers"], []);
  assert.equal(p["ready"], true);
  const tie = p["tie_out"] as Array<Record<string, string>>;
  assert.deepEqual(tie.map((t) => [t["control"], t["books_minor"], t["open_items_minor"], t["difference_minor"]]), [
    ["Accounts Receivable", "38995", "38995", "0"],
    ["Accounts Payable", "4500", "4500", "0"],
  ]);
  assert.deepEqual((p["documents"] as Array<Record<string, string>>).map((d) => d["id"]),
    ["1037", "1038", "QBO-BILL-77"]);
  assert.equal(await journalSize(s), before);
});

test("cutover is month-end only, and only once the month has ended", async () => {
  const s = await mirrored();
  assert.match(String((await plan(s, "2026-10-15"))["blockers"]), /not the last day of a month/);
  assert.match(String((await plan(s, "2026-11-30"))["blockers"]), /hasn't ended yet/);
});

test("it won't plan from books that aren't a level, pure QuickBooks copy", async () => {
  const stale = await mirrored(new InMemoryBackend(), "2026-10-20");
  assert.match(String((await plan(stale))["blockers"]), /copied on 2026-10-20.*Refresh after 2026-10-31/);
  const s = await mirrored();
  await call(s, "POST", "/t/acme/settings", { book_of_record: "rgnr8" });
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-11-03", lines: [{ code: "1000", side: "DEBIT", amount_minor: "5" }, { code: "4000", side: "CREDIT", amount_minor: "5" }],
  });
  const blockers = String((await plan(s))["blockers"]);
  assert.match(blockers, /already the book of record/);
  assert.match(blockers, /1 entry that didn't come from QuickBooks/);
});

test("execute needs the signer and the fingerprint of the plan they signed", async () => {
  const s = await mirrored();
  const p = await plan(s);
  const unsigned = await call(s, "POST", "/t/acme/cutover/execute",
    { cutover_date: "2026-10-31", signed_by: " ", report_hash: p["report_hash"] });
  assert.equal(unsigned.status, 409);
  const other = await call(s, "POST", "/t/acme/cutover/execute",
    { cutover_date: "2026-10-31", signed_by: "J. Accountant", report_hash: "0".repeat(64) });
  assert.equal(other.status, 409);
  assert.match(String(obj(other)["error"]), /changed after the report was signed/);
});

test("a difference has to be accepted by the signer", async () => {
  const s = await mirrored();
  await call(s, "POST", "/t/acme/mirror/open-items", {
    as_of: "2026-11-02",
    invoices: [{ qbo_id: "130", number: "1037", party: "Northwind", date: "2026-10-01", open_minor: "28995" }],
    bills: [{ qbo_id: "77", party: "Copyshop", date: "2026-10-10", open_minor: "4500" }],
  });
  const p = await plan(s);
  const refused = await call(s, "POST", "/t/acme/cutover/execute",
    { cutover_date: "2026-10-31", signed_by: "J. Accountant", report_hash: p["report_hash"] });
  assert.equal(refused.status, 409);
  assert.match(String(obj(refused)["error"]), /Accounts Receivable don't tie/);
  const accepted = await call(s, "POST", "/t/acme/cutover/execute",
    { cutover_date: "2026-10-31", signed_by: "J. Accountant", report_hash: p["report_hash"], accept_differences: true });
  assert.equal(accepted.status, 200);
});

test("cutover makes the open items real documents without posting them twice", async () => {
  const s = await mirrored();
  const p = await plan(s);
  const before = await journalSize(s);
  const done = await call(s, "POST", "/t/acme/cutover/execute",
    { cutover_date: "2026-10-31", signed_by: "J. Accountant", report_hash: p["report_hash"] });
  assert.equal(done.status, 200, JSON.stringify(done.body));
  assert.equal(await journalSize(s), before, "nothing posted");
  const inv = obj(await call(s, "GET", "/t/acme/invoices/1037"));
  const doc = inv["document"] as Record<string, unknown>;
  assert.equal(doc["open_minor"], "28995");
  assert.equal(doc["status"], "OPEN");
  const settings = obj(await call(s, "GET", "/t/acme/settings"))["settings"] as Record<string, unknown>;
  assert.equal(settings["book_of_record"], "rgnr8");
  assert.equal(obj(await call(s, "GET", "/t/acme/mirror/open-items"))["as_of"], "");
  // October is closed: nothing can be posted back into it.
  const late = await call(s, "POST", "/t/acme/entries", {
    date: "2026-10-20", lines: [{ code: "1000", side: "DEBIT", amount_minor: "5" }, { code: "4000", side: "CREDIT", amount_minor: "5" }],
  });
  assert.equal(late.status, 409);
  // Collecting the invoice clears A/R: the document and the balance were the same money.
  const paid = await call(s, "POST", "/t/acme/invoices/1037/payments", { date: "2026-11-06", amount_minor: "28995", bank_code: "1000" });
  assert.equal(paid.status, 201, JSON.stringify(paid.body));
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"))["rows"] as Array<Record<string, string>>;
  const ar = tb.find((r) => r["code"] === "1200")!;
  assert.equal(BigInt(ar["debit_minor"]!) - BigInt(ar["credit_minor"]!), 10000n);
});

test("backout restores QuickBooks while RGNR8 has recorded nothing of its own", async () => {
  const s = await mirrored();
  const p = await plan(s);
  await call(s, "POST", "/t/acme/cutover/execute",
    { cutover_date: "2026-10-31", signed_by: "J. Accountant", report_hash: p["report_hash"] });
  assert.equal(obj(await call(s, "GET", "/t/acme/cutover"))["backout_blocker"], "");
  const back = await call(s, "POST", "/t/acme/cutover/backout");
  assert.equal(back.status, 200, JSON.stringify(back.body));
  assert.equal((obj(back)["record"] as Record<string, unknown>)["state"], "backed_out");
  assert.equal((await call(s, "GET", "/t/acme/invoices/1037")).status, 404);
  const settings = obj(await call(s, "GET", "/t/acme/settings"))["settings"] as Record<string, unknown>;
  assert.equal(settings["book_of_record"], "quickbooks");
});

test("there is no way back once RGNR8 has recorded its first entry or payment", async () => {
  for (const firstThing of ["entry", "payment"]) {
    const s = await mirrored();
    const p = await plan(s);
    await call(s, "POST", "/t/acme/cutover/execute",
      { cutover_date: "2026-10-31", signed_by: "J. Accountant", report_hash: p["report_hash"] });
    if (firstThing === "entry") {
      await call(s, "POST", "/t/acme/entries", {
        date: "2026-11-06", lines: [{ code: "1000", side: "DEBIT", amount_minor: "5" }, { code: "4000", side: "CREDIT", amount_minor: "5" }],
      });
    } else {
      await call(s, "POST", "/t/acme/invoices/1038/payments", { date: "2026-11-06", amount_minor: "100", bank_code: "1000" });
    }
    const refused = await call(s, "POST", "/t/acme/cutover/backout");
    assert.equal(refused.status, 409, firstThing);
    assert.notEqual(obj(await call(s, "GET", "/t/acme/cutover"))["backout_blocker"], "", firstThing);
  }
});

test("the whole cutover and backout run on Postgres too", async () => {
  const pool = new (newDb().adapters.createPg().Pool)();
  const s = await mirrored(new PostgresBackend(pool as never, { enforceRls: false }));
  const p = await plan(s);
  assert.deepEqual(p["blockers"], []);
  const done = await call(s, "POST", "/t/acme/cutover/execute",
    { cutover_date: "2026-10-31", signed_by: "J. Accountant", report_hash: p["report_hash"] });
  assert.equal(done.status, 200, JSON.stringify(done.body));
  assert.equal((await call(s, "GET", "/t/acme/invoices/1037")).status, 200);
  assert.equal((await call(s, "POST", "/t/acme/cutover/backout")).status, 200);
  assert.equal((await call(s, "GET", "/t/acme/invoices/1037")).status, 404);
});
