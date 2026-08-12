import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import type { RawRecord } from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const fixtures = join(here, "..", "fixtures");

const FETCHED_AT = "2026-08-11T00:00:00Z";

interface PlaidLike {
  transaction_id: string;
  account_id: string;
}

export function plaidRawRecords(): RawRecord[] {
  const txns = JSON.parse(readFileSync(join(fixtures, "plaid_transactions.json"), "utf-8")) as PlaidLike[];
  return txns.map((t) => ({
    provider: "plaid",
    accountId: t.account_id,
    externalId: t.transaction_id,
    payload: t,
    fetchedAt: FETCHED_AT,
    sourceVersion: "1",
  }));
}

export function gustoRawRecord(): RawRecord {
  const run = JSON.parse(readFileSync(join(fixtures, "gusto_payroll.json"), "utf-8")) as {
    payroll_id: string;
    account_id: string;
  };
  return {
    provider: "gusto",
    accountId: run.account_id,
    externalId: run.payroll_id,
    payload: run,
    fetchedAt: FETCHED_AT,
    sourceVersion: "1",
  };
}
