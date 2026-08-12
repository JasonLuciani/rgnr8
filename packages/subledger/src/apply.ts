import type { Money } from "@rgnr8/ledger-kernel";
import type { APSubledger } from "./ap.js";
import type { ARSubledger } from "./ar.js";

/** A cash movement to match against open documents (from the ingested feed). */
export interface CashItem {
  readonly id: string;
  readonly date: string; // ISO
  readonly amount: Money; // positive magnitude
  readonly description: string;
  readonly counterparty?: string;
}

export type MatchReason = "reference" | "counterparty_amount";

export interface AppliedMatch {
  readonly cashId: string;
  readonly documentId: string;
  readonly amount: Money;
  readonly reason: MatchReason;
}

export interface ApplyResult {
  readonly applied: readonly AppliedMatch[];
  readonly unmatched: readonly string[]; // cash ids that found no confident match
}

/** Maps a bank counterparty label to a subledger party id (customer or vendor). */
export type PartyAliases = Readonly<Record<string, string>>;

interface OpenDoc {
  readonly id: string;
  readonly partyId: string;
  readonly openAmount: Money;
}

/**
 * Deterministic cash matching. Applies a cash item to an open document when:
 *   1. the document id appears in the cash description (a remittance reference), or
 *   2. the counterparty maps to the document's party AND the amount exactly
 *      equals a single open document's balance.
 * Ambiguous or unmatched items are returned for manual review — never guessed.
 */
function match(
  items: readonly CashItem[],
  openDocs: readonly OpenDoc[],
  aliases: PartyAliases,
): { applied: AppliedMatch[]; unmatched: string[] } {
  const applied: AppliedMatch[] = [];
  const unmatched: string[] = [];
  const consumed = new Set<string>();

  const remaining = () => openDocs.filter((d) => !consumed.has(d.id));

  for (const item of items) {
    // 1) explicit reference in the description
    const desc = item.description.toLowerCase();
    const byRef = remaining().find((d) => desc.includes(d.id.toLowerCase()));
    if (byRef) {
      applied.push({ cashId: item.id, documentId: byRef.id, amount: item.amount, reason: "reference" });
      consumed.add(byRef.id);
      continue;
    }

    // 2) counterparty + exact single open-amount match
    const partyId = item.counterparty ? aliases[item.counterparty] : undefined;
    if (partyId) {
      const candidates = remaining().filter(
        (d) => d.partyId === partyId && d.openAmount.minorUnits === item.amount.minorUnits,
      );
      if (candidates.length === 1) {
        const doc = candidates[0]!;
        applied.push({ cashId: item.id, documentId: doc.id, amount: item.amount, reason: "counterparty_amount" });
        consumed.add(doc.id);
        continue;
      }
    }

    unmatched.push(item.id);
  }

  return { applied, unmatched };
}

/** Match customer receipts to open invoices and apply them to the AR subledger. */
export function applyReceiptsToAR(
  ar: ARSubledger,
  receipts: readonly CashItem[],
  aliases: PartyAliases = {},
): ApplyResult {
  const open: OpenDoc[] = ar.openInvoices().map((i) => ({ id: i.id, partyId: i.customerId, openAmount: i.openAmount }));
  const { applied, unmatched } = match(receipts, open, aliases);
  for (const m of applied) {
    ar.applyPayment({ documentId: m.documentId, date: dateOf(receipts, m.cashId), amount: m.amount });
  }
  return { applied, unmatched };
}

/** Match vendor payments to open bills and apply them to the AP subledger. */
export function applyPaymentsToAP(
  ap: APSubledger,
  payments: readonly CashItem[],
  aliases: PartyAliases = {},
): ApplyResult {
  const open: OpenDoc[] = ap.openBills().map((b) => ({ id: b.id, partyId: b.vendorId, openAmount: b.openAmount }));
  const { applied, unmatched } = match(payments, open, aliases);
  for (const m of applied) {
    ap.applyPayment({ documentId: m.documentId, date: dateOf(payments, m.cashId), amount: m.amount });
  }
  return { applied, unmatched };
}

function dateOf(items: readonly CashItem[], id: string): string {
  return items.find((i) => i.id === id)?.date ?? "";
}
