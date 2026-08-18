import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Sales tax, credit memos and refunds — the three things that turn invoicing
 * from a demo into something a real business can run on.
 *
 * The distinctions being defended here:
 *   - collected sales tax is a LIABILITY, not revenue. It is the state's money.
 *   - a credit reduces what is owed; a refund moves cash. Conflating them puts
 *     money on the balance sheet that has already been sent back.
 *   - nothing is ever edited. A wrong invoice keeps saying what it said, and a
 *     credit says what changed.
 */

const NOW = "2026-08-20T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string, body: unknown = "",
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query: {},
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Row = Record<string, unknown>;

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await call(s, "POST", "/t/acme/customers", { id: "halcyon", name: "Halcyon LLC", terms_days: 30 });
  await call(s, "POST", "/t/acme/vendors", { id: "copyshop", name: "Copyshop", terms_days: 15 });
  return s;
}

async function balance(s: LedgerService, code: string): Promise<bigint> {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const row = (tb["rows"] as Row[]).find((r) => r["code"] === code);
  if (!row) return 0n;
  return BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
}

const inBalance = async (s: LedgerService): Promise<boolean> =>
  obj(await call(s, "GET", "/t/acme/trial-balance"))["in_balance"] === true;

const doc = async (s: LedgerService, kind: string, id: string): Promise<Row> =>
  obj(await call(s, "GET", `/t/acme/${kind}/${id}`))["document"] as Row;

// --- sales tax ---------------------------------------------------------------

test("sales tax is collected as a liability, not booked as revenue", async () => {
  const s = await ready();
  // $1,000 of work at 8.25%
  const r = await call(s, "POST", "/t/acme/invoices", {
    id: "INV-1", party_id: "halcyon", date: "2026-08-01", memo: "August work",
    tax_rate_ppm: 82_500,
    lines: [{ description: "Consulting", unit_amount_minor: "100000", account_code: "4100" }],
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const d = (obj(r)["document"]) as Row;
  assert.equal(d["net_minor"], "100000");
  assert.equal(d["tax_minor"], "8250");
  assert.equal(d["total_minor"], "108250", "the customer owes net plus tax");
  assert.equal(d["open_minor"], "108250");

  assert.equal(await balance(s, "1200"), 108250n, "AR is the full amount owed");
  assert.equal(await balance(s, "4100"), -100000n, "revenue is the NET only");
  assert.equal(-(await balance(s, "2200")), 8250n, "the tax is a liability");
  assert.ok(await inBalance(s));
});

test("tax is rounded half-up on the exact minor units, never through a float", async () => {
  const s = await ready();
  // 7.25% of $33.33 = 2.416425 → 2.42
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-R", party_id: "halcyon", date: "2026-08-01",
    tax_rate_ppm: 72_500,
    lines: [{ unit_amount_minor: "3333", account_code: "4100" }],
  });
  const d = await doc(s, "invoices", "INV-R");
  assert.equal(d["tax_minor"], "242");
  assert.equal(d["total_minor"], "3575");
});

test("an exempt line is not taxed while a taxable one on the same invoice is", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-2", party_id: "halcyon", date: "2026-08-01",
    tax_rate_ppm: 100_000,   // a round 10% to make the arithmetic readable
    lines: [
      { description: "Goods", unit_amount_minor: "50000", account_code: "4100" },
      { description: "Exempt services", unit_amount_minor: "50000", account_code: "4100", taxable: false },
    ],
  });
  const d = await doc(s, "invoices", "INV-2");
  assert.equal(d["net_minor"], "100000");
  assert.equal(d["tax_minor"], "5000", "only the taxable half is taxed");
  assert.equal((d["lines"] as Row[])[1]!["taxable"], false);
});

test("no rate means no tax line at all, and the old behaviour is unchanged", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-3", party_id: "halcyon", date: "2026-08-01",
    lines: [{ unit_amount_minor: "100000", account_code: "4100" }],
  });
  const d = await doc(s, "invoices", "INV-3");
  assert.equal(d["tax_minor"], "0");
  assert.equal(d["total_minor"], "100000");
  assert.equal(await balance(s, "2200"), 0n);
});

test("a nonsensical tax rate is refused", async () => {
  const s = await ready();
  const bad = async (rate: unknown): Promise<number> =>
    (await call(s, "POST", "/t/acme/invoices", {
      id: `INV-${String(rate)}`, party_id: "halcyon", date: "2026-08-01",
      tax_rate_ppm: rate,
      lines: [{ unit_amount_minor: "1000", account_code: "4100" }],
    })).status;
  assert.equal(await bad(-1), 400);
  assert.equal(await bad(1_500_000), 400);   // 150% is a typo
  assert.equal(await bad(82.5), 400);        // percent, not ppm — caught
});

test("a bill is not sales-taxed — the tax it bears is part of its cost", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/bills", {
    id: "BILL-1", party_id: "copyshop", date: "2026-08-02",
    tax_rate_ppm: 82_500,
    lines: [{ unit_amount_minor: "10000", account_code: "6400" }],
  });
  const d = await doc(s, "bills", "BILL-1");
  assert.equal(d["tax_minor"], "0");
  assert.equal(d["total_minor"], "10000");
  assert.equal(await balance(s, "2200"), 0n);
});

test("collecting a taxed invoice clears the whole amount including the tax", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-4", party_id: "halcyon", date: "2026-08-01",
    tax_rate_ppm: 82_500,
    lines: [{ unit_amount_minor: "100000", account_code: "4100" }],
  });
  const paid = await call(s, "POST", "/t/acme/invoices/INV-4/payments", {
    date: "2026-08-15", amount_minor: "108250",
  });
  assert.equal(paid.status, 201, JSON.stringify(paid.body));
  const d = await doc(s, "invoices", "INV-4");
  assert.equal(d["status"], "PAID");
  assert.equal(await balance(s, "1000"), 108250n);
  assert.equal(await balance(s, "1200"), 0n);
  assert.equal(-(await balance(s, "2200")), 8250n, "the tax is still owed to the state");
});

// --- credit memos ------------------------------------------------------------

async function withInvoice(s: LedgerService, id = "INV-1"): Promise<void> {
  await call(s, "POST", "/t/acme/invoices", {
    id, party_id: "halcyon", date: "2026-08-01", memo: "August work",
    lines: [{ description: "Consulting", unit_amount_minor: "100000", account_code: "4100" }],
  });
}

test("a credit reduces what is owed and reverses the revenue", async () => {
  const s = await ready();
  await withInvoice(s);
  const r = await call(s, "POST", "/t/acme/invoices/INV-1/credits", {
    date: "2026-08-10", amount_minor: "25000", memo: "Overbilled two hours",
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(obj(r)["kind"], "credit");
  assert.equal(obj(r)["applied_minor"], "25000");
  assert.equal(((obj(r)["document"]) as Row)["open_minor"], "75000");

  const d = await doc(s, "invoices", "INV-1");
  assert.equal(d["open_minor"], "75000");
  assert.equal(d["status"], "PARTIAL");
  assert.equal(d["total_minor"], "100000", "the invoice still says what it said");

  assert.equal(await balance(s, "1200"), 75000n);
  assert.equal(await balance(s, "4100"), -75000n, "revenue came back down");
  assert.ok(await inBalance(s));

  // and BOTH entries exist — nothing was edited
  const entries = obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Row[];
  assert.equal(entries.length, 2);
});

test("crediting the whole balance settles the invoice", async () => {
  const s = await ready();
  await withInvoice(s);
  await call(s, "POST", "/t/acme/invoices/INV-1/credits", { date: "2026-08-10" });
  const d = await doc(s, "invoices", "INV-1");
  assert.equal(d["open_minor"], "0");
  assert.equal(d["status"], "PAID");
  assert.equal(await balance(s, "1200"), 0n);
  assert.equal(await balance(s, "4100"), 0n);
});

test("crediting more than is outstanding is refused", async () => {
  const s = await ready();
  await withInvoice(s);
  const r = await call(s, "POST", "/t/acme/invoices/INV-1/credits", {
    date: "2026-08-10", amount_minor: "150000",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /1000\.00 outstanding/);
});

test("crediting a settled invoice points at a refund instead", async () => {
  const s = await ready();
  await withInvoice(s);
  await call(s, "POST", "/t/acme/invoices/INV-1/payments", {
    date: "2026-08-05", amount_minor: "100000",
  });
  const r = await call(s, "POST", "/t/acme/invoices/INV-1/credits", { date: "2026-08-10" });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /refund, not a credit/);
});

test("a vendor credit reduces a bill the same way", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/bills", {
    id: "BILL-1", party_id: "copyshop", date: "2026-08-02",
    lines: [{ unit_amount_minor: "40000", account_code: "6400" }],
  });
  const r = await call(s, "POST", "/t/acme/bills/BILL-1/credits", {
    date: "2026-08-06", amount_minor: "10000", memo: "Returned the wrong paper",
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const d = await doc(s, "bills", "BILL-1");
  assert.equal(d["open_minor"], "30000");
  assert.equal(-(await balance(s, "2000")), 30000n, "AP came down");
  assert.equal(await balance(s, "6400"), 30000n, "so did the expense");
  assert.ok(await inBalance(s));
});

test("a credit is refused on an unknown document or a bad date", async () => {
  const s = await ready();
  await withInvoice(s);
  assert.equal((await call(s, "POST", "/t/acme/invoices/NOPE/credits",
    { date: "2026-08-10" })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/invoices/INV-1/credits",
    { date: "August" })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/invoices/INV-1/credits",
    { date: "2026-08-10", amount_minor: "0" })).status, 400);
});

// --- refunds -----------------------------------------------------------------

test("a refund sends the money back and reverses the revenue", async () => {
  const s = await ready();
  await withInvoice(s);
  await call(s, "POST", "/t/acme/invoices/INV-1/payments", {
    date: "2026-08-05", amount_minor: "100000",
  });
  assert.equal(await balance(s, "1000"), 100000n);

  const r = await call(s, "POST", "/t/acme/invoices/INV-1/refunds", {
    date: "2026-08-12", amount_minor: "30000", memo: "Cancelled the last sprint",
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(obj(r)["kind"], "refund");

  assert.equal(await balance(s, "1000"), 70000n, "cash physically went back out");
  assert.equal(await balance(s, "4100"), -70000n, "revenue came down with it");
  assert.equal(await balance(s, "1200"), 0n, "the invoice stays settled");
  assert.ok(await inBalance(s));
});

test("refunding more than was ever collected is refused", async () => {
  const s = await ready();
  await withInvoice(s);
  await call(s, "POST", "/t/acme/invoices/INV-1/payments", {
    date: "2026-08-05", amount_minor: "40000",
  });
  const r = await call(s, "POST", "/t/acme/invoices/INV-1/refunds", {
    date: "2026-08-12", amount_minor: "100000",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /only 400\.00 has been collected/);
});

test("refunding an unpaid invoice points at a credit instead", async () => {
  const s = await ready();
  await withInvoice(s);
  const r = await call(s, "POST", "/t/acme/invoices/INV-1/refunds", { date: "2026-08-12" });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /credit instead/);
});

test("a bill cannot be refunded — that is a vendor credit", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/bills", {
    id: "BILL-1", party_id: "copyshop", date: "2026-08-02",
    lines: [{ unit_amount_minor: "10000", account_code: "6400" }],
  });
  const r = await call(s, "POST", "/t/acme/bills/BILL-1/refunds", { date: "2026-08-12" });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /only an invoice can be refunded/);
});

test("credits and refunds respect a closed period", async () => {
  const s = await ready();
  await withInvoice(s);
  await call(s, "POST", "/t/acme/periods/2026-08/lock", {});
  assert.equal((await call(s, "POST", "/t/acme/invoices/INV-1/credits",
    { date: "2026-08-10" })).status, 409);
});
