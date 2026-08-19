import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

const NOW = "2026-08-31T00:00:00Z";
const TOKEN = "svc-secret";

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { authToken: TOKEN, now: () => NOW });
}

function call(
  s: LedgerService,
  method: string,
  path: string,
  body: unknown = "",
  query: Record<string, string> = {},
  token: string = TOKEN,
): Promise<ServiceResponse> {
  return s.handle({
    method,
    path,
    query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: { authorization: `Bearer ${token}` },
  });
}

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

/** A minimal set of books: seed a chart, then a cash sale and a rent payment. */
async function seededBooks(s: LedgerService, tenant = "acme"): Promise<void> {
  await call(s, "POST", `/t/${tenant}/accounts/seed`, { category: "SERVICE_GENERAL" });
  await call(s, "POST", `/t/${tenant}/entries`, {
    date: "2026-08-05",
    memo: "Cash sale",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "500000" },
      { code: "4000", side: "CREDIT", amount_minor: "500000" },
    ],
  });
  await call(s, "POST", `/t/${tenant}/entries`, {
    date: "2026-08-10",
    memo: "Pay rent",
    lines: [
      { code: "6300", side: "DEBIT", amount_minor: "200000" },
      { code: "1000", side: "CREDIT", amount_minor: "200000" },
    ],
  });
}

test("health and readiness need no auth; everything else does", async () => {
  const s = svc();
  const h = await s.handle({ method: "GET", path: "/health", query: {}, body: "", headers: {} });
  assert.equal(h.status, 200);
  const ready = await s.handle({ method: "GET", path: "/ready", query: {}, body: "", headers: {} });
  assert.equal(ready.status, 200);
  assert.equal((ready.body as Record<string, unknown>)["status"], "ready");
  const unauthed = await s.handle({
    method: "GET", path: "/t/acme/accounts", query: {}, body: "", headers: {},
  });
  assert.equal(unauthed.status, 401);
  assert.equal((await call(s, "GET", "/t/acme/accounts", "", {}, "wrong")).status, 401);
  // the constant-time compare is length-independent: a prefix of the real token
  // and a token longer than it are both rejected, not just an equal-length miss.
  assert.equal((await call(s, "GET", "/t/acme/accounts", "", {}, "svc-secre")).status, 401);
  assert.equal((await call(s, "GET", "/t/acme/accounts", "", {}, "svc-secret-plus")).status, 401);
  assert.equal((await call(s, "GET", "/t/acme/accounts", "", {}, "")).status, 401);
  // and the correct token still gets in
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  assert.equal((await call(s, "GET", "/t/acme/accounts")).status, 200);
});

test("seeding a chart gives a real chart of accounts", async () => {
  const s = svc();
  const seed = await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  assert.equal(seed.status, 201);
  const list = obj(await call(s, "GET", "/t/acme/accounts"));
  const accounts = list["accounts"] as Array<Record<string, unknown>>;
  assert.ok(accounts.length >= 25);
  assert.ok(accounts.some((a) => a["name"] === "Job Materials"));
  const cash = accounts.find((a) => a["code"] === "1000")!;
  assert.equal(cash["subtype"], "BANK");
});

test("an owner can add an account, and a bad subtype is rejected", async () => {
  const s = svc();
  const r = await call(s, "POST", "/t/acme/accounts", {
    code: "6510", name: "Software", subtype: "EXPENSE",
  });
  assert.equal(r.status, 201);
  assert.equal((obj(r)["account"] as Record<string, unknown>)["type"], "EXPENSE");
  // duplicate code
  assert.equal((await call(s, "POST", "/t/acme/accounts", { code: "6510", name: "Dup", subtype: "EXPENSE" })).status, 409);
  // bogus subtype
  assert.equal((await call(s, "POST", "/t/acme/accounts", { code: "9", name: "X", subtype: "WIZARD" })).status, 400);
  // no type at all
  assert.equal((await call(s, "POST", "/t/acme/accounts", { code: "8", name: "Y" })).status, 400);
});

test("posting a journal entry keeps the books in balance", async () => {
  const s = svc();
  await seededBooks(s);

  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  assert.equal(tb["in_balance"], true);
  assert.equal(tb["total_debit_minor"], tb["total_credit_minor"]);
  const rows = tb["rows"] as Array<Record<string, unknown>>;
  const cash = rows.find((r) => r["code"] === "1000")!;
  assert.equal(cash["debit_minor"], "300000"); // 5000 in - 2000 out
  const rev = rows.find((r) => r["code"] === "4000")!;
  assert.equal(rev["credit_minor"], "500000");
});

test("an unbalanced or malformed entry is refused", async () => {
  const s = svc();
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  const unbalanced = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "500000" },
      { code: "4000", side: "CREDIT", amount_minor: "400000" },
    ],
  });
  assert.equal(unbalanced.status, 400);
  // unknown account
  assert.equal((await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05",
    lines: [{ code: "9999", side: "DEBIT", amount_minor: "1" }, { code: "4000", side: "CREDIT", amount_minor: "1" }],
  })).status, 400);
  // negative amount (side conveys direction)
  assert.equal((await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05",
    lines: [{ code: "1000", side: "DEBIT", amount_minor: "-5" }, { code: "4000", side: "CREDIT", amount_minor: "-5" }],
  })).status, 400);
  // one-sided
  assert.equal((await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05", lines: [{ code: "1000", side: "DEBIT", amount_minor: "5" }],
  })).status, 400);
  // bad date
  assert.equal((await call(s, "POST", "/t/acme/entries", { date: "Aug 5", lines: [] })).status, 400);
});

test("posting is idempotent under an explicit key", async () => {
  const s = svc();
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  const body = {
    date: "2026-08-05", idempotency_key: "invoice-42",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100000" },
      { code: "4000", side: "CREDIT", amount_minor: "100000" },
    ],
  };
  const a = await call(s, "POST", "/t/acme/entries", body);
  const b = await call(s, "POST", "/t/acme/entries", body);
  assert.equal((obj(a)["entry"] as Record<string, unknown>)["id"],
               (obj(b)["entry"] as Record<string, unknown>)["id"]);
  const entries = obj(await call(s, "GET", "/t/acme/entries"))["entries"] as unknown[];
  assert.equal(entries.length, 1);
});

test("a locked period refuses further posting (409)", async () => {
  const s = svc();
  await seededBooks(s);
  assert.equal((await call(s, "POST", "/t/acme/periods/2026-08/lock", {})).status, 200);
  const late = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-20",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "4000", side: "CREDIT", amount_minor: "100" },
    ],
  });
  assert.equal(late.status, 409);
  // a later, open period still posts
  assert.equal((await call(s, "POST", "/t/acme/entries", {
    date: "2026-09-02",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "4000", side: "CREDIT", amount_minor: "100" },
    ],
  })).status, 201);
});

test("statements are computed from the tenant's own posted books", async () => {
  const s = svc();
  await seededBooks(s);
  const st = obj(await call(s, "GET", "/t/acme/statements", "", { from: "2026-08-01", to: "2026-08-31" }));
  assert.equal(st["contract"], "financial-statements/1");
  const income = st["income_statement"] as Record<string, unknown>;
  assert.equal(income["total_revenue"], 500000);
  assert.equal(income["total_expenses"], 200000);
  assert.equal(income["net_income"], 300000);
  const bs = st["balance_sheet"] as Record<string, unknown>;
  assert.equal(bs["balanced"], true);
  assert.equal(bs["total_assets"], 300000); // cash: 5000 in - 2000 rent
});

test("a mid-year balance sheet balances — prior-period earnings are in equity", async () => {
  // The trap: revenue/expense earned before the reporting window is never swept
  // into retained earnings (a period lock posts nothing), so a balance sheet for
  // any month after the first used to be out of balance by the accumulated prior
  // net income. Equity as-of the close date has to carry it.
  const s = svc();
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-07-10", memo: "July sale",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100000" },
      { code: "4000", side: "CREDIT", amount_minor: "100000" },
    ],
  });
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-10", memo: "August sale",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "50000" },
      { code: "4000", side: "CREDIT", amount_minor: "50000" },
    ],
  });

  // August-only statements: the income statement is the month; the balance sheet
  // is as-of Aug 31 and must still balance.
  const st = obj(await call(s, "GET", "/t/acme/statements", "", { from: "2026-08-01", to: "2026-08-31" }));
  const income = st["income_statement"] as Record<string, unknown>;
  assert.equal(income["net_income"], 50000, "the income statement is the period, not cumulative");
  const bs = st["balance_sheet"] as Record<string, unknown>;
  assert.equal(bs["balanced"], true, "the sheet balances despite July's earnings");
  assert.equal(bs["total_assets"], 150000, "cash from both months");
  assert.equal(bs["total_equity"], 150000, "equity carries July's retained earnings plus August's income");
});

test("an account register shows the running balance", async () => {
  const s = svc();
  await seededBooks(s);
  const reg = obj(await call(s, "GET", "/t/acme/accounts/1000/register"));
  assert.equal(reg["code"], "1000");
  const rows = reg["rows"] as Array<Record<string, unknown>>;
  assert.equal(rows.length, 2);
  assert.equal(rows[0]!["balance_minor"], "500000");
  assert.equal(rows[1]!["balance_minor"], "300000");
  assert.equal(reg["closing_minor"], "300000");
  assert.equal((await call(s, "GET", "/t/acme/accounts/9999/register")).status, 404);
});

test("MULTI-TENANT: two clients' books are fully isolated", async () => {
  const s = svc();
  await seededBooks(s, "acme");

  // beta is a different business, a different chart, different numbers
  await call(s, "POST", "/t/beta/accounts/seed", { category: "RESTAURANT" });
  await call(s, "POST", "/t/beta/entries", {
    date: "2026-08-07",
    memo: "Food sales",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "125000" },
      { code: "4100", side: "CREDIT", amount_minor: "125000" },
    ],
  });

  const acmeTb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  const betaTb = obj(await call(s, "GET", "/t/beta/trial-balance"));
  const cashOf = (tb: Record<string, unknown>): unknown =>
    (tb["rows"] as Array<Record<string, unknown>>).find((r) => r["code"] === "1000")!["debit_minor"];
  assert.equal(cashOf(acmeTb), "300000");
  assert.equal(cashOf(betaTb), "125000");
  assert.equal(acmeTb["in_balance"], true);
  assert.equal(betaTb["in_balance"], true);

  // acme's chart has no restaurant accounts; beta's has no generic 6300 rent posting
  const acmeAccounts = (obj(await call(s, "GET", "/t/acme/accounts"))["accounts"] as Array<Record<string, unknown>>);
  const betaAccounts = (obj(await call(s, "GET", "/t/beta/accounts"))["accounts"] as Array<Record<string, unknown>>);
  assert.equal(acmeAccounts.some((a) => a["name"] === "Tips Payable"), false);
  assert.ok(betaAccounts.some((a) => a["name"] === "Tips Payable"));

  // entries never leak across tenants
  const acmeEntries = obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Array<Record<string, unknown>>;
  const betaEntries = obj(await call(s, "GET", "/t/beta/entries"))["entries"] as Array<Record<string, unknown>>;
  assert.equal(acmeEntries.length, 2);
  assert.equal(betaEntries.length, 1);
  assert.equal(betaEntries[0]!["memo"], "Food sales");

  // and locking acme's period does not lock beta's
  await call(s, "POST", "/t/acme/periods/2026-08/lock", {});
  assert.equal((await call(s, "POST", "/t/beta/entries", {
    date: "2026-08-15",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "4100", side: "CREDIT", amount_minor: "100" },
    ],
  })).status, 201);
});

test("go-live over the API opens a client's books and locks the cutover", async () => {
  const s = svc();
  const r = await call(s, "POST", "/t/newco/go-live", {
    contract: "go-live/1",
    tenant_id: "newco",
    source_system: "quickbooks",
    cutover_date: "2026-08-31",
    currency: "USD",
    coa_category: "PROFESSIONAL_SERVICES",
    opening_balance_equity_code: "3010",
    source_accounts: [
      { code: "1000", name: "Checking", balance_minor: "2500000", subtype: "BANK" },
      { code: "1200", name: "A/R", balance_minor: "800000", subtype: "ACCOUNTS_RECEIVABLE" },
      { code: "2000", name: "A/P", balance_minor: "-300000", subtype: "ACCOUNTS_PAYABLE" },
      { code: "3900", name: "Retained Earnings", balance_minor: "-3000000", subtype: "RETAINED_EARNINGS" },
    ],
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const out = obj(r);
  assert.equal(out["locked_period"], "2026-08");
  assert.equal(out["opening_total_minor"], "3300000");

  // the chart persisted, and the opening books balance
  const accounts = obj(await call(s, "GET", "/t/newco/accounts"))["accounts"] as unknown[];
  assert.ok(accounts.length >= 25);
  const tb = obj(await call(s, "GET", "/t/newco/trial-balance"));
  assert.equal(tb["in_balance"], true);
  // and the cutover period is frozen
  assert.equal((await call(s, "POST", "/t/newco/entries", {
    date: "2026-08-15",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "3900", side: "CREDIT", amount_minor: "100" },
    ],
  })).status, 409);
});
