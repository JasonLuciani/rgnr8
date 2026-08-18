import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Job costing.
 *
 * The claim being defended is that a job is not a report built beside the
 * ledger — it is a view of the ledger. Costs reach a job as dimensions on
 * ordinary journal lines, which means every existing posting path costs a job
 * for free, and the job report can never disagree with the trial balance.
 *
 * The other thing under test is the distinction between what was bid and what
 * it is now expected to cost. A system that lets a revision overwrite the
 * original estimate cannot answer "are we over what we bid", which is the only
 * question the estimate was for.
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

const JOB = {
  id: "harper-kitchen",
  customer_id: "harper",
  name: "Harper kitchen remodel",
  billing_method: "PROGRESS",
  contract_minor: "8500000",         // $85,000
  retainage_ppm: 100_000,            // 10%
  start_date: "2026-06-01",
};

const makeJob = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/jobs", { ...JOB, ...extra });

/** Post a job cost the way any ordinary entry would. */
async function cost(
  s: LedgerService, date: string, minor: string, costCode: string,
  accountCode = "5100", jobId = "harper-kitchen",
): Promise<ServiceResponse> {
  return call(s, "POST", "/t/acme/entries", {
    date, memo: `${costCode} cost`,
    lines: [
      {
        code: accountCode, side: "DEBIT", amount_minor: minor,
        dimensions: { job: jobId, cost_code: costCode },
      },
      { code: "1000", side: "CREDIT", amount_minor: minor },
    ],
  });
}

const report = (s: LedgerService, id = "harper-kitchen", query: Record<string, string> = {}) =>
  call(s, "GET", `/t/acme/jobs/${id}/cost`, "", query);

// --- the structures ----------------------------------------------------------

test("a starter set of cost codes exists so nobody has to invent one", async () => {
  const s = await ready();
  const codes = obj(await call(s, "GET", "/t/acme/cost-codes"))["cost_codes"] as Row[];
  assert.deepEqual(codes.map((c) => c["code"]), ["EQP", "LAB", "MAT", "OTH", "SUB"]);
  assert.equal(codes.find((c) => c["code"] === "LAB")!["account_code"], "5500");
});

test("seeding twice does not duplicate or overwrite an edited code", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/cost-codes", {
    code: "LAB", name: "Field labor", category: "LABOR", account_code: "5500",
  });
  await call(s, "POST", "/t/acme/cost-codes/seed", {});
  const codes = obj(await call(s, "GET", "/t/acme/cost-codes"))["cost_codes"] as Row[];
  assert.equal(codes.length, 5);
  assert.equal(codes.find((c) => c["code"] === "LAB")!["name"], "Field labor");
});

test("a cost code has to point at somewhere cost can land", async () => {
  const s = await ready();
  const revenue = await call(s, "POST", "/t/acme/cost-codes", {
    code: "X", name: "Nope", category: "OTHER", account_code: "4100",
  });
  assert.equal(revenue.status, 400);
  assert.match(String(obj(revenue)["error"]), /revenue account/);
  assert.equal((await call(s, "POST", "/t/acme/cost-codes", {
    code: "Y", name: "Nope", category: "OTHER", account_code: "9999",
  })).status, 400);
});

test("creating a job creates the accounts job costing needs", async () => {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  await call(s, "POST", "/t/acme/customers", { id: "harper", name: "Harper" });
  const before = obj(await call(s, "GET", "/t/acme/accounts"))["accounts"] as Row[];
  assert.equal(before.some((a) => a["code"] === "1250"), false, "the general chart has none");

  const made = await makeJob(s, { billing_method: "TIME_AND_MATERIALS", contract_minor: "0" });
  assert.equal(made.status, 201, JSON.stringify(made.body));
  const after = obj(await call(s, "GET", "/t/acme/accounts"))["accounts"] as Row[];
  for (const code of ["1250", "1260", "1270", "2400", "2450", "5500"]) {
    assert.ok(after.some((a) => a["code"] === code), `${code} was created`);
  }
});

test("a job belongs to a real customer and a fixed-price job needs a contract", async () => {
  const s = await ready();
  assert.equal((await makeJob(s, { customer_id: "nobody" })).status, 400);
  assert.equal((await makeJob(s, { customer_id: "" })).status, 400);
  const noContract = await makeJob(s, { contract_minor: "0" });
  assert.equal(noContract.status, 400);
  assert.match(String(obj(noContract)["error"]), /contract value/);
  // …but a T&M job legitimately has none until the work is done
  assert.equal((await makeJob(s, {
    id: "tm", billing_method: "TIME_AND_MATERIALS", contract_minor: "0",
  })).status, 201);
});

// --- costing -----------------------------------------------------------------

test("an ordinary journal entry costs a job, and the report reads the journal", async () => {
  const s = await ready();
  await makeJob(s);
  const posted = await cost(s, "2026-06-15", "1250000", "MAT");
  assert.equal(posted.status, 201, JSON.stringify(posted.body));

  const r = await report(s);
  assert.equal(r.status, 200);
  assert.equal(obj(r)["contract"], "job-cost/1");
  const mat = (obj(r)["rows"] as Row[]).find((x) => x["cost_code"] === "MAT")!;
  assert.equal(mat["actual_cost_minor"], "1250000");

  // and the same money is in the trial balance, because it is the same row
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const materials = (tb["rows"] as Row[]).find((x) => x["code"] === "5100")!;
  assert.equal(materials["debit_minor"], "1250000");
});

test("a mistyped job or cost code is refused at the door", async () => {
  const s = await ready();
  await makeJob(s);
  const wrongJob = await cost(s, "2026-06-15", "1000", "MAT", "5100", "harper-kitchn");
  assert.equal(wrongJob.status, 400);
  assert.match(String(obj(wrongJob)["error"]), /harper-kitchn/);
  const wrongCode = await cost(s, "2026-06-15", "1000", "MATT");
  assert.equal(wrongCode.status, 400);
});

test("budget keeps what was bid apart from what it is now expected to cost", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [
      { cost_code: "MAT", budget_cost_minor: "2000000" },
      { cost_code: "LAB", budget_cost_minor: "3000000" },
    ],
  });
  // the framing came in over: revise the estimate, don't rewrite the bid
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [
      { cost_code: "MAT", budget_cost_minor: "2000000", revised_cost_minor: "2400000" },
      { cost_code: "LAB", budget_cost_minor: "3000000" },
    ],
  });
  const rows = obj(await report(s))["rows"] as Row[];
  const mat = rows.find((x) => x["cost_code"] === "MAT")!;
  assert.equal(mat["budget_cost_minor"], "2000000", "what we bid");
  assert.equal(mat["revised_cost_minor"], "2400000", "what we now think");
  const lab = rows.find((x) => x["cost_code"] === "LAB")!;
  assert.equal(lab["revised_cost_minor"], "3000000", "unrevised lines follow the bid");
});

test("cost to complete is the revised estimate less what has been spent", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "2000000" }],
  });
  await cost(s, "2026-06-15", "1500000", "MAT");
  const mat = (obj(await report(s))["rows"] as Row[])
    .find((x) => x["cost_code"] === "MAT")!;
  assert.equal(mat["remaining_minor"], "500000");
  assert.equal(mat["percent_spent_ppm"], 750_000, "75% of the materials budget");
  assert.equal(mat["over_budget"], false);
});

test("spending past the estimate says so rather than showing a smaller percentage", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "1000000" }],
  });
  await cost(s, "2026-06-15", "1400000", "MAT");
  const mat = (obj(await report(s))["rows"] as Row[])
    .find((x) => x["cost_code"] === "MAT")!;
  assert.equal(mat["over_budget"], true);
  assert.equal(mat["remaining_minor"], "-400000");
  assert.equal(mat["percent_spent_ppm"], 1_400_000);
});

test("spend in a code nobody budgeted is surfaced, not folded in silently", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "1000000" }],
  });
  await cost(s, "2026-07-02", "450000", "SUB", "5200");
  const r = obj(await report(s));
  assert.deepEqual(r["unbudgeted"], ["SUB"]);
  const sub = (r["rows"] as Row[]).find((x) => x["cost_code"] === "SUB")!;
  assert.equal(sub["actual_cost_minor"], "450000");
  assert.equal(sub["budget_cost_minor"], "0");
});

test("cost carried on the job with no cost code is counted and called out", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-07-04", memo: "Dump fees",
    lines: [
      { code: "5400", side: "DEBIT", amount_minor: "35000",
        dimensions: { job: "harper-kitchen" } },
      { code: "1000", side: "CREDIT", amount_minor: "35000" },
    ],
  });
  const r = obj(await report(s));
  assert.equal(r["uncoded_minor"], "35000");
  assert.equal((r["totals"] as Row)["actual_cost_minor"], "35000",
    "it still counts against the job");
});

test("a reversal takes the cost back off the job", async () => {
  const s = await ready();
  await makeJob(s);
  const posted = await cost(s, "2026-06-15", "1250000", "MAT");
  const entryId = String((obj(posted)["entry"] as Row)["id"]);
  await call(s, "POST", `/t/acme/entries/${entryId}/reverse`, { date: "2026-06-20" });
  const mat = (obj(await report(s))["rows"] as Row[]).find((x) => x["cost_code"] === "MAT");
  assert.equal(mat?.["actual_cost_minor"] ?? "0", "0", "the job cost is undone, not deleted");
});

test("a through-date lets the job be read as at a month end", async () => {
  const s = await ready();
  await makeJob(s);
  await cost(s, "2026-06-15", "1000000", "MAT");
  await cost(s, "2026-07-15", "500000", "MAT");
  const june = (obj(await report(s, "harper-kitchen", { through: "2026-06-30" }))["rows"] as Row[])
    .find((x) => x["cost_code"] === "MAT")!;
  assert.equal(june["actual_cost_minor"], "1000000");
  const july = (obj(await report(s))["rows"] as Row[]).find((x) => x["cost_code"] === "MAT")!;
  assert.equal(july["actual_cost_minor"], "1500000");
});

// --- the whole-job figures ---------------------------------------------------

test("the job's totals carry the contract, the revenue and both margins", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [
      { cost_code: "MAT", budget_cost_minor: "2000000", revised_cost_minor: "2400000" },
      { cost_code: "LAB", budget_cost_minor: "3000000" },
    ],
  });
  await cost(s, "2026-06-15", "1500000", "MAT");
  // an invoice against the job
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-06-30", memo: "Progress bill 1",
    lines: [
      { code: "1200", side: "DEBIT", amount_minor: "2000000" },
      { code: "4100", side: "CREDIT", amount_minor: "2000000",
        dimensions: { job: "harper-kitchen" } },
    ],
  });

  const totals = obj(await report(s))["totals"] as Row;
  assert.equal(totals["contract_minor"], "8500000");
  assert.equal(totals["revenue_minor"], "2000000");
  assert.equal(totals["actual_cost_minor"], "1500000");
  assert.equal(totals["margin_minor"], "500000", "billed less spent, so far");
  assert.equal(totals["projected_margin_minor"], "3100000",
    "contract less the current estimate of total cost");
});

test("the job list is one line per job with the figures that fit on it", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "2000000" }],
  });
  await cost(s, "2026-06-15", "500000", "MAT");
  const jobs = obj(await call(s, "GET", "/t/acme/jobs"))["jobs"] as Row[];
  assert.equal(jobs.length, 1);
  assert.equal(jobs[0]!["name"], "Harper kitchen remodel");
  assert.equal(jobs[0]!["cost_to_date_minor"], "500000");
  assert.equal(jobs[0]!["percent_spent_ppm"], 250_000);
});

// --- refusals and isolation --------------------------------------------------

test("a budget line for a cost code nobody defined is refused", async () => {
  const s = await ready();
  await makeJob(s);
  const r = await call(s, "POST", "/t/acme/jobs/harper-kitchen/budget", {
    lines: [{ cost_code: "FRAMING", budget_cost_minor: "100" }],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /FRAMING/);
});

test("a budget for a job that doesn't exist is refused", async () => {
  const s = await ready();
  assert.equal((await call(s, "POST", "/t/acme/jobs/ghost/budget", { lines: [] })).status, 400);
  assert.equal((await call(s, "GET", "/t/acme/jobs/ghost")).status, 404);
});

test("one tenant's jobs and cost codes are invisible to another", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  assert.equal((obj(await call(s, "GET", "/t/beta/jobs"))["jobs"] as Row[]).length, 0);
  assert.equal(
    (obj(await call(s, "GET", "/t/beta/cost-codes"))["cost_codes"] as Row[]).length, 0,
  );
  // and beta cannot cost alpha's job
  const r = await call(s, "POST", "/t/beta/entries", {
    date: "2026-06-15", memo: "x",
    lines: [
      { code: "5100", side: "DEBIT", amount_minor: "100",
        dimensions: { job: "harper-kitchen" } },
      { code: "1000", side: "CREDIT", amount_minor: "100" },
    ],
  });
  assert.equal(r.status, 400);
});

test("classes still work alongside jobs, and both are validated", async () => {
  const s = await ready();
  await makeJob(s);
  await call(s, "POST", "/t/acme/dimensions", {
    key: "class", label: "Line of business", values: ["Remodel", "New build"],
  });
  const good = await call(s, "POST", "/t/acme/entries", {
    date: "2026-06-15", memo: "Lumber",
    lines: [
      { code: "5100", side: "DEBIT", amount_minor: "1000",
        dimensions: { job: "harper-kitchen", cost_code: "MAT", class: "Remodel" } },
      { code: "1000", side: "CREDIT", amount_minor: "1000" },
    ],
  });
  assert.equal(good.status, 201, JSON.stringify(good.body));
  const bad = await call(s, "POST", "/t/acme/entries", {
    date: "2026-06-15", memo: "Lumber",
    lines: [
      { code: "5100", side: "DEBIT", amount_minor: "1000",
        dimensions: { job: "harper-kitchen", cost_code: "MAT", class: "Remoddel" } },
      { code: "1000", side: "CREDIT", amount_minor: "1000" },
    ],
  });
  assert.equal(bad.status, 400);
});
