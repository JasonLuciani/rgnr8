import { test } from "node:test";
import assert from "node:assert/strict";
import { newDb } from "pg-mem";
import { InMemoryBackend, LedgerService, PostgresBackend, type ServiceResponse } from "../src/index.js";

/**
 * Editing the chart of accounts. Every rule here is enforced by the ledger, so a
 * client that skips the web app still gets the same refusal. See chartEdit.ts.
 */

const NOW = "2026-10-30T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string, body: unknown = "",
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query: {},
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
const error = (r: ServiceResponse): string => String(obj(r)["error"] ?? "");

async function service(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  // PROFESSIONAL_SERVICES: 1000 bank, 1200 AR (system), 4000 services income, 6000 advertising.
  assert.equal((await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" })).status, 201);
  return s;
}

/** Post `amount` (minor units) Dr to `code` against 4000 Services Income. */
async function postTo(s: LedgerService, code: string, amount: string): Promise<ServiceResponse> {
  return call(s, "POST", "/t/acme/entries", {
    date: "2026-10-05",
    memo: `Test posting to ${code}`,
    lines: [
      { code, side: "DEBIT", amount_minor: amount },
      { code: "4000", side: "CREDIT", amount_minor: amount },
    ],
  });
}

const patch = (s: LedgerService, code: string, body: unknown): Promise<ServiceResponse> =>
  call(s, "PATCH", `/t/acme/accounts/${code}`, body);

async function chartRows(s: LedgerService): Promise<Array<Record<string, unknown>>> {
  const r = await call(s, "GET", "/t/acme/accounts");
  return obj(r)["accounts"] as Array<Record<string, unknown>>;
}

test("editing a name changes it on the chart and keeps everything else", async () => {
  const s = await service();
  const r = await patch(s, "6000", { name: "Marketing" });
  assert.equal(r.status, 200);
  assert.equal((obj(r)["account"] as Record<string, unknown>)["name"], "Marketing");
  const row = (await chartRows(s)).find((a) => a["code"] === "6000")!;
  assert.equal(row["name"], "Marketing");
  assert.equal(row["subtype"], "EXPENSE");
  assert.equal(row["active"], true);
});

test("an edit that changes nothing is a no-op", async () => {
  const s = await service();
  const r = await patch(s, "6000", { name: "Advertising & Marketing", active: true });
  assert.equal(r.status, 200);
});

test("the account number can change, and then only the new number resolves", async () => {
  const s = await service();
  assert.equal((await patch(s, "6000", { number: "6005" })).status, 200);
  assert.equal((await postTo(s, "6000", "1000")).status, 400); // no such code any more
  assert.equal((await postTo(s, "6005", "1000")).status, 201);
});

test("a number already in use is refused", async () => {
  const s = await service();
  const r = await patch(s, "6000", { number: "4000" });
  assert.equal(r.status, 409);
  assert.match(error(r), /already exists/);
});

test("the type cannot change once the account has posted entries", async () => {
  const s = await service();
  // Before any posting the change is allowed.
  const before = await patch(s, "6000", { type: "ASSET", subtype: "OTHER_ASSET" });
  assert.equal(before.status, 200);
  assert.equal((await patch(s, "6000", { type: "EXPENSE", subtype: "EXPENSE" })).status, 200);

  assert.equal((await postTo(s, "6000", "5000")).status, 201);
  const after = await patch(s, "6000", { type: "ASSET", subtype: "OTHER_ASSET" });
  assert.equal(after.status, 409);
  assert.match(error(after), /posted entries, so its type cannot change/);
});

test("the detail type can change after postings within its type, but not across types", async () => {
  const s = await service();
  assert.equal((await postTo(s, "6000", "5000")).status, 201);
  const sameType = await patch(s, "6000", { subtype: "COST_OF_GOODS_SOLD" });
  assert.equal(sameType.status, 200);
  // Moving to another type's detail type is a retype, and posted accounts can't be retyped.
  const otherType = await patch(s, "6000", { subtype: "BANK" });
  assert.equal(otherType.status, 409);
  assert.match(error(otherType), /posted entries, so its type cannot change/);
});

test("a system account can be renamed but not made inactive or retyped", async () => {
  const s = await service();
  assert.equal((await patch(s, "1200", { name: "Trade Debtors" })).status, 200);
  const off = await patch(s, "1200", { active: false });
  assert.equal(off.status, 409);
  assert.match(error(off), /system account and cannot be made inactive/);
  const retype = await patch(s, "1200", { subtype: "OTHER_CURRENT_ASSET" });
  assert.equal(retype.status, 409);
  assert.match(error(retype), /system account/);
  const row = (await chartRows(s)).find((a) => a["code"] === "1200")!;
  assert.equal(row["system"], true);
  assert.equal(row["locked_by"], "system");
  assert.equal(row["active"], true);
});

test("an account with a balance cannot be made inactive, and the message says what it holds", async () => {
  const s = await service();
  const posted = await postTo(s, "6000", "150000");
  assert.equal(posted.status, 201);
  const off = await patch(s, "6000", { active: false });
  assert.equal(off.status, 409);
  assert.match(error(off), /balance of 1500\.00 USD \(debit\)/);

  // Reversing the entry brings the balance to zero, and then it can go.
  const entryId = obj(posted)["entry"] as Record<string, unknown>;
  const reversed = await call(s, "POST", `/t/acme/entries/${entryId["id"]}/reverse`, {});
  assert.equal(reversed.status, 201);
  assert.equal((await patch(s, "6000", { active: false })).status, 200);
});

test("an inactive account takes no postings, and posts again once reactivated", async () => {
  const s = await service();
  assert.equal((await patch(s, "6000", { active: false })).status, 200);
  const refused = await postTo(s, "6000", "5000");
  assert.equal(refused.status, 409);
  assert.match(error(refused), /6000 is inactive and cannot receive postings/);
  assert.equal((await patch(s, "6000", { active: true })).status, 200);
  assert.equal((await postTo(s, "6000", "5000")).status, 201);
});

test("reversing an entry onto an account that is now inactive is refused", async () => {
  const s = await service();
  const posted = await postTo(s, "6000", "7000");
  const entryId = (obj(posted)["entry"] as Record<string, unknown>)["id"];
  // Clear the balance with a second entry, so the account can be made inactive.
  const cleared = await call(s, "POST", `/t/acme/entries/${entryId}/reverse`, {});
  assert.equal(cleared.status, 201);
  assert.equal((await patch(s, "6000", { active: false })).status, 200);
  // The reversal of the reversal would put a line back on 6000.
  const again = await call(s, "POST", `/t/acme/entries/${(obj(cleared)["entry"] as Record<string, unknown>)["id"]}/reverse`, {});
  assert.equal(again.status, 409);
});

test("a parent must exist, be active and have the same type", async () => {
  const s = await service();
  const wrongType = await call(s, "POST", "/t/acme/accounts", {
    code: "7950", name: "Consulting", subtype: "INCOME", parent_code: "6000",
  });
  assert.equal(wrongType.status, 400);
  assert.match(error(wrongType), /must have the same type as its parent/);

  const missing = await call(s, "POST", "/t/acme/accounts", {
    code: "7200", name: "Travel", subtype: "EXPENSE", parent_code: "9999",
  });
  assert.equal(missing.status, 400);
  assert.match(error(missing), /does not exist/);

  const sameType = await call(s, "POST", "/t/acme/accounts", {
    code: "7100", name: "Social Media", subtype: "EXPENSE", parent_code: "6000",
  });
  assert.equal(sameType.status, 201);
  assert.equal((obj(sameType)["account"] as Record<string, unknown>)["parent_code"], "6000");

  // An edit that moves an account under a parent of another type is refused too.
  const moved = await patch(s, "7100", { parent_code: "4000" });
  assert.equal(moved.status, 400);

  // An inactive parent takes no new sub-accounts.
  assert.equal((await patch(s, "6000", { active: false })).status, 200);
  const inactive = await call(s, "POST", "/t/acme/accounts", {
    code: "7300", name: "Printing", subtype: "EXPENSE", parent_code: "6000",
  });
  assert.equal(inactive.status, 400);
  assert.match(error(inactive), /parent account 6000 is inactive/);
});

test("a cycle in the hierarchy is refused, including an account as its own parent", async () => {
  const s = await service();
  assert.equal((await call(s, "POST", "/t/acme/accounts", {
    code: "7400", name: "Parent Expense", subtype: "EXPENSE",
  })).status, 201);
  assert.equal((await call(s, "POST", "/t/acme/accounts", {
    code: "7410", name: "Child Expense", subtype: "EXPENSE", parent_code: "7400",
  })).status, 201);

  const cycle = await patch(s, "7400", { parent_code: "7410" });
  assert.equal(cycle.status, 400);
  assert.match(error(cycle), /cycle/);

  const self = await patch(s, "7400", { parent_code: "7400" });
  assert.equal(self.status, 400);
  assert.match(error(self), /own parent/);
});

test("the chart nests at most five levels", async () => {
  const s = await service();
  const levels = ["7500", "7501", "7502", "7503", "7504"];
  for (let i = 0; i < levels.length; i++) {
    const r = await call(s, "POST", "/t/acme/accounts", {
      code: levels[i], name: `Level ${i + 1}`, subtype: "EXPENSE",
      ...(i > 0 ? { parent_code: levels[i - 1] } : {}),
    });
    assert.equal(r.status, 201, `level ${i + 1}`);
  }
  const sixth = await call(s, "POST", "/t/acme/accounts", {
    code: "7505", name: "Level 6", subtype: "EXPENSE", parent_code: "7504",
  });
  assert.equal(sixth.status, 400);
  assert.match(error(sixth), /at most 5 levels/);

  // Moving a two-level branch under the fifth level would push its leaves to six.
  assert.equal((await call(s, "POST", "/t/acme/accounts", {
    code: "7600", name: "Branch", subtype: "EXPENSE",
  })).status, 201);
  assert.equal((await call(s, "POST", "/t/acme/accounts", {
    code: "7601", name: "Branch leaf", subtype: "EXPENSE", parent_code: "7600",
  })).status, 201);
  const deep = await patch(s, "7600", { parent_code: "7504" });
  assert.equal(deep.status, 400);
});

test("create takes a detail type, a parent and a number, and refuses a duplicate number", async () => {
  const s = await service();
  const r = await call(s, "POST", "/t/acme/accounts", {
    code: "7700", name: "Software", subtype: "EXPENSE", parent_code: "6000",
  });
  assert.equal(r.status, 201);
  const account = obj(r)["account"] as Record<string, unknown>;
  assert.equal(account["code"], "7700");
  assert.equal(account["subtype"], "EXPENSE");
  assert.equal(account["parent_code"], "6000");
  assert.equal(account["origin"], "rgnr8");
  const dup = await call(s, "POST", "/t/acme/accounts", { code: "7700", name: "Again", subtype: "EXPENSE" });
  assert.equal(dup.status, 409);
});

test("an account created as a system detail type is marked system", async () => {
  const s = await service();
  const r = await call(s, "POST", "/t/acme/accounts", {
    code: "1290", name: "Second Receivables", subtype: "ACCOUNTS_RECEIVABLE",
  });
  assert.equal(r.status, 201);
  assert.equal((obj(r)["account"] as Record<string, unknown>)["system"], true);
});

test("a QuickBooks account is read-only while QuickBooks is the book of record, and editable after cutover", async () => {
  const s = await service();
  const goLive = await call(s, "POST", "/t/acme/go-live", {
    contract: "go-live/1", tenant_id: "acme", source_system: "quickbooks",
    cutover_date: "2026-09-30", currency: "USD", coa_category: "PROFESSIONAL_SERVICES",
    opening_balance_equity_code: "3010",
    source_accounts: [
      { code: "1000", name: "Checking", balance_minor: "2500000", subtype: "BANK" },
      { code: "3900", name: "Retained Earnings", balance_minor: "-2500000", subtype: "RETAINED_EARNINGS" },
    ],
  });
  assert.equal(goLive.status, 200, JSON.stringify(goLive.body));

  assert.equal((await call(s, "POST", "/t/acme/settings", {
    book_of_record: "quickbooks", mirror_realm_id: "9341453432535209",
  })).status, 200);
  const locked = await patch(s, "1000", { name: "Operating" });
  assert.equal(locked.status, 409);
  assert.equal(error(locked), "edit this account in QuickBooks");
  const bank = (await chartRows(s)).find((a) => a["code"] === "1000")!;
  assert.equal(bank["origin"], "quickbooks");
  assert.equal(bank["locked_by"], "quickbooks");

  // Sending a QuickBooks account its current values is not a change, so it is not refused.
  assert.equal((await patch(s, "1000", { name: "Business Checking" })).status, 200);

  // Cutover hands the books to RGNR8, and the same account can be edited.
  assert.equal((await call(s, "POST", "/t/acme/settings", { book_of_record: "rgnr8" })).status, 200);
  assert.equal((await patch(s, "1000", { name: "Operating" })).status, 200);
});

test("a Postgres-backed chart keeps its edits, its parents, and its inactive flag across a restart", async () => {
  const pool = new (newDb().adapters.createPg().Pool)();
  const backend = new PostgresBackend(pool as never, { enforceRls: false });
  await backend.migrate();
  const s = new LedgerService(backend, { now: () => NOW });
  assert.equal((await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" })).status, 201);
  assert.equal((await call(s, "POST", "/t/acme/accounts", {
    code: "7800", name: "Software", subtype: "EXPENSE", parent_code: "6000",
  })).status, 201);
  // Renaming the parent rewrites its row, which moves it after its child in storage.
  assert.equal((await patch(s, "6000", { name: "Marketing & Ads" })).status, 200);
  assert.equal((await patch(s, "7800", { name: "Ad Software" })).status, 200);
  assert.equal((await patch(s, "7799", { active: false })).status, 404);

  // A second service over the same database: the books outlive the process.
  const again = new LedgerService(new PostgresBackend(pool as never, { enforceRls: false }), { now: () => NOW });
  const rows = obj(await call(again, "GET", "/t/acme/accounts"))["accounts"] as Array<Record<string, unknown>>;
  const child = rows.find((a) => a["code"] === "7800")!;
  assert.equal(child["name"], "Ad Software");
  assert.equal(child["parent_code"], "6000");
  assert.equal(rows.find((a) => a["code"] === "6000")!["name"], "Marketing & Ads");

  assert.equal((await patch(s, "7800", { active: false })).status, 200);
  const reread = obj(await call(new LedgerService(new PostgresBackend(pool as never, { enforceRls: false }), { now: () => NOW }),
    "GET", "/t/acme/accounts"))["accounts"] as Array<Record<string, unknown>>;
  assert.equal(reread.find((a) => a["code"] === "7800")!["active"], false);
  const refused = await call(again, "POST", "/t/acme/entries", {
    date: "2026-10-05", memo: "to an inactive account",
    lines: [
      { code: "7800", side: "DEBIT", amount_minor: "5000" },
      { code: "4000", side: "CREDIT", amount_minor: "5000" },
    ],
  });
  assert.equal(refused.status, 409);
});

test("clearing a parent makes the account top-level again", async () => {
  const s = await service();
  assert.equal((await call(s, "POST", "/t/acme/accounts", {
    code: "7100", name: "Social Media", subtype: "EXPENSE", parent_code: "6000",
  })).status, 201);
  const cleared = await patch(s, "7100", { parent_code: null });
  assert.equal(cleared.status, 200);
  assert.equal((obj(cleared)["account"] as Record<string, unknown>)["parent_code"], null);
  assert.equal((await chartRows(s)).find((a) => a["code"] === "7100")!["parent_code"], null);
});
