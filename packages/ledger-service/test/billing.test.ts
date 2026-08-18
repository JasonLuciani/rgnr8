import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * The four billing methods.
 *
 * A contractor does not have one billing method; they have four, often on the
 * same customer. All four go through the ordinary AR path here, because a
 * second way to bill a customer is how you end up with two revenue figures and
 * no idea which one the tax return used.
 *
 * Two things are worth defending hardest. Retainage is earned revenue that is
 * not collectible yet, so it must not sit in accounts receivable making the
 * aging report lie for months. And a deposit is a liability — money taken for
 * work not yet done — because calling it income is how a small contractor
 * overstates a good year and gets a tax bill for it.
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

async function ready(job: Record<string, unknown> = {}): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  await call(s, "POST", "/t/acme/cost-codes/seed", {});
  await call(s, "POST", "/t/acme/customers", { id: "harper", name: "Harper Residence" });
  await call(s, "POST", "/t/acme/jobs", {
    id: "harper", customer_id: "harper", name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "10000000", retainage_ppm: 100_000,
    ...job,
  });
  return s;
}

const SCHEDULE = {
  lines: [
    { description: "Demolition", cost_code: "LAB", scheduled_value_minor: "1500000" },
    { description: "Cabinets", cost_code: "MAT", scheduled_value_minor: "5500000" },
    { description: "Finish", cost_code: "LAB", scheduled_value_minor: "3000000" },
  ],
};

const balances = async (s: LedgerService): Promise<Record<string, bigint>> => {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const out: Record<string, bigint> = {};
  for (const row of tb["rows"] as Row[]) {
    out[String(row["code"])] =
      BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
  }
  return out;
};

const view = async (s: LedgerService, job = "harper"): Promise<Row> =>
  obj(await call(s, "GET", `/t/acme/jobs/${job}/billing`));

// --- schedule of values ------------------------------------------------------

test("a schedule of values has to add up to the contract", async () => {
  const s = await ready();
  const short = await call(s, "POST", "/t/acme/jobs/harper/schedule", {
    lines: [{ description: "Everything", scheduled_value_minor: "9000000" }],
  });
  assert.equal(short.status, 400);
  assert.match(String(obj(short)["error"]), /they have to agree/);
  assert.equal((await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE)).status, 200);
});

test("a progress bill is the value earned less what was already billed", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  const first = await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30",
    lines: [{ line_no: 1, percent_ppm: 1_000_000 }, { line_no: 2, percent_ppm: 200_000 }],
  });
  assert.equal(first.status, 201, JSON.stringify(first.body));
  assert.equal(obj(first)["gross_minor"], "2600000", "all the demo + 20% of cabinets");

  const second = await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-2", date: "2026-07-31",
    lines: [{ line_no: 2, percent_ppm: 500_000 }],
  });
  assert.equal(obj(second)["gross_minor"], "1650000", "30% more of the cabinets, not 50%");
});

test("retainage is held out of AR and posted against the same revenue", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  const r = await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  assert.equal(obj(r)["retainage_minor"], "150000", "10% of 15,000");
  assert.ok(String(obj(r)["retainage_entry_id"]).length > 0);

  const after = await balances(s);
  assert.equal(after["1200"], 1350000n, "AR is what is collectible now");
  assert.equal(after["1260"], 150000n, "…and the held-back part is somewhere it can be chased");
  assert.equal(after["4100"], -1500000n, "revenue is the whole thing — it was all earned");

  // and the aging report agrees with AR rather than with the gross
  const ar = obj(await call(s, "GET", "/t/acme/invoices"))["documents"] as Row[];
  assert.equal(ar[0]!["open_minor"], "1350000");
});

test("releasing retainage moves it to AR without inventing revenue twice", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  const released = await call(s, "POST", "/t/acme/jobs/harper/retainage/release", {
    id: "RET-1", date: "2026-12-01",
  });
  assert.equal(released.status, 201, JSON.stringify(released.body));

  const after = await balances(s);
  assert.equal(after["1260"] ?? 0n, 0n, "nothing held back any more");
  assert.equal(after["1200"], 1500000n, "…it is all collectible");
  assert.equal(after["4100"], -1500000n, "and revenue did not move");
});

test("releasing more retainage than is held is refused", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  const r = await call(s, "POST", "/t/acme/jobs/harper/retainage/release", {
    id: "RET-1", date: "2026-12-01", amount_minor: "500000",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /only 150000 of retainage/);
});

test("billing a line past its scheduled value is a change order, not a progress bill", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  const r = await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, amount_minor: "2000000" }],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /change order/);
});

test("billing backwards is refused — that is what a credit is for", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  const r = await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-2", date: "2026-07-31", lines: [{ line_no: 1, percent_ppm: 500_000 }],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /issue a credit/);
});

test("a refused invoice leaves the schedule untouched", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/acme/periods/2026-06/lock", {});
  const r = await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  assert.equal(r.status, 409);
  const schedule = (await view(s))["schedule"] as Row[];
  assert.equal(schedule[0]!["billed_minor"], "0");
});

test("the billing view shows what is scheduled, billed and left", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  const v = await view(s);
  assert.equal(v["contract"], "job-billing/1");
  const totals = v["totals"] as Row;
  assert.equal(totals["scheduled_minor"], "10000000");
  assert.equal(totals["billed_minor"], "1500000");
  assert.equal(totals["remaining_minor"], "8500000");
  assert.equal(totals["retainage_held_minor"], "150000");
});

// --- milestones --------------------------------------------------------------

test("milestones cannot add up to more than the contract", async () => {
  const s = await ready({ id: "fixed", billing_method: "FIXED_MILESTONES" });
  const r = await call(s, "POST", "/t/acme/jobs/fixed/milestones", {
    milestones: [
      { name: "Deposit", amount_minor: "3000000", due_date: "2026-06-01" },
      { name: "Rough-in", amount_minor: "9000000", due_date: "2026-07-15" },
    ],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /against a contract of/);
});

test("a milestone bills once and says so the second time", async () => {
  const s = await ready({ id: "fixed", billing_method: "FIXED_MILESTONES", retainage_ppm: 0 });
  await call(s, "POST", "/t/acme/jobs/fixed/milestones", {
    milestones: [
      { id: "M1", name: "Rough-in complete", amount_minor: "4000000", due_date: "2026-07-15" },
    ],
  });
  const first = await call(s, "POST", "/t/acme/jobs/fixed/bill/milestone", {
    id: "INV-1", date: "2026-07-16", milestone_id: "M1",
  });
  assert.equal(first.status, 201, JSON.stringify(first.body));
  assert.equal((obj(first)["invoice"] as Row)["total_minor"], "4000000");

  const again = await call(s, "POST", "/t/acme/jobs/fixed/bill/milestone", {
    id: "INV-2", date: "2026-08-01", milestone_id: "M1",
  });
  assert.equal(again.status, 400);
  assert.match(String(obj(again)["error"]), /BILLED/);
});

test("a billed milestone cannot be quietly rewritten", async () => {
  const s = await ready({ id: "fixed", billing_method: "FIXED_MILESTONES", retainage_ppm: 0 });
  await call(s, "POST", "/t/acme/jobs/fixed/milestones", {
    milestones: [{ id: "M1", name: "Rough-in", amount_minor: "4000000" }],
  });
  await call(s, "POST", "/t/acme/jobs/fixed/bill/milestone", {
    id: "INV-1", date: "2026-07-16", milestone_id: "M1",
  });
  const r = await call(s, "POST", "/t/acme/jobs/fixed/milestones", {
    milestones: [{ id: "M1", name: "Rough-in", amount_minor: "5000000" }],
  });
  assert.equal(r.status, 400);
});

// --- time and materials ------------------------------------------------------

async function withWork(): Promise<LedgerService> {
  const s = await ready({
    id: "tm", billing_method: "TIME_AND_MATERIALS", contract_minor: "0", retainage_ppm: 0,
  });
  await call(s, "POST", "/t/acme/payroll/employees", {
    id: "marco", name: "Marco Diaz", cost_rate_minor: "5200", bill_rate_minor: "11000",
  });
  await call(s, "POST", "/t/acme/work-orders", {
    id: "WO-1", job_id: "tm", title: "Service call", scheduled_date: "2026-07-14",
  });
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-07-01", memo: "Payroll",
    lines: [
      { code: "6200", side: "DEBIT", amount_minor: "500000" },
      { code: "1000", side: "CREDIT", amount_minor: "500000" },
    ],
  });
  await call(s, "POST", "/t/acme/work-orders/WO-1/entries", {
    kind: "LABOR", date: "2026-07-14", cost_code: "LAB",
    employee_id: "marco", quantity_milli: "8000", description: "Diagnose and repair",
  });
  await call(s, "POST", "/t/acme/work-orders/WO-1/entries", {
    kind: "LABOR", date: "2026-07-15", cost_code: "LAB",
    employee_id: "marco", quantity_milli: "3000", description: "Return visit",
  });
  return s;
}

test("time and materials bills the unbilled hours, one line per visit", async () => {
  const s = await withWork();
  const r = await call(s, "POST", "/t/acme/jobs/tm/bill/time-and-materials", {
    id: "INV-1", date: "2026-07-31",
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  assert.equal(obj(r)["gross_minor"], "121000", "11 hours at 110.00");
  const lines = (obj(r)["invoice"] as Row)["lines"] as Row[];
  assert.equal(lines.length, 2, "a customer querying a T&M invoice asks about a day");
  assert.match(String(lines[0]!["description"]), /2026-07-14 Diagnose and repair/);
});

test("hours already billed are not billed again", async () => {
  const s = await withWork();
  await call(s, "POST", "/t/acme/jobs/tm/bill/time-and-materials", {
    id: "INV-1", date: "2026-07-31",
  });
  const again = await call(s, "POST", "/t/acme/jobs/tm/bill/time-and-materials", {
    id: "INV-2", date: "2026-08-31",
  });
  assert.equal(again.status, 400);
  assert.match(String(obj(again)["error"]), /no unbilled billable work/);
});

test("a through-date bills only what was done by then", async () => {
  const s = await withWork();
  const r = await call(s, "POST", "/t/acme/jobs/tm/bill/time-and-materials", {
    id: "INV-1", date: "2026-07-31", through: "2026-07-14",
  });
  assert.equal(obj(r)["gross_minor"], "88000", "just the first visit");
  const v = await view(s, "tm");
  assert.equal((v["totals"] as Row)["unbilled_work_minor"], "33000", "the return visit waits");
});

test("summarizing gives one line per work order", async () => {
  const s = await withWork();
  const r = await call(s, "POST", "/t/acme/jobs/tm/bill/time-and-materials", {
    id: "INV-1", date: "2026-07-31", summarize: true,
  });
  assert.equal(((obj(r)["invoice"] as Row)["lines"] as Row[]).length, 1);
});

test("the margin on a T&M job is visible once it is billed", async () => {
  const s = await withWork();
  await call(s, "POST", "/t/acme/jobs/tm/bill/time-and-materials", {
    id: "INV-1", date: "2026-07-31",
  });
  const totals = obj(await call(s, "GET", "/t/acme/jobs/tm/cost"))["totals"] as Row;
  assert.equal(totals["revenue_minor"], "121000");
  assert.equal(totals["actual_cost_minor"], "57200", "11 hours at the burdened 52.00");
  assert.equal(totals["margin_minor"], "63800");
});

// --- deposits ----------------------------------------------------------------

test("a deposit is a liability, not income", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/jobs/harper/deposits", {
    date: "2026-05-15", amount_minor: "2000000", memo: "Signed contract deposit",
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const after = await balances(s);
  assert.equal(after["1000"], 2000000n, "the cash is real");
  assert.equal(after["2400"], -2000000n, "…and so is the obligation");
  assert.equal(after["4100"] ?? 0n, 0n, "no revenue — nothing has been built yet");
});

test("applying a deposit closes the invoice and clears the liability", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/deposits", {
    date: "2026-05-15", amount_minor: "2000000",
  });
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  const applied = await call(s, "POST", "/t/acme/jobs/harper/deposits/apply", {
    invoice_id: "APP-1", date: "2026-06-30",
  });
  assert.equal(applied.status, 201, JSON.stringify(applied.body));
  assert.equal((obj(applied)["invoice"] as Row)["open_minor"], "0", "the invoice is settled");

  const after = await balances(s);
  assert.equal(after["2400"], -650000n, "what is left of the deposit");
  assert.equal(after["1200"] ?? 0n, 0n);
});

test("applying more deposit than was taken is refused", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/deposits", {
    date: "2026-05-15", amount_minor: "100000",
  });
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  const r = await call(s, "POST", "/t/acme/jobs/harper/deposits/apply", {
    invoice_id: "APP-1", date: "2026-06-30", amount_minor: "500000",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /only 100000 of deposit/);
});

test("a deposit cannot be applied to an invoice that is already settled", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/deposits", {
    date: "2026-05-15", amount_minor: "2000000",
  });
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/acme/jobs/harper/bill/progress", {
    id: "APP-1", date: "2026-06-30", lines: [{ line_no: 1, percent_ppm: 1_000_000 }],
  });
  await call(s, "POST", "/t/acme/jobs/harper/deposits/apply", {
    invoice_id: "APP-1", date: "2026-06-30",
  });
  const r = await call(s, "POST", "/t/acme/jobs/harper/deposits/apply", {
    invoice_id: "APP-1", date: "2026-07-01", amount_minor: "10000",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /only has 0 outstanding/);
});

test("one tenant's schedules and milestones are invisible to another", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/schedule", SCHEDULE);
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  assert.equal((await call(s, "GET", "/t/beta/jobs/harper/billing")).status, 400);
});
