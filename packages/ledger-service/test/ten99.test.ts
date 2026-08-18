import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * 1099-NEC contractor reporting.
 *
 * The mechanics are easy and the timing is what hurts. The threshold is crossed
 * in March, the W-9 is never collected, and it surfaces in January when the
 * contractor is unreachable. So what these tests defend is the *warnings*: who
 * is over the threshold with no tax id, who is under it but heading there, and
 * what money left the bank to a contractor without ever going through a bill.
 */

const NOW = "2027-01-15T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string,
  body: unknown = "", query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Row = Record<string, unknown>;

async function vendor(
  s: LedgerService, id: string, name: string, extra: Record<string, unknown> = {},
): Promise<void> {
  const r = await call(s, "POST", "/t/acme/vendors", { id, name, ...extra });
  assert.equal(r.status, 201, JSON.stringify(r.body));
}

/** Bill a vendor and pay it — the path where a payment is tied to a vendor. */
async function billAndPay(
  s: LedgerService, vendorId: string, docId: string, date: string, minor: string,
  paid = minor,
): Promise<void> {
  await call(s, "POST", "/t/acme/bills", {
    id: docId, party_id: vendorId, date,
    lines: [{ unit_amount_minor: minor, account_code: "6600" }],
  });
  if (paid !== "0") {
    const r = await call(s, "POST", `/t/acme/bills/${docId}/payments`, {
      date, amount_minor: paid,
    });
    assert.equal(r.status, 201, JSON.stringify(r.body));
  }
}

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  return s;
}

const report = (s: LedgerService, year = "2026") =>
  call(s, "GET", `/t/acme/1099/${year}`);

// --- flagging ----------------------------------------------------------------

test("a vendor can be flagged as a contractor before their W-9 arrives", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana Ruiz Design", { is_1099: true });
  const listed = obj(await call(s, "GET", "/t/acme/vendors"))["parties"] as Row[];
  assert.equal(listed[0]!["is1099"], true);
  assert.equal(listed[0]!["taxId"], undefined);
});

test("a tax id is stored when it is collected", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana Ruiz Design", { is_1099: true, tax_id: "12-3456789" });
  const listed = obj(await call(s, "GET", "/t/acme/vendors"))["parties"] as Row[];
  assert.equal(listed[0]!["taxId"], "12-3456789");
});

// --- the report --------------------------------------------------------------

test("only flagged vendors appear, and only what was actually paid", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana Ruiz Design", { is_1099: true, tax_id: "12-3456789" });
  await vendor(s, "landlord", "Riverside Properties");   // not a contractor
  await billAndPay(s, "dana", "B-1", "2026-03-10", "400000");
  await billAndPay(s, "landlord", "B-2", "2026-03-01", "350000");
  // billed but unpaid: not compensation until it is paid
  await billAndPay(s, "dana", "B-3", "2026-11-01", "100000", "0");

  const r = await report(s);
  assert.equal(r.status, 200);
  assert.equal(obj(r)["contract"], "form-1099/1");
  const rows = obj(r)["rows"] as Row[];
  assert.equal(rows.length, 1);
  assert.equal(rows[0]!["vendor_name"], "Dana Ruiz Design");
  assert.equal(rows[0]!["amount_minor"], "400000");
  assert.equal(rows[0]!["needs_w9"], false);
  assert.equal(obj(r)["total_minor"], "400000");
});

test("a partial payment counts what was paid, not what was billed", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana", { is_1099: true, tax_id: "1" });
  await billAndPay(s, "dana", "B-1", "2026-03-10", "400000", "150000");
  const rows = obj(await report(s))["rows"] as Row[];
  assert.equal(rows[0]!["amount_minor"], "150000");
});

test("the year is a boundary, not a suggestion", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana", { is_1099: true, tax_id: "1" });
  await billAndPay(s, "dana", "B-1", "2025-12-30", "300000");
  await billAndPay(s, "dana", "B-2", "2026-01-02", "400000");
  await billAndPay(s, "dana", "B-3", "2027-01-02", "500000");
  assert.equal((obj(await report(s, "2026"))["rows"] as Row[])[0]!["amount_minor"], "400000");
  assert.equal((obj(await report(s, "2025"))["rows"] as Row[])[0]!["amount_minor"], "300000");
});

// --- the warnings that matter ------------------------------------------------

test("a contractor over the threshold with no tax id is called out", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana Ruiz Design", { is_1099: true });   // no W-9 yet
  await billAndPay(s, "dana", "B-1", "2026-03-10", "400000");

  const r = await report(s);
  assert.deepEqual(obj(r)["missing_tax_id"], ["dana"]);
  const rows = obj(r)["rows"] as Row[];
  assert.equal(rows[0]!["needs_w9"], true);
  assert.equal(rows[0]!["tax_id"], "");
});

test("a contractor under the threshold is tracked with how far short they are", async () => {
  const s = await ready();
  await vendor(s, "sam", "Sam Okonkwo", { is_1099: true, tax_id: "9" });
  await billAndPay(s, "sam", "B-1", "2026-05-01", "54000");   // $540

  const r = await report(s);
  assert.equal((obj(r)["rows"] as Row[]).length, 0, "no form is filed yet");
  const below = obj(r)["below_threshold"] as Row[];
  assert.equal(below.length, 1);
  assert.equal(below[0]!["vendor_name"], "Sam Okonkwo");
  assert.equal(below[0]!["amount_minor"], "54000");
  assert.equal(below[0]!["short_by_minor"], "6000", "$60 from needing a form");
});

test("crossing the threshold moves a vendor from tracked to reportable", async () => {
  const s = await ready();
  await vendor(s, "sam", "Sam", { is_1099: true, tax_id: "9" });
  await billAndPay(s, "sam", "B-1", "2026-05-01", "54000");
  assert.equal((obj(await report(s))["below_threshold"] as Row[]).length, 1);

  await billAndPay(s, "sam", "B-2", "2026-06-01", "10000");
  const r = await report(s);
  assert.equal((obj(r)["below_threshold"] as Row[]).length, 0);
  assert.equal((obj(r)["rows"] as Row[])[0]!["amount_minor"], "64000");
});

test("a bank payment to a contractor that never went through a bill is flagged", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana Ruiz Design", { is_1099: true, tax_id: "1" });
  await billAndPay(s, "dana", "B-1", "2026-03-10", "400000");

  // paid directly from the bank, categorized as an expense, never billed
  await call(s, "POST", "/t/acme/feed/1000", {
    transactions: [{
      id: "bk-1", date: "2026-09-04", amount_minor: "-120000",
      description: "TRANSFER TO DANA", counterparty: "Dana Ruiz Design",
    }],
  });
  await call(s, "POST", "/t/acme/feed/txn/bk-1/accept", { category_code: "6600" });

  const r = await report(s);
  const missing = obj(r)["possibly_missing"] as Row[];
  assert.equal(missing.length, 1);
  assert.equal(missing[0]!["vendor_name"], "Dana Ruiz Design");
  assert.equal(missing[0]!["amount_minor"], "-120000");
  // it is a PROMPT, not folded into the total — guessing produces a figure
  // nobody can defend
  assert.equal((obj(r)["rows"] as Row[])[0]!["amount_minor"], "400000");
});

test("a name that differs only in case or punctuation still matches", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana Ruiz Design", { is_1099: true, tax_id: "1" });
  await call(s, "POST", "/t/acme/feed/1000", {
    transactions: [{
      id: "bk-1", date: "2026-09-04", amount_minor: "-1000",
      description: "X", counterparty: "DANA RUIZ  DESIGN.",
    }],
  });
  await call(s, "POST", "/t/acme/feed/txn/bk-1/accept", { category_code: "6600" });
  assert.equal((obj(await report(s))["possibly_missing"] as Row[]).length, 1);
});

test("money IN from a contractor is not a payment to them", async () => {
  const s = await ready();
  await vendor(s, "dana", "Dana", { is_1099: true, tax_id: "1" });
  await call(s, "POST", "/t/acme/feed/1000", {
    transactions: [{
      id: "bk-1", date: "2026-09-04", amount_minor: "50000",
      description: "REFUND", counterparty: "Dana",
    }],
  });
  await call(s, "POST", "/t/acme/feed/txn/bk-1/accept", { category_code: "6600" });
  assert.equal((obj(await report(s))["possibly_missing"] as Row[]).length, 0);
});

// --- refusals and isolation --------------------------------------------------

test("a nonsense year is refused", async () => {
  const s = await ready();
  assert.equal((await report(s, "last")).status, 400);
  assert.equal((await report(s, "26")).status, 400);
});

test("a year with no contractors reports nothing rather than failing", async () => {
  const s = await ready();
  const r = await report(s);
  assert.equal(r.status, 200);
  assert.equal((obj(r)["rows"] as Row[]).length, 0);
  assert.equal(obj(r)["total_minor"], "0");
});

test("one tenant's contractors are invisible to another", async () => {
  const s = await ready();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await vendor(s, "dana", "Dana", { is_1099: true, tax_id: "1" });
  await billAndPay(s, "dana", "B-1", "2026-03-10", "400000");
  const other = obj(await call(s, "GET", "/t/beta/1099/2026"));
  assert.equal((other["rows"] as Row[]).length, 0);
  assert.equal(other["total_minor"], "0");
});
