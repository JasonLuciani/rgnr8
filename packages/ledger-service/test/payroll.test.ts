import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Payroll over the service. The point of every test here is that payroll is an
 * accrual, not the cash that left the bank: the employer's own taxes are a real
 * cost, and the withholdings are a real liability until they're deposited.
 */

const NOW = "2026-08-20T00:00:00Z";

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

async function ready(tenant = "acme"): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", `/t/${tenant}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
  await call(s, "POST", `/t/${tenant}/payroll/employees`, { id: "ada", name: "Ada Reyes" });
  await call(s, "POST", `/t/${tenant}/payroll/employees`, { id: "jo", name: "Jo Okafor" });
  return s;
}

/** A run: Ada grosses 5,000 (1,100 withheld, 200 to benefits); Jo 3,000 (600). */
const RUN = {
  id: "PR-2026-08-15",
  date: "2026-08-15",
  memo: "August 1–15",
  employer_taxes_minor: "61200",
  lines: [
    { employee_id: "ada", gross_minor: "500000", employee_taxes_minor: "110000", deductions_minor: "20000" },
    { employee_id: "jo", gross_minor: "300000", employee_taxes_minor: "60000" },
  ],
};

async function balance(s: LedgerService, code: string, tenant = "acme"): Promise<bigint> {
  const tb = obj(await call(s, "GET", `/t/${tenant}/trial-balance`));
  const row = (tb["rows"] as Row[]).find((r) => r["code"] === code);
  if (!row) return 0n;
  return BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
}

// --- employees ---------------------------------------------------------------

test("employees are stored, listed, and need a name", async () => {
  const s = await ready();
  const list = obj(await call(s, "GET", "/t/acme/payroll/employees"))["employees"] as Row[];
  assert.equal(list.length, 2);
  assert.equal(list[0]!["name"], "Ada Reyes");
  assert.equal((await call(s, "POST", "/t/acme/payroll/employees", { name: "" })).status, 400);
  // an id is derived from the name when one isn't given
  const made = await call(s, "POST", "/t/acme/payroll/employees", { name: "Sam O'Neil" });
  assert.equal((obj(made)["employee"] as Row)["id"], "sam-o-neil");
});

// --- drafting ----------------------------------------------------------------

test("a draft computes net pay so nobody does the arithmetic by hand", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/payroll/runs", RUN);
  assert.equal(r.status, 201);
  const run = obj(r)["run"] as Row;
  assert.equal(run["status"], "DRAFT");
  const lines = run["lines"] as Row[];
  assert.equal(lines[0]!["net_minor"], "370000");  // 5000 − 1100 − 200
  assert.equal(lines[1]!["net_minor"], "240000");  // 3000 − 600

  const totals = run["totals"] as Row;
  assert.equal(totals["gross_minor"], "800000");
  assert.equal(totals["net_minor"], "610000");
  assert.equal(totals["employee_taxes_minor"], "170000");
  assert.equal(totals["employer_taxes_minor"], "61200");
  // what it really costs, and what is owed afterwards
  assert.equal(totals["total_cost_minor"], "861200");
  assert.equal(totals["liability_minor"], "251200");  // 1700 + 200 + 612
});

test("drafting posts nothing to the ledger", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  assert.equal((obj(await call(s, "GET", "/t/acme/trial-balance"))["rows"] as Row[]).length, 0);
});

test("a nonsensical run is refused", async () => {
  const s = await ready();
  const bad = async (body: unknown): Promise<number> =>
    (await call(s, "POST", "/t/acme/payroll/runs", body)).status;

  assert.equal(await bad({ date: "August 15", lines: RUN.lines }), 400);
  assert.equal(await bad({ date: "2026-08-15", lines: [] }), 400);
  assert.equal(await bad({ date: "2026-08-15", lines: [{ employee_id: "ghost", gross_minor: "1" }] }), 400);
  // withholdings bigger than gross would mean a negative paycheque
  const negative = await call(s, "POST", "/t/acme/payroll/runs", {
    date: "2026-08-15",
    lines: [{ employee_id: "ada", gross_minor: "100000", employee_taxes_minor: "200000" }],
  });
  assert.equal(negative.status, 400);
  assert.match(String(obj(negative)["error"]), /more than gross pay/);
  // the same employee twice is a duplicate, not two paycheques
  const twice = await call(s, "POST", "/t/acme/payroll/runs", {
    date: "2026-08-15",
    lines: [
      { employee_id: "ada", gross_minor: "100000" },
      { employee_id: "ada", gross_minor: "100000" },
    ],
  });
  assert.equal(twice.status, 400);
  assert.match(String(obj(twice)["error"]), /appears twice/);
  assert.equal(await bad({ date: "2026-08-15", lines: [{ employee_id: "ada", gross_minor: "0" }] }), 400);
  assert.equal(await bad({ date: "2026-08-15", lines: [{ employee_id: "ada", gross_minor: "-1" }] }), 400);
  assert.equal(await bad({ date: "2026-08-15", lines: [{ employee_id: "ada", gross_minor: "50.00" }] }), 400);
});

// --- posting -----------------------------------------------------------------

test("posting books the full gross-to-net entry, not just the cash", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  const r = await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.ok(obj(r)["entry_id"]);

  assert.equal(await balance(s, "6200"), 800000n, "wages expense is GROSS, not net");
  assert.equal(await balance(s, "6210"), 61200n, "the employer's own taxes are a real cost");
  assert.equal(await balance(s, "1000"), -610000n, "only net pay actually left the bank");
  assert.equal(-(await balance(s, "2300")), 251200n, "withholdings + employer taxes are owed");
  assert.equal(obj(await call(s, "GET", "/t/acme/trial-balance"))["in_balance"], true);
});

test("the cash that left is smaller than what payroll cost", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  const cashOut = -(await balance(s, "1000"));
  const realCost = (await balance(s, "6200")) + (await balance(s, "6210"));
  assert.ok(realCost > cashOut);
  assert.equal(realCost - cashOut, 251200n, "the gap is exactly what is still owed");
});

test("a run cannot be posted twice, or posted when unknown", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  const twice = await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  assert.equal(twice.status, 400);
  assert.match(String(obj(twice)["error"]), /already posted/);
  assert.equal((await call(s, "POST", "/t/acme/payroll/runs/nope/post", {})).status, 400);
  // and a posted run can't be quietly redrafted
  const redraft = await call(s, "POST", "/t/acme/payroll/runs", RUN);
  assert.equal(redraft.status, 400);
});

test("a run dated into a closed month is refused", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/periods/2026-08/lock", {});
  assert.equal((await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {})).status, 409);
});

test("voiding reverses the entry rather than deleting it", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  const v = await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/void", {});
  assert.equal(v.status, 200);
  assert.equal((obj(v)["run"] as Row)["status"], "VOID");
  assert.equal(await balance(s, "6200"), 0n);
  const entries = obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Row[];
  assert.equal(entries.length, 2);
  assert.ok(entries.some((e) => e["status"] === "REVERSAL"));
  // a draft has nothing to void
  assert.equal((await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/void", {})).status, 400);
});

test("re-running a voided pay date actually re-posts the expense", async () => {
  // The trap: a voided run keeps its id, so a corrected re-run once shared the
  // original's idempotency key — the engine handed back the already-reversed
  // entry and posted nothing, silently dropping a real payroll while the status
  // read POSTED. The revision must make the re-run a distinct event.
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/void", {});
  assert.equal(await balance(s, "6200"), 0n, "voided: nets to zero");

  // re-enter the same pay date, corrected (Ada's gross was really 6,000)
  const corrected = {
    ...RUN,
    lines: [
      { employee_id: "ada", gross_minor: "600000", employee_taxes_minor: "110000", deductions_minor: "20000" },
      { employee_id: "jo", gross_minor: "300000", employee_taxes_minor: "60000" },
    ],
  };
  assert.equal((await call(s, "POST", "/t/acme/payroll/runs", corrected)).status, 201);
  const reposted = await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  assert.equal(reposted.status, 200, JSON.stringify(reposted.body));
  assert.equal(await balance(s, "6200"), 900000n, "the corrected wages are really in the books");
  assert.equal((obj(await call(s, "GET", "/t/acme/payroll/runs/PR-2026-08-15"))["run"] as Row)["status"], "POSTED");

  // and it can be voided again without a key collision
  assert.equal(
    (await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/void", {})).status, 200,
  );
  assert.equal(await balance(s, "6200"), 0n);
});

// --- what is still owed ------------------------------------------------------

test("liabilities say what is owed before the deposit is due", async () => {
  const s = await ready();
  let view = obj(await call(s, "GET", "/t/acme/payroll/liabilities"));
  assert.equal(view["owed_minor"], "0");
  assert.equal(view["posted_runs"], 0);

  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  view = obj(await call(s, "GET", "/t/acme/payroll/liabilities"));
  assert.equal(view["owed_minor"], "0", "a draft owes nothing yet");
  assert.equal(view["draft_runs"], 1);

  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  view = obj(await call(s, "GET", "/t/acme/payroll/liabilities"));
  assert.equal(view["owed_minor"], "251200");
  assert.equal(view["posted_runs"], 1);
  assert.equal(view["account_name"], "Payroll Liabilities");
});

test("remitting clears the liability and moves the cash", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});

  const part = await call(s, "POST", "/t/acme/payroll/remit", {
    date: "2026-08-18", amount_minor: "170000", memo: "Federal deposit",
  });
  assert.equal(part.status, 200);
  assert.equal(obj(part)["remaining_minor"], "81200");
  assert.equal(-(await balance(s, "2300")), 81200n);
  assert.equal(await balance(s, "1000"), -780000n);

  const rest = await call(s, "POST", "/t/acme/payroll/remit", {
    date: "2026-08-19", amount_minor: "81200",
  });
  assert.equal(obj(rest)["remaining_minor"], "0");
  assert.equal(await balance(s, "2300"), 0n);
  assert.equal(obj(await call(s, "GET", "/t/acme/trial-balance"))["in_balance"], true);
});

test("two equal remittances on the same day both post — the second is not swallowed", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});

  // two separate $500 deposits to the same agency the same day: distinct events.
  const a = await call(s, "POST", "/t/acme/payroll/remit", {
    date: "2026-08-18", amount_minor: "50000",
  });
  const b = await call(s, "POST", "/t/acme/payroll/remit", {
    date: "2026-08-18", amount_minor: "50000",
  });
  assert.equal(a.status, 200, JSON.stringify(a.body));
  assert.equal(b.status, 200, JSON.stringify(b.body));
  assert.notEqual(obj(a)["entry_id"], obj(b)["entry_id"], "each remittance is its own entry");
  // 251200 owed − two 50000 payments = 151200 left, and the bank moved twice.
  assert.equal(-(await balance(s, "2300")), 151200n, "both payments relieved the liability");
  assert.equal(await balance(s, "1000"), -710000n, "both payments left the bank");
  assert.equal(obj(await call(s, "GET", "/t/acme/trial-balance"))["in_balance"], true);
});

test("a retried remittance carrying the same id posts only once", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  const once = await call(s, "POST", "/t/acme/payroll/remit", {
    id: "eftps-556677", date: "2026-08-18", amount_minor: "50000",
  });
  const retry = await call(s, "POST", "/t/acme/payroll/remit", {
    id: "eftps-556677", date: "2026-08-18", amount_minor: "50000",
  });
  assert.equal(obj(once)["entry_id"], obj(retry)["entry_id"], "the same id is the same event");
  assert.equal(-(await balance(s, "2300")), 201200n, "the retry did not pay twice");
});

test("remitting more than is owed is refused", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});
  const over = await call(s, "POST", "/t/acme/payroll/remit", {
    date: "2026-08-18", amount_minor: "300000",
  });
  assert.equal(over.status, 400);
  assert.match(String(obj(over)["error"]), /would overpay/);
  assert.equal((await call(s, "POST", "/t/acme/payroll/remit", {
    date: "2026-08-18", amount_minor: "0",
  })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/payroll/remit", {
    date: "bad", amount_minor: "100",
  })).status, 400);
});

// --- isolation ---------------------------------------------------------------

test("one tenant's payroll is invisible to another", async () => {
  const s = await ready();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await call(s, "POST", "/t/acme/payroll/runs", RUN);
  await call(s, "POST", "/t/acme/payroll/runs/PR-2026-08-15/post", {});

  assert.equal((obj(await call(s, "GET", "/t/beta/payroll/employees"))["employees"] as Row[]).length, 0);
  assert.equal((obj(await call(s, "GET", "/t/beta/payroll/runs"))["runs"] as Row[]).length, 0);
  assert.equal(obj(await call(s, "GET", "/t/beta/payroll/liabilities"))["owed_minor"], "0");
  assert.equal((await call(s, "POST", "/t/beta/payroll/runs/PR-2026-08-15/post", {})).status, 400);
});
