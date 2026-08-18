import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Purchase orders, receiving, and the three-way match.
 *
 * A purchase order nobody matches is a PDF. The control exists only when what
 * was ordered, what arrived, and what the vendor billed all have to agree — so
 * these tests are mostly about refusals: you cannot receive more than you
 * ordered, you cannot bill more than arrived, and a price that differs from the
 * order has to be accepted deliberately.
 *
 * The other thing under test is committed cost, which is the number that stops
 * a job going over budget invisibly: the framing budget is not fine because
 * only half of it has been billed, if the rest is on a signed order.
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
  await call(s, "POST", "/t/acme/vendors", { id: "buildmart", name: "BuildMart Supply" });
  await call(s, "POST", "/t/acme/jobs", {
    id: "harper-kitchen", customer_id: "harper", name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "8500000",
  });
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "2000000" }],
  });
  return s;
}

/** 100 sheets of ply at $48.00, and 20 doors at $220.00. */
const PO = {
  id: "PO-1",
  vendor_id: "buildmart",
  job_id: "harper-kitchen",
  date: "2026-06-10",
  expected_date: "2026-06-20",
  lines: [
    { description: "Plywood", cost_code: "MAT", quantity_milli: "100000",
      unit_price_minor: "4800" },
    { description: "Doors", cost_code: "MAT", quantity_milli: "20000",
      unit_price_minor: "22000" },
  ],
};

const make = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/purchase-orders", { ...PO, ...extra });

const get = async (s: LedgerService, id = "PO-1"): Promise<Row> =>
  obj(await call(s, "GET", `/t/acme/purchase-orders/${id}`))["purchase_order"] as Row;

const balances = async (s: LedgerService): Promise<Record<string, bigint>> => {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const out: Record<string, bigint> = {};
  for (const row of tb["rows"] as Row[]) {
    out[String(row["code"])] =
      BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
  }
  return out;
};

// --- ordering ----------------------------------------------------------------

test("an order records the commitment and posts nothing", async () => {
  const s = await ready();
  const r = await make(s);
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const totals = (obj(r)["purchase_order"] as Row)["totals"] as Row;
  assert.equal(totals["ordered_minor"], "920000", "480,000 of ply + 440,000 of doors");
  assert.equal(totals["committed_minor"], "920000");
  assert.deepEqual((obj(await call(s, "GET", "/t/acme/trial-balance")))["rows"], []);
});

test("an order needs a real vendor, a real job and priced lines that can be costed", async () => {
  const s = await ready();
  assert.equal((await make(s, { vendor_id: "ghost" })).status, 400);
  assert.equal((await make(s, { job_id: "ghost" })).status, 400);
  assert.equal((await make(s, { lines: [] })).status, 400);
  const toRevenue = await make(s, {
    lines: [{ quantity_milli: "1000", unit_price_minor: "100", account_code: "4100" }],
  });
  assert.equal(toRevenue.status, 400);
  assert.match(String(obj(toRevenue)["error"]), /neither a cost account nor inventory/);
});

// --- committed cost ----------------------------------------------------------

test("committed cost shows on the job beside budget and actual", async () => {
  const s = await ready();
  await make(s);
  const mat = (obj(await call(s, "GET", "/t/acme/jobs/harper-kitchen/cost"))["rows"] as Row[])
    .find((r) => r["cost_code"] === "MAT")!;
  assert.equal(mat["actual_cost_minor"], "0", "nothing spent yet");
  assert.equal(mat["committed_minor"], "920000");
  assert.equal(mat["remaining_minor"], "1080000", "budget less spent less promised");
});

test("a budget that looks fine on actuals alone is flagged once commitments count", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "800000" }],
  });
  await make(s);
  const mat = (obj(await call(s, "GET", "/t/acme/jobs/harper-kitchen/cost"))["rows"] as Row[])
    .find((r) => r["cost_code"] === "MAT")!;
  assert.equal(mat["over_budget"], true, "nothing spent, and already over");
  assert.equal(mat["remaining_minor"], "-120000");
});

test("billing an order releases the commitment", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", { date: "2026-06-20" });
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25",
  });
  const mat = (obj(await call(s, "GET", "/t/acme/jobs/harper-kitchen/cost"))["rows"] as Row[])
    .find((r) => r["cost_code"] === "MAT")!;
  assert.equal(mat["committed_minor"], "0");
  assert.equal(mat["actual_cost_minor"], "920000", "it is a cost now, not a promise");
});

test("the commitments list is the where-is-it view, soonest first", async () => {
  const s = await ready();
  await make(s);
  await make(s, { id: "PO-2", expected_date: "2026-06-15", lines: [
    { description: "Hinges", cost_code: "MAT", quantity_milli: "1000",
      unit_price_minor: "15000" },
  ] });
  const c = obj(await call(s, "GET", "/t/acme/purchase-orders/committed"));
  assert.equal(c["contract"], "purchase-commitments/1");
  assert.equal(c["committed_minor"], "935000");
  assert.deepEqual((c["orders"] as Row[]).map((o) => o["id"]), ["PO-2", "PO-1"]);
});

// --- receiving ---------------------------------------------------------------

test("a partial delivery is recorded and the rest stays outstanding", async () => {
  const s = await ready();
  await make(s);
  const r = await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", lines: [{ line_no: 1, quantity_milli: "60000" }],
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const order = obj(r)["purchase_order"] as Row;
  assert.equal(order["status"], "PARTIAL");
  assert.equal(((order["lines"] as Row[])[0]!)["received_milli"], "60000");
  assert.deepEqual((obj(await call(s, "GET", "/t/acme/trial-balance")))["rows"], [],
    "receiving without accruing posts nothing");
});

test("receiving more than was ordered is refused", async () => {
  const s = await ready();
  await make(s);
  const r = await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", lines: [{ line_no: 1, quantity_milli: "120000" }],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /change the order, or send it back/);
});

test("receiving everything marks the order received", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", { date: "2026-06-20" });
  assert.equal((await get(s))["status"], "RECEIVED");
  const again = await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-21",
  });
  assert.equal(again.status, 400);
  assert.match(String(obj(again)["error"]), /fully received/);
});

test("accruing on receipt puts the cost on the job before the invoice arrives", async () => {
  const s = await ready();
  await make(s);
  const r = await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", accrue: true, lines: [{ line_no: 1 }],
  });
  assert.equal(obj(r)["accrued_minor"], "480000");
  const after = await balances(s);
  assert.equal(after["5100"], 480000n, "the job carries the material cost");
  assert.equal(after["2150"], -480000n, "…owed to a vendor who hasn't invoiced yet");

  const mat = (obj(await call(s, "GET", "/t/acme/jobs/harper-kitchen/cost"))["rows"] as Row[])
    .find((x) => x["cost_code"] === "MAT")!;
  assert.equal(mat["actual_cost_minor"], "480000");
  assert.equal(mat["committed_minor"], "440000", "only the doors are still a promise");
});

// --- the match ---------------------------------------------------------------

test("billing more than arrived is refused — you pay for what turned up", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", lines: [{ line_no: 1, quantity_milli: "60000" }],
  });
  const r = await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25",
    lines: [{ line_no: 1, quantity_milli: "100000" }],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /pay for what arrived/);
});

test("billing before anything arrived is refused", async () => {
  const s = await ready();
  await make(s);
  const r = await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /receive it first/);
});

test("a price that differs from the order is refused until it is accepted on purpose", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", { date: "2026-06-20" });

  const refused = await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25",
    lines: [{ line_no: 1, unit_price_minor: "5200" }],
  });
  assert.equal(refused.status, 400);
  assert.match(String(obj(refused)["error"]), /accept the variance deliberately/);

  const accepted = await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25", accept_variance: true,
    lines: [{ line_no: 1, unit_price_minor: "5200" }],
  });
  assert.equal(accepted.status, 201, JSON.stringify(accepted.body));
  const variances = obj(accepted)["variances"] as Row[];
  assert.equal(variances.length, 1);
  assert.equal(variances[0]!["variance_minor"], "40000", "40c a sheet over 100 sheets");
  assert.equal((obj(accepted)["bill"] as Row)["total_minor"], "520000");
});

test("billing against an accrual relieves it rather than booking the cost twice", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", accrue: true, lines: [{ line_no: 1 }],
  });
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25", lines: [{ line_no: 1 }],
  });
  const after = await balances(s);
  assert.equal(after["5100"], 480000n, "still one lot of material cost, not two");
  assert.equal(after["2150"] ?? 0n, 0n, "the accrual is cleared");
  assert.equal(after["2000"], -480000n, "…and it is owed to the vendor now");
});

test("a price change after an accrual posts an explicit variance on the job", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", accrue: true, lines: [{ line_no: 1 }],
  });
  const billed = await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25", accept_variance: true,
    lines: [{ line_no: 1, unit_price_minor: "5200" }],
  });
  assert.equal(billed.status, 201, JSON.stringify(billed.body));
  assert.ok(String(obj(billed)["variance_entry_id"]).length > 0, "the correction is a real entry");

  const after = await balances(s);
  assert.equal(after["5100"], 520000n, "the job carries what was actually charged");
  assert.equal(after["2150"] ?? 0n, 0n, "and nothing is left stranded in the clearing account");
  assert.equal(after["2000"], -520000n);
});

test("a cheaper bill than the accrual takes cost back off the job", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", accrue: true, lines: [{ line_no: 1 }],
  });
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25", accept_variance: true,
    lines: [{ line_no: 1, unit_price_minor: "4500" }],
  });
  const after = await balances(s);
  assert.equal(after["5100"], 450000n);
  assert.equal(after["2150"] ?? 0n, 0n);
});

test("two deliveries can be billed separately and the order keeps count", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", lines: [{ line_no: 1, quantity_milli: "60000" }],
  });
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-22", lines: [{ line_no: 1, quantity_milli: "60000" }],
  });
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", { date: "2026-07-01" });
  const second = await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-2", date: "2026-07-02",
  });
  assert.equal(second.status, 201, JSON.stringify(second.body));
  const order = obj(second)["purchase_order"] as Row;
  const totals = order["totals"] as Row;
  assert.equal(totals["billed_minor"], "920000");
  assert.equal(totals["committed_minor"], "0");
});

test("a failed bill leaves the order undrawn", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", { date: "2026-06-20" });
  await call(s, "POST", "/t/acme/periods/2026-06/lock", {});
  const r = await call(s, "POST", "/t/acme/purchase-orders/PO-1/bill", {
    id: "BILL-1", date: "2026-06-25",
  });
  assert.equal(r.status, 409);
  const order = await get(s);
  assert.equal(((order["lines"] as Row[])[0]!)["billed_milli"], "0");
});

// --- lifecycle and isolation -------------------------------------------------

test("an order with deliveries against it cannot be rewritten or cancelled", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/receipts", {
    date: "2026-06-20", lines: [{ line_no: 1, quantity_milli: "1000" }],
  });
  assert.equal((await make(s, { memo: "changed" })).status, 400);
  const cancel = await call(s, "POST", "/t/acme/purchase-orders/PO-1/status", {
    status: "CANCELLED",
  });
  assert.equal(cancel.status, 400);
  assert.match(String(obj(cancel)["error"]), /close it instead/);
});

test("a closed order stops counting as committed", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/purchase-orders/PO-1/status", { status: "CLOSED" });
  const c = obj(await call(s, "GET", "/t/acme/purchase-orders/committed"));
  assert.equal(c["committed_minor"], "0");
});

test("one tenant's purchase orders are invisible to another", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  assert.equal(
    (obj(await call(s, "GET", "/t/beta/purchase-orders"))["purchase_orders"] as Row[]).length, 0,
  );
  assert.equal((await call(s, "GET", "/t/beta/purchase-orders/PO-1")).status, 404);
});
