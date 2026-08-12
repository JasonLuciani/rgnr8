/**
 * Runnable demo:  node --import tsx examples/demo.ts
 * Ingests the bank + payroll fixtures, prints the canonical transactions and
 * what the pipeline did, then maps them to journals and shows the trial balance.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import {
  AccountType,
  ChartOfAccounts,
  InMemoryLedgerStore,
  PeriodRegistry,
  PostingEngine,
  USD,
  asAccountId,
  asTenantId,
  computeTrialBalance,
} from "@rgnr8/ledger-kernel";
import type { Account } from "@rgnr8/ledger-kernel";
import {
  BankPlaidLikeAdapter,
  IngestionPipeline,
  PayrollGustoLikeAdapter,
  defaultAccountMap,
  toPostingCommands,
  type RawRecord,
} from "../src/index.js";

const here = dirname(fileURLToPath(import.meta.url));
const fx = join(here, "..", "fixtures");
const FETCHED = "2026-08-11T00:00:00Z";

const bank = JSON.parse(readFileSync(join(fx, "plaid_transactions.json"), "utf-8")) as Array<{
  transaction_id: string;
  account_id: string;
}>;
const bankRaws: RawRecord[] = bank.map((t) => ({
  provider: "plaid",
  accountId: t.account_id,
  externalId: t.transaction_id,
  payload: t,
  fetchedAt: FETCHED,
  sourceVersion: "1",
}));
const run = JSON.parse(readFileSync(join(fx, "gusto_payroll.json"), "utf-8")) as { payroll_id: string; account_id: string };
const payrollRaw: RawRecord = {
  provider: "gusto",
  accountId: run.account_id,
  externalId: run.payroll_id,
  payload: run,
  fetchedAt: FETCHED,
  sourceVersion: "1",
};

function acct(code: string, type: AccountType): Account {
  return { id: asAccountId(`gl.${code}`), code, name: code, type, currency: USD };
}

async function main(): Promise<void> {
  const pipeline = new IngestionPipeline()
    .register(new BankPlaidLikeAdapter("acme"))
    .register(new PayrollGustoLikeAdapter("acme"));

  const res = pipeline.ingest([...bankRaws, payrollRaw]);
  console.log(
    `Ingested: archived ${res.archived}, added ${res.added.length}, ` +
      `pending→posted ${res.pendingSuperseded}, transfers ${res.transfersDetected}\n`,
  );

  console.log("Canonical transactions (active):");
  console.log("  date        amount        dir      kind            note");
  for (const t of pipeline.active()) {
    const flag = t.isInternalTransfer ? "internal-transfer" : "";
    console.log(
      `  ${t.date}  ${t.amount.toDecimalString().padStart(11)}  ${t.direction.padEnd(7)}  ` +
        `${t.kind.padEnd(14)}  ${flag}`,
    );
  }

  const { commands, skippedTransfers } = toPostingCommands(pipeline.active(), defaultAccountMap());
  const chart = new ChartOfAccounts([
    acct("cash", AccountType.ASSET),
    acct("income", AccountType.REVENUE),
    acct("expense", AccountType.EXPENSE),
    acct("fees", AccountType.EXPENSE),
    acct("payroll_expense", AccountType.EXPENSE),
    acct("payroll_tax", AccountType.EXPENSE),
    acct("interest_income", AccountType.REVENUE),
  ]);
  const store = new InMemoryLedgerStore();
  const engine = new PostingEngine(chart, store, new PeriodRegistry());
  for (const cmd of commands) await engine.post(cmd, { postedAt: FETCHED });

  const tb = await computeTrialBalance(store, asTenantId("acme"), chart, USD);
  console.log(`\nPosted ${commands.length} journals (${skippedTransfers} transfer legs skipped).`);
  console.log("\nTrial balance:");
  for (const r of tb.rows) {
    console.log(
      `  ${r.code.padEnd(16)} Dr ${r.debit.toDecimalString().padStart(10)}  Cr ${r.credit.toDecimalString().padStart(10)}`,
    );
  }
  console.log(`  ${"TOTAL".padEnd(16)} Dr ${tb.totalDebit.toDecimalString().padStart(10)}  Cr ${tb.totalCredit.toDecimalString().padStart(10)}`);
  console.log(`\nIn balance: ${tb.inBalance ? "YES" : "NO"}`);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
