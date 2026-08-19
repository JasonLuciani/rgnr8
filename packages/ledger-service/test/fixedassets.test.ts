import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Fixed assets and depreciation.
 *
 * Buy once, expense over the life. Depreciation posts the delta from what has
 * already accumulated (so re-running a month books it once), lands on the
 * depreciable base or salvage exactly, and disposal books the gain or loss
 * against net book value. The register ties out to the balance sheet.
 */

const NOW = "2026-09-01T00:00:00Z";

const call = (
  s: LedgerService, method: string, rawPath: string, body: unknown = "",
): Promise<ServiceResponse> => {
  const [path, qs] = rawPath.split("?");
  const query: Record<string, string> = {};
  if (qs) for (const pair of qs.split("&")) {
    const [k, v] = pair.split("=");
    if (k) query[k] = decodeURIComponent(v ?? "");
  }
  return s.handle({
    method, path: path!, query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });
};

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Row = Record<string, unknown>;

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  return s;
}

const balances = async (s: LedgerService): Promise<Record<string, bigint>> => {
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const out: Record<string, bigint> = {};
  for (const row of tb["rows"] as Row[]) {
    out[String(row["code"])] =
      BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
  }
  return out;
};

// A $12,000 truck, $0 salvage, straight-line over 48 months, bought from checking.
const buyTruck = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/assets", {
    id: "truck", name: "Work Truck", category: "Vehicles",
    in_service_date: "2026-01-31", cost_minor: "1200000", salvage_minor: "0",
    method: "STRAIGHT_LINE", useful_life_months: 48, paid_from_code: "1000", ...extra,
  });

test("buying an asset capitalises it: asset up, cash down", async () => {
  const s = await ready();
  const bought = await buyTruck(s);
  assert.equal(bought.status, 201, JSON.stringify(bought.body));
  const bal = await balances(s);
  assert.equal(bal["1500"], 1200000n, "Equipment carries the cost");
  assert.equal(bal["1000"], -1200000n, "checking paid for it");
});

test("straight-line depreciation posts evenly and re-running a month is a no-op", async () => {
  const s = await ready();
  await buyTruck(s);
  // one month: 1,200,000 / 48 = 25,000
  const m1 = await call(s, "POST", "/t/acme/assets/truck/depreciate", { through_date: "2026-02-28" });
  assert.equal(m1.status, 201, JSON.stringify(m1.body));
  assert.equal(obj(m1)["posted_minor"], "25000");
  assert.equal((obj(m1)["asset"] as Row)["accumulated_minor"], "25000");
  assert.equal((obj(m1)["asset"] as Row)["book_value_minor"], "1175000");

  // re-running the same month posts nothing
  const again = await call(s, "POST", "/t/acme/assets/truck/depreciate", { through_date: "2026-02-28" });
  assert.equal(obj(again)["posted_minor"], "0", "the month was already booked");

  const bal = await balances(s);
  assert.equal(bal["6900"], 25000n, "one month of depreciation expense");
  assert.equal(bal["1510"], -25000n, "accumulated depreciation is a contra-asset credit");
});

test("catching up several months at once posts the whole delta", async () => {
  const s = await ready();
  await buyTruck(s);
  // jump straight to month 4: 4 × 25,000 = 100,000
  const caught = await call(s, "POST", "/t/acme/assets/truck/depreciate", { through_date: "2026-05-31" });
  assert.equal(obj(caught)["posted_minor"], "100000", "four months in one run");
  assert.equal((obj(caught)["asset"] as Row)["accumulated_minor"], "100000");
});

test("straight-line lands on the depreciable base exactly, no dust", async () => {
  const s = await ready();
  // an awkward base: 1,000,000 / 3 doesn't divide evenly
  await call(s, "POST", "/t/acme/assets", {
    id: "rig", name: "Rig", in_service_date: "2026-01-31", cost_minor: "1000000",
    salvage_minor: "100000", method: "STRAIGHT_LINE", useful_life_months: 3, paid_from_code: "1000",
  });
  const detail = obj(await call(s, "GET", "/t/acme/assets/rig"));
  const sched = detail["schedule"] as Row[];
  assert.equal(sched.length, 3);
  const total = sched.reduce((a, r) => a + BigInt(String(r["expense_minor"])), 0n);
  assert.equal(total, 900000n, "total depreciation equals cost minus salvage exactly");
  assert.equal(sched[2]!["book_value_minor"], "100000", "ends at salvage");
});

test("declining-balance takes more early and never dips below salvage", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/assets", {
    id: "server", name: "Server", in_service_date: "2026-01-31", cost_minor: "1000000",
    salvage_minor: "100000", method: "DECLINING_BALANCE", useful_life_months: 12,
    declining_factor_micro: "2000000", paid_from_code: "1000",
  });
  const detail = obj(await call(s, "GET", "/t/acme/assets/server"));
  const sched = detail["schedule"] as Row[];
  // double-declining monthly rate = 2/12; first month = 1,000,000 × 2/12 ≈ 166,667
  assert.equal(sched[0]!["expense_minor"], "166667");
  // later months are smaller (reducing balance)
  assert.ok(BigInt(String(sched[1]!["expense_minor"])) < BigInt(String(sched[0]!["expense_minor"])));
  // never below salvage
  const lastBook = BigInt(String(sched[sched.length - 1]!["book_value_minor"]));
  assert.equal(lastBook, 100000n, "book value floored at salvage");
});

test("units-of-production depreciates by output, not the calendar", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/assets", {
    id: "press", name: "Press", in_service_date: "2026-01-31", cost_minor: "1000000",
    salvage_minor: "0", method: "UNITS_OF_PRODUCTION", total_units_milli: "100000",
    paid_from_code: "1000",
  });
  // run 10,000 of 100,000 units → 10% → 100,000 depreciation
  const used = await call(s, "POST", "/t/acme/assets/press/usage", {
    date: "2026-02-15", units_milli: "10000",
  });
  assert.equal(used.status, 201, JSON.stringify(used.body));
  assert.equal(obj(used)["posted_minor"], "100000");
  assert.equal((obj(used)["asset"] as Row)["units_used_milli"], "10000");
  // the calendar depreciation route refuses a usage asset
  const wrong = await call(s, "POST", "/t/acme/assets/press/depreciate", { through_date: "2026-03-31" });
  assert.equal(wrong.status, 400);
  assert.match(String(obj(wrong)["error"]), /usage/);
});

test("disposal clears the asset and books a gain over book value", async () => {
  const s = await ready();
  await buyTruck(s);
  await call(s, "POST", "/t/acme/assets/truck/depreciate", { through_date: "2026-05-31" }); // accum 100,000, book 1,100,000
  // sell for 1,150,000 → gain of 50,000
  const sold = await call(s, "POST", "/t/acme/assets/truck/dispose", {
    date: "2026-06-15", proceeds_minor: "1150000", proceeds_to_code: "1000", gain_loss_code: "4900",
  });
  assert.equal(sold.status, 201, JSON.stringify(sold.body));
  assert.equal(obj(sold)["gain_loss_minor"], "50000", "proceeds over net book value");
  assert.equal((obj(sold)["asset"] as Row)["status"], "DISPOSED");

  const bal = await balances(s);
  assert.equal(bal["1500"], 0n, "asset removed at cost");
  assert.equal(bal["1510"], 0n, "accumulated depreciation cleared");
  assert.equal(bal["4900"], -50000n, "gain booked to other income");
});

test("disposal at a loss books the loss", async () => {
  const s = await ready();
  await buyTruck(s);
  await call(s, "POST", "/t/acme/assets/truck/depreciate", { through_date: "2026-05-31" }); // book 1,100,000
  const sold = await call(s, "POST", "/t/acme/assets/truck/dispose", {
    date: "2026-06-15", proceeds_minor: "900000", proceeds_to_code: "1000", gain_loss_code: "4900",
  });
  assert.equal(obj(sold)["gain_loss_minor"], "-200000", "sold below book value");
  assert.equal((await balances(s))["4900"], 200000n, "loss is a debit to the P&L account");
});

test("the register totals cost, accumulated, and net book value", async () => {
  const s = await ready();
  await buyTruck(s);
  await call(s, "POST", "/t/acme/assets/truck/depreciate", { through_date: "2026-02-28" }); // 25,000
  const reg = obj(await call(s, "GET", "/t/acme/assets"));
  const totals = reg["totals"] as Row;
  assert.equal(totals["cost_minor"], "1200000");
  assert.equal(totals["accumulated_minor"], "25000");
  assert.equal(totals["book_value_minor"], "1175000");
  assert.equal(totals["active_count"], 1);
});

test("an asset needs a life for a time-based method and units for usage", async () => {
  const s = await ready();
  const noLife = await call(s, "POST", "/t/acme/assets", {
    id: "x", name: "X", in_service_date: "2026-01-01", cost_minor: "1000",
    method: "STRAIGHT_LINE", useful_life_months: 0, paid_from_code: "1000",
  });
  assert.equal(noLife.status, 400);
  const noUnits = await call(s, "POST", "/t/acme/assets", {
    id: "y", name: "Y", in_service_date: "2026-01-01", cost_minor: "1000",
    method: "UNITS_OF_PRODUCTION", total_units_milli: "0", paid_from_code: "1000",
  });
  assert.equal(noUnits.status, 400);
});
