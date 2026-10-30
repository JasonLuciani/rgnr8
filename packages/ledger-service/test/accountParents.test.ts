import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { AccountSubtype, AccountType, asAccountId, asTenantId, type Account } from "@rgnr8/ledger-kernel";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

// The chart and trial balance carry each account's parent code and detail
// subtype: the web app nests sub-accounts on the one and sorts on the other.

const NOW = "2026-08-31T00:00:00Z";
const TOKEN = "svc-secret";

function tenantTok(path: string): string {
  const m = /^\/t\/([^/]+)/.exec(path);
  return m ? createHmac("sha256", TOKEN).update(m[1]!).digest("hex") : TOKEN;
}

function call(s: LedgerService, method: string, path: string, body: unknown = ""): Promise<ServiceResponse> {
  return s.handle({
    method,
    path,
    query: {},
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: { authorization: `Bearer ${tenantTok(path)}` },
  });
}

const rowsOf = (r: ServiceResponse): Array<Record<string, unknown>> =>
  ((r.body as Record<string, unknown>)["accounts"] ?? (r.body as Record<string, unknown>)["rows"]) as Array<
    Record<string, unknown>
  >;

test("sub-accounts carry their parent's code, and the detail subtype is kept", async () => {
  const backend = new InMemoryBackend();
  const s = new LedgerService(backend, { authToken: TOKEN, now: () => NOW });
  // A parent bank account and a sub-account under it, written straight to the backend
  // (the create endpoint does not take a parent).
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  const parent: Account = {
    id: asAccountId("acct:parent"), code: "9001", name: "Operating Bank", type: AccountType.ASSET,
    subtype: AccountSubtype.BANK, currency: (await backend.chart(asTenantId("acme"))).list()[0]!.currency, active: true,
  };
  const child: Account = {
    id: asAccountId("acct:child"), code: "9002", name: "Payroll Bank", type: AccountType.ASSET,
    subtype: AccountSubtype.BANK, parentId: parent.id, currency: parent.currency, active: true,
  };
  await backend.saveAccount(asTenantId("acme"), parent);
  await backend.saveAccount(asTenantId("acme"), child);

  const chart = rowsOf(await call(s, "GET", "/t/acme/accounts"));
  const byCode = new Map(chart.map((a) => [a["code"], a]));
  assert.equal(byCode.get("9002")!["parent_code"], "9001");
  assert.equal(byCode.get("9001")!["parent_code"], null);
  assert.equal(byCode.get("9002")!["subtype"], "BANK");
  assert.equal(byCode.get("9002")!["type"], "ASSET");
});

test("trial balance rows carry subtype and parent code", async () => {
  const backend = new InMemoryBackend();
  const s = new LedgerService(backend, { authToken: TOKEN, now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  const chart = (await backend.chart(asTenantId("acme"))).list();
  const bank = chart.find((a) => a.subtype === AccountSubtype.BANK)!;
  const sub: Account = {
    id: asAccountId("acct:sub"), code: "1099", name: "Sub Bank", type: AccountType.ASSET,
    subtype: AccountSubtype.BANK, parentId: bank.id, currency: bank.currency, active: true,
  };
  await backend.saveAccount(asTenantId("acme"), sub);
  const income = chart.find((a) => a.type === AccountType.REVENUE)!;
  const posted = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05",
    memo: "Sub-account sale",
    lines: [
      { code: "1099", side: "DEBIT", amount_minor: "100000" },
      { code: income.code, side: "CREDIT", amount_minor: "100000" },
    ],
  });
  assert.equal(posted.status, 201);

  const rows = rowsOf(await call(s, "GET", "/t/acme/trial-balance"));
  const row = rows.find((r) => r["code"] === "1099")!;
  assert.equal(row["parent_code"], bank.code);
  assert.equal(row["subtype"], "BANK");
  const revenue = rows.find((r) => r["code"] === income.code)!;
  assert.equal(revenue["parent_code"], null);
});
