import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Inventory.
 *
 * The design claim is that quantity and value move together, and both move when
 * the cost enters the books. Most small-business inventory tracks units in one
 * place and money in another, and the two drift within a month.
 *
 * The report that proves it is the tie-out: what the item records say stock is
 * worth against what the inventory account on the balance sheet says. Two
 * systems that can disagree eventually will, and the only useful response is to
 * show the gap.
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
    id: "harper", customer_id: "harper", name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "10000000",
  });
  await call(s, "POST", "/t/acme/inventory/items", {
    sku: "PLY-34", name: "3/4in plywood", unit: "sheet", reorder_point_milli: "20000",
    inventory_account_code: "1300", cost_account_code: "5100",
  });
  return s;
}

const receive = (s: LedgerService, quantity: string, unitCost: string, date = "2026-06-01") =>
  call(s, "POST", "/t/acme/inventory/receipts", {
    sku: "PLY-34", date, quantity_milli: quantity, unit_cost_minor: unitCost,
  });

const item = async (s: LedgerService, sku = "PLY-34"): Promise<Row> =>
  obj(await call(s, "GET", `/t/acme/inventory/items/${sku}`))["item"] as Row;

const balances = async (s: LedgerService): Promise<Record<string, bigint>> => {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const out: Record<string, bigint> = {};
  for (const row of tb["rows"] as Row[]) {
    out[String(row["code"])] =
      BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
  }
  return out;
};

// --- receiving ---------------------------------------------------------------

test("receiving stock puts units on the shelf and money on the balance sheet", async () => {
  const s = await ready();
  const r = await receive(s, "100000", "4800");     // 100 sheets at $48
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const i = await item(s);
  assert.equal(i["quantity_milli"], "100000");
  assert.equal(i["value_minor"], "480000");
  assert.equal(i["unit_cost_minor"], "4800");
  assert.equal((await balances(s))["1300"], 480000n);
});

test("a second delivery at a different price moves the average", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  await receive(s, "100000", "5200", "2026-06-15");
  const i = await item(s);
  assert.equal(i["quantity_milli"], "200000");
  assert.equal(i["value_minor"], "1000000");
  assert.equal(i["unit_cost_minor"], "5000", "the weighted average of 48 and 52");
});

test("the average is derived from the totals, so rounding cannot compound", async () => {
  const s = await ready();
  await receive(s, "3000", "3333");
  await receive(s, "3000", "3334", "2026-06-15");
  const i = await item(s);
  assert.equal(i["value_minor"], "20001", "the exact sum of what was paid");
  assert.equal((await balances(s))["1300"], 20001n, "…and the ledger agrees to the cent");
});

// --- issuing -----------------------------------------------------------------

test("issuing to a job costs the job at the average and empties the shelf", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  await receive(s, "100000", "5200", "2026-06-15");
  const r = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "40000",
    job_id: "harper", cost_code: "MAT",
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  assert.equal(obj(r)["value_minor"], "200000", "40 sheets at the average 50.00");

  const after = await balances(s);
  assert.equal(after["1300"], 800000n);
  assert.equal(after["5100"], 200000n);

  const mat = (obj(await call(s, "GET", "/t/acme/jobs/harper/cost"))["rows"] as Row[])
    .find((x) => x["cost_code"] === "MAT")!;
  assert.equal(mat["actual_cost_minor"], "200000",
    "material bought in June lands on the job that used it in July");
});

test("issuing more than is on hand is refused", async () => {
  const s = await ready();
  await receive(s, "10000", "4800");
  const r = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "20000", job_id: "harper",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /count it before you cost it/);
});

test("issuing the last of an item leaves it worth exactly nothing", async () => {
  const s = await ready();
  await receive(s, "3000", "3333");
  await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "3000", job_id: "harper",
  });
  const i = await item(s);
  assert.equal(i["quantity_milli"], "0");
  assert.equal(i["value_minor"], "0", "not a few cents of rounding");
  assert.equal((await balances(s))["1300"] ?? 0n, 0n);
});

test("stock can be issued without a job, to a plain cost account", async () => {
  const s = await ready();
  await receive(s, "10000", "4800");
  const r = await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "1000",
    cost_account_code: "6400",
  });
  assert.equal(r.status, 201);
  assert.equal((await balances(s))["6400"], 4800n);
});

// --- counting ----------------------------------------------------------------

test("a count that finds less posts shrinkage and follows the shelf", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  const r = await call(s, "POST", "/t/acme/inventory/counts", {
    sku: "PLY-34", date: "2026-07-31", counted_milli: "94000",
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  assert.equal(obj(r)["difference_milli"], "-6000");
  const after = await balances(s);
  assert.equal(after["5300"], 28800n, "6 sheets at 48.00 gone");
  assert.equal(after["1300"], 451200n);
  assert.equal((await item(s))["quantity_milli"], "94000");
});

test("a count that finds more writes the value back up", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  await call(s, "POST", "/t/acme/inventory/counts", {
    sku: "PLY-34", date: "2026-07-31", counted_milli: "102000",
  });
  const after = await balances(s);
  assert.equal(after["1300"], 489600n);
  assert.equal(after["5300"], -9600n, "found stock is a credit to shrinkage");
});

test("a count that agrees with the record posts nothing", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  const r = await call(s, "POST", "/t/acme/inventory/counts", {
    sku: "PLY-34", date: "2026-07-31", counted_milli: "100000",
  });
  assert.equal(obj(r)["entry_id"], "");
  assert.equal(obj(r)["difference_milli"], "0");
});

// --- the report --------------------------------------------------------------

test("the valuation report ties the items to the balance sheet", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "10000", job_id: "harper",
  });
  const v = obj(await call(s, "GET", "/t/acme/inventory"));
  assert.equal(v["contract"], "inventory-valuation/1");
  const totals = v["totals"] as Row;
  assert.equal(totals["items_value_minor"], "432000");
  assert.equal(totals["ledger_balance_minor"], "432000");
  assert.equal(totals["ties_out"], true);
});

test("a journal entry behind inventory's back shows up as a difference", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  // somebody codes a materials purchase straight to the inventory account
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-07-04", memo: "Coded to inventory by hand",
    lines: [
      { code: "1300", side: "DEBIT", amount_minor: "50000" },
      { code: "1000", side: "CREDIT", amount_minor: "50000" },
    ],
  });
  const totals = obj(await call(s, "GET", "/t/acme/inventory"))["totals"] as Row;
  assert.equal(totals["ties_out"], false);
  assert.equal(totals["difference_minor"], "-50000", "the ledger has 500 the shelf does not");
});

test("items at or below their reorder point are listed", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  assert.deepEqual((obj(await call(s, "GET", "/t/acme/inventory"))["reorder"] as Row[]), []);
  await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "85000", job_id: "harper",
  });
  const reorder = obj(await call(s, "GET", "/t/acme/inventory"))["reorder"] as Row[];
  assert.deepEqual(reorder.map((r) => r["sku"]), ["PLY-34"]);
  assert.equal((await item(s))["below_reorder_point"], true);
});

test("every movement is on the record with the entry that posted it", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  await call(s, "POST", "/t/acme/inventory/issues", {
    sku: "PLY-34", date: "2026-07-01", quantity_milli: "10000", job_id: "harper",
  });
  const movements = obj(await call(s, "GET", "/t/acme/inventory/items/PLY-34"))["movements"] as Row[];
  assert.deepEqual(movements.map((m) => m["kind"]), ["RECEIPT", "ISSUE"]);
  assert.equal(movements[1]!["quantity_milli"], "-10000");
  assert.ok(String(movements[1]!["entry_id"]).length > 0);
});

// --- refusals and isolation --------------------------------------------------

test("a service item has no stock to move", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/inventory/items", {
    sku: "LABOR", name: "Installation labor", kind: "SERVICE",
  });
  const r = await call(s, "POST", "/t/acme/inventory/receipts", {
    sku: "LABOR", date: "2026-06-01", quantity_milli: "1000", unit_cost_minor: "100",
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /no stock to move/);
});

test("an item's accounts have to be the right kind of account", async () => {
  const s = await ready();
  assert.equal((await call(s, "POST", "/t/acme/inventory/items", {
    sku: "X", name: "X", inventory_account_code: "4100",
  })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/inventory/items", {
    sku: "X", name: "X", cost_account_code: "1000",
  })).status, 400);
});

test("redefining an item keeps its stock", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  await call(s, "POST", "/t/acme/inventory/items", {
    sku: "PLY-34", name: "3/4in plywood (CDX)", unit: "sheet",
  });
  const i = await item(s);
  assert.equal(i["name"], "3/4in plywood (CDX)");
  assert.equal(i["quantity_milli"], "100000");
  assert.equal(i["value_minor"], "480000");
});

test("one tenant's stock is invisible to another", async () => {
  const s = await ready();
  await receive(s, "100000", "4800");
  await call(s, "POST", "/t/beta/accounts/seed", { category: "CONTRACTOR_TRADES" });
  const v = obj(await call(s, "GET", "/t/beta/inventory"));
  assert.deepEqual(v["items"], []);
  assert.equal((await call(s, "GET", "/t/beta/inventory/items/PLY-34")).status, 404);
});
