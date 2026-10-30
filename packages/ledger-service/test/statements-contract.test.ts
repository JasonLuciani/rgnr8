import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

// The shape the web screens read, checked in under the web package because the
// web app is the consumer. This test is the producer side: if the ledger's
// statements response drifts from the fixture, it fails here. The Python side
// (packages/web/tests/test_statements_contract.py) reads the same file and fails
// if the screen stops reading the fields it carries.
const FIXTURE = fileURLToPath(new URL("../../web/tests/contracts/statements.json", import.meta.url));

const TOKEN = "svc-secret";
const NOW = "2026-08-31T00:00:00Z";

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { authToken: TOKEN, now: () => NOW });
}

function call(s: LedgerService, method: string, path: string, body: unknown = "",
  query: Record<string, string> = {}): Promise<ServiceResponse> {
  const m = /^\/t\/([^/]+)/.exec(path);
  const bearer = m ? createHmac("sha256", TOKEN).update(m[1]!).digest("hex") : TOKEN;
  return s.handle({
    method,
    path,
    query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: { authorization: `Bearer ${bearer}` },
  });
}

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

/** A loss month, so the fixture carries negative net income and a negative cash balance. */
async function seedLossMonth(s: LedgerService): Promise<void> {
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05",
    memo: "Cash sale",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "500000" },
      { code: "4000", side: "CREDIT", amount_minor: "500000" },
    ],
  });
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-10",
    memo: "Pay rent",
    lines: [
      { code: "6300", side: "DEBIT", amount_minor: "800000" },
      { code: "1000", side: "CREDIT", amount_minor: "800000" },
    ],
  });
}

test("GET /statements matches the checked-in financial-statements/1 fixture", async () => {
  const s = svc();
  await seedLossMonth(s);
  const st = obj(await call(s, "GET", "/t/acme/statements", "", { from: "2026-08-01", to: "2026-08-31" }));

  // RGNR8_WRITE_CONTRACT=1 regenerates the fixture after an intended contract change.
  if (process.env["RGNR8_WRITE_CONTRACT"] === "1") {
    writeFileSync(FIXTURE, `${JSON.stringify(st, null, 2)}\n`);
  }
  const fixture = JSON.parse(readFileSync(FIXTURE, "utf8")) as Record<string, unknown>;

  // Every statement line is exactly {label, amount_minor}; a rename fails with a clear message.
  for (const [section, key] of [
    ["income_statement", "revenue"], ["income_statement", "expenses"],
    ["balance_sheet", "assets"], ["balance_sheet", "liabilities"], ["balance_sheet", "equity"],
  ] as const) {
    const lines = (st[section] as Record<string, unknown[]>)[key] ?? [];
    for (const line of lines) {
      assert.deepEqual(Object.keys(line as object).sort(), ["amount_minor", "label"],
        `${section}.${key} line shape changed`);
    }
  }

  assert.deepStrictEqual(st, fixture, "statements response drifted from the web fixture; " +
    "if intended, regenerate with RGNR8_WRITE_CONTRACT=1 and update the Python screen");
});
