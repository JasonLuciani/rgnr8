import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Work orders.
 *
 * Nobody works on a job; they work on "go back Tuesday and hang the doors".
 * The hours that come back are three things at once — a cost to the job, a line
 * on a T&M invoice, and a record of what the crew did — and the thing under
 * test is that recording them once produces all three without booking the
 * labor twice.
 *
 * That last part is the whole design. The wage is already in the books through
 * payroll; a time entry that posts a *cost* would double it. So a time entry
 * posts an **allocation** — debit the job's labor account, credit the payroll
 * account it came from — which leaves the P&L total alone and moves the cost
 * onto the job that consumed it.
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
  await call(s, "POST", "/t/acme/jobs", {
    id: "harper-kitchen", customer_id: "harper", name: "Harper kitchen",
    billing_method: "TIME_AND_MATERIALS", contract_minor: "0",
  });
  await call(s, "POST", "/t/acme/payroll/employees", {
    id: "marco", name: "Marco Diaz", cost_rate_minor: "5200", bill_rate_minor: "11000",
  });
  return s;
}

const makeWO = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/work-orders", {
    id: "WO-1", job_id: "harper-kitchen", title: "Hang the doors",
    scheduled_date: "2026-07-14", assignee_id: "marco", ...extra,
  });

const time = (s: LedgerService, extra: Record<string, unknown> = {}, wo = "WO-1") =>
  call(s, "POST", `/t/acme/work-orders/${wo}/entries`, {
    kind: "LABOR", date: "2026-07-14", cost_code: "LAB",
    employee_id: "marco", quantity_milli: "8000", ...extra,
  });

const balances = async (s: LedgerService): Promise<Record<string, bigint>> => {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const out: Record<string, bigint> = {};
  for (const row of tb["rows"] as Row[]) {
    out[String(row["code"])] =
      BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
  }
  return out;
};

// --- the work order ----------------------------------------------------------

test("a work order belongs to a job and carries a day and a person", async () => {
  const s = await ready();
  const r = await makeWO(s);
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const wo = obj(r)["work_order"] as Row;
  assert.equal(wo["job_id"], "harper-kitchen");
  assert.equal(wo["assignee"], "Marco Diaz", "the name comes from the employee record");
  assert.equal(wo["status"], "SCHEDULED");
});

test("a work order without a real job, title or employee is refused", async () => {
  const s = await ready();
  assert.equal((await makeWO(s, { job_id: "ghost" })).status, 400);
  assert.equal((await makeWO(s, { title: "" })).status, 400);
  assert.equal((await makeWO(s, { assignee_id: "nobody" })).status, 400);
  assert.equal((await makeWO(s, { scheduled_date: "Tuesday" })).status, 400);
});

test("a work order can hang off a sales order, but not someone else's", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/sales-orders", {
    id: "SO-1", customer_id: "harper", job_id: "harper-kitchen", date: "2026-06-01",
    lines: [{ quantity_milli: "1000", unit_price_minor: "500000", account_code: "4100" }],
  });
  assert.equal((await makeWO(s, { sales_order_id: "SO-1" })).status, 201);

  await call(s, "POST", "/t/acme/jobs", {
    id: "other", customer_id: "harper", name: "Other",
    billing_method: "TIME_AND_MATERIALS", contract_minor: "0",
  });
  const wrong = await makeWO(s, { id: "WO-2", job_id: "other", sales_order_id: "SO-1" });
  assert.equal(wrong.status, 400);
  assert.match(String(obj(wrong)["error"]), /belongs to job harper-kitchen/);
});

// --- the allocation, and not double-counting ---------------------------------

test("time posts an allocation, not a second wage", async () => {
  const s = await ready();
  await makeWO(s);

  // payroll has already booked the wage
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-07-15", memo: "Payroll",
    lines: [
      { code: "6200", side: "DEBIT", amount_minor: "200000" },
      { code: "1000", side: "CREDIT", amount_minor: "200000" },
    ],
  });
  const before = await balances(s);
  assert.equal(before["6200"], 200000n);

  const r = await time(s);
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const entry = obj(r)["entry"] as Row;
  assert.equal(entry["extended_cost_minor"], "41600", "8 hours at the burdened 52.00");
  assert.equal(entry["extended_bill_minor"], "88000", "…billing at 110.00");
  assert.equal(entry["posted"], true);

  const after = await balances(s);
  assert.equal(after["5500"], 41600n, "the job's labor account carries the cost");
  assert.equal(after["6200"], 158400n, "…taken out of undifferentiated wages");
  assert.equal(
    (after["5500"] ?? 0n) + (after["6200"] ?? 0n), 200000n,
    "total labor cost is unchanged — the money moved, it did not multiply",
  );
});

test("the allocated cost lands on the job and its cost code", async () => {
  const s = await ready();
  await makeWO(s);
  await time(s);
  const rows = obj(await call(s, "GET", "/t/acme/jobs/harper-kitchen/cost"))["rows"] as Row[];
  const labor = rows.find((r) => r["cost_code"] === "LAB")!;
  assert.equal(labor["actual_cost_minor"], "41600");
});

test("rates come from the employee unless they are overridden", async () => {
  const s = await ready();
  await makeWO(s);
  const overtime = await time(s, {
    quantity_milli: "2000", unit_cost_minor: "7800", unit_bill_minor: "16500",
  });
  const entry = obj(overtime)["entry"] as Row;
  assert.equal(entry["extended_cost_minor"], "15600");
  assert.equal(entry["extended_bill_minor"], "33000");
});

test("an entry with nowhere honest to take the cost from is recorded, not invented", async () => {
  const s = await ready();
  await makeWO(s);
  const r = await call(s, "POST", "/t/acme/work-orders/WO-1/entries", {
    kind: "EQUIPMENT", date: "2026-07-14", cost_code: "EQP",
    quantity_milli: "4000", unit_cost_minor: "9000",
  });
  assert.equal(r.status, 201);
  assert.equal((obj(r)["entry"] as Row)["posted"], false);
  assert.match(String(obj(r)["unposted_reason"]), /nothing to take the cost from/);
  // and the books are untouched
  assert.deepEqual(await balances(s), {});
});

test("naming the account it comes from posts the equipment allocation", async () => {
  const s = await ready();
  await makeWO(s);
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-07-01", memo: "Fuel",
    lines: [
      { code: "6810", side: "DEBIT", amount_minor: "100000" },
      { code: "1000", side: "CREDIT", amount_minor: "100000" },
    ],
  });
  const r = await call(s, "POST", "/t/acme/work-orders/WO-1/entries", {
    kind: "EQUIPMENT", date: "2026-07-14", cost_code: "EQP",
    quantity_milli: "4000", unit_cost_minor: "9000", relieve_account_code: "6810",
  });
  assert.equal((obj(r)["entry"] as Row)["posted"], true);
  const after = await balances(s);
  assert.equal(after["5300"], 36000n);
  assert.equal(after["6810"], 64000n);
});

test("material issued from stock relieves inventory", async () => {
  const s = await ready();
  await makeWO(s);
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-07-01", memo: "Bought stock",
    lines: [
      { code: "1300", side: "DEBIT", amount_minor: "500000" },
      { code: "1000", side: "CREDIT", amount_minor: "500000" },
    ],
  });
  await call(s, "POST", "/t/acme/work-orders/WO-1/entries", {
    kind: "MATERIAL", date: "2026-07-14", cost_code: "MAT",
    quantity_milli: "6000", unit_cost_minor: "12000",
  });
  const after = await balances(s);
  assert.equal(after["5100"], 72000n, "6 units at 120.00 became job cost");
  assert.equal(after["1300"], 428000n, "…and left the shelf");
});

test("a closed period stops the allocation", async () => {
  const s = await ready();
  await makeWO(s);
  await call(s, "POST", "/t/acme/periods/2026-07/lock", {});
  const r = await time(s);
  assert.equal(r.status, 409);
});

test("an allocated entry cannot be quietly deleted", async () => {
  const s = await ready();
  await makeWO(s);
  const posted = await time(s);
  const entryId = String((obj(posted)["entry"] as Row)["id"]);
  const r = await call(s, "DELETE", `/t/acme/work-orders/WO-1/entries/${entryId}`);
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /reverse entry/);
});

test("a posted entry cannot be edited into a different quantity", async () => {
  // The trap: re-POSTing a posted entry with a new quantity overwrote the
  // record but posted no correcting journal entry, so the roll-up and the T&M
  // bill drifted from the ledger and over-billed the customer.
  const s = await ready();
  await makeWO(s);
  const posted = await time(s, { id: "WO-1-eX", quantity_milli: "8000" });
  assert.equal(posted.status, 201);
  const before = await balances(s);

  const edit = await time(s, { id: "WO-1-eX", quantity_milli: "16000" });
  assert.equal(edit.status, 400);
  assert.match(String(obj(edit)["error"]), /reverse entry .* and add a new one/);
  assert.deepEqual(await balances(s), before, "the ledger did not move");

  // an identical re-POST is a harmless retry, not an error
  assert.equal((await time(s, { id: "WO-1-eX", quantity_milli: "8000" })).status, 201);
});

test("an unposted entry can be removed", async () => {
  const s = await ready();
  await makeWO(s);
  const r = await call(s, "POST", "/t/acme/work-orders/WO-1/entries", {
    kind: "OTHER", date: "2026-07-14", quantity_milli: "1000", unit_cost_minor: "5000",
  });
  const entryId = String((obj(r)["entry"] as Row)["id"]);
  assert.equal((await call(s, "DELETE", `/t/acme/work-orders/WO-1/entries/${entryId}`)).status, 200);
});

// --- roll-up -----------------------------------------------------------------

test("a work order totals its hours, cost, billable value and margin", async () => {
  const s = await ready();
  await makeWO(s);
  await time(s);
  await time(s, { date: "2026-07-15", quantity_milli: "4000" });
  const wo = obj(await call(s, "GET", "/t/acme/work-orders/WO-1"))["work_order"] as Row;
  const totals = wo["totals"] as Row;
  assert.equal(totals["hours_milli"], "12000");
  assert.equal(totals["cost_minor"], "62400");
  assert.equal(totals["billable_minor"], "132000");
  assert.equal(totals["margin_minor"], "69600");
});

test("work orders roll up into the job", async () => {
  const s = await ready();
  await makeWO(s);
  await makeWO(s, { id: "WO-2", title: "Trim out", scheduled_date: "2026-07-20" });
  await time(s);
  await time(s, { quantity_milli: "6000" }, "WO-2");
  await call(s, "POST", "/t/acme/work-orders/WO-1/complete", { date: "2026-07-14" });

  const roll = obj(await call(s, "GET", "/t/acme/jobs/harper-kitchen/work-orders"));
  assert.equal(roll["contract"], "job-work-orders/1");
  assert.equal((roll["work_orders"] as Row[]).length, 2);
  const totals = roll["totals"] as Row;
  assert.equal(totals["hours_milli"], "14000");
  assert.equal(totals["cost_minor"], "72800");
  assert.equal(totals["unbilled_minor"], "154000");
  assert.equal(totals["open"], 1, "one is still to do");
});

test("non-billable time costs the job and bills nobody", async () => {
  const s = await ready();
  await makeWO(s);
  await time(s, { billable: false, description: "Rework — our mistake" });
  const wo = obj(await call(s, "GET", "/t/acme/work-orders/WO-1"))["work_order"] as Row;
  const totals = wo["totals"] as Row;
  assert.equal(totals["cost_minor"], "41600", "it still cost us");
  assert.equal(totals["unbilled_minor"], "0", "…and it is not going on an invoice");
});

test("completing a work order records the day without touching its costs", async () => {
  const s = await ready();
  await makeWO(s);
  await time(s);
  const r = await call(s, "POST", "/t/acme/work-orders/WO-1/complete", { date: "2026-07-16" });
  const wo = obj(r)["work_order"] as Row;
  assert.equal(wo["status"], "COMPLETE");
  assert.equal(wo["completed_date"], "2026-07-16");
  assert.equal((await balances(s))["5500"], 41600n);
});

test("a cancelled work order takes no more time", async () => {
  const s = await ready();
  await makeWO(s, { status: "CANCELLED" });
  const r = await time(s);
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /cancelled/);
});

test("one tenant's work orders are invisible to another", async () => {
  const s = await ready();
  await makeWO(s);
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  assert.equal(
    (obj(await call(s, "GET", "/t/beta/work-orders"))["work_orders"] as Row[]).length, 0,
  );
  assert.equal((await call(s, "GET", "/t/beta/work-orders/WO-1")).status, 404);
});
