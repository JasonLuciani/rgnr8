import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Bank reconciliation over the service: an owner types in the statement's ending
 * balance and date, ticks the lines that appear on it, watches the difference
 * fall to zero, and finishes. Finishing locks those lines as RECONCILED forever.
 *
 * The point of these tests is the *refusals*: you cannot finish out of balance,
 * you cannot un-tick a line a finished reconciliation locked in, and you cannot
 * tick an entry that never touched the account.
 */

const NOW = "2026-08-20T00:00:00Z";

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { now: () => NOW });
}

const call = (
  s: LedgerService,
  method: string,
  path: string,
  body: unknown = "",
  query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({
    method,
    path,
    query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Line = Record<string, unknown>;
const lines = (r: ServiceResponse): Line[] => obj(r)["lines"] as Line[];

/** A deposit (+) or a payment (−) against cash, posted as a real journal entry. */
async function cash(
  s: LedgerService, id: string, date: string, minor: string, memo: string,
): Promise<string> {
  const into = !minor.startsWith("-");
  const abs = into ? minor : minor.slice(1);
  const res = await call(s, "POST", "/t/acme/entries", {
    idempotency_key: id,
    date,
    memo,
    lines: into
      ? [
          { code: "1000", side: "DEBIT", amount_minor: abs },
          { code: "4000", side: "CREDIT", amount_minor: abs },
        ]
      : [
          { code: "6400", side: "DEBIT", amount_minor: abs },
          { code: "1000", side: "CREDIT", amount_minor: abs },
        ],
  });
  assert.equal(res.status, 201, JSON.stringify(res.body));
  return String((obj(res)["entry"] as Record<string, unknown>)["id"]);
}

async function ready(): Promise<LedgerService> {
  const s = svc();
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  return s;
}

const view = (
  s: LedgerService, date: string, balance: string,
): Promise<ServiceResponse> =>
  call(s, "GET", "/t/acme/accounts/1000/reconcile", "", {
    statement_date: date,
    statement_balance_minor: balance,
  });

const tick = (
  s: LedgerService, entryId: string, cleared: boolean, date: string, balance: string,
): Promise<ServiceResponse> =>
  call(s, "POST", "/t/acme/accounts/1000/reconcile/toggle", {
    entry_id: entryId,
    cleared,
    statement_date: date,
    statement_balance_minor: balance,
  });

// --- the view ----------------------------------------------------------------

test("an untouched account shows every line uncleared and the full difference", async () => {
  const s = await ready();
  await cash(s, "je-1", "2026-08-03", "50000", "Client deposit");
  await cash(s, "je-2", "2026-08-05", "-20000", "Software");

  const r = await view(s, "2026-08-31", "30000");
  assert.equal(r.status, 200);
  const b = obj(r);
  assert.equal(b["account_code"], "1000");
  assert.equal(b["reconciled_through"], null);
  assert.equal(b["cleared_balance_minor"], "0");
  assert.equal(b["difference_minor"], "30000");
  assert.equal(b["can_finish"], false);
  assert.equal(lines(r).length, 2);
  assert.ok(lines(r).every((l) => l["status"] === "UNCLEARED"));
  // register lines are signed from the account's point of view
  assert.equal(lines(r)[0]!["amount_minor"], "50000");
  assert.equal(lines(r)[1]!["amount_minor"], "-20000");
});

test("the view refuses a missing or malformed statement date and balance", async () => {
  const s = await ready();
  assert.equal((await view(s, "", "30000")).status, 400);
  assert.equal((await view(s, "Aug 31", "30000")).status, 400);
  assert.equal((await view(s, "2026-08-31", "")).status, 400);
  assert.equal((await view(s, "2026-08-31", "300.00")).status, 400);
  assert.equal(
    (await call(s, "GET", "/t/acme/accounts/9999/reconcile", "", {
      statement_date: "2026-08-31", statement_balance_minor: "0",
    })).status,
    400,
  );
});

// --- ticking -----------------------------------------------------------------

test("ticking the statement's lines drives the difference to zero", async () => {
  const s = await ready();
  const d1 = await cash(s, "je-1", "2026-08-03", "50000", "Client deposit");
  const p1 = await cash(s, "je-2", "2026-08-05", "-20000", "Software");
  await cash(s, "je-3", "2026-08-30", "-5000", "Check not yet cashed");

  let r = await tick(s, d1, true, "2026-08-31", "30000");
  assert.equal(r.status, 200);
  assert.equal(obj(r)["cleared_this_session_minor"], "50000");
  assert.equal(obj(r)["difference_minor"], "-20000");
  assert.equal(obj(r)["can_finish"], false);

  r = await tick(s, p1, true, "2026-08-31", "30000");
  assert.equal(obj(r)["cleared_balance_minor"], "30000");
  assert.equal(obj(r)["difference_minor"], "0");
  assert.equal(obj(r)["can_finish"], true);
  // the outstanding check stays uncleared — that is the in-transit item
  assert.equal(lines(r).filter((l) => l["status"] === "UNCLEARED").length, 1);
});

test("un-ticking a line puts the difference back", async () => {
  const s = await ready();
  const d1 = await cash(s, "je-1", "2026-08-03", "50000", "Client deposit");
  await tick(s, d1, true, "2026-08-31", "50000");
  const r = await tick(s, d1, false, "2026-08-31", "50000");
  assert.equal(obj(r)["difference_minor"], "50000");
  assert.equal(lines(r)[0]!["status"], "UNCLEARED");
});

test("an entry that does not touch the account cannot be ticked", async () => {
  const s = await ready();
  const res = await call(s, "POST", "/t/acme/entries", {
    idempotency_key: "je-x",
    date: "2026-08-04",
    memo: "Accrual, no cash",
    lines: [
      { code: "6400", side: "DEBIT", amount_minor: "1000" },
      { code: "2000", side: "CREDIT", amount_minor: "1000" },
    ],
  });
  const id = String((obj(res)["entry"] as Record<string, unknown>)["id"]);
  const r = await tick(s, id, true, "2026-08-31", "0");
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /does not affect account 1000/);
  assert.equal((await tick(s, "", true, "2026-08-31", "0")).status, 400);
  assert.equal(
    (await call(s, "POST", "/t/acme/accounts/1000/reconcile/toggle", { entry_id: "x" })).status,
    400,
  );
});

// --- finishing ---------------------------------------------------------------

test("finishing locks the ticked lines and records what is reconciled through", async () => {
  const s = await ready();
  const d1 = await cash(s, "je-1", "2026-08-03", "50000", "Client deposit");
  const p1 = await cash(s, "je-2", "2026-08-05", "-20000", "Software");
  await cash(s, "je-3", "2026-08-30", "-5000", "Check not yet cashed");
  await tick(s, d1, true, "2026-08-31", "30000");
  await tick(s, p1, true, "2026-08-31", "30000");

  const fin = await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-08-31", statement_balance_minor: "30000",
  });
  assert.equal(fin.status, 200);
  assert.equal(obj(fin)["reconciled_entries"], 2);
  assert.equal(obj(fin)["reconciled_through"], "2026-08-31");

  const after = await view(s, "2026-09-30", "25000");
  assert.equal(obj(after)["reconciled_through"], "2026-08-31");
  assert.equal(obj(after)["reconciled_balance_minor"], "30000");
  assert.equal(lines(after).filter((l) => l["status"] === "RECONCILED").length, 2);
  // September opens with only the outstanding check left to tick
  assert.equal(obj(after)["difference_minor"], "-5000");
});

test("an out-of-balance reconciliation cannot be finished", async () => {
  const s = await ready();
  const d1 = await cash(s, "je-1", "2026-08-03", "50000", "Client deposit");
  await tick(s, d1, true, "2026-08-31", "45000");
  const fin = await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-08-31", statement_balance_minor: "45000",
  });
  assert.equal(fin.status, 400);
  assert.match(String(obj(fin)["error"]), /off by/);
});

test("finishing with nothing ticked is refused, and says so plainly", async () => {
  const s = await ready();
  // The difference here is genuinely zero, so "off by 0.00" would be nonsense.
  const fin = await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-08-31", statement_balance_minor: "0",
  });
  assert.equal(fin.status, 400);
  assert.match(String(obj(fin)["error"]), /nothing has been ticked/);
});

test("a refusal names the account by its code, not an internal id", async () => {
  const s = await ready();
  const d1 = await cash(s, "je-1", "2026-08-03", "50000", "Client deposit");
  await tick(s, d1, true, "2026-08-31", "45000");
  const fin = await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-08-31", statement_balance_minor: "45000",
  });
  assert.doesNotMatch(String(obj(fin)["error"]), /acct:/);
});

test("a reconciled line is immutable — it cannot be un-ticked later", async () => {
  const s = await ready();
  const d1 = await cash(s, "je-1", "2026-08-03", "50000", "Client deposit");
  await tick(s, d1, true, "2026-08-31", "50000");
  await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-08-31", statement_balance_minor: "50000",
  });
  const r = await tick(s, d1, false, "2026-09-30", "50000");
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /locked in/);
});

test("a second month reconciles on top of the first", async () => {
  const s = await ready();
  const d1 = await cash(s, "je-1", "2026-08-03", "50000", "August deposit");
  await tick(s, d1, true, "2026-08-31", "50000");
  await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-08-31", statement_balance_minor: "50000",
  });
  const d2 = await cash(s, "je-2", "2026-09-04", "30000", "September deposit");
  const r = await tick(s, d2, true, "2026-09-30", "80000");
  assert.equal(obj(r)["difference_minor"], "0");
  const fin = await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-09-30", statement_balance_minor: "80000",
  });
  assert.equal(obj(fin)["reconciled_entries"], 1);
  assert.equal(obj(fin)["reconciled_through"], "2026-09-30");
});

// --- isolation ---------------------------------------------------------------

test("one tenant's ticks are invisible to another", async () => {
  const s = await ready();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  const mine = await cash(s, "je-1", "2026-08-03", "50000", "Acme deposit");
  await tick(s, mine, true, "2026-08-31", "50000");

  const theirs = await call(s, "GET", "/t/beta/accounts/1000/reconcile", "", {
    statement_date: "2026-08-31", statement_balance_minor: "0",
  });
  assert.equal(lines(theirs).length, 0);
  assert.equal(obj(theirs)["cleared_balance_minor"], "0");
  assert.equal(obj(theirs)["reconciled_through"], null);
  // and beta cannot tick acme's entry
  const r = await call(s, "POST", "/t/beta/accounts/1000/reconcile/toggle", {
    entry_id: mine, cleared: true,
    statement_date: "2026-08-31", statement_balance_minor: "0",
  });
  assert.equal(r.status, 400);
});
