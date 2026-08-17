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

/** Resolve a bare set of lines for one side (fill item defaults, compute amounts). */
export function resolveLines(
  lines: readonly DocumentLine[],
  side: "income" | "expense",
  currency: Money["currency"],
  opts: ResolveOptions = {},
): readonly ResolvedLine[] {
  if (lines.length === 0) throw new DocumentError(`document has no lines`);
  return lines.map((l) => resolveLine(l, side, currency, opts.items));
}

/** Resolve a document's lines (fill item defaults, compute amounts). */
export function resolveInvoiceLines(
  doc: InvoiceDoc,
  currency: Money["currency"],
  opts: ResolveOptions = {},
): readonly ResolvedLine[] {
  return resolveLines(doc.lines, "income", currency, opts);
}

export function resolveBillLines(
  doc: BillDoc,
  currency: Money["currency"],
  opts: ResolveOptions = {},
): readonly ResolvedLine[] {
  return resolveLines(doc.lines, "expense", currency, opts);
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

/**
 * Build a balanced posting command with a single **anchor** line (the control /
 * cash / bank account) carrying the document total on one side, and the resolved
 * item lines carrying their amounts on the other. This is the shared shape behind
 * every line-item document (invoice, bill, sales receipt, credit memo, refund,
 * vendor credit, check, deposit) — only the anchor account + side differ.
 */
export function anchoredCommand(
  idKey: string,
  entryDate: string,
  anchor: { readonly accountId: AccountId; readonly side: "DEBIT" | "CREDIT" },
  lines: readonly ResolvedLine[],
  ctx: DocPostContext,
  memo?: string,
): PostCommand {
  const total = sumLines(lines, ctx.currency);
  const lineSide: "DEBIT" | "CREDIT" = anchor.side === "DEBIT" ? "CREDIT" : "DEBIT";
  const anchorLine: JournalLineInput = { accountId: anchor.accountId, side: anchor.side, amount: total };
  const itemLines: JournalLineInput[] = lines.map((l) => ({
    accountId: l.accountId,
    side: lineSide,
    amount: l.amount,
    ...(l.description !== "" ? { memo: l.description } : {}),
  }));
  const journal = anchor.side === "DEBIT" ? [anchorLine, ...itemLines] : [...itemLines, anchorLine];
  return command(idKey, entryDate, journal, ctx, memo);
}

/** An invoice → Dr AR-control (total) / Cr each line's income account. */
export function invoiceToPostCommand(
  doc: InvoiceDoc,
  arControl: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveInvoiceLines(doc, ctx.currency, opts);
  return anchoredCommand(`invoice:${doc.id}`, doc.issueDate, { accountId: arControl, side: "DEBIT" }, lines, ctx, doc.memo);
}

/** A bill → Cr AP-control (total) / Dr each line's expense account. */
export function billToPostCommand(
  doc: BillDoc,
  apControl: AccountId,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): PostCommand {
  const lines = resolveBillLines(doc, ctx.currency, opts);
  return anchoredCommand(`bill:${doc.id}`, doc.billDate, { accountId: apControl, side: "CREDIT" }, lines, ctx, doc.memo);
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
