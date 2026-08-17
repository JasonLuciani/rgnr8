import type { AccountId, PostCommand } from "@rgnr8/ledger-kernel";

import {
  anchoredCommand,
  resolveLines,
  type DocPostContext,
  type DocumentLine,
  type ResolveOptions,
} from "./documents.js";

/**
 * The rest of QBO's posting documents beyond invoice/bill. Each is a line-item
 * document anchored to one account; they reuse the same balanced-posting shape:
 *
 *  - **Sales Receipt** (cash sale, no AR): Dr Cash/Undeposited / Cr income lines
 *  - **Credit Memo** (customer credit, reduces AR): Dr income lines / Cr AR-control
 *  - **Refund Receipt** (cash back to a customer): Dr income lines / Cr Cash
 *  - **Vendor Credit** (reduces AP): Dr AP-control / Cr expense lines
 *  - **Check / Expense** (cash purchase, no bill): Dr expense lines / Cr Cash
 *  - **Deposit** (money into a bank account): Dr Bank / Cr source (income/undeposited)
 */

/** A customer-side line-item document (sales receipt, credit memo, refund). */
export interface CustomerDoc {
  readonly id: string;
  readonly customerId: string;
  readonly date: string; // ISO
  readonly lines: readonly DocumentLine[];
  readonly memo?: string;
}

/** A vendor/cash-out line-item document (vendor credit, check, expense). */
export interface VendorDoc {
  readonly id: string;
  readonly vendorId?: string;
  readonly date: string; // ISO
  readonly lines: readonly DocumentLine[];
  readonly memo?: string;
}

/** A deposit into a bank account, sourced from income / undeposited funds lines. */
export interface DepositDoc {
  readonly id: string;
  readonly date: string; // ISO
  readonly lines: readonly DocumentLine[];
  readonly memo?: string;
}

export function salesReceiptToPostCommand(
  doc: CustomerDoc,
  cashAccount: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveLines(doc.lines, "income", ctx.currency, opts);
  return anchoredCommand(`salesreceipt:${doc.id}`, doc.date, { accountId: cashAccount, side: "DEBIT" }, lines, ctx, doc.memo);
}

export function creditMemoToPostCommand(
  doc: CustomerDoc,
  arControl: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveLines(doc.lines, "income", ctx.currency, opts);
  // reduces AR: Cr AR-control (total) / Dr income lines (reverse of an invoice)
  return anchoredCommand(`creditmemo:${doc.id}`, doc.date, { accountId: arControl, side: "CREDIT" }, lines, ctx, doc.memo);
}

export function refundReceiptToPostCommand(
  doc: CustomerDoc,
  cashAccount: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveLines(doc.lines, "income", ctx.currency, opts);
  // cash back to the customer: Cr Cash (total) / Dr income lines
  return anchoredCommand(`refund:${doc.id}`, doc.date, { accountId: cashAccount, side: "CREDIT" }, lines, ctx, doc.memo);
}

export function vendorCreditToPostCommand(
  doc: VendorDoc,
  apControl: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveLines(doc.lines, "expense", ctx.currency, opts);
  // reduces AP: Dr AP-control (total) / Cr expense lines (reverse of a bill)
  return anchoredCommand(`vendorcredit:${doc.id}`, doc.date, { accountId: apControl, side: "DEBIT" }, lines, ctx, doc.memo);
}

/** A check or card expense that pays cash directly (no bill): Dr expense / Cr cash. */
export function expenseToPostCommand(
  doc: VendorDoc,
  cashAccount: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveLines(doc.lines, "expense", ctx.currency, opts);
  return anchoredCommand(`expense:${doc.id}`, doc.date, { accountId: cashAccount, side: "CREDIT" }, lines, ctx, doc.memo);
}

/** A deposit into a bank account: Dr Bank (total) / Cr source lines (income/undeposited). */
export function depositToPostCommand(
  doc: DepositDoc,
  bankAccount: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveLines(doc.lines, "income", ctx.currency, opts);
  return anchoredCommand(`deposit:${doc.id}`, doc.date, { accountId: bankAccount, side: "DEBIT" }, lines, ctx, doc.memo);
}
