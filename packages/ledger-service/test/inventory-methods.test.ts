import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * LIFO and specific-identification inventory costing.
 *
 * The companion to `inventory-fifo.test.ts`: the same layered machinery, but the
 * issue relieves the *newest* layer (LIFO) or a *named* layer (specific-ID)
 * instead of the oldest. All three lot methods must tie out to the GL exactly,
 * and moving average must still ignore layers entirely.
 */

const NOW = "2026-09-01T00:00:00Z";

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

async function ready(method: string): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  await call(s, "POST", "/t/acme/cost-codes/seed", {});
  await call(s, "POST", "/t/acme/customers", { id: "harper", name: "Harper Residence" });
  await call(s, "POST", "/t/acme/jobs", {
    id: "harper", customer_id: "harper", name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "10000000",
  });
  await call(s, "POST", "/t/acme/inventory/items", {
    sku: "PLY-34", name: "3/4in plywood", unit: "sheet",
    inventory_account_code: "1300", cost_account_code: "5100",
  });
  const set = await call(s, "POST", "/t/acme/settings", { inventory_costing_method: method });
  assert.equal(set.status, 200, JSON.stringify(set.body));
  return s;
}

const receive = (s: LedgerService, qty: string, unitCost: string, date: string) =>
  call(s, "POST", "/t/acme/inventory/receipts", {
    sku: "PLY-34", date, quantity_milli: qty, unit_cost_minor: unitCost,
  });

const item = async (s: LedgerService): Promise<Row> =>
  obj(await call(s, "GET", "/t/acme/inventory/items/PLY-34"))["item"] as Row;

const balances = async (s: LedgerService): Promise<Record<string, bigint>> => {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const out: Record<string, bigint> = {};
  for (const row of tb["rows"] as Row[]) {
    out[String(row["code"])] =
      BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
  }
  return out;
};

const tiesOut = async (s: LedgerService): Promise<boolean> =>
  (obj(await call(s, "GET", "/t/acme/inventory"))["totals"] as Row)["ties_out"] === true;

test("LIFO issues the newest layer first, the mirror image of FIFO", async () => {
  const s = await ready("LIFO");
  await receive(s, "100000", "4800", "2026-06-01");   // 100 @ $48 = $480 (old)
  await receive(s, "100000", "5200", "2026-06-15");   // 100 @ $52 = $520 (new)

  // issue 150: LIFO takes 100@52 + 50@48 = 520 + 240 = $760 (vs FIFO's $740)
  const issued = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "150000", job_id: "harper",
  });
  assert.equal(issued.status, 201, JSON.stringify(issued.body));
  assert.equal(obj(issued)["value_minor"], "760000", "LIFO relieves the newest, dearer layer first");

  const i = await item(s);
  assert.equal(i["quantity_milli"], "50000", "50 sheets left");
  assert.equal(i["value_minor"], "240000", "…and they are the older, $48 layer");
  const bal = await balances(s);
  assert.equal(bal["1300"], 240000n, "inventory asset is the remaining old layer");
  assert.equal(bal["5100"], 760000n, "COGS is what LIFO relieved");
  assert.equal(await tiesOut(s), true);
});

test("LIFO empties the shelf to exactly zero with no dust", async () => {
  const s = await ready("LIFO");
  await receive(s, "100000", "4800", "2026-06-01");
  await receive(s, "100000", "5200", "2026-06-15");
  await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "150000", job_id: "harper",
  });
  const last = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-10", quantity_milli: "50000", job_id: "harper",
  });
  assert.equal(obj(last)["value_minor"], "240000", "the last of the $48 layer");
  const i = await item(s);
  assert.equal(i["quantity_milli"], "0");
  assert.equal(i["value_minor"], "0", "no rounding dust");
  assert.equal((await balances(s))["1300"], 0n);
  assert.equal(await tiesOut(s), true);
});

test("specific-identification draws from the exact lot the caller names", async () => {
  const s = await ready("SPECIFIC");
  await receive(s, "10000", "4800", "2026-06-01");   // lot seq 1: 10 @ $48
  await receive(s, "10000", "9000", "2026-06-15");   // lot seq 2: 10 @ $90 (a premium batch)

  // the lots are visible, with their seq numbers
  const lots = obj(await call(s, "GET", "/t/acme/inventory/items/PLY-34/lots"))["lots"] as Row[];
  assert.equal(lots.length, 2);
  assert.deepEqual(lots.map((l) => l["seq"]), [1, 2]);

  // name the premium lot 2 explicitly — cost must be $90/unit, not oldest-first
  const issued = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "4000", job_id: "harper", lot_seq: 2,
  });
  assert.equal(issued.status, 201, JSON.stringify(issued.body));
  assert.equal(obj(issued)["value_minor"], "36000", "4 @ $90 from the named lot");

  const i = await item(s);
  assert.equal(i["quantity_milli"], "16000", "16 sheets remain across both lots");
  assert.equal(i["value_minor"], "102000", "48000 (lot1) + 54000 (lot2 remainder)");
  assert.equal(await tiesOut(s), true);
});

test("specific-identification refuses an issue that does not name a lot", async () => {
  const s = await ready("SPECIFIC");
  await receive(s, "10000", "4800", "2026-06-01");
  const issued = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "4000", job_id: "harper",
  });
  assert.equal(issued.status, 400, JSON.stringify(issued.body));
  assert.match(String(obj(issued)["error"]), /name .* lot|requires naming the lot/i);
});

test("specific-identification refuses a lot that is short", async () => {
  const s = await ready("SPECIFIC");
  await receive(s, "10000", "4800", "2026-06-01");   // lot 1: only 10 on hand
  await receive(s, "10000", "9000", "2026-06-15");   // lot 2
  const issued = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "15000", job_id: "harper", lot_seq: 1,
  });
  assert.equal(issued.status, 400, JSON.stringify(issued.body));
  assert.match(String(obj(issued)["error"]), /does not hold|enough on hand/i);
});

test("moving average ignores layers entirely — the blended cost", async () => {
  const s = await ready("MOVING_AVERAGE");
  await receive(s, "100000", "4800", "2026-06-01");
  await receive(s, "100000", "5200", "2026-06-15");   // blended $50
  const issued = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "150000", job_id: "harper",
  });
  assert.equal(obj(issued)["value_minor"], "750000", "150 @ blended $50, not a layer");
  assert.equal((await item(s))["value_minor"], "250000");
  // moving-average keeps no lots
  const lots = obj(await call(s, "GET", "/t/acme/inventory/items/PLY-34/lots"))["lots"] as Row[];
  assert.equal(lots.length, 0);
  assert.equal(await tiesOut(s), true);
});

test("every method is offered by the settings endpoint", async () => {
  const s = await ready("MOVING_AVERAGE");
  for (const m of ["MOVING_AVERAGE", "FIFO", "LIFO", "SPECIFIC"]) {
    const set = await call(s, "POST", "/t/acme/settings", { inventory_costing_method: m });
    assert.equal(set.status, 200, `${m}: ${JSON.stringify(set.body)}`);
    assert.equal(
      (obj(set)["settings"] as Row)["inventory_costing_method"], m,
    );
  }
  const bad = await call(s, "POST", "/t/acme/settings", { inventory_costing_method: "WISHFUL" });
  assert.equal(bad.status, 400);
});
