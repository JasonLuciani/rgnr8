import type { Money } from "@rgnr8/ledger-kernel";

export type DocStatus = "OPEN" | "PARTIAL" | "PAID" | "DISPUTED" | "WRITTEN_OFF";

/** An accounts-receivable invoice (money a customer owes). */
export interface ARInvoiceInput {
  readonly id: string;
  readonly customerId: string;
  readonly issueDate: string; // ISO
  readonly dueDate: string; // ISO
  readonly amount: Money; // positive, original amount
  readonly disputed?: boolean;
}

/** An accounts-payable bill (money owed to a vendor). */
export interface APBillInput {
  readonly id: string;
  readonly vendorId: string;
  readonly billDate: string;
  readonly dueDate: string;
  readonly amount: Money;
}

/** A payment applied to a specific invoice or bill. */
export interface Application {
  readonly documentId: string;
  readonly date: string; // ISO
  readonly amount: Money; // positive amount applied
}

/** The live state of an AR invoice in the subledger. */
export interface ARInvoice {
  readonly id: string;
  readonly customerId: string;
  readonly issueDate: string;
  readonly dueDate: string;
  readonly originalAmount: Money;
  readonly openAmount: Money;
  readonly status: DocStatus;
  /** Date of the payment that fully settled the invoice (if PAID). */
  readonly settledDate?: string;
}

export interface APBill {
  readonly id: string;
  readonly vendorId: string;
  readonly billDate: string;
  readonly dueDate: string;
  readonly originalAmount: Money;
  readonly openAmount: Money;
  readonly status: DocStatus;
  readonly settledDate?: string;
}

export interface AgingBucket {
  readonly label: string;
  readonly minDays: number; // inclusive lower bound of days past due
  readonly maxDays: number | null; // exclusive upper bound, null = open-ended
  readonly amount: Money;
}

export interface Aging {
  readonly asOf: string;
  readonly buckets: readonly AgingBucket[];
  readonly total: Money;
}

/** A (due, paid) observation for learning customer payment timing. */
export interface PaymentObservation {
  readonly dueDate: string;
  readonly paidDate: string;
}

export interface CustomerHistory {
  readonly customerId: string;
  readonly observations: readonly PaymentObservation[];
}

/** Events emitted by the AR subledger, for posting to the general ledger. */
export type AREventKind = "INVOICE" | "PAYMENT" | "WRITEOFF";
export interface AREvent {
  readonly id: string;
  readonly kind: AREventKind;
  readonly invoiceId: string;
  readonly customerId: string;
  readonly date: string;
  readonly amount: Money; // positive
}

export type APEventKind = "BILL" | "PAYMENT";
export interface APEvent {
  readonly id: string;
  readonly kind: APEventKind;
  readonly billId: string;
  readonly vendorId: string;
  readonly date: string;
  readonly amount: Money;
}
