import { Money, sumMoney, type Currency } from "@rgnr8/ledger-kernel";
import { ageItems } from "./aging.js";
import type {
  Aging,
  AREvent,
  ARInvoice,
  ARInvoiceInput,
  Application,
  CustomerHistory,
  DocStatus,
} from "./types.js";

interface MutableInvoice {
  id: string;
  customerId: string;
  issueDate: string;
  dueDate: string;
  originalAmount: Money;
  openAmount: Money;
  status: DocStatus;
  settledDate?: string;
}

/**
 * Accounts-receivable subledger. Tracks open invoices, applies customer
 * payments (supporting partials), ages the balance, exposes the control total
 * (which ties to the GL AR control account), and derives customer payment
 * histories from settled invoices for the forecast's timing model.
 */
export class ARSubledger {
  private readonly invoices = new Map<string, MutableInvoice>();
  private readonly log: AREvent[] = [];
  private evSeq = 0;

  constructor(private readonly currency: Currency) {}

  addInvoice(input: ARInvoiceInput): void {
    if (this.invoices.has(input.id)) throw new Error(`duplicate invoice ${input.id}`);
    this.invoices.set(input.id, {
      id: input.id,
      customerId: input.customerId,
      issueDate: input.issueDate,
      dueDate: input.dueDate,
      originalAmount: input.amount,
      openAmount: input.amount,
      status: input.disputed ? "DISPUTED" : "OPEN",
    });
    this.log.push({
      id: `${input.id}#issue`,
      kind: "INVOICE",
      invoiceId: input.id,
      customerId: input.customerId,
      date: input.issueDate,
      amount: input.amount,
    });
  }

  /** Apply a customer payment to an invoice. Overpayment is clamped to zero. */
  applyPayment(app: Application): ARInvoice {
    const inv = this.invoices.get(app.documentId);
    if (!inv) throw new Error(`unknown invoice ${app.documentId}`);
    if (inv.status === "WRITTEN_OFF") throw new Error(`invoice ${inv.id} is written off`);

    const before = inv.openAmount;
    const remaining = inv.openAmount.minus(app.amount);
    inv.openAmount = remaining.isNegative() ? Money.zero(this.currency) : remaining;
    if (inv.openAmount.isZero()) {
      inv.status = "PAID";
      inv.settledDate = app.date;
    } else {
      inv.status = inv.status === "DISPUTED" ? "DISPUTED" : "PARTIAL";
    }
    this.log.push({
      id: `${inv.id}#pay-${++this.evSeq}`,
      kind: "PAYMENT",
      invoiceId: inv.id,
      customerId: inv.customerId,
      date: app.date,
      amount: before.minus(inv.openAmount), // amount actually applied (overpay clamped)
    });
    return this.freeze(inv);
  }

  writeOff(id: string, date: string): ARInvoice {
    const inv = this.invoices.get(id);
    if (!inv) throw new Error(`unknown invoice ${id}`);
    const before = inv.openAmount;
    inv.openAmount = Money.zero(this.currency);
    inv.status = "WRITTEN_OFF";
    inv.settledDate = date;
    if (before.isPositive()) {
      this.log.push({
        id: `${inv.id}#writeoff`,
        kind: "WRITEOFF",
        invoiceId: inv.id,
        customerId: inv.customerId,
        date,
        amount: before,
      });
    }
    return this.freeze(inv);
  }

  /** Events for posting to the general ledger, in order. */
  events(): readonly AREvent[] {
    return [...this.log];
  }

  get(id: string): ARInvoice | undefined {
    const i = this.invoices.get(id);
    return i ? this.freeze(i) : undefined;
  }

  all(): readonly ARInvoice[] {
    return [...this.invoices.values()].map((i) => this.freeze(i));
  }

  openInvoices(): readonly ARInvoice[] {
    return this.all().filter((i) => i.openAmount.isPositive());
  }

  /** Control balance = sum of open receivables. Ties to the GL AR control account. */
  controlBalance(): Money {
    return sumMoney(
      [...this.invoices.values()].map((i) => i.openAmount),
      this.currency,
    );
  }

  aging(asOf: string): Aging {
    return ageItems(
      this.openInvoices().map((i) => ({ dueDate: i.dueDate, openAmount: i.openAmount })),
      asOf,
      this.currency,
    );
  }

  /** (due, paid) observations per customer from fully-settled invoices. */
  customerHistories(): readonly CustomerHistory[] {
    const byCustomer = new Map<string, { dueDate: string; paidDate: string }[]>();
    for (const inv of this.invoices.values()) {
      if (inv.status === "PAID" && inv.settledDate) {
        const list = byCustomer.get(inv.customerId) ?? [];
        list.push({ dueDate: inv.dueDate, paidDate: inv.settledDate });
        byCustomer.set(inv.customerId, list);
      }
    }
    return [...byCustomer.entries()]
      .map(([customerId, observations]) => ({
        customerId,
        observations: observations.sort((a, b) => (a.paidDate < b.paidDate ? -1 : 1)),
      }))
      .sort((a, b) => (a.customerId < b.customerId ? -1 : 1));
  }

  private freeze(i: MutableInvoice): ARInvoice {
    return Object.freeze({ ...i });
  }
}
