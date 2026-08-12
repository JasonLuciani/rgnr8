import { Money, sumMoney, type Currency } from "@rgnr8/ledger-kernel";
import { ageItems } from "./aging.js";
import type { Aging, APBill, APBillInput, APEvent, Application, DocStatus } from "./types.js";

interface MutableBill {
  id: string;
  vendorId: string;
  billDate: string;
  dueDate: string;
  originalAmount: Money;
  openAmount: Money;
  status: DocStatus;
  settledDate?: string;
}

/**
 * Accounts-payable subledger. Tracks open bills, applies vendor payments
 * (supporting partials), ages the balance, and exposes the control total that
 * ties to the GL AP control account.
 */
export class APSubledger {
  private readonly bills = new Map<string, MutableBill>();
  private readonly log: APEvent[] = [];
  private evSeq = 0;

  constructor(private readonly currency: Currency) {}

  addBill(input: APBillInput): void {
    if (this.bills.has(input.id)) throw new Error(`duplicate bill ${input.id}`);
    this.bills.set(input.id, {
      id: input.id,
      vendorId: input.vendorId,
      billDate: input.billDate,
      dueDate: input.dueDate,
      originalAmount: input.amount,
      openAmount: input.amount,
      status: "OPEN",
    });
    this.log.push({
      id: `${input.id}#bill`,
      kind: "BILL",
      billId: input.id,
      vendorId: input.vendorId,
      date: input.billDate,
      amount: input.amount,
    });
  }

  applyPayment(app: Application): APBill {
    const bill = this.bills.get(app.documentId);
    if (!bill) throw new Error(`unknown bill ${app.documentId}`);
    const before = bill.openAmount;
    const remaining = bill.openAmount.minus(app.amount);
    bill.openAmount = remaining.isNegative() ? Money.zero(this.currency) : remaining;
    if (bill.openAmount.isZero()) {
      bill.status = "PAID";
      bill.settledDate = app.date;
    } else {
      bill.status = "PARTIAL";
    }
    this.log.push({
      id: `${bill.id}#pay-${++this.evSeq}`,
      kind: "PAYMENT",
      billId: bill.id,
      vendorId: bill.vendorId,
      date: app.date,
      amount: before.minus(bill.openAmount),
    });
    return this.freeze(bill);
  }

  events(): readonly APEvent[] {
    return [...this.log];
  }

  get(id: string): APBill | undefined {
    const b = this.bills.get(id);
    return b ? this.freeze(b) : undefined;
  }

  all(): readonly APBill[] {
    return [...this.bills.values()].map((b) => this.freeze(b));
  }

  openBills(): readonly APBill[] {
    return this.all().filter((b) => b.openAmount.isPositive());
  }

  controlBalance(): Money {
    return sumMoney(
      [...this.bills.values()].map((b) => b.openAmount),
      this.currency,
    );
  }

  aging(asOf: string): Aging {
    return ageItems(
      this.openBills().map((b) => ({ dueDate: b.dueDate, openAmount: b.openAmount })),
      asOf,
      this.currency,
    );
  }

  private freeze(b: MutableBill): APBill {
    return Object.freeze({ ...b });
  }
}
