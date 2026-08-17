/**
 * Multi-entity consolidation.
 *
 * A group with several legal entities reports consolidated financials: sum each
 * account across entities, then post intercompany *eliminations* so the group
 * doesn't double-count internal activity (an intercompany receivable in one
 * entity and the matching payable in another must cancel; intercompany revenue
 * against the other entity's expense must cancel). This combines a set of
 * per-entity trial balances (aligned by account id) and applies balanced
 * elimination adjustments, yielding a consolidated trial balance the existing
 * statement builders consume unchanged.
 *
 * Everything is signed, debit-positive integer `Money`, so a sound group still
 * nets to zero after consolidation.
 */

import { Money } from "@rgnr8/ledger-kernel";
import type { AccountId, Currency } from "@rgnr8/ledger-kernel";
import type { TrialBalance, TrialBalanceEntry } from "./accounts.js";

export interface EntityTrialBalance {
  readonly entityId: string;
  readonly tb: TrialBalance;
}

/** A signed elimination adjustment applied during consolidation. */
export type EliminationLine = TrialBalanceEntry;

export class ConsolidationError extends Error {}

export interface ConsolidationResult {
  readonly consolidated: TrialBalance;
  /** Sum of every entity before eliminations (for the consolidation worksheet). */
  readonly combined: TrialBalance;
  /** The eliminations that were applied. */
  readonly eliminations: readonly EliminationLine[];
  /** True when the consolidated signed balances net to exactly zero. */
  readonly balanced: boolean;
}

function mergeSigned(
  target: Map<AccountId, TrialBalanceEntry>,
  entry: TrialBalanceEntry,
  currency: Currency,
): void {
  const existing = target.get(entry.accountId);
  if (existing) {
    target.set(entry.accountId, { ...existing, signed: existing.signed.plus(entry.signed) });
  } else {
    target.set(entry.accountId, { ...entry, signed: Money.zero(currency).plus(entry.signed) });
  }
}

function toTrialBalance(map: Map<AccountId, TrialBalanceEntry>, currency: Currency): TrialBalance {
  const entries = [...map.values()]
    .filter((e) => !e.signed.isZero())
    .sort((a, b) => a.code.localeCompare(b.code));
  return { currency, entries };
}

/**
 * Consolidate several entities' trial balances with optional intercompany
 * eliminations. Entities must share a currency. Eliminations must themselves be
 * balanced (sum of signed = 0), so consolidation can't create or destroy net
 * value; a non-balanced elimination set is rejected.
 */
export function consolidateTrialBalances(
  entities: readonly EntityTrialBalance[],
  eliminations: readonly EliminationLine[] = [],
): ConsolidationResult {
  if (entities.length === 0) throw new ConsolidationError("no entities to consolidate");
  const currency = entities[0]!.tb.currency;
  for (const e of entities) {
    if (e.tb.currency.code !== currency.code) {
      throw new ConsolidationError(
        `entity ${e.entityId} currency ${e.tb.currency.code} != ${currency.code} (translate first)`,
      );
    }
  }

  // Combine: sum every entity, account by account.
  const combinedMap = new Map<AccountId, TrialBalanceEntry>();
  for (const e of entities) {
    for (const entry of e.tb.entries) mergeSigned(combinedMap, entry, currency);
  }
  const combined = toTrialBalance(new Map(combinedMap), currency);

  // Eliminations must net to zero.
  const elimSum = eliminations.reduce((acc, e) => acc.plus(e.signed), Money.zero(currency));
  if (!elimSum.isZero()) {
    throw new ConsolidationError(
      `eliminations are unbalanced (net ${elimSum.toDecimalString()}); they must sum to zero`,
    );
  }

  // Apply eliminations onto the combined map.
  for (const elim of eliminations) mergeSigned(combinedMap, elim, currency);
  const consolidated = toTrialBalance(combinedMap, currency);

  const netSigned = consolidated.entries.reduce(
    (acc, e) => acc.plus(e.signed),
    Money.zero(currency),
  );

  return {
    consolidated,
    combined,
    eliminations,
    balanced: netSigned.isZero(),
  };
}
