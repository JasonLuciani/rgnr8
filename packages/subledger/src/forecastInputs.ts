import type { Money } from "@rgnr8/ledger-kernel";
import type { APSubledger } from "./ap.js";
import type { ARSubledger } from "./ar.js";
import type { DocStatus } from "./types.js";

/** DTO fragments matching the forecast-inputs/1 contract (Python rgnr8_forecast.io). */
export interface MoneyDTO {
  minor: number | string;
  currency: string;
}
export interface InvoiceDTO {
  id: string;
  customer_id: string;
  issue_date: string;
  due_date: string;
  open_amount: MoneyDTO;
  status: "OPEN" | "PARTIAL" | "DISPUTED";
}
export interface CustomerHistoryDTO {
  customer_id: string;
  observations: { due_date: string; paid_date: string }[];
}
export interface BillDTO {
  id: string;
  vendor_id: string;
  due_date: string;
  amount: MoneyDTO;
  scheduled_date?: string | null;
}

export interface SubledgerForecastParts {
  invoices: InvoiceDTO[];
  customer_histories: CustomerHistoryDTO[];
  bills: BillDTO[];
}

export function moneyToDto(m: Money): MoneyDTO {
  const minor = m.minorUnits;
  const asNumber = Number(minor);
  const safe = BigInt(Number.isSafeInteger(asNumber) ? asNumber : NaN) === minor;
  return { minor: safe ? asNumber : minor.toString(), currency: m.currency.code };
}

function invoiceStatus(s: DocStatus): "OPEN" | "PARTIAL" | "DISPUTED" | null {
  switch (s) {
    case "OPEN":
      return "OPEN";
    case "PARTIAL":
      return "PARTIAL";
    case "DISPUTED":
      return "DISPUTED";
    case "PAID":
    case "WRITTEN_OFF":
      return null; // nothing left to forecast
  }
}

/**
 * Emit the forecast-input fragments the AR/AP subledgers contribute: open
 * invoices (with their remaining balance and due date), customer payment
 * histories (for the timing model), and open bills.
 */
export function subledgerForecastParts(ar: ARSubledger, ap: APSubledger): SubledgerForecastParts {
  const invoices: InvoiceDTO[] = [];
  for (const inv of ar.openInvoices()) {
    const status = invoiceStatus(inv.status);
    if (!status) continue;
    invoices.push({
      id: inv.id,
      customer_id: inv.customerId,
      issue_date: inv.issueDate,
      due_date: inv.dueDate,
      open_amount: moneyToDto(inv.openAmount),
      status,
    });
  }

  const customer_histories: CustomerHistoryDTO[] = ar.customerHistories().map((h) => ({
    customer_id: h.customerId,
    observations: h.observations.map((o) => ({ due_date: o.dueDate, paid_date: o.paidDate })),
  }));

  const bills: BillDTO[] = ap.openBills().map((b) => ({
    id: b.id,
    vendor_id: b.vendorId,
    due_date: b.dueDate,
    amount: moneyToDto(b.openAmount),
  }));

  return { invoices, customer_histories, bills };
}
