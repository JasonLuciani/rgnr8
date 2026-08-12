import type { CanonicalTransaction } from "@rgnr8/ingestion";
import type { BookItem } from "./types.js";

/**
 * Bridge ingestion output to reconciliation book items for one account.
 *
 * Canonical transactions already use the "+ = into the account" convention, so
 * they map directly. Internal transfers are kept (a transfer leg still appears
 * on that account's statement). Pass `pipeline.active()` so superseded pending
 * rows are already excluded.
 */
export function bookItemsFromCanonical(
  txns: readonly CanonicalTransaction[],
  accountId: string,
): BookItem[] {
  return txns
    .filter((t) => t.accountId === accountId)
    .map((t) => ({
      id: t.id,
      date: t.date,
      amount: t.amount,
      description: t.description,
      ...(t.externalId ? { externalId: t.externalId } : {}),
    }));
}
