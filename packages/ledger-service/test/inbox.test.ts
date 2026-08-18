import { test } from "node:test";
import assert from "node:assert/strict";
import {
  InMemoryBackend, LedgerService, normalizeDescription, type ServiceResponse,
} from "../src/index.js";

/**
 * The bank feed review inbox: lines land PENDING, get a suggestion from rules or
 * from what the tenant did before, and only become journal entries when somebody
 * accepts, matches or excludes them.
 *
 * The properties that matter here are the ones that keep the books honest:
 * nothing posts without a decision, a re-synced window never duplicates, a line
 * can't be actioned twice, undo reverses rather than deletes, and bulk-accept
 * refuses to clear the queue on guesses.
 */

const NOW = "2026-08-20T00:00:00Z";

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { now: () => NOW });
}

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
const items = (r: ServiceResponse): Row[] => obj(r)["items"] as Row[];

async function ready(tenant = "acme"): Promise<LedgerService> {
  const s = svc();
  await call(s, "POST", `/t/${tenant}/accounts/seed`, { category: "PROFESSIONAL_SERVICES" });
  return s;
}

const deliver = (
  s: LedgerService, txns: unknown[], tenant = "acme", code = "1000",
): Promise<ServiceResponse> =>
  call(s, "POST", `/t/${tenant}/feed/${code}`, { source: "plaid-like", transactions: txns });

const queue = (s: LedgerService, tenant = "acme", query: Record<string, string> = {}) =>
  call(s, "GET", `/t/${tenant}/feed`, "", query);

const act = (
  s: LedgerService, id: string, action: string, body: unknown = {}, tenant = "acme",
): Promise<ServiceResponse> =>
  call(s, "POST", `/t/${tenant}/feed/txn/${id}/${action}`, body);

async function balance(s: LedgerService, code: string, tenant = "acme"): Promise<bigint> {
  const tb = obj(await call(s, "GET", `/t/${tenant}/trial-balance`));
  const row = (tb["rows"] as Row[]).find((r) => r["code"] === code);
  if (!row) return 0n;
  return BigInt(String(row["debit_minor"])) - BigInt(String(row["credit_minor"]));
}

const LINES = [
  { id: "bk-1", date: "2026-08-03", amount_minor: "500000", description: "DEPOSIT HALCYON LLC", counterparty: "Halcyon LLC" },
  { id: "bk-2", date: "2026-08-05", amount_minor: "-24900", description: "SQ *COFFEE 1187", counterparty: "Square" },
  { id: "bk-3", date: "2026-08-09", amount_minor: "-350000", description: "RIVERSIDE PROPERTIES RENT", counterparty: "Riverside Properties" },
];

// --- landing lines -----------------------------------------------------------

test("delivered lines land PENDING and post nothing", async () => {
  const s = await ready();
  const r = await deliver(s, LINES);
  assert.equal(r.status, 201);
  assert.equal(obj(r)["added"], 3);
  assert.equal(obj(r)["auto_posted"], 0);

  // the ledger is untouched — a bank claim is not yet an accounting fact
  const tb = obj(await call(s, "GET", "/t/acme/trial-balance"));
  assert.equal((tb["rows"] as Row[]).length, 0);

  const q = await queue(s);
  assert.equal(obj(q)["pending"], 3);
  assert.equal(obj(q)["pending_in_minor"], "500000");
  assert.equal(obj(q)["pending_out_minor"], "-374900");
  assert.equal(items(q).length, 3);
  assert.equal(items(q)[0]!["date"], "2026-08-03"); // oldest first
});

test("re-syncing an overlapping window adds nothing", async () => {
  const s = await ready();
  await deliver(s, LINES);
  const again = await deliver(s, [
    ...LINES,
    { id: "bk-4", date: "2026-08-12", amount_minor: "-5000", description: "BANK FEE" },
  ]);
  assert.equal(obj(again)["added"], 1);
  assert.equal(obj(again)["duplicates"], 3);
  assert.equal(obj(await queue(s))["pending"], 4);
});

test("a delivery is refused on a bad account, date, amount or empty batch", async () => {
  const s = await ready();
  assert.equal((await deliver(s, LINES, "acme", "9999")).status, 400);
  assert.equal((await deliver(s, [])).status, 400);
  assert.equal((await deliver(s, [{ id: "x", date: "Aug 3", amount_minor: "1" }])).status, 400);
  assert.equal((await deliver(s, [{ id: "x", date: "2026-08-03", amount_minor: "1.50" }])).status, 400);
  assert.equal((await deliver(s, [{ id: "x", date: "2026-08-03", amount_minor: "0" }])).status, 400);
  assert.equal((await deliver(s, [{ id: "", date: "2026-08-03", amount_minor: "1" }])).status, 400);
});

// --- suggestions -------------------------------------------------------------

test("with no rules and no history there is no suggestion, not a guess", async () => {
  const s = await ready();
  await deliver(s, LINES);
  for (const item of items(await queue(s))) {
    const sug = item["suggestion"] as Row;
    assert.equal(sug["account_code"], "");
    assert.equal(sug["confidence"], 0);
    assert.equal(sug["source"], "none");
  }
});

test("a rule suggests with certainty and names itself", async () => {
  const s = await ready();
  await deliver(s, LINES);
  const made = await call(s, "POST", "/t/acme/feed-rules", {
    id: "rent", account_code: "6300", description_contains: "RIVERSIDE", sign: "out",
  });
  assert.equal(made.status, 201);
  assert.equal(obj(made)["would_match"], 1); // the UI can say what it will do

  const rent = items(await queue(s)).find((i) => i["id"] === "bk-3")!;
  const sug = rent["suggestion"] as Row;
  assert.equal(sug["account_code"], "6300");
  assert.equal(sug["confidence"], 1);
  assert.equal(sug["source"], "rule");
  assert.equal(sug["rule_id"], "rent");
  assert.ok(String(sug["account_name"]).length > 0);
});

test("a rule with no conditions is refused — it would categorize everything", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/feed-rules", { id: "all", account_code: "6300" });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /at least one condition/);
  assert.equal((await call(s, "POST", "/t/acme/feed-rules", {
    account_code: "9999", description_contains: "X",
  })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/feed-rules", {
    account_code: "6300", description_contains: "X", sign: "sideways",
  })).status, 400);
});

test("history teaches the suggester, and card noise collapses to one signal", async () => {
  // "SQ *COFFEE 1187" and "SQ *COFFEE 9987" are the same vendor to a human
  assert.equal(normalizeDescription("SQ *COFFEE 1187"), normalizeDescription("SQ *COFFEE 9987"));

  const s = await ready();
  await deliver(s, LINES);
  await act(s, "bk-2", "accept", { category_code: "6400" });

  await deliver(s, [
    { id: "bk-9", date: "2026-08-19", amount_minor: "-31200", description: "SQ *COFFEE 9987", counterparty: "Square" },
  ]);
  const sug = items(await queue(s)).find((i) => i["id"] === "bk-9")!["suggestion"] as Row;
  assert.equal(sug["account_code"], "6400");
  assert.equal(sug["source"], "learned");
  assert.equal(sug["confidence"], 1); // one-for-one so far
  assert.match(String(sug["reason"]), /Square/);
});

test("a rule outranks learned history", async () => {
  const s = await ready();
  await deliver(s, LINES);
  await act(s, "bk-2", "accept", { category_code: "6400" });
  await call(s, "POST", "/t/acme/feed-rules", {
    id: "coffee", account_code: "6500", counterparty_equals: "square",
  });
  await deliver(s, [
    { id: "bk-9", date: "2026-08-19", amount_minor: "-1000", description: "SQ *COFFEE 5", counterparty: "Square" },
  ]);
  const sug = items(await queue(s)).find((i) => i["id"] === "bk-9")!["suggestion"] as Row;
  assert.equal(sug["account_code"], "6500");
  assert.equal(sug["source"], "rule");
});

// --- accepting ---------------------------------------------------------------

test("accepting posts a balanced entry and links it to the bank line", async () => {
  const s = await ready();
  await deliver(s, LINES);
  const r = await act(s, "bk-1", "accept", { category_code: "4100" });
  assert.equal(r.status, 200);
  assert.equal(obj(r)["status"], "POSTED");
  const entryId = String(obj(r)["entry_id"]);
  assert.ok(entryId);

  assert.equal(await balance(s, "1000"), 500000n);   // cash debited
  assert.equal(await balance(s, "4100"), -500000n);  // income credited
  assert.equal(obj(await call(s, "GET", "/t/acme/trial-balance"))["in_balance"], true);

  // and it leaves the queue
  const q = await queue(s);
  assert.equal(obj(q)["pending"], 2);
  assert.equal(obj(q)["posted"], 1);
  // the posted line is still readable, with its entry
  const posted = items(await queue(s, "acme", { status: "POSTED" }));
  assert.equal(posted[0]!["entry_id"], entryId);
  assert.equal(posted[0]!["category_code"], "4100");
});

test("money out debits the expense and credits the bank", async () => {
  const s = await ready();
  await deliver(s, LINES);
  await act(s, "bk-3", "accept", { category_code: "6300" });
  assert.equal(await balance(s, "6300"), 350000n);
  assert.equal(await balance(s, "1000"), -350000n);
});

test("accepting is refused without a category, to the bank account itself, or twice", async () => {
  const s = await ready();
  await deliver(s, LINES);
  assert.equal((await act(s, "bk-1", "accept", {})).status, 400);
  assert.equal((await act(s, "bk-1", "accept", { category_code: "9999" })).status, 400);
  const own = await act(s, "bk-1", "accept", { category_code: "1000" });
  assert.equal(own.status, 400);
  assert.match(String(obj(own)["error"]), /its own bank account/);

  await act(s, "bk-1", "accept", { category_code: "4100" });
  const twice = await act(s, "bk-1", "accept", { category_code: "4100" });
  assert.equal(twice.status, 400);
  assert.match(String(obj(twice)["error"]), /already actioned/);
  assert.equal((await act(s, "nope", "accept", { category_code: "4100" })).status, 400);
});

test("a line dated into a locked period cannot be accepted", async () => {
  const s = await ready();
  await deliver(s, LINES);
  await call(s, "POST", "/t/acme/periods/2026-08/lock", {});
  const r = await act(s, "bk-1", "accept", { category_code: "4100" });
  assert.equal(r.status, 409);
  // and it stays in the queue rather than vanishing
  assert.equal(obj(await queue(s))["pending"], 3);
});

// --- matching ----------------------------------------------------------------

async function withInvoice(s: LedgerService): Promise<void> {
  await call(s, "POST", "/t/acme/customers", { id: "halcyon", name: "Halcyon LLC", terms_days: 30 });
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-1", party_id: "halcyon", date: "2026-08-01", memo: "August retainer",
    lines: [{ description: "Retainer", unit_amount_minor: "500000", account_code: "4100" }],
  });
}

test("an inflow offers the open invoice it would settle, exact fit first", async () => {
  const s = await ready();
  await withInvoice(s);
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-2", party_id: "halcyon", date: "2026-08-02", memo: "Other work",
    lines: [{ description: "Other", unit_amount_minor: "900000", account_code: "4100" }],
  });
  await deliver(s, LINES);

  const matches = items(await queue(s)).find((i) => i["id"] === "bk-1")!["matches"] as Row[];
  assert.ok(matches.length >= 1);
  assert.equal(matches[0]!["id"], "INV-1");
  assert.equal(matches[0]!["fit"], "exact");
  assert.equal(matches[0]!["kind"], "invoice");
  // the 9,000 invoice is a partial fit, offered after the exact one
  assert.equal(matches[1]!["id"], "INV-2");
  assert.equal(matches[1]!["fit"], "partial");

  // an outflow is never offered an invoice
  const outMatches = items(await queue(s)).find((i) => i["id"] === "bk-3")!["matches"] as Row[];
  assert.equal(outMatches.filter((m) => m["kind"] === "invoice").length, 0);
});

test("matching settles the invoice instead of booking revenue twice", async () => {
  const s = await ready();
  await withInvoice(s);
  await deliver(s, LINES);
  const revenueBefore = await balance(s, "4100");
  assert.equal(revenueBefore, -500000n); // the invoice already booked it

  const r = await act(s, "bk-1", "match", { doc_kind: "invoice", doc_id: "INV-1" });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  assert.equal(obj(r)["status"], "MATCHED");

  assert.equal(await balance(s, "4100"), revenueBefore, "revenue is NOT booked a second time");
  assert.equal(await balance(s, "1000"), 500000n);   // cash arrived
  assert.equal(await balance(s, "1200"), 0n);        // AR cleared
  const inv = obj(await call(s, "GET", "/t/acme/invoices/INV-1"));
  assert.equal((inv["document"] as Row)["status"], "PAID");
});

test("a match is refused on the wrong side, an unknown document, or an overpayment", async () => {
  const s = await ready();
  await withInvoice(s);
  await deliver(s, LINES);
  const wrongSide = await act(s, "bk-3", "match", { doc_kind: "invoice", doc_id: "INV-1" });
  assert.equal(wrongSide.status, 400);
  assert.match(String(obj(wrongSide)["error"]), /money out can only settle a bill/);
  assert.equal((await act(s, "bk-1", "match", { doc_kind: "invoice", doc_id: "NOPE" })).status, 400);
  assert.equal((await act(s, "bk-1", "match", { doc_kind: "receipt", doc_id: "INV-1" })).status, 400);
});

test("the opposite leg of a transfer between two accounts is offered as a match", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/accounts", { code: "1010", name: "Savings", subtype: "CASH" });
  await deliver(s, [
    { id: "tf-out", date: "2026-08-14", amount_minor: "-200000", description: "TRANSFER TO SAVINGS" },
  ]);
  await deliver(
    s, [{ id: "tf-in", date: "2026-08-14", amount_minor: "200000", description: "TRANSFER FROM CHECKING" }],
    "acme", "1010",
  );
  const out = items(await queue(s)).find((i) => i["id"] === "tf-out")!["matches"] as Row[];
  const transfer = out.find((m) => m["kind"] === "transfer");
  assert.ok(transfer, "the other leg should be offered");
  assert.equal(transfer!["id"], "tf-in");
});

// --- excluding and undoing ---------------------------------------------------

test("excluding records the decision and posts nothing", async () => {
  const s = await ready();
  await deliver(s, LINES);
  const r = await act(s, "bk-2", "exclude", { reason: "Personal card, not the business" });
  assert.equal(obj(r)["status"], "EXCLUDED");
  assert.equal(obj(r)["entry_id"], "");
  assert.equal((obj(await call(s, "GET", "/t/acme/trial-balance"))["rows"] as Row[]).length, 0);
  const excluded = items(await queue(s, "acme", { status: "EXCLUDED" }));
  assert.equal(excluded[0]!["note"], "Personal card, not the business");
});

test("undo reverses the entry rather than deleting it, and requeues the line", async () => {
  const s = await ready();
  await deliver(s, LINES);
  const posted = await act(s, "bk-1", "accept", { category_code: "4100" });
  const entryId = String(obj(posted)["entry_id"]);

  const undone = await act(s, "bk-1", "undo");
  assert.equal(undone.status, 200);
  assert.equal(obj(undone)["status"], "PENDING");
  assert.equal(await balance(s, "1000"), 0n, "the cash effect is backed out");

  // BOTH entries are still in the journal — the original and its reversal
  const entries = obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Row[];
  assert.equal(entries.length, 2);
  assert.ok(entries.some((e) => e["id"] === entryId), "the original is never deleted");
  assert.ok(entries.some((e) => e["status"] === "REVERSAL"));

  // and it can be re-categorized to the right account
  const again = await act(s, "bk-1", "accept", { category_code: "4900" });
  assert.equal(again.status, 200);
  assert.equal(await balance(s, "4900"), -500000n);
});

test("undoing a match gives the invoice its balance back", async () => {
  const s = await ready();
  await withInvoice(s);
  await deliver(s, LINES);
  await act(s, "bk-1", "match", { doc_kind: "invoice", doc_id: "INV-1" });
  await act(s, "bk-1", "undo");

  const inv = (obj(await call(s, "GET", "/t/acme/invoices/INV-1"))["document"]) as Row;
  assert.equal(inv["open_minor"], "500000");
  assert.equal(inv["status"], "OPEN");
  assert.equal(await balance(s, "1000"), 0n);
  assert.equal(obj(await queue(s))["pending"], 3);
});

test("undoing a line that was never actioned is refused", async () => {
  const s = await ready();
  await deliver(s, LINES);
  assert.equal((await act(s, "bk-1", "undo")).status, 400);
});

// --- bulk accept and auto-post ----------------------------------------------

test("bulk accept only clears lines that meet the confidence bar", async () => {
  const s = await ready();
  await deliver(s, LINES);
  await call(s, "POST", "/t/acme/feed-rules", {
    id: "rent", account_code: "6300", description_contains: "RIVERSIDE",
  });
  const r = await call(s, "POST", "/t/acme/feed/bulk-accept", { min_confidence: 1 });
  assert.equal(r.status, 200);
  assert.equal(obj(r)["accepted"], 1);   // only the rule-certain one
  assert.equal(obj(r)["skipped"], 2);    // the two with no suggestion are left alone
  assert.equal(obj(await queue(s))["pending"], 2);
  assert.equal(await balance(s, "6300"), 350000n);
});

test("bulk accept refuses a zero or nonsensical confidence floor", async () => {
  const s = await ready();
  await deliver(s, LINES);
  assert.equal((await call(s, "POST", "/t/acme/feed/bulk-accept", { min_confidence: 0 })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/feed/bulk-accept", { min_confidence: 2 })).status, 400);
  assert.equal((await call(s, "POST", "/t/acme/feed/bulk-accept", {})).status, 400);
});

test("an auto-post rule skips the queue; everything else still waits", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/feed-rules", {
    id: "rent", account_code: "6300", description_contains: "RIVERSIDE", auto_post: true,
  });
  const r = await deliver(s, LINES);
  assert.equal(obj(r)["auto_posted"], 1);
  assert.equal(obj(r)["pending"], 2);
  assert.equal(await balance(s, "6300"), 350000n);
});

test("rules can be listed and deleted", async () => {
  const s = await ready();
  await call(s, "POST", "/t/acme/feed-rules", {
    id: "rent", account_code: "6300", description_contains: "RIVERSIDE",
  });
  let listed = obj(await call(s, "GET", "/t/acme/feed-rules"))["rules"] as Row[];
  assert.equal(listed.length, 1);
  assert.equal(listed[0]!["account_code"], "6300");
  await call(s, "DELETE", "/t/acme/feed-rules/rent");
  listed = obj(await call(s, "GET", "/t/acme/feed-rules"))["rules"] as Row[];
  assert.equal(listed.length, 0);
});

// --- isolation ---------------------------------------------------------------

test("one tenant's feed, rules and history are invisible to another", async () => {
  const s = await ready();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await deliver(s, LINES);
  await call(s, "POST", "/t/acme/feed-rules", {
    id: "rent", account_code: "6300", description_contains: "RIVERSIDE",
  });

  assert.equal(obj(await queue(s, "beta"))["pending"], 0);
  assert.equal((obj(await call(s, "GET", "/t/beta/feed-rules"))["rules"] as Row[]).length, 0);
  // beta cannot action acme's line
  assert.equal((await act(s, "bk-1", "accept", { category_code: "4100" }, "beta")).status, 400);
  // and the same feed id can exist independently in both books
  const mine = await deliver(s, [LINES[0]!], "beta");
  assert.equal(obj(mine)["added"], 1);
});
