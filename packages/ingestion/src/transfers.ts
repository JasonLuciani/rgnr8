import type { Money } from "@rgnr8/ledger-kernel";
import { TransactionKind } from "./types.js";

/** Minimal shape transfer detection needs (works on the pipeline's mutable rows). */
export interface TransferCandidate {
  id: string;
  accountId: string;
  status: "PENDING" | "POSTED";
  amount: Money;
  date: string;
  kind: TransactionKind;
  isInternalTransfer: boolean;
  superseded?: boolean;
}

function dayNumber(iso: string): number {
  const d = Date.parse(iso.slice(0, 10) + "T00:00:00Z");
  return Math.floor(d / 86_400_000);
}

/**
 * Detect internal transfers: an equal-and-opposite pair of POSTED transactions
 * in two different own-accounts within `windowDays`. Both sides are flagged so
 * they are excluded from net cash and are not booked as income/expense.
 * Returns the number of pairs newly matched.
 */
export function detectInternalTransfers(txns: TransferCandidate[], windowDays = 3): number {
  const open = txns.filter(
    (t) => t.status === "POSTED" && !t.isInternalTransfer && !t.superseded && t.amount.minorUnits !== 0n,
  );
  const matched = new Set<string>();
  let pairs = 0;

  for (let i = 0; i < open.length; i++) {
    const a = open[i]!;
    if (matched.has(a.id)) continue;
    for (let j = i + 1; j < open.length; j++) {
      const b = open[j]!;
      if (matched.has(b.id)) continue;
      if (a.accountId === b.accountId) continue;
      if (a.amount.currency.code !== b.amount.currency.code) continue;
      if (a.amount.minorUnits !== -b.amount.minorUnits) continue;
      if (Math.abs(dayNumber(a.date) - dayNumber(b.date)) > windowDays) continue;

      a.isInternalTransfer = true;
      b.isInternalTransfer = true;
      a.kind = TransactionKind.TRANSFER;
      b.kind = TransactionKind.TRANSFER;
      matched.add(a.id);
      matched.add(b.id);
      pairs++;
      break;
    }
  }
  return pairs;
}
