import {
  Money,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  type AccountId,
  type JournalLineInput,
  type PostCommand,
  type Provenance,
} from "@rgnr8/ledger-kernel";

import type { Item, MasterDataStore } from "./masterdata.js";

/**
 * Line-item source documents — the layer QBO has and the scalar subledger lacked.
 * An invoice/bill is now a set of lines (item, quantity, unit price, GL account),
 * so revenue and expense post to the *right* accounts per line, and the document
 * total is the sum of its lines. Each document posts a balanced journal entry:
 *
 *  - **Invoice**: Dr AR-control (total) / Cr each line's income account (line total)
 *  - **Bill**:    Cr AP-control (total) / Dr each line's expense account (line total)
 *
 * Amounts are exact integer `Money`; quantity is an integer multiplier.
 */

export interface DocumentLine {
  /** Optional catalog item — its default account/price fill in unset fields. */
  readonly itemId?: string;
  readonly description?: string;
  /** Whole units (default 1). */
  readonly quantity?: number;
  /** Price per unit. Required unless resolvable from the item's unitPrice. */
  readonly unitAmount?: Money;
  /** GL account this line codes to (income for an invoice, expense for a bill). */
  readonly accountId?: AccountId;
}

export interface InvoiceDoc {
  readonly id: string;
  readonly customerId: string;
  readonly issueDate: string; // ISO
  readonly dueDate: string; // ISO
  readonly lines: readonly DocumentLine[];
  readonly memo?: string;
}

export interface BillDoc {
  readonly id: string;
  readonly vendorId: string;
  readonly billDate: string; // ISO
  readonly dueDate: string; // ISO
  readonly lines: readonly DocumentLine[];
  readonly memo?: string;
}

export interface ResolvedLine {
  readonly description: string;
  readonly quantity: number;
  readonly unitAmount: Money;
  readonly amount: Money; // unitAmount * quantity
  readonly accountId: AccountId;
}

export class DocumentError extends Error {}

function resolveLine(
  line: DocumentLine,
  side: "income" | "expense",
  currency: Money["currency"],
  items?: { tenant: string; store: MasterDataStore },
): ResolvedLine {
  let item: Item | undefined;
  if (line.itemId !== undefined && items !== undefined) {
    item = items.store.getItem(items.tenant, line.itemId);
  }
  const unitAmount = line.unitAmount ?? item?.unitPrice;
  if (unitAmount === undefined) {
    throw new DocumentError(`Line has no unitAmount and item has no unitPrice`);
  }
  const accountId =
    line.accountId ??
    (side === "income" ? item?.incomeAccountId : item?.expenseAccountId);
  if (accountId === undefined) {
    throw new DocumentError(`Line has no accountId and item has no default ${side} account`);
  }
  if (unitAmount.currency.code !== currency.code) {
    throw new DocumentError(`Line currency ${unitAmount.currency.code} != ${currency.code}`);
  }
  const quantity = line.quantity ?? 1;
  if (!Number.isInteger(quantity) || quantity <= 0) {
    throw new DocumentError(`Line quantity must be a positive integer, got ${quantity}`);
  }
  return {
    description: line.description ?? item?.name ?? "",
    quantity,
    unitAmount,
    amount: unitAmount.timesInteger(quantity),
    accountId,
  };
}

export interface ResolveOptions {
  /** Master data store + tenant, to fill line defaults from items. */
  readonly items?: { tenant: string; store: MasterDataStore };
}

/** Resolve a document's lines (fill item defaults, compute amounts). */
export function resolveInvoiceLines(
  doc: InvoiceDoc,
  currency: Money["currency"],
  opts: ResolveOptions = {},
): readonly ResolvedLine[] {
  if (doc.lines.length === 0) throw new DocumentError(`Invoice ${doc.id} has no lines`);
  return doc.lines.map((l) => resolveLine(l, "income", currency, opts.items));
}

export function resolveBillLines(
  doc: BillDoc,
  currency: Money["currency"],
  opts: ResolveOptions = {},
): readonly ResolvedLine[] {
  if (doc.lines.length === 0) throw new DocumentError(`Bill ${doc.id} has no lines`);
  return doc.lines.map((l) => resolveLine(l, "expense", currency, opts.items));
}

export function sumLines(lines: readonly ResolvedLine[], currency: Money["currency"]): Money {
  return lines.reduce((acc, l) => acc.plus(l.amount), Money.zero(currency));
}

export interface DocPostContext {
  readonly tenantId: string;
  readonly currency: Money["currency"];
  readonly provenance: Provenance;
  readonly mappingVersion?: string;
}

/** An invoice → Dr AR-control (total) / Cr each line's income account. */
export function invoiceToPostCommand(
  doc: InvoiceDoc,
  arControl: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveInvoiceLines(doc, ctx.currency, opts);
  const total = sumLines(lines, ctx.currency);
  const journal: JournalLineInput[] = [
    { accountId: arControl, side: "DEBIT", amount: total },
    ...lines.map((l): JournalLineInput => ({ accountId: l.accountId, side: "CREDIT", amount: l.amount, memo: l.description })),
  ];
  return command(`invoice:${doc.id}`, doc.issueDate, journal, ctx, doc.memo);
}

/** A bill → Cr AP-control (total) / Dr each line's expense account. */
export function billToPostCommand(
  doc: BillDoc,
  apControl: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveBillLines(doc, ctx.currency, opts);
  const total = sumLines(lines, ctx.currency);
  const journal: JournalLineInput[] = [
    ...lines.map((l): JournalLineInput => ({ accountId: l.accountId, side: "DEBIT", amount: l.amount, memo: l.description })),
    { accountId: apControl, side: "CREDIT", amount: total },
  ];
  return command(`bill:${doc.id}`, doc.billDate, journal, ctx, doc.memo);
}

function command(
  idKey: string,
  entryDate: string,
  lines: JournalLineInput[],
  ctx: DocPostContext,
  memo: string | undefined,
): PostCommand {
  return {
    tenantId: asTenantId(ctx.tenantId),
    idempotencyKey: asIdempotencyKey(idKey),
    periodKey: asPeriodKey(entryDate.slice(0, 7)),
    currency: ctx.currency,
    entryDate,
    provenance: { ...ctx.provenance, mappingVersion: ctx.mappingVersion ?? ctx.provenance.mappingVersion },
    lines,
    ...(memo !== undefined ? { memo } : {}),
  };
}
