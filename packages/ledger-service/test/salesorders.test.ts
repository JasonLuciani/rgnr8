import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Sales orders.
 *
 * The order is the only place a *commitment* can live. Invoice the whole job up
 * front and the books carry revenue and a receivable for work not yet done;
 * don't record it at all and the backlog exists only in somebody's head. So the
 * order does not post — it records what was agreed, what has been billed
 * against it, and what is left.
 *
 * The property most worth defending is that over-invoicing is refused per line.
 * An order for doors and windows that has been fully billed for doors must not
 * accept another door because the windows are outstanding.
 */

const NOW = "2026-09-01T00:00:00Z";

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

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  await call(s, "POST", "/t/acme/cost-codes/seed", {});
  await call(s, "POST", "/t/acme/customers", { id: "harper", name: "Harper Residence" });
  return s;
}

const ORDER = {
  id: "SO-1",
  customer_id: "harper",
  date: "2026-06-01",
  requested_date: "2026-08-15",
  memo: "Doors and windows",
  lines: [
    { description: "Interior doors", cost_code: "MAT", quantity_milli: "10000",
      unit_price_minor: "45000", account_code: "4100" },
    { description: "Windows", cost_code: "MAT", quantity_milli: "4000",
      unit_price_minor: "120000", account_code: "4100" },
  ],
};

const make = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/sales-orders", { ...ORDER, ...extra });

const get = async (s: LedgerService, id = "SO-1"): Promise<Row> =>
  obj(await call(s, "GET", `/t/acme/sales-orders/${id}`))["order"] as Row;

// --- the commitment ----------------------------------------------------------

test("an order records what was agreed and posts nothing", async () => {
  const s = await ready();
  const r = await make(s);
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const totals = (obj(r)["order"] as Row)["totals"] as Row;
  assert.equal(totals["ordered_minor"], "930000", "10 doors at 450 + 4 windows at 1,200");
  assert.equal(totals["remaining_minor"], "930000");

  // nothing is in the books: agreeing to work is not revenue
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  assert.deepEqual(tb["rows"], []);
});

test("an order can be raised straight off an accepted estimate", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/estimates", {
    id: "EST-9", customer_id: "harper", date: "2026-05-01", memo: "Harper kitchen",
    lines: [{
      description: "Cabinets", cost_code: "MAT", quantity_milli: "1000",
      unit_cost_minor: "1000000", markup_ppm: 200_000, account_code: "4100",
    }],
  });
  await call(s, "POST", "/t/acme/estimates/EST-9/accept", { job_name: "Harper kitchen" });

  const r = await call(s, "POST", "/t/acme/sales-orders", { from_estimate: "EST-9" });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const order = obj(r)["order"] as Row;
  assert.equal(order["id"], "SO-EST-9");
  assert.equal(order["customer_id"], "harper");
  assert.ok(String(order["job_id"]).length > 0, "it inherits the estimate's job");
  assert.equal(((order["lines"] as Row[])[0]!)["unit_price_minor"], "1200000",
    "the price, not the cost");
});

test("an estimate that was never accepted cannot become an order", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/estimates", {
    id: "EST-9", customer_id: "harper", date: "2026-05-01",
    lines: [{ cost_code: "MAT", unit_cost_minor: "100", account_code: "4100" }],
  });
  const r = await call(s, "POST", "/t/acme/sales-orders", { from_estimate: "EST-9" });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /only an accepted estimate/);
});

// --- drawing it down ---------------------------------------------------------

test("invoicing part of an order bills only that part and leaves the rest", async () => {
  const s = await ready();
  await make(s);
  const r = await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-1", date: "2026-07-01",
    lines: [{ line_no: 1, quantity_milli: "4000" }],
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  assert.equal((obj(r)["invoice"] as Row)["total_minor"], "180000", "4 doors at 450");

  const order = await get(s);
  assert.equal(order["status"], "PARTIAL");
  const lines = order["lines"] as Row[];
  assert.equal(lines[0]!["invoiced_milli"], "4000");
  assert.equal(lines[0]!["remaining_milli"], "6000");
  assert.equal((order["totals"] as Row)["remaining_minor"], "750000");
});

test("invoicing with no lines named bills everything still outstanding", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-1", date: "2026-07-01", lines: [{ line_no: 1, quantity_milli: "4000" }],
  });
  const rest = await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-2", date: "2026-08-01",
  });
  assert.equal((obj(rest)["invoice"] as Row)["total_minor"], "750000");
  const order = await get(s);
  assert.equal(order["status"], "FULFILLED");
  assert.equal((order["totals"] as Row)["remaining_minor"], "0");
});

test("over-invoicing is refused per line, not on the total", async () => {
  const s = await ready();
  await make(s);
  // bill all the doors
  await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-1", date: "2026-07-01", lines: [{ line_no: 1, quantity_milli: "10000" }],
  });
  // …then try another one, while the windows are still outstanding
  const r = await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-2", date: "2026-07-15", lines: [{ line_no: 1, quantity_milli: "1000" }],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /only 0 thousandths are left/);
  assert.equal((obj(await call(s, "GET", "/t/acme/invoices"))["documents"] as Row[]).length, 1);
});

test("an order with nothing left says so rather than posting an empty invoice", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", { id: "INV-1", date: "2026-07-01" });
  const r = await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-2", date: "2026-07-02",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /nothing left to invoice/);
});

test("a failed invoice does not draw the order down", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/periods/2026-07/lock", {});
  const r = await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-1", date: "2026-07-01",
  });
  assert.equal(r.status, 409, "the period is closed");
  const order = await get(s);
  assert.equal(order["status"], "OPEN");
  assert.equal(((order["lines"] as Row[])[0]!)["invoiced_milli"], "0");
});

test("what an order bills lands on its job", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs", {
    id: "harper-kitchen", customer_id: "harper", name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "930000",
  });
  await make(s, { job_id: "harper-kitchen" });
  await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", { id: "INV-1", date: "2026-07-01" });
  const cost = obj(await call(s, "GET", "/t/acme/jobs/harper-kitchen/cost"));
  assert.equal((cost["totals"] as Row)["revenue_minor"], "930000");
});

// --- the backlog -------------------------------------------------------------

test("the backlog is what is sold and unbilled, in the order it is wanted", async () => {
  const s = await ready();
  await make(s);
  await make(s, { id: "SO-2", requested_date: "2026-07-01", lines: [
    { description: "Deck", quantity_milli: "1000", unit_price_minor: "500000",
      account_code: "4100" },
  ] });
  await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-1", date: "2026-07-01", lines: [{ line_no: 1, quantity_milli: "10000" }],
  });

  const b = obj(await call(s, "GET", "/t/acme/sales-orders/backlog"));
  assert.equal(b["contract"], "sales-backlog/1");
  assert.equal(b["remaining_minor"], "980000", "the windows plus the deck");
  const rows = b["orders"] as Row[];
  assert.deepEqual(rows.map((o) => o["id"]), ["SO-2", "SO-1"], "soonest wanted first");
});

test("a fulfilled or cancelled order is out of the backlog", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", { id: "INV-1", date: "2026-07-01" });
  const b = obj(await call(s, "GET", "/t/acme/sales-orders/backlog"));
  assert.equal((b["orders"] as Row[]).length, 0);
  assert.equal(b["remaining_minor"], "0");
});

test("the backlog can be read for one job", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs", {
    id: "j1", customer_id: "harper", name: "J1",
    billing_method: "TIME_AND_MATERIALS", contract_minor: "0",
  });
  await make(s, { job_id: "j1" });
  await make(s, { id: "SO-2" });
  const b = obj(await call(s, "GET", "/t/acme/sales-orders/backlog", "", { job_id: "j1" }));
  assert.deepEqual((b["orders"] as Row[]).map((o) => o["id"]), ["SO-1"]);
});

// --- status and refusals -----------------------------------------------------

test("an order that has been billed against cannot be cancelled, only closed", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-1", date: "2026-07-01", lines: [{ line_no: 1, quantity_milli: "1000" }],
  });
  const cancel = await call(s, "POST", "/t/acme/sales-orders/SO-1/status", {
    status: "CANCELLED",
  });
  assert.equal(cancel.status, 400);
  assert.match(String(obj(cancel)["error"]), /close it instead/);
  assert.equal((await call(s, "POST", "/t/acme/sales-orders/SO-1/status", {
    status: "CLOSED",
  })).status, 200);
});

test("a cancelled order cannot be invoiced", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/sales-orders/SO-1/status", { status: "CANCELLED" });
  const r = await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-1", date: "2026-07-01",
  });
  assert.equal(r.status, 400);
});

test("an order that has been billed against cannot be rewritten", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/sales-orders/SO-1/invoice", {
    id: "INV-1", date: "2026-07-01", lines: [{ line_no: 1, quantity_milli: "1000" }],
  });
  const r = await make(s, { memo: "different" });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /already been invoiced/);
});

test("an order needs a real customer, a real job, and priced lines", async () => {
  const s = await ready();
  assert.equal((await make(s, { customer_id: "ghost" })).status, 400);
  assert.equal((await make(s, { job_id: "ghost" })).status, 400);
  assert.equal((await make(s, { lines: [] })).status, 400);
  assert.equal((await make(s, {
    lines: [{ quantity_milli: "1000", unit_price_minor: "0", account_code: "4100" }],
  })).status, 400);
  assert.equal((await make(s, {
    lines: [{ quantity_milli: "1000", unit_price_minor: "100", account_code: "5100" }],
  })).status, 400);
});

test("one tenant's orders are invisible to another", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  assert.equal((obj(await call(s, "GET", "/t/beta/sales-orders"))["orders"] as Row[]).length, 0);
  assert.equal(
    obj(await call(s, "GET", "/t/beta/sales-orders/backlog"))["remaining_minor"], "0",
  );
});
