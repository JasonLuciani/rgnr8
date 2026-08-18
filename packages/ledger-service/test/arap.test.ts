import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Accounts receivable and payable end to end: invoice a customer and collect it,
 * enter a bill and pay it, and see aging — with the books staying balanced and
 * the control accounts tying to the open documents throughout.
 */

const NOW = "2026-08-20T00:00:00Z";

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { now: () => NOW });
}

const call = (
  s: LedgerService,
  method: string,
  path: string,
  body: unknown = "",
  query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({
    method,
    path,
    query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

/** A tenant with a chart, one customer and one vendor. */
async function ready(tenant = "acme"): Promise<LedgerService> {
  const s = svc();
  await call(s, "POST", `/t/${tenant}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
  await call(s, "POST", `/t/${tenant}/customers`, {
    id: "cust-1", name: "Northwind Ltd", email: "ap@northwind.com", terms_days: 30,
  });
  await call(s, "POST", `/t/${tenant}/vendors`, { id: "vend-1", name: "Copyshop", terms_days: 15 });
  return s;
}

/** The signed balance of one account from the trial balance (debit-positive). */
async function balance(s: LedgerService, tenant: string, code: string): Promise<bigint> {
  const tb = obj(await call(s, "GET", `/t/${tenant}/trial-balance`));
  const row = (tb["rows"] as Array<Record<string, unknown>>).find((r) => r["code"] === code);
  if (!row) return 0n;
  return BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
}

async function inBalance(s: LedgerService, tenant: string): Promise<boolean> {
  return obj(await call(s, "GET", `/t/${tenant}/trial-balance`))["in_balance"] === true;
}

// --- customers / vendors -----------------------------------------------------

test("customers and vendors are stored and listed", async () => {
  const s = await ready();
  const custs = obj(await call(s, "GET", "/t/acme/customers"))["parties"] as Array<Record<string, unknown>>;
  assert.equal(custs.length, 1);
  assert.equal(custs[0]!["name"], "Northwind Ltd");
  assert.equal(custs[0]!["termsDays"], 30);
  const vends = obj(await call(s, "GET", "/t/acme/vendors"))["parties"] as unknown[];
  assert.equal(vends.length, 1);
  // a party needs an id and a name
  assert.equal((await call(s, "POST", "/t/acme/customers", { name: "No id" })).status, 400);
});

// --- invoicing ---------------------------------------------------------------

test("an invoice debits AR, credits income, and shows as owed", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/invoices", {
    id: "INV-1001", party_id: "cust-1", date: "2026-08-01", memo: "August consulting",
    lines: [
      { description: "Consulting", quantity: 10, unit_amount_minor: "15000", account_code: "4100" },
      { description: "Travel", quantity: 1, unit_amount_minor: "42500", account_code: "4000" },
    ],
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const doc = obj(r)["document"] as Record<string, unknown>;
  assert.equal(doc["total_minor"], "192500"); // 10 × 150.00 + 425.00
  assert.equal(doc["open_minor"], "192500");
  assert.equal(doc["status"], "OPEN");
  assert.equal(doc["due_date"], "2026-08-31"); // net 30 from the customer's terms

  // the journal: AR up, income up, still balanced
  assert.equal(await balance(s, "acme", "1200"), 192500n);
  assert.equal(await balance(s, "acme", "4100"), -150000n);
  assert.equal(await balance(s, "acme", "4000"), -42500n);
  assert.ok(await inBalance(s, "acme"));
});

test("collecting an invoice moves AR to the bank and settles the document", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-1", party_id: "cust-1", date: "2026-08-01",
    lines: [{ unit_amount_minor: "100000", account_code: "4100" }],
  });

  // a partial collection
  const part = await call(s, "POST", "/t/acme/invoices/INV-1/payments", {
    date: "2026-08-15", amount_minor: "40000",
  });
  assert.equal(part.status, 201);
  let doc = obj(part)["document"] as Record<string, unknown>;
  assert.equal(doc["open_minor"], "60000");
  assert.equal(doc["status"], "PARTIAL");
  assert.equal(await balance(s, "acme", "1000"), 40000n); // cash in
  assert.equal(await balance(s, "acme", "1200"), 60000n); // AR down

  // the rest
  const rest = await call(s, "POST", "/t/acme/invoices/INV-1/payments", {
    date: "2026-08-20", amount_minor: "60000",
  });
  doc = obj(rest)["document"] as Record<string, unknown>;
  assert.equal(doc["open_minor"], "0");
  assert.equal(doc["status"], "PAID");
  assert.equal(await balance(s, "acme", "1200"), 0n); // AR cleared
  assert.equal(await balance(s, "acme", "1000"), 100000n);
  assert.ok(await inBalance(s, "acme"));

  // payments are recorded against the document
  const detail = obj(await call(s, "GET", "/t/acme/invoices/INV-1"));
  assert.equal((detail["payments"] as unknown[]).length, 2);
});

test("overpayment and paying a settled invoice are refused", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-2", party_id: "cust-1", date: "2026-08-01",
    lines: [{ unit_amount_minor: "50000", account_code: "4100" }],
  });
  const over = await call(s, "POST", "/t/acme/invoices/INV-2/payments", {
    date: "2026-08-10", amount_minor: "60000",
  });
  assert.equal(over.status, 400);
  assert.match(String(obj(over)["error"]), /exceeds/);

  await call(s, "POST", "/t/acme/invoices/INV-2/payments", { date: "2026-08-10", amount_minor: "50000" });
  const again = await call(s, "POST", "/t/acme/invoices/INV-2/payments", {
    date: "2026-08-11", amount_minor: "100",
  });
  assert.equal(again.status, 400);
  assert.match(String(obj(again)["error"]), /already settled/);
});

test("an invoice for an unknown customer, or with a bad line, is refused", async () => {
  const s = await ready();
  assert.equal((await call(s, "POST", "/t/acme/invoices", {
    id: "X", party_id: "nobody", date: "2026-08-01",
    lines: [{ unit_amount_minor: "100", account_code: "4100" }],
  })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/invoices", {
    id: "X", party_id: "cust-1", date: "2026-08-01",
    lines: [{ unit_amount_minor: "100", account_code: "9999" }],
  })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/invoices", {
    id: "X", party_id: "cust-1", date: "2026-08-01", lines: [],
  })).status, 400);
  // a duplicate id never books twice
  await call(s, "POST", "/t/acme/invoices", {
    id: "DUP", party_id: "cust-1", date: "2026-08-01",
    lines: [{ unit_amount_minor: "100", account_code: "4100" }],
  });
  assert.equal((await call(s, "POST", "/t/acme/invoices", {
    id: "DUP", party_id: "cust-1", date: "2026-08-02",
    lines: [{ unit_amount_minor: "999", account_code: "4100" }],
  })).status, 400);
});

// --- bills -------------------------------------------------------------------

test("a bill credits AP and debits expense; paying it clears AP from the bank", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/bills", {
    id: "BILL-1", party_id: "vend-1", date: "2026-08-02", memo: "Print run",
    lines: [{ description: "Brochures", unit_amount_minor: "36000", account_code: "6400" }],
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  assert.equal((obj(r)["document"] as Record<string, unknown>)["due_date"], "2026-08-17"); // net 15
  assert.equal(await balance(s, "acme", "2000"), -36000n); // AP is a credit balance
  assert.equal(await balance(s, "acme", "6400"), 36000n);

  const pay = await call(s, "POST", "/t/acme/bills/BILL-1/payments", {
    date: "2026-08-16", amount_minor: "36000",
  });
  assert.equal(pay.status, 201);
  assert.equal((obj(pay)["document"] as Record<string, unknown>)["status"], "PAID");
  assert.equal(await balance(s, "acme", "2000"), 0n);
  assert.equal(await balance(s, "acme", "1000"), -36000n); // cash out
  assert.ok(await inBalance(s, "acme"));
});

// --- the invariant that matters ---------------------------------------------

test("CONTROL TIE-OUT: the AR/AP control accounts equal the sum of open documents", async () => {
  const s = await ready();
  // three invoices, one partly collected, one fully collected
  for (const [id, amt] of [["A", "100000"], ["B", "250000"], ["C", "70000"]] as const) {
    await call(s, "POST", "/t/acme/invoices", {
      id, party_id: "cust-1", date: "2026-08-01",
      lines: [{ unit_amount_minor: amt, account_code: "4100" }],
    });
  }
  await call(s, "POST", "/t/acme/invoices/B/payments", { date: "2026-08-10", amount_minor: "100000" });
  await call(s, "POST", "/t/acme/invoices/C/payments", { date: "2026-08-11", amount_minor: "70000" });

  // two bills, one partly paid
  for (const [id, amt] of [["V1", "40000"], ["V2", "15000"]] as const) {
    await call(s, "POST", "/t/acme/bills", {
      id, party_id: "vend-1", date: "2026-08-03",
      lines: [{ unit_amount_minor: amt, account_code: "6400" }],
    });
  }
  await call(s, "POST", "/t/acme/bills/V1/payments", { date: "2026-08-12", amount_minor: "10000" });

  const sumOpen = async (kind: string): Promise<bigint> => {
    const docs = obj(await call(s, "GET", `/t/acme/${kind}`))["documents"] as Array<Record<string, unknown>>;
    return docs.reduce((acc, d) => acc + BigInt(String(d["open_minor"])), 0n);
  };

  // AR control (debit-positive) == open receivables; AP control == open payables
  assert.equal(await balance(s, "acme", "1200"), await sumOpen("invoices"));
  assert.equal(-(await balance(s, "acme", "2000")), await sumOpen("bills"));
  assert.equal(await sumOpen("invoices"), 250000n); // 100k open + 150k left on B
  assert.equal(await sumOpen("bills"), 45000n); // 30k left on V1 + 15k V2
  assert.ok(await inBalance(s, "acme"));
});

// --- aging -------------------------------------------------------------------

test("aging buckets the open receivables by how late they are", async () => {
  const s = await ready();
  // due 2026-08-31 (not yet due as of 08-20), and one long overdue
  await call(s, "POST", "/t/acme/invoices", {
    id: "CUR", party_id: "cust-1", date: "2026-08-01",
    lines: [{ unit_amount_minor: "100000", account_code: "4100" }],
  });
  await call(s, "POST", "/t/acme/invoices", {
    id: "LATE", party_id: "cust-1", date: "2026-05-01", due_date: "2026-05-31",
    lines: [{ unit_amount_minor: "30000", account_code: "4100" }],
  });
  const ar = obj(await call(s, "GET", "/t/acme/aging/ar", "", { as_of: "2026-08-20" }));
  assert.equal(ar["contract"], "aging/1");
  assert.equal(ar["kind"], "AR");
  assert.equal(ar["grand_total_minor"], "130000");
  const labels = ar["bucket_labels"] as string[];
  const row = (ar["rows"] as Array<Record<string, unknown>>)[0]!;
  const buckets = row["buckets_minor"] as string[];
  assert.equal(buckets[labels.indexOf("Current")], "100000"); // not yet due
  assert.equal(buckets[labels.indexOf("61-90")], "30000"); // 81 days late

  // once collected, it leaves the aging entirely
  await call(s, "POST", "/t/acme/invoices/LATE/payments", { date: "2026-08-20", amount_minor: "30000" });
  const after = obj(await call(s, "GET", "/t/acme/aging/ar", "", { as_of: "2026-08-20" }));
  assert.equal(after["grand_total_minor"], "100000");
});

test("AP aging reports what we owe vendors", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/bills", {
    id: "B1", party_id: "vend-1", date: "2026-06-01", due_date: "2026-06-15",
    lines: [{ unit_amount_minor: "22000", account_code: "6400" }],
  });
  const ap = obj(await call(s, "GET", "/t/acme/aging/ap", "", { as_of: "2026-08-20" }));
  assert.equal(ap["kind"], "AP");
  assert.equal(ap["grand_total_minor"], "22000");
});

// --- period locks + isolation ------------------------------------------------

test("a closed period refuses a new invoice", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/periods/2026-08/lock", {});
  const r = await call(s, "POST", "/t/acme/invoices", {
    id: "LOCKED", party_id: "cust-1", date: "2026-08-15",
    lines: [{ unit_amount_minor: "1000", account_code: "4100" }],
  });
  assert.equal(r.status, 409);
});

test("AR/AP is tenant-isolated", async () => {
  const s = await ready("acme");
  await call(s, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await call(s, "POST", "/t/beta/customers", { id: "cust-1", name: "Beta's own customer" });
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-9", party_id: "cust-1", date: "2026-08-01",
    lines: [{ unit_amount_minor: "500000", account_code: "4100" }],
  });

  const betaDocs = obj(await call(s, "GET", "/t/beta/invoices"))["documents"] as unknown[];
  assert.equal(betaDocs.length, 0);
  assert.equal(obj(await call(s, "GET", "/t/beta/aging/ar"))["grand_total_minor"], "0");
  // and beta's customer list is its own
  const betaCust = obj(await call(s, "GET", "/t/beta/customers"))["parties"] as Array<Record<string, unknown>>;
  assert.equal(betaCust[0]!["name"], "Beta's own customer");
});
