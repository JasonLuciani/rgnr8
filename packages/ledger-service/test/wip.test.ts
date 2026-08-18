import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Work in progress and percent complete.
 *
 * A contractor bills on the schedule the customer agreed to and spends on the
 * schedule the weather agreed to. Left alone, a job 70% built and 40% billed
 * reports a loss, and the same job next month reports a windfall. Neither is
 * true, and a business run off that P&L hires in the wrong month.
 *
 * The property that matters most here is that the entry is a **delta to the
 * correct balance**, not a fresh accrual: running it twice must be a no-op, and
 * running it after a late cost arrives must simply correct the difference.
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

async function ready(costMethod = "AS_INCURRED"): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  await call(s, "POST", "/t/acme/cost-codes/seed", {});
  await call(s, "POST", "/t/acme/customers", { id: "harper", name: "Harper Residence" });
  await call(s, "POST", "/t/acme/jobs", {
    id: "harper", customer_id: "harper", name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "10000000",   // $100,000
    cost_method: costMethod,
  });
  await call(s, "POST", "/t/acme/jobs/harper/budget", {
    // $80,000 of estimated cost — a 20% margin, if it holds
    lines: [{ cost_code: "MAT", budget_cost_minor: "8000000" }],
  });
  return s;
}

/** Spend on the job. */
const spend = (s: LedgerService, date: string, minor: string, job = "harper") =>
  call(s, "POST", "/t/acme/entries", {
    date, memo: "Job cost",
    lines: [
      { code: "5100", side: "DEBIT", amount_minor: minor,
        dimensions: { job, cost_code: "MAT" } },
      { code: "1000", side: "CREDIT", amount_minor: minor },
    ],
  });

/** Bill the customer. */
const bill = (s: LedgerService, date: string, minor: string, job = "harper") =>
  call(s, "POST", "/t/acme/entries", {
    date, memo: "Progress bill",
    lines: [
      { code: "1200", side: "DEBIT", amount_minor: minor },
      { code: "4100", side: "CREDIT", amount_minor: minor, dimensions: { job } },
    ],
  });

const schedule = async (s: LedgerService, through = "2026-06-30"): Promise<Row> => {
  const r = obj(await call(s, "GET", "/t/acme/wip", "", { through }));
  return (r["rows"] as Row[])[0] ?? {};
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

// --- the schedule ------------------------------------------------------------

test("percent complete is cost against the estimate, and earning follows it", async () => {
  const s = await ready();
  await spend(s, "2026-06-15", "2000000");        // $20,000 of $80,000
  const row = await schedule(s);
  assert.equal(row["percent_complete_ppm"], 250_000, "25% complete");
  assert.equal(row["earned_revenue_minor"], "2500000", "25% of the $100,000 contract");
  assert.equal(row["cost_to_complete_minor"], "6000000");
  assert.equal(row["gross_profit_minor"], "500000", "the margin, earned so far");
});

test("underbilling shows as an asset, overbilling as a liability", async () => {
  const s = await ready();
  await spend(s, "2026-06-15", "4000000");        // 50% complete → earned 50,000
  await bill(s, "2026-06-20", "3000000");         // billed 30,000
  const under = await schedule(s);
  assert.equal(under["under_billed_minor"], "2000000", "20,000 built and not billed");
  assert.equal(under["over_billed_minor"], "0");

  await bill(s, "2026-06-25", "4000000");         // billed 70,000 in total
  const over = await schedule(s);
  assert.equal(over["under_billed_minor"], "0");
  assert.equal(over["over_billed_minor"], "2000000", "20,000 taken for work not done");
});

test("a job with no estimate is 100% complete rather than pretending", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/budget", { lines: [] });
  await spend(s, "2026-06-15", "2000000");
  const row = await schedule(s);
  assert.equal(row["percent_complete_ppm"], 1_000_000);
  assert.equal(row["estimated_cost_minor"], "2000000");
});

test("cost past the estimate caps the percentage and says the estimate is blown", async () => {
  const s = await ready();
  await spend(s, "2026-06-15", "9000000");        // $90,000 spent of an $80,000 estimate
  const row = await schedule(s);
  assert.equal(row["percent_complete_ppm"], 1_000_000, "not 112%");
  assert.equal(row["estimate_exceeded"], true);
  assert.equal(row["projected_loss_minor"], "0", "the estimate, not the actual, decides that");
});

test("an estimate above the contract is reported as a projected loss", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "8000000",
              revised_cost_minor: "11000000" }],
  });
  await spend(s, "2026-06-15", "2000000");
  const row = await schedule(s);
  assert.equal(row["projected_loss_minor"], "1000000", "$10,000 under water");
});

// --- the entry ---------------------------------------------------------------

test("the adjustment makes the balance sheet agree with the schedule", async () => {
  const s = await ready();
  await spend(s, "2026-06-15", "4000000");
  await bill(s, "2026-06-20", "3000000");

  const r = await call(s, "POST", "/t/acme/wip/post", {
    date: "2026-06-30", through: "2026-06-30",
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(obj(r)["posted"], true);

  const after = await balances(s);
  assert.equal(after["1250"], 2000000n, "costs in excess of billings");
  assert.equal(after["4100"], -5000000n, "revenue is what was earned, not what was billed");
  assert.equal(after["5100"], 4000000n);
  assert.equal(obj(await call(s, "GET", "/t/acme/trial-balance"))["in_balance"], true);
});

test("running it twice cannot double the adjustment", async () => {
  const s = await ready();
  await spend(s, "2026-06-15", "4000000");
  await bill(s, "2026-06-20", "3000000");
  await call(s, "POST", "/t/acme/wip/post", { date: "2026-06-30" });
  const again = await call(s, "POST", "/t/acme/wip/post", { date: "2026-06-30" });
  assert.equal(again.status, 200);
  assert.equal((await balances(s))["1250"], 2000000n, "still 20,000, not 40,000");
});

test("the next month posts the delta, not a second accrual", async () => {
  const s = await ready();
  await spend(s, "2026-06-15", "4000000");
  await bill(s, "2026-06-20", "3000000");
  await call(s, "POST", "/t/acme/wip/post", { date: "2026-06-30" });

  // July: billing catches up and passes the work
  await bill(s, "2026-07-10", "4000000");         // 70,000 billed, 50,000 earned
  const july = await call(s, "POST", "/t/acme/wip/post", {
    date: "2026-07-31", through: "2026-07-31",
  });
  assert.equal(july.status, 200, JSON.stringify(july.body));

  const after = await balances(s);
  assert.equal(after["1250"] ?? 0n, 0n, "the asset is gone");
  assert.equal(after["2450"], -2000000n, "…and it is a liability now");
  assert.equal(after["4100"], -5000000n, "revenue is still what was earned");
});

test("a schedule already agreed with posts nothing and says so", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/wip/post", { date: "2026-06-30" });
  assert.equal(obj(r)["posted"], false);
  assert.match(String(obj(r)["reason"]), /already agrees/);
});

test("the adjustment can be run for one job", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs", {
    id: "other", customer_id: "harper", name: "Other job",
    billing_method: "PROGRESS", contract_minor: "5000000",
  });
  await spend(s, "2026-06-15", "4000000");
  await spend(s, "2026-06-15", "1000000", "other");
  const r = await call(s, "POST", "/t/acme/wip/post", {
    date: "2026-06-30", job_id: "harper",
  });
  assert.deepEqual((obj(r)["jobs"] as Row[]).map((j) => j["job_id"]), ["harper"]);
});

test("a loss provision is only posted when it is asked for", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs/harper/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "8000000",
              revised_cost_minor: "11000000" }],
  });
  await spend(s, "2026-06-15", "5500000");        // 50% of the revised estimate
  await call(s, "POST", "/t/acme/wip/post", { date: "2026-06-30" });
  const withoutProvision = (await balances(s))["4100"];
  assert.equal(withoutProvision, -5000000n, "half the contract earned, no provision");

  const s2 = await ready();
  await call(s2, "POST", "/t/acme/jobs/harper/budget", {
    lines: [{ cost_code: "MAT", budget_cost_minor: "8000000",
              revised_cost_minor: "11000000" }],
  });
  await spend(s2, "2026-06-15", "5500000");
  const r = await call(s2, "POST", "/t/acme/wip/post", {
    date: "2026-06-30", include_loss_provision: true,
  });
  // $10,000 of foreseen loss, of which $5,000 is already in the numbers
  // (earned 50,000 against 55,000 spent), so the provision is the other half.
  assert.equal(String((obj(r)["jobs"] as Row[])[0]!["loss_provision_minor"]), "500000");
  assert.equal((await balances(s2))["4100"], -4500000n, "the whole foreseen loss, now");
});

// --- completed contract ------------------------------------------------------

test("a completed-contract job keeps cost and billing off the P&L until it is done", async () => {
  const s = await ready("CAPITALIZE");
  await spend(s, "2026-06-15", "4000000");
  await bill(s, "2026-06-20", "3000000");
  await call(s, "POST", "/t/acme/wip/post", { date: "2026-06-30" });

  const after = await balances(s);
  assert.equal(after["1270"], 4000000n, "cost is on the balance sheet");
  assert.equal(after["5100"] ?? 0n, 0n, "…and not in the P&L");
  assert.equal(after["2450"], -3000000n, "billings are deferred");
  assert.equal(after["4100"] ?? 0n, 0n, "…and no revenue is recognized yet");
});

test("completing the job releases everything at once", async () => {
  const s = await ready("CAPITALIZE");
  await spend(s, "2026-06-15", "4000000");
  await bill(s, "2026-06-20", "3000000");
  await call(s, "POST", "/t/acme/wip/post", { date: "2026-06-30" });

  await call(s, "POST", "/t/acme/jobs", {
    id: "harper", customer_id: "harper", name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "10000000",
    cost_method: "CAPITALIZE", status: "COMPLETE",
  });
  const r = await call(s, "POST", "/t/acme/wip/post", {
    date: "2026-07-31", through: "2026-07-31",
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));

  const after = await balances(s);
  assert.equal(after["1270"] ?? 0n, 0n);
  assert.equal(after["2450"] ?? 0n, 0n);
  assert.equal(after["5100"], 4000000n, "the cost lands in the month it finished");
  assert.equal(after["4100"], -3000000n);
});

// --- isolation ---------------------------------------------------------------

test("the schedule totals every job and skips the ones not started", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/jobs", {
    id: "bid", customer_id: "harper", name: "Still bidding",
    billing_method: "PROGRESS", contract_minor: "5000000", status: "ESTIMATING",
  });
  await spend(s, "2026-06-15", "4000000");
  const all = obj(await call(s, "GET", "/t/acme/wip", "", { through: "2026-06-30" }));
  assert.deepEqual((all["rows"] as Row[]).map((r) => r["job_id"]), ["harper"]);
  assert.equal((all["totals"] as Row)["earned_revenue_minor"], "5000000");
});

test("one tenant's work in progress is invisible to another", async () => {
  const s = await ready();
  await spend(s, "2026-06-15", "4000000");
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  const b = obj(await call(s, "GET", "/t/beta/wip"));
  assert.deepEqual(b["rows"], []);
});
