import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * FIFO inventory costing.
 *
 * When the account is set to FIFO, an issue is costed from the oldest cost
 * layers first — not the blended moving average. The two diverge the moment a
 * SKU has been bought at two prices, and the difference lands in COGS and in
 * what the shelf is still worth. The tie-out to the GL must hold either way.
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

async function fifoReady(): Promise<LedgerService> {
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
  // switch the account to FIFO before anything is on the shelf
  const set = await call(s, "POST", "/t/acme/settings", { inventory_costing_method: "FIFO" });
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

test("FIFO issues the oldest layer first, not the blended average", async () => {
  const s = await fifoReady();
  await receive(s, "100000", "4800", "2026-06-01");   // 100 @ $48 = $480
  await receive(s, "100000", "5200", "2026-06-15");   // 100 @ $52 = $520, total $1,000

  // issue 150 to the job: FIFO takes 100@48 + 50@52 = 480 + 260 = $740 (COGS)
  const issued = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "150000", job_id: "harper",
  });
  assert.equal(issued.status, 201, JSON.stringify(issued.body));
  assert.equal(obj(issued)["value_minor"], "740000", "FIFO COGS, not the 750000 a blended average would give");

  const i = await item(s);
  assert.equal(i["quantity_milli"], "50000", "50 sheets left");
  assert.equal(i["value_minor"], "260000", "…and they are the newer, $52 layer");
  assert.equal(i["unit_cost_minor"], "5200");

  const bal = await balances(s);
  assert.equal(bal["1300"], 260000n, "inventory asset is the remaining layer");
  assert.equal(bal["5100"], 740000n, "COGS is what FIFO relieved");
  assert.equal((obj(await call(s, "GET", "/t/acme/inventory"))["totals"] as Row)["ties_out"], true);
});

test("issuing the rest empties the shelf to exactly zero", async () => {
  const s = await fifoReady();
  await receive(s, "100000", "4800", "2026-06-01");
  await receive(s, "100000", "5200", "2026-06-15");
  await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "150000", job_id: "harper",
  });
  const last = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-10", quantity_milli: "50000", job_id: "harper",
  });
  assert.equal(obj(last)["value_minor"], "260000", "the last of the $52 layer");
  const i = await item(s);
  assert.equal(i["quantity_milli"], "0");
  assert.equal(i["value_minor"], "0", "no rounding dust left behind");
  assert.equal((await balances(s))["1300"], 0n);
  assert.equal((obj(await call(s, "GET", "/t/acme/inventory"))["totals"] as Row)["ties_out"], true);
});

test("the costing method cannot be switched while stock is on hand", async () => {
  const s = await fifoReady();
  await receive(s, "100000", "4800", "2026-06-01");
  const flip = await call(s, "POST", "/t/acme/settings", { inventory_costing_method: "MOVING_AVERAGE" });
  assert.equal(flip.status, 400, JSON.stringify(flip.body));
  assert.match(String(obj(flip)["error"]), /stock on hand/);

  // but once the shelf is empty, the switch is allowed
  await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "100000", job_id: "harper",
  });
  const ok = await call(s, "POST", "/t/acme/settings", { inventory_costing_method: "MOVING_AVERAGE" });
  assert.equal(ok.status, 200, JSON.stringify(ok.body));
});

test("two same-day FIFO receipts at different prices are two distinct layers", async () => {
  const s = await fifoReady();
  await receive(s, "100000", "4800", "2026-06-01");
  await receive(s, "100000", "5200", "2026-06-01");   // same day, new price
  // issue 120: 100@48 + 20@52 = 480 + 104 = 584
  const issued = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "120000", job_id: "harper",
  });
  assert.equal(obj(issued)["value_minor"], "584000", "layered oldest-first even within one day");
  assert.equal((await item(s))["value_minor"], "416000", "80 sheets of the $52 layer remain");
});
