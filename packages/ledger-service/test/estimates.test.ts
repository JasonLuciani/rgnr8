import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Estimates.
 *
 * What is under test is the half of an estimate most software throws away: the
 * cost side. An estimate that stores only prices cannot seed a job budget, so
 * the contractor types it in twice, and cannot answer "we bid 22% and finished
 * at 9%", which is the only reason to keep the document at all.
 *
 * Also under test: markup and margin are different numbers and both are
 * reported, and a sent estimate is revised rather than edited.
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

/** Cost 10,000 marked up 20% → price 12,000, margin 16.67%. */
const EST = {
  id: "EST-1",
  customer_id: "harper",
  date: "2026-05-01",
  expiry_date: "2026-06-30",
  memo: "Harper kitchen remodel",
  lines: [
    {
      description: "Framing labor", cost_code: "LAB", quantity_milli: "40000",
      unit_cost_minor: "6500", markup_ppm: 200_000, account_code: "4100",
    },
    {
      description: "Cabinets", cost_code: "MAT", quantity_milli: "1000",
      unit_cost_minor: "1800000", markup_ppm: 150_000, account_code: "4100",
    },
  ],
};

const make = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/estimates", { ...EST, ...extra });

const get = async (s: LedgerService, id = "EST-1"): Promise<Row> =>
  obj(await call(s, "GET", `/t/acme/estimates/${id}`))["estimate"] as Row;

// --- the arithmetic ----------------------------------------------------------

test("an estimate line carries cost, markup and the price that follows", async () => {
  const s = await ready();
  const r = await make(s);
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const lines = (obj(r)["estimate"] as Row)["lines"] as Row[];

  const labor = lines[0]!;
  assert.equal(labor["extended_cost_minor"], "260000", "40 hours at $65");
  assert.equal(labor["unit_price_minor"], "7800", "$65 marked up 20%");
  assert.equal(labor["extended_price_minor"], "312000");

  const cabinets = lines[1]!;
  assert.equal(cabinets["extended_cost_minor"], "1800000");
  assert.equal(cabinets["extended_price_minor"], "2070000", "$18,000 marked up 15%");
});

test("markup and margin are both reported, because they are not the same number", async () => {
  const s = await ready();
  await make(s, {
    lines: [{
      description: "One item", cost_code: "MAT", quantity_milli: "1000",
      unit_cost_minor: "1000000", markup_ppm: 200_000, account_code: "4100",
    }],
  });
  const totals = (await get(s))["totals"] as Row;
  assert.equal(totals["cost_minor"], "1000000");
  assert.equal(totals["price_minor"], "1200000");
  assert.equal(totals["markup_ppm"], 200_000, "20% added to cost");
  assert.equal(totals["margin_ppm"], 166_667, "…is a 16.7% margin, not 20%");
});

test("a price typed directly wins and the markup is derived from it", async () => {
  const s = await ready();
  await make(s, {
    lines: [{
      description: "Just quote it", cost_code: "MAT", quantity_milli: "1000",
      unit_cost_minor: "400000", unit_price_minor: "500000", account_code: "4100",
    }],
  });
  const line = ((await get(s))["lines"] as Row[])[0]!;
  assert.equal(line["unit_price_minor"], "500000");
  assert.equal(line["markup_ppm"], 250_000);
  assert.equal(line["margin_ppm"], 200_000);
});

test("fractional quantities are exact, not floating point", async () => {
  const s = await ready();
  await make(s, {
    lines: [{
      description: "2.5 days", cost_code: "LAB", quantity_milli: "2500",
      unit_cost_minor: "45000", markup_ppm: 0, account_code: "4100",
    }],
  });
  const line = ((await get(s))["lines"] as Row[])[0]!;
  assert.equal(line["extended_cost_minor"], "112500", "2.5 × $450.00 exactly");
});

test("tax is computed on taxable lines only, and only at the total", async () => {
  const s = await ready();
  await make(s, {
    tax_rate_ppm: 82_500,       // 8.25%
    lines: [
      { description: "Materials", cost_code: "MAT", quantity_milli: "1000",
        unit_cost_minor: "1000000", markup_ppm: 0, account_code: "4100" },
      { description: "Labor (exempt)", cost_code: "LAB", quantity_milli: "1000",
        unit_cost_minor: "500000", markup_ppm: 0, account_code: "4100", taxable: false },
    ],
  });
  const totals = (await get(s))["totals"] as Row;
  assert.equal(totals["price_minor"], "1500000");
  assert.equal(totals["tax_minor"], "82500", "8.25% of the taxable 10,000 only");
  assert.equal(totals["total_minor"], "1582500");
});

// --- refusals ----------------------------------------------------------------

test("an estimate line bills to income, and cost codes must exist", async () => {
  const s = await ready();
  const toExpense = await make(s, {
    lines: [{ cost_code: "MAT", unit_cost_minor: "100", account_code: "5100" }],
  });
  assert.equal(toExpense.status, 400);
  assert.match(String(obj(toExpense)["error"]), /revenue account/);
  assert.equal((await make(s, {
    lines: [{ cost_code: "NOPE", unit_cost_minor: "100", account_code: "4100" }],
  })).status, 400);
});

test("an estimate needs a real customer, a date and at least one line", async () => {
  const s = await ready();
  assert.equal((await make(s, { customer_id: "ghost" })).status, 400);
  assert.equal((await make(s, { date: "May" })).status, 400);
  assert.equal((await make(s, { lines: [] })).status, 400);
  assert.equal((await make(s, { expiry_date: "2026-04-01" })).status, 400);
});

// --- revisions ---------------------------------------------------------------

test("revising creates revision 2 and supersedes revision 1 rather than editing it", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/estimates/EST-1/status", { status: "SENT" });

  const revised = await call(s, "POST", "/t/acme/estimates/EST-1/revise", {
    lines: [{
      description: "Cabinets (upgraded)", cost_code: "MAT", quantity_milli: "1000",
      unit_cost_minor: "2400000", markup_ppm: 150_000, account_code: "4100",
    }],
  });
  assert.equal(revised.status, 201, JSON.stringify(revised.body));
  const r2 = obj(revised)["estimate"] as Row;
  assert.equal(r2["id"], "EST-1-r2");
  assert.equal(r2["revision"], 2);
  assert.equal(r2["root_id"], "EST-1");
  assert.equal(r2["status"], "DRAFT");

  const r1 = await get(s, "EST-1");
  assert.equal(r1["status"], "SUPERSEDED", "what the customer saw is still readable");
  assert.equal(((r1["lines"] as Row[])[1]!)["unit_cost_minor"], "1800000");
});

test("a revision with no lines keeps the ones it inherited", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/estimates/EST-1/revise", { memo: "Chased for signature" });
  const r2 = await get(s, "EST-1-r2");
  assert.equal((r2["lines"] as Row[]).length, 2);
  assert.equal(r2["memo"], "Chased for signature");
});

test("a sent estimate cannot be edited in place", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/estimates/EST-1/status", { status: "SENT" });
  await call(s, "POST", "/t/acme/estimates/EST-1/accept", { job_name: "Harper kitchen" });
  const edit = await make(s);
  assert.equal(edit.status, 400);
  assert.match(String(obj(edit)["error"]), /revise it instead/);
});

test("the list shows the current revision, and everything on request", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/estimates/EST-1/revise", {});
  const current = obj(await call(s, "GET", "/t/acme/estimates"))["estimates"] as Row[];
  assert.deepEqual(current.map((e) => e["id"]), ["EST-1-r2"]);
  const all = obj(await call(s, "GET", "/t/acme/estimates", "", { all: "1" }))["estimates"] as Row[];
  assert.equal(all.length, 2);
});

// --- acceptance --------------------------------------------------------------

test("accepting creates the job and seeds its budget from the cost lines", async () => {
  const s = await ready();
  await make(s);
  const accepted = await call(s, "POST", "/t/acme/estimates/EST-1/accept", {
    job_name: "Harper kitchen remodel",
    billing_method: "PROGRESS",
    retainage_ppm: 100_000,
    start_date: "2026-06-01",
  });
  assert.equal(accepted.status, 200, JSON.stringify(accepted.body));
  const jobId = String(obj(accepted)["job_id"]);
  assert.equal(obj(accepted)["job_created"], true);
  assert.equal(obj(accepted)["budget_lines_seeded"], 2);

  const job = obj(await call(s, "GET", `/t/acme/jobs/${jobId}`));
  assert.equal((job["job"] as Row)["contract_minor"], "2382000", "the estimate's price");
  assert.equal((job["job"] as Row)["retainage_ppm"], 100_000);
  const budget = job["budget"] as Row[];
  assert.deepEqual(
    budget.map((b) => [b["cost_code"], b["budget_cost_minor"]]),
    [["LAB", "260000"], ["MAT", "1800000"]],
    "the budget is the estimate's cost, not its price",
  );
  assert.equal(budget[0]!["revised_cost_minor"], "260000", "revised starts at the bid");
});

test("accepting onto an existing job leaves the job alone but still budgets", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs", {
    id: "existing", customer_id: "harper", name: "Existing job",
    billing_method: "TIME_AND_MATERIALS", contract_minor: "0",
  });
  await make(s);
  const accepted = await call(s, "POST", "/t/acme/estimates/EST-1/accept", {
    job_id: "existing",
  });
  assert.equal(obj(accepted)["job_created"], false);
  const job = obj(await call(s, "GET", "/t/acme/jobs/existing"));
  assert.equal((job["job"] as Row)["contract_minor"], "0", "untouched");
  assert.equal((job["budget"] as Row[]).length, 2);
});

test("an expired estimate is refused unless accepted with eyes open", async () => {
  const s = await ready();
  await make(s);
  const late = await call(s, "POST", "/t/acme/estimates/EST-1/accept", {
    job_name: "Late", start_date: "2026-08-01",
  });
  assert.equal(late.status, 400);
  assert.match(String(obj(late)["error"]), /expired on 2026-06-30/);

  const anyway = await call(s, "POST", "/t/acme/estimates/EST-1/accept", {
    job_name: "Late anyway", start_date: "2026-08-01", ignore_expiry: true,
  });
  assert.equal(anyway.status, 200);
});

test("an estimate can only be accepted once", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/estimates/EST-1/accept", { job_name: "First" });
  const again = await call(s, "POST", "/t/acme/estimates/EST-1/accept", { job_name: "Again" });
  assert.equal(again.status, 400);
  assert.match(String(obj(again)["error"]), /ACCEPTED/);
});

test("a declined estimate cannot be accepted", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/acme/estimates/EST-1/status", { status: "DECLINED" });
  assert.equal((await call(s, "POST", "/t/acme/estimates/EST-1/accept", {})).status, 400);
});

// --- becoming an invoice -----------------------------------------------------

test("an accepted estimate becomes an ordinary invoice, tagged to the job", async () => {
  const s = await ready();
  await make(s, { tax_rate_ppm: 82_500 });
  const accepted = await call(s, "POST", "/t/acme/estimates/EST-1/accept", {
    job_name: "Harper kitchen remodel",
  });
  const jobId = String(obj(accepted)["job_id"]);

  const invoiced = await call(s, "POST", "/t/acme/estimates/EST-1/invoice", {
    id: "INV-1", date: "2026-07-01",
  });
  assert.equal(invoiced.status, 201, JSON.stringify(invoiced.body));
  const invoice = obj(invoiced)["invoice"] as Row;
  assert.equal(invoice["net_minor"], "2382000");
  assert.equal(invoice["tax_minor"], "196515", "8.25%, held as a liability");

  // the revenue landed on the job, so the job report sees it
  const cost = obj(await call(s, "GET", `/t/acme/jobs/${jobId}/cost`));
  assert.equal((cost["totals"] as Row)["revenue_minor"], "2382000");

  // …and it is a normal invoice: it shows in AR
  const ar = obj(await call(s, "GET", "/t/acme/invoices"))["documents"] as Row[];
  assert.equal(ar.length, 1);
  assert.equal(ar[0]!["open_minor"], "2578515");
});

test("an estimate that was never accepted cannot be invoiced", async () => {
  const s = await ready();
  await make(s);
  const r = await call(s, "POST", "/t/acme/estimates/EST-1/invoice", {
    id: "INV-1", date: "2026-07-01",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /not ACCEPTED/);
});

// --- isolation ---------------------------------------------------------------

test("one tenant's estimates are invisible to another", async () => {
  const s = await ready();
  await make(s);
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  assert.equal((obj(await call(s, "GET", "/t/beta/estimates"))["estimates"] as Row[]).length, 0);
  assert.equal((await call(s, "GET", "/t/beta/estimates/EST-1")).status, 404);
});
