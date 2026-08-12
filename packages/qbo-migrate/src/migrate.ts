import {
  InMemoryLedgerStore,
  Money,
  PeriodRegistry,
  PostingEngine,
  computeTrialBalance,
  getCurrency,
  type Currency,
  type LedgerStore,
  type TenantId,
  type TrialBalance,
} from "@rgnr8/ledger-kernel";
import { buildChartFromQbo, type QboChart } from "./accountMap.js";
import { compareTrialBalances, type DiffReport } from "./compare.js";
import { importQbo, type ImportReport } from "./import.js";
import type {
  MoneyDTO,
  OverlayBillDTO,
  OverlayForecastInputsDTO,
  OverlayInvoiceDTO,
  QboOpenBill,
  QboOpenInvoice,
} from "./overlay.js";
import type { QboExport } from "./types.js";

/**
 * The **migration** onboarding path (Stage 2) — RGNR8 *becomes* the system of
 * record. Where the overlay reads QBO's reported numbers and layers cash
 * intelligence on top, migration imports the full QBO GeneralLedger into the
 * RGNR8 double-entry ledger, verifies the import against QBO's own trial balance
 * (the parallel close), and then derives the cash forecast's opening position
 * from *RGNR8's own posted ledger* rather than from QBO's reported balances.
 *
 * That difference is the whole point of Stage 2: the opening cash is now backed
 * by immutable, balanced-by-construction journal entries RGNR8 posted itself,
 * and the parallel-close diff is the evidence the migration was faithful. Open
 * AR/AP timing (invoice/bill due dates) still comes from the transaction-level
 * QBO snapshot, since a trial balance alone doesn't carry per-document dates.
 */

function moneyToDto(m: Money): MoneyDTO {
  const minor = m.minorUnits;
  const asNumber = Number(minor);
  const safe = BigInt(Number.isSafeInteger(asNumber) ? asNumber : NaN) === minor;
  return { minor: safe ? asNumber : minor.toString(), currency: m.currency.code };
}

export interface MigrationBuildOptions {
  readonly currency?: string;
  /** ISO date the migration snapshot reflects — the forecast opening as-of. */
  readonly asOf: string;
  /** Open AR carried over from the QBO snapshot (for invoice-level timing). */
  readonly openInvoices?: readonly QboOpenInvoice[];
  /** Open AP carried over from the QBO snapshot (for bill-level timing). */
  readonly openBills?: readonly QboOpenBill[];
  /**
   * Whether the opening position is verified. In the orchestrator this is set to
   * the parallel-close result: opening cash the RGNR8 ledger agrees with QBO on
   * is `verified: true`.
   */
  readonly openingVerified?: boolean;
}

/**
 * Build a `forecast-inputs/1` DTO from an imported RGNR8 ledger. Opening cash is
 * the net balance of every Bank-type account **as posted in the RGNR8 ledger**
 * (bank account names come from the QBO chart, since the kernel collapses Bank →
 * ASSET). Open invoices/bills come from the caller-supplied QBO snapshot.
 */
export function buildMigrationForecastInputs(
  exp: QboExport,
  trialBalance: TrialBalance,
  opts: MigrationBuildOptions,
): { dto: OverlayForecastInputsDTO; notes: readonly string[] } {
  const ccy: Currency = getCurrency(opts.currency ?? trialBalance.currency.code);
  const notes: string[] = [];

  const bankNames = new Set(
    exp.accounts.filter((a) => a.type === "Bank").map((a) => a.name),
  );
  let openingMinor = 0n;
  let bankCount = 0;
  for (const row of trialBalance.rows) {
    if (!bankNames.has(row.name)) continue;
    openingMinor += row.debit.minorUnits - row.credit.minorUnits;
    bankCount++;
  }
  const available = Money.fromMinorUnits(openingMinor, ccy);

  const invoices: OverlayInvoiceDTO[] = [];
  for (const inv of opts.openInvoices ?? []) {
    const amount = Money.fromDecimal(inv.balance, ccy);
    if (!amount.isPositive()) continue;
    invoices.push({
      id: inv.id,
      customer_id: inv.customerId,
      issue_date: inv.issueDate,
      due_date: inv.dueDate || inv.issueDate,
      open_amount: moneyToDto(amount),
      status: "OPEN",
    });
  }

  const bills: OverlayBillDTO[] = [];
  for (const bill of opts.openBills ?? []) {
    const amount = Money.fromDecimal(bill.balance, ccy);
    if (!amount.isPositive()) continue;
    bills.push({
      id: bill.id,
      vendor_id: bill.vendorId,
      due_date: bill.dueDate || bill.txnDate,
      amount: moneyToDto(amount),
      scheduled_date: null,
    });
  }

  notes.push(
    `migration: opening ${available.toString()} from ${bankCount} RGNR8-posted bank account(s), ` +
      `${invoices.length} open invoice(s), ${bills.length} open bill(s)`,
  );

  const dto: OverlayForecastInputsDTO = {
    contract: "forecast-inputs/1",
    currency: ccy.code,
    opening: {
      as_of: opts.asOf.slice(0, 10),
      available: moneyToDto(available),
      restricted: { minor: 0, currency: ccy.code },
      verified: opts.openingVerified === true,
    },
    invoices,
    customer_histories: [],
    bills,
    recurring: [],
    payroll: [],
    debt: [],
    one_time: [],
    pipeline: [],
  };
  return { dto, notes };
}

export interface MigrationOnboardOptions {
  readonly tenantId: TenantId;
  readonly currency: Currency;
  /** ISO date the migration reflects — forecast opening as-of. */
  readonly asOf: string;
  readonly ingestedAt: string;
  readonly postedAt: string;
  /** Production supplies a real store; defaults to an in-memory ledger. */
  readonly store?: LedgerStore;
  readonly periods?: PeriodRegistry;
  readonly openInvoices?: readonly QboOpenInvoice[];
  readonly openBills?: readonly QboOpenBill[];
  /** Parallel-close tolerance in minor units before a row counts as a mismatch. */
  readonly toleranceMinor?: bigint;
}

export interface MigrationResult {
  readonly chart: QboChart;
  readonly store: LedgerStore;
  readonly importReport: ImportReport;
  readonly trialBalance: TrialBalance;
  /** Parallel-close diff vs QBO's reported TB (undefined if none provided). */
  readonly diff: DiffReport | undefined;
  /** True when the RGNR8 ledger agrees with QBO's reported trial balance. */
  readonly verified: boolean;
  readonly dto: OverlayForecastInputsDTO;
  readonly notes: readonly string[];
}

/**
 * Run the full Stage-2 migration end to end: build the chart from the QBO
 * export, import the GeneralLedger into the RGNR8 ledger, compute the RGNR8
 * trial balance, verify it against QBO's reported TB (the parallel close), and
 * emit the `forecast-inputs/1` DTO — with opening cash derived from the posted
 * RGNR8 ledger and marked `verified` iff the parallel close agrees.
 */
export async function runMigrationOnboarding(
  exp: QboExport,
  opts: MigrationOnboardOptions,
): Promise<MigrationResult> {
  const chart = buildChartFromQbo(exp.accounts, opts.currency);
  const store = opts.store ?? new InMemoryLedgerStore();
  const engine = new PostingEngine(chart.coa, store, opts.periods ?? new PeriodRegistry());

  const importReport = await importQbo(
    engine,
    exp,
    chart,
    {
      tenantId: opts.tenantId,
      currency: opts.currency,
      ingestedAt: opts.ingestedAt,
    },
    opts.postedAt,
  );

  const trialBalance = await computeTrialBalance(store, opts.tenantId, chart.coa, opts.currency);

  let diff: DiffReport | undefined;
  if (exp.reportedTrialBalance && exp.reportedTrialBalance.length > 0) {
    diff = compareTrialBalances(trialBalance, exp.reportedTrialBalance, {
      currency: opts.currency,
      ...(opts.toleranceMinor !== undefined ? { toleranceMinor: opts.toleranceMinor } : {}),
    });
  }
  const verified = diff !== undefined && diff.inAgreement;

  const { dto, notes } = buildMigrationForecastInputs(exp, trialBalance, {
    currency: opts.currency.code,
    asOf: opts.asOf,
    ...(opts.openInvoices !== undefined ? { openInvoices: opts.openInvoices } : {}),
    ...(opts.openBills !== undefined ? { openBills: opts.openBills } : {}),
    openingVerified: verified,
  });

  const allNotes = [
    `imported ${importReport.posted} entr(ies), ${importReport.skipped} skipped` +
      (importReport.issues.length > 0 ? ` (${importReport.issues.map((i) => i.kind).join(", ")})` : ""),
    diff
      ? `parallel close: ${diff.matched} matched, ${diff.mismatches.length} mismatch(es), ` +
        `${diff.onlyInRgnr8.length} only-RGNR8, ${diff.onlyInQbo.length} only-QBO → ${verified ? "VERIFIED" : "NEEDS REVIEW"}`
      : "parallel close: no reported trial balance supplied — opening unverified",
    ...notes,
  ];

  return { chart, store, importReport, trialBalance, diff, verified, dto, notes: allNotes };
}
