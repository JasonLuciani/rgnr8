import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Importing a bank statement and ticking off what it matches.
 *
 * The machine does the obvious part. What it deliberately does not do is
 * finish: matching on an equal amount within a few days is a good heuristic and
 * a bad authority, and the value of a reconciliation is that a person looked.
 *
 * The two lists it returns are the point. A statement line with no book entry is
 * a transaction the business doesn't know happened — the fraud case and the
 * forgotten-subscription case. A book line the statement doesn't show is either
 * an outstanding cheque, which is normal, or a duplicate, which is not.
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

/** Money in (+) or out (−) of the bank, as a posted entry. */
async function cash(
  s: LedgerService, key: string, date: string, minor: string, memo: string,
): Promise<string> {
  const into = !minor.startsWith("-");
  const abs = into ? minor : minor.slice(1);
  const r = await call(s, "POST", "/t/acme/entries", {
    idempotency_key: key, date, memo,
    lines: into
      ? [
          { code: "1000", side: "DEBIT", amount_minor: abs },
          { code: "4100", side: "CREDIT", amount_minor: abs },
        ]
      : [
          { code: "6400", side: "DEBIT", amount_minor: abs },
          { code: "1000", side: "CREDIT", amount_minor: abs },
        ],
  });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  return String((obj(r)["entry"] as Row)["id"]);
}

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await cash(s, "d1", "2026-08-03", "500000", "Client deposit");
  await cash(s, "p1", "2026-08-05", "-20000", "Software");
  await cash(s, "p2", "2026-08-30", "-5000", "Cheque not cashed");
  return s;
}

/** The two matching lines, as a bank would export them. */
const CSV = [
  "Date,Description,Amount",
  "2026-08-03,ACH CREDIT CLIENT,5000.00",
  "2026-08-06,ADOBE SUBSCRIPTION,-200.00",
].join("\n");

const OFX = `OFXHEADER:100
<OFX><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD
<BANKTRANLIST>
<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260803<TRNAMT>5000.00<FITID>A1<NAME>ACH CREDIT CLIENT</STMTTRN>
<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260806<TRNAMT>-200.00<FITID>A2<NAME>ADOBE</STMTTRN>
</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>`;

const importIt = (
  s: LedgerService, text: string, balance = "480000",
): Promise<ServiceResponse> =>
  call(s, "POST", "/t/acme/accounts/1000/reconcile/import", {
    statement_text: text,
    statement_date: "2026-08-31",
    statement_balance_minor: balance,
  });

// --- matching ----------------------------------------------------------------

test("a CSV statement ticks off everything it matches", async () => {
  const s = await ready();
  const r = await importIt(s, CSV);
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(obj(r)["parsed"], 2);
  assert.equal(obj(r)["matched"], 2);
  assert.equal(obj(r)["newly_cleared"], 2);
  // 4,800 statement balance vs 5,000 − 200 cleared: it ties out
  assert.equal(obj(r)["difference_minor"], "0");
  assert.equal(obj(r)["can_finish"], true);
});

test("an OFX statement reaches the same answer", async () => {
  const s = await ready();
  const r = await importIt(s, OFX);
  assert.equal(obj(r)["parsed"], 2);
  assert.equal(obj(r)["newly_cleared"], 2);
  assert.equal(obj(r)["difference_minor"], "0");
});

test("a match a few days apart is still a match", async () => {
  // the software payment posted on the 5th and cleared the bank on the 6th
  const s = await ready();
  const view = obj(await importIt(s, CSV))["view"] as Row;
  const lines = view["lines"] as Row[];
  const software = lines.find((l) => l["memo"] === "Software")!;
  assert.equal(software["status"], "CLEARED");
});

test("the import CLEARS but never RECONCILES — a person still presses finish", async () => {
  const s = await ready();
  const view = obj(await importIt(s, CSV))["view"] as Row;
  const lines = view["lines"] as Row[];
  assert.equal(lines.filter((l) => l["status"] === "RECONCILED").length, 0);
  assert.equal(lines.filter((l) => l["status"] === "CLEARED").length, 2);
  // and finishing afterwards works, locking them in
  const fin = await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-08-31", statement_balance_minor: "480000",
  });
  assert.equal(fin.status, 200);
  assert.equal(obj(fin)["reconciled_entries"], 2);
});

// --- the two lists that matter ----------------------------------------------

test("a statement line with no book entry is reported, not quietly ignored", async () => {
  const s = await ready();
  const csv = CSV + "\n2026-08-20,MYSTERY DEBIT,-1250.00";
  const r = await importIt(s, csv, "355000");
  const missing = obj(r)["missing_from_books"] as Row[];
  assert.equal(missing.length, 1);
  assert.equal(missing[0]!["description"], "MYSTERY DEBIT");
  assert.equal(missing[0]!["amount_minor"], "-125000");
  // and it does not tie out, because the books really are wrong
  assert.equal(obj(r)["can_finish"], false);
});

test("a book line the statement doesn't show is reported as outstanding", async () => {
  const s = await ready();
  const r = await importIt(s, CSV);
  const outstanding = obj(r)["not_on_statement"] as Row[];
  assert.equal(outstanding.length, 1);
  assert.equal(outstanding[0]!["memo"], "Cheque not cashed");
  assert.equal(outstanding[0]!["amount_minor"], "-5000");
});

test("importing twice does not double-tick or double-count", async () => {
  const s = await ready();
  await importIt(s, CSV);
  const again = await importIt(s, CSV);
  assert.equal(obj(again)["newly_cleared"], 0, "already cleared, nothing new");
  assert.equal(obj(again)["difference_minor"], "0");
  const view = obj(again)["view"] as Row;
  assert.equal((view["lines"] as Row[]).filter((l) => l["status"] === "CLEARED").length, 2);
});

// --- refusals ----------------------------------------------------------------

test("an empty or unreadable file is refused with a reason a person can act on", async () => {
  const s = await ready();
  assert.equal((await importIt(s, "")).status, 400);

  const nonsense = await importIt(s, "this is my bank statement, roughly");
  assert.equal(nonsense.status, 400);
  assert.match(String(obj(nonsense)["error"]), /no transactions were found/);

  const summaryOnly = await importIt(s, "Date,Description,Amount");
  assert.equal(summaryOnly.status, 400);
});

test("a missing date, balance or account is refused", async () => {
  const s = await ready();
  assert.equal((await call(s, "POST", "/t/acme/accounts/1000/reconcile/import", {
    statement_text: CSV, statement_date: "August", statement_balance_minor: "1",
  })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/accounts/1000/reconcile/import", {
    statement_text: CSV, statement_date: "2026-08-31",
  })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/accounts/9999/reconcile/import", {
    statement_text: CSV, statement_date: "2026-08-31", statement_balance_minor: "1",
  })).status, 400);
});

test("an already-reconciled line is never re-ticked by an import", async () => {
  const s = await ready();
  await importIt(s, CSV);
  await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", {
    statement_date: "2026-08-31", statement_balance_minor: "480000",
  });
  const again = await importIt(s, CSV, "480000");
  assert.equal(obj(again)["newly_cleared"], 0);
  const view = obj(again)["view"] as Row;
  assert.equal((view["lines"] as Row[]).filter((l) => l["status"] === "RECONCILED").length, 2);
});

test("one tenant cannot import into another's account", async () => {
  const s = await ready();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  const r = await call(s, "POST", "/t/beta/accounts/1000/reconcile/import", {
    statement_text: CSV, statement_date: "2026-08-31", statement_balance_minor: "480000",
  });
  // beta has the account but none of acme's entries, so nothing matches
  assert.equal(obj(r)["newly_cleared"], 0);
  assert.equal((obj(r)["missing_from_books"] as Row[]).length, 2);
});
