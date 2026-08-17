import { Money, type Currency } from "@rgnr8/ledger-kernel";
import type { DocumentLine, InvoiceDoc, BillDoc } from "./documents.js";

/**
 * Non-posting source documents: Estimates (quotes) and Purchase Orders. Unlike
 * invoices and bills, these never touch the ledger — they're commitments, not
 * transactions. They carry a status lifecycle and convert into the posting
 * document (invoice / bill) when accepted/received, which is the moment the
 * money actually moves. Keeping them non-posting is what keeps the ledger a
 * record of what *happened*, not what's merely planned.
 */

export type EstimateStatus = "DRAFT" | "SENT" | "ACCEPTED" | "DECLINED" | "CLOSED";
export type PurchaseOrderStatus = "DRAFT" | "OPEN" | "RECEIVED" | "CLOSED";

export interface Estimate {
  readonly id: string;
  readonly customerId: string;
  readonly date: string; // ISO
  readonly expiryDate?: string;
  readonly status: EstimateStatus;
  readonly lines: readonly DocumentLine[];
  readonly memo?: string;
}

export interface PurchaseOrder {
  readonly id: string;
  readonly vendorId: string;
  readonly date: string;
  readonly status: PurchaseOrderStatus;
  readonly lines: readonly DocumentLine[];
  readonly memo?: string;
}

export class NonPostingError extends Error {}

/** Sum a document's line amounts (quantity × unitAmount), for display/limits. */
export function documentTotal(lines: readonly DocumentLine[], currency: Currency): Money {
  let total = Money.zero(currency);
  for (const l of lines) {
    if (l.unitAmount === undefined) continue;
    const qty = l.quantity ?? 1;
    total = total.plus(l.unitAmount.timesInteger(BigInt(Math.trunc(qty))));
  }
  return total;
}

/**
 * Convert an accepted estimate into an invoice document input (same lines).
 * Refuses unless the estimate is ACCEPTED — you don't bill a quote that wasn't
 * agreed to.
 */
export function estimateToInvoice(
  est: Estimate,
  invoice: { id: string; date: string; dueDate: string; memo?: string },
): InvoiceDoc {
  if (est.status !== "ACCEPTED") {
    throw new NonPostingError(`estimate ${est.id} is ${est.status}, not ACCEPTED`);
  }
  return {
    id: invoice.id,
    customerId: est.customerId,
    issueDate: invoice.date,
    dueDate: invoice.dueDate,
    lines: est.lines,
    ...(invoice.memo ?? est.memo ? { memo: invoice.memo ?? est.memo } : {}),
  };
}

/**
 * Convert a received purchase order into a bill document input. Refuses unless
 * the PO is RECEIVED (goods/services in hand).
 */
export function purchaseOrderToBill(
  po: PurchaseOrder,
  bill: { id: string; date: string; dueDate: string; memo?: string },
): BillDoc {
  if (po.status !== "RECEIVED") {
    throw new NonPostingError(`purchase order ${po.id} is ${po.status}, not RECEIVED`);
  }
  return {
    id: bill.id,
    vendorId: po.vendorId,
    billDate: bill.date,
    dueDate: bill.dueDate,
    lines: po.lines,
    ...(bill.memo ?? po.memo ? { memo: bill.memo ?? po.memo } : {}),
  };
}
