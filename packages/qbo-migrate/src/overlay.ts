import { Money, getCurrency, sumMoney, type Currency } from "@rgnr8/ledger-kernel";
import { parseAmount } from "./csv.js";

/**
 * The **overlay** onboarding path — RGNR8 sitting *on top of* QuickBooks Online.
 *
 * QBO stays the system of record; RGNR8 pulls a live read-only snapshot of the
 * three things a cash forecast needs — bank balances (opening cash), open
 * customer invoices (AR inflows), open vendor bills (AP outflows) — and maps
 * them into the cross-language `forecast-inputs/1` DTO the Python forecaster
 * consumes. No import, no re-posting, no becoming the ledger: the owner keeps
 * running QBO and gets a verified 13-week cash outlook layered over it on day
 * one. (Stage two, the full migration where RGNR8 *becomes* the ledger, is the
 * GeneralLedger import path in `api.ts` / `import.ts`.)
 *
 * Everything is built from a normalized `QboOverlaySnapshot`, so the mapping is
 * unit-tested against fixture data with no network; the `QboApiClient` methods
 * here just fetch that snapshot from Intuit's REST API over the same injected
 * `QboHttpClient` seam used elsewhere. Amounts are decimal strings at the
 * boundary, parsed into integer-minor-unit `Money` — never floats.
 */

// --- normalized snapshot -----------------------------------------------------

export interface QboBankBalance {
  readonly id: string;
  readonly name: string;
  /** QBO `CurrentBalance` as a decimal string (e.g. "48210.55"). */
  readonly currentBalance: string;
  /** Optional QBO currency code; defaults to the snapshot currency. */
  readonly currency?: string;
}

export interface QboOpenInvoice {
  readonly id: string;
  readonly customerId: string;
  readonly customerName?: string;
  /** QBO `TxnDate` (issue date), ISO. */
  readonly issueDate: string;
  /** QBO `DueDate`, ISO. Falls back to issue date if QBO omits it. */
  readonly dueDate: string;
  /** Remaining `Balance` (open amount), decimal string. */
  readonly balance: string;
}

export interface QboOpenBill {
  readonly id: string;
  readonly vendorId: string;
  readonly vendorName?: string;
  readonly txnDate: string;
  readonly dueDate: string;
  /** Remaining `Balance` (open amount), decimal string. */
  readonly balance: string;
}

/** A point-in-time read of the QBO data a cash forecast needs. */
export interface QboOverlaySnapshot {
  /** ISO date the snapshot was taken — becomes the forecast's opening as-of. */
  readonly asOf: string;
  readonly bankAccounts: readonly QboBankBalance[];
  readonly openInvoices: readonly QboOpenInvoice[];
  readonly openBills: readonly QboOpenBill[];
}

// --- forecast-inputs/1 DTO (mirrors rgnr8_forecast.io) -----------------------

export interface MoneyDTO {
  minor: number | string;
  currency: string;
}
export interface OverlayInvoiceDTO {
  id: string;
  customer_id: string;
  issue_date: string;
  due_date: string;
  open_amount: MoneyDTO;
  status: "OPEN";
}
export interface OverlayBillDTO {
  id: string;
  vendor_id: string;
  due_date: string;
  amount: MoneyDTO;
  scheduled_date: string | null;
}
export interface OverlayOpeningDTO {
  as_of: string;
  available: MoneyDTO;
  restricted: MoneyDTO;
  verified: boolean;
}
export interface OverlayForecastInputsDTO {
  contract: "forecast-inputs/1";
  currency: string;
  opening: OverlayOpeningDTO;
  invoices: OverlayInvoiceDTO[];
  customer_histories: unknown[];
  bills: OverlayBillDTO[];
  recurring: unknown[];
  payroll: unknown[];
  debt: unknown[];
  one_time: unknown[];
  pipeline: unknown[];
}

function moneyToDto(m: Money): MoneyDTO {
  const minor = m.minorUnits;
  const asNumber = Number(minor);
  const safe = BigInt(Number.isSafeInteger(asNumber) ? asNumber : NaN) === minor;
  return { minor: safe ? asNumber : minor.toString(), currency: m.currency.code };
}

export interface OverlayBuildOptions {
  /** ISO 4217 code; defaults to USD. */
  readonly currency?: string;
  /**
   * Verify opening cash is real before the forecast trusts it. The overlay reads
   * QBO's *reported* balances rather than reconciling to a bank statement, so the
   * opening is marked `verified: false` unless the caller explicitly asserts it
   * has been confirmed against the bank.
   */
  readonly openingVerified?: boolean;
}

export interface OverlayBuildResult {
  readonly dto: OverlayForecastInputsDTO;
  readonly notes: readonly string[];
}

/**
 * Map a QBO overlay snapshot into a `forecast-inputs/1` DTO:
 *   opening cash  = sum of bank `CurrentBalance`
 *   invoices      = open AR (remaining balance, due date) → forecast inflows
 *   bills         = open AP (remaining balance, due date) → forecast outflows
 *
 * Rows QBO reports with a zero or unparseable balance are dropped (an invoice
 * with nothing left to collect is not a future inflow); each drop is noted.
 */
export function buildOverlayForecastInputs(
  snapshot: QboOverlaySnapshot,
  opts: OverlayBuildOptions = {},
): OverlayBuildResult {
  const ccy: Currency = getCurrency(opts.currency ?? "USD");
  const notes: string[] = [];

  const bankMoney: Money[] = [];
  for (const b of snapshot.bankAccounts) {
    const parsed = parseAmount(b.currentBalance);
    if (parsed === undefined) {
      notes.push(`bank "${b.name}" (${b.id}): unparseable balance "${b.currentBalance}" — skipped`);
      continue;
    }
    bankMoney.push(Money.fromDecimal(parsed, ccy));
  }
  const available = sumMoney(bankMoney, ccy);

  const invoices: OverlayInvoiceDTO[] = [];
  let droppedInvoices = 0;
  for (const inv of snapshot.openInvoices) {
    const parsed = parseAmount(inv.balance);
    if (parsed === undefined) {
      droppedInvoices++;
      continue;
    }
    const amount = Money.fromDecimal(parsed, ccy);
    if (!amount.isPositive()) {
      droppedInvoices++;
      continue;
    }
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
  let droppedBills = 0;
  for (const bill of snapshot.openBills) {
    const parsed = parseAmount(bill.balance);
    if (parsed === undefined) {
      droppedBills++;
      continue;
    }
    const amount = Money.fromDecimal(parsed, ccy);
    if (!amount.isPositive()) {
      droppedBills++;
      continue;
    }
    bills.push({
      id: bill.id,
      vendor_id: bill.vendorId,
      due_date: bill.dueDate || bill.txnDate,
      amount: moneyToDto(amount),
      scheduled_date: null,
    });
  }

  notes.push(
    `overlay: ${bankMoney.length} bank account(s) → opening ${available.toString()}, ` +
      `${invoices.length} open invoice(s), ${bills.length} open bill(s)`,
  );
  if (droppedInvoices > 0) notes.push(`${droppedInvoices} invoice(s) dropped (zero/unparseable balance)`);
  if (droppedBills > 0) notes.push(`${droppedBills} bill(s) dropped (zero/unparseable balance)`);

  const dto: OverlayForecastInputsDTO = {
    contract: "forecast-inputs/1",
    currency: ccy.code,
    opening: {
      as_of: snapshot.asOf.slice(0, 10),
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
