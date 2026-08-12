import { asAccountId, asIdempotencyKey, asPeriodKey, asTenantId } from "@rgnr8/ledger-kernel";
import type {
  AccountId,
  Currency,
  JournalLineInput,
  PostCommand,
  Provenance,
} from "@rgnr8/ledger-kernel";
import type { AREvent, APEvent } from "./types.js";

/** GL accounts the subledger posts to. */
export interface SubledgerAccounts {
  readonly arControl: AccountId;
  readonly apControl: AccountId;
  readonly revenue: AccountId;
  readonly expense: AccountId;
  readonly cash: AccountId;
  readonly badDebt: AccountId;
  readonly mappingVersion?: string;
}

export function defaultSubledgerAccounts(prefix = "gl."): SubledgerAccounts {
  return {
    arControl: asAccountId(`${prefix}ar_control`),
    apControl: asAccountId(`${prefix}ap_control`),
    revenue: asAccountId(`${prefix}revenue`),
    expense: asAccountId(`${prefix}expense`),
    cash: asAccountId(`${prefix}cash`),
    badDebt: asAccountId(`${prefix}bad_debt`),
    mappingVersion: "sub-map-1",
  };
}

export interface PostingContext {
  readonly tenantId: string;
  readonly currency: Currency;
}

function command(
  ctx: PostingContext,
  eventId: string,
  kind: string,
  date: string,
  memo: string,
  debit: AccountId,
  credit: AccountId,
  amount: JournalLineInput["amount"],
  map: SubledgerAccounts,
): PostCommand {
  const provenance: Provenance = {
    sourceSystem: "subledger",
    sourceObject: kind,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: date,
    normalizationVersion: "sub-1",
    mappingVersion: map.mappingVersion ?? "sub-map-1",
  };
  return {
    tenantId: asTenantId(ctx.tenantId),
    idempotencyKey: asIdempotencyKey(`sub:${eventId}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo,
    provenance,
    lines: [
      { accountId: debit, side: "DEBIT", amount },
      { accountId: credit, side: "CREDIT", amount },
    ],
  };
}

/**
 * Map AR subledger events to balanced GL journals:
 *   INVOICE  → Dr AR control / Cr Revenue
 *   PAYMENT  → Dr Cash       / Cr AR control
 *   WRITEOFF → Dr Bad debt   / Cr AR control
 */
export function toARPostingCommands(
  events: readonly AREvent[],
  map: SubledgerAccounts,
  ctx: PostingContext,
): PostCommand[] {
  return events.map((e) => {
    switch (e.kind) {
      case "INVOICE":
        return command(ctx, e.id, "ar.invoice", e.date, `Invoice ${e.invoiceId}`, map.arControl, map.revenue, e.amount, map);
      case "PAYMENT":
        return command(ctx, e.id, "ar.payment", e.date, `Receipt on ${e.invoiceId}`, map.cash, map.arControl, e.amount, map);
      case "WRITEOFF":
        return command(ctx, e.id, "ar.writeoff", e.date, `Write-off ${e.invoiceId}`, map.badDebt, map.arControl, e.amount, map);
    }
  });
}

/**
 * Map AP subledger events to balanced GL journals:
 *   BILL    → Dr Expense    / Cr AP control
 *   PAYMENT → Dr AP control / Cr Cash
 */
export function toAPPostingCommands(
  events: readonly APEvent[],
  map: SubledgerAccounts,
  ctx: PostingContext,
): PostCommand[] {
  return events.map((e) => {
    switch (e.kind) {
      case "BILL":
        return command(ctx, e.id, "ap.bill", e.date, `Bill ${e.billId}`, map.expense, map.apControl, e.amount, map);
      case "PAYMENT":
        return command(ctx, e.id, "ap.payment", e.date, `Payment on ${e.billId}`, map.apControl, map.cash, e.amount, map);
    }
  });
}
