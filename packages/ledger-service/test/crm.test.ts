import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * The pipeline.
 *
 * A contractor with no CRM still has a pipeline; it is in a notebook, and the
 * follow-up that never happened is the most expensive thing in the business.
 *
 * The property defended hardest here is that **none of it touches the ledger**.
 * A pipeline is worth value × probability, and that number belongs on a screen
 * where somebody decides whether to hire — not in the accounts. Winning a deal
 * books nothing; the money appears when the job is billed.
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
  return s;
}

const lead = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/leads", {
    id: "L-1", name: "Dana Harper", company: "Harper Residence",
    email: "dana@example.com", source: "Referral", owner: "jason",
    created_date: "2026-05-02", ...extra,
  });

// --- leads -------------------------------------------------------------------

test("a lead becomes a real customer, and everything downstream just works", async () => {
  const s = await ready();
  await lead(s);
  const r = await call(s, "POST", "/t/acme/leads/L-1/convert", {});
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const customerId = String(obj(r)["customer_id"]);
  assert.equal(customerId, "harper-residence");

  // it is an ordinary customer: a job can be opened on it with no special case
  const job = await call(s, "POST", "/t/acme/jobs", {
    id: "harper", customer_id: customerId, name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "5000000",
  });
  assert.equal(job.status, 201, JSON.stringify(job.body));
});

test("converting a lead twice is refused", async () => {
  const s = await ready();
  await lead(s);
  await call(s, "POST", "/t/acme/leads/L-1/convert", {});
  const again = await call(s, "POST", "/t/acme/leads/L-1/convert", {});
  assert.equal(again.status, 400);
  assert.match(String(obj(again)["error"]), /already customer/);
});

test("a disqualified lead has to be reopened before it converts", async () => {
  const s = await ready();
  await lead(s, { status: "DISQUALIFIED" });
  const r = await call(s, "POST", "/t/acme/leads/L-1/convert", {});
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /reopen it/);
});

test("converting can open the opportunity at the same time", async () => {
  const s = await ready();
  await lead(s);
  const r = await call(s, "POST", "/t/acme/leads/L-1/convert", {
    opportunity: {
      id: "OPP-1", name: "Kitchen remodel", value_minor: "8500000",
      expected_close_date: "2026-06-15", owner: "jason",
    },
  });
  const o = obj(r)["opportunity"] as Row;
  assert.equal(o["stage"], "NEW");
  assert.equal(o["customer_id"], "harper-residence");
  assert.equal(o["lead_id"], "L-1");
});

// --- opportunities -----------------------------------------------------------

async function withOpportunity(): Promise<LedgerService> {
  const s = await ready();
  await lead(s);
  await call(s, "POST", "/t/acme/leads/L-1/convert", {
    opportunity: {
      id: "OPP-1", name: "Kitchen remodel", value_minor: "8500000",
      expected_close_date: "2026-06-15", owner: "jason",
    },
  });
  return s;
}

test("a stage carries a default probability, and both values are reported", async () => {
  const s = await withOpportunity();
  const moved = await call(s, "POST", "/t/acme/opportunities", {
    id: "OPP-1", name: "Kitchen remodel", customer_id: "harper-residence",
    value_minor: "8500000", stage: "NEGOTIATION",
  });
  const o = obj(moved)["opportunity"] as Row;
  assert.equal(o["probability_ppm"], 750_000);
  assert.equal(o["weighted_value_minor"], "6375000");
});

test("raising an estimate moves the opportunity to proposal and links them", async () => {
  const s = await withOpportunity();
  const r = await call(s, "POST", "/t/acme/opportunities/OPP-1/estimate", {
    id: "EST-1", date: "2026-05-20",
    lines: [{
      description: "Everything", cost_code: "MAT", quantity_milli: "1000",
      unit_cost_minor: "6000000", markup_ppm: 200_000, account_code: "4100",
    }],
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const o = obj(r)["opportunity"] as Row;
  assert.equal(o["stage"], "PROPOSAL");
  assert.equal(o["estimate_id"], "EST-1");
  assert.equal((obj(r)["estimate"] as Row)["customer_id"], "harper-residence");
});

test("winning records the win and books absolutely nothing", async () => {
  const s = await withOpportunity();
  const r = await call(s, "POST", "/t/acme/opportunities/OPP-1/win", {});
  assert.equal(r.status, 200);
  const o = obj(r)["opportunity"] as Row;
  assert.equal(o["stage"], "WON");
  assert.equal(o["probability_ppm"], 1_000_000);
  assert.deepEqual(obj(await call(s, "GET", "/t/acme/trial-balance"))["rows"], [],
    "a pipeline that leaks into the accounts books revenue for work never won");
});

test("losing needs a reason", async () => {
  const s = await withOpportunity();
  const silent = await call(s, "POST", "/t/acme/opportunities/OPP-1/lose", {});
  assert.equal(silent.status, 400);
  assert.match(String(obj(silent)["error"]), /say why it was lost/);
  const r = await call(s, "POST", "/t/acme/opportunities/OPP-1/lose", {
    reason: "Price — went with a cheaper bid",
  });
  assert.equal(r.status, 200);
  assert.equal((obj(r)["opportunity"] as Row)["probability_ppm"], 0);
});

test("a closed opportunity cannot be edited back open by accident", async () => {
  const s = await withOpportunity();
  await call(s, "POST", "/t/acme/opportunities/OPP-1/lose", { reason: "Price" });
  const r = await call(s, "POST", "/t/acme/opportunities", {
    id: "OPP-1", name: "Kitchen remodel", customer_id: "harper-residence",
    stage: "NEGOTIATION", value_minor: "8500000",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /reopen it explicitly/);
});

test("an opportunity has to belong to somebody", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/opportunities", {
    name: "Floating deal", value_minor: "100",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /belongs to a lead or a customer/);
  assert.equal((await call(s, "POST", "/t/acme/opportunities", {
    name: "X", customer_id: "ghost",
  })).status, 400);
});

// --- the pipeline view -------------------------------------------------------

test("the pipeline reports raw and weighted value per stage, and the win rate", async () => {
  const s = await withOpportunity();
  await call(s, "POST", "/t/acme/opportunities", {
    id: "OPP-2", name: "Deck", customer_id: "harper-residence",
    value_minor: "2000000", stage: "PROPOSAL", owner: "jason",
  });
  await call(s, "POST", "/t/acme/opportunities", {
    id: "OPP-3", name: "Bath", customer_id: "harper-residence",
    value_minor: "4000000", stage: "QUALIFIED", owner: "sam",
  });
  await call(s, "POST", "/t/acme/opportunities/OPP-3/lose", { reason: "Timing" });

  const p = obj(await call(s, "GET", "/t/acme/pipeline"));
  assert.equal(p["contract"], "sales-pipeline/1");
  const stages = p["stages"] as Row[];
  assert.equal(stages.find((x) => x["stage"] === "PROPOSAL")!["weighted_value_minor"], "1000000");
  const totals = p["totals"] as Row;
  assert.equal(totals["open_value_minor"], "10500000");
  assert.equal(totals["open_weighted_minor"], "1850000", "850,000 at 10% + 2m at 50%");
  assert.equal(totals["lost_count"], 1);
  assert.equal(totals["win_rate_ppm"], 0);
  assert.deepEqual(p["lost_reasons"], [{ reason: "Timing", count: 1 }]);
});

test("the pipeline can be read for one owner", async () => {
  const s = await withOpportunity();
  await call(s, "POST", "/t/acme/opportunities", {
    id: "OPP-3", name: "Bath", customer_id: "harper-residence",
    value_minor: "4000000", stage: "QUALIFIED", owner: "sam",
  });
  const mine = obj(await call(s, "GET", "/t/acme/pipeline", "", { owner: "jason" }));
  assert.deepEqual((mine["opportunities"] as Row[]).map((o) => o["id"]), ["OPP-1"]);
});

// --- the event feed ----------------------------------------------------------

test("every change appends to a feed an external system can read with a cursor", async () => {
  const s = await withOpportunity();
  const first = obj(await call(s, "GET", "/t/acme/events"));
  assert.equal(first["contract"], "crm-events/1");
  const kinds = (first["events"] as Row[]).map((e) => e["kind"]);
  assert.deepEqual(kinds, ["lead.created", "lead.converted", "opportunity.created"]);
  const cursor = Number(first["cursor"]);

  await call(s, "POST", "/t/acme/opportunities/OPP-1/win", {});
  const next = obj(await call(s, "GET", "/t/acme/events", "", { since: String(cursor) }));
  assert.deepEqual((next["events"] as Row[]).map((e) => e["kind"]), ["opportunity.won"]);
});

test("a stage change is reported as a stage change, with where it came from", async () => {
  const s = await withOpportunity();
  await call(s, "POST", "/t/acme/opportunities", {
    id: "OPP-1", name: "Kitchen remodel", customer_id: "harper-residence",
    value_minor: "8500000", stage: "QUALIFIED",
  });
  const feed = (obj(await call(s, "GET", "/t/acme/events"))["events"] as Row[]);
  const change = feed.find((e) => e["kind"] === "opportunity.stage_changed")!;
  assert.deepEqual(change["payload"], {
    from: "NEW", to: "QUALIFIED", value_minor: "8500000",
  });
});

test("accepting an estimate reaches the feed, so a CRM sees the deal close", async () => {
  const s = await withOpportunity();
  await call(s, "POST", "/t/acme/opportunities/OPP-1/estimate", {
    id: "EST-1", date: "2026-05-20",
    lines: [{
      cost_code: "MAT", quantity_milli: "1000",
      unit_cost_minor: "6000000", markup_ppm: 200_000, account_code: "4100",
    }],
  });
  await call(s, "POST", "/t/acme/estimates/EST-1/accept", { job_name: "Harper kitchen" });
  const feed = (obj(await call(s, "GET", "/t/acme/events"))["events"] as Row[]);
  const accepted = feed.find((e) => e["kind"] === "estimate.accepted");
  assert.ok(accepted, "the feed carries it");
  assert.equal((accepted!["payload"] as Row)["job_created"], true);
});

test("one tenant's pipeline and feed are invisible to another", async () => {
  const s = await withOpportunity();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  assert.deepEqual((obj(await call(s, "GET", "/t/beta/leads"))["leads"] as Row[]), []);
  assert.deepEqual((obj(await call(s, "GET", "/t/beta/events"))["events"] as Row[]), []);
  assert.equal(
    (obj(await call(s, "GET", "/t/beta/pipeline"))["totals"] as Row)["open_value_minor"], "0",
  );
});
