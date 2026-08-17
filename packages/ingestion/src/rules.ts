import type { AccountId } from "@rgnr8/ledger-kernel";

import type { CanonicalTransaction } from "./types.js";
import type { Direction, TransactionKind } from "./types.js";

/**
 * Categorization rules — how a bank/card feed line gets coded to the *right* GL
 * account instead of one catch-all "expense". A rule matches on the transaction's
 * text/counterparty/direction/kind/amount and names the account (and optional
 * memo/category) to use. Rules are evaluated by ascending `priority` (lower wins),
 * so specific rules can override general ones; the first match applies.
 *
 * This is the "memory" QBO's bank feed has: once you tell it "STARBUCKS →
 * Meals", every future match codes itself. The `RuleSet` here is deterministic
 * and pure; persistence (per-tenant remembered rules) layers on top.
 */

export interface RuleMatch {
  /** Case-insensitive substring of the description. */
  readonly descriptionContains?: string;
  /** Exact (case-insensitive) counterparty. */
  readonly counterpartyEquals?: string;
  readonly direction?: Direction;
  readonly kind?: TransactionKind;
  /** Inclusive bounds on the *absolute* amount, in minor units. */
  readonly minAmountMinor?: bigint;
  readonly maxAmountMinor?: bigint;
}

export interface CategoryRule {
  readonly id: string;
  /** Lower is evaluated first; ties break on insertion order. */
  readonly priority: number;
  readonly match: RuleMatch;
  /** GL account a matching transaction codes to. */
  readonly accountId: AccountId;
  /** Optional human category label + memo carried onto the entry. */
  readonly category?: string;
  readonly memo?: string;
}

function matches(rule: CategoryRule, t: CanonicalTransaction): boolean {
  const m = rule.match;
  if (m.direction !== undefined && t.direction !== m.direction) return false;
  if (m.kind !== undefined && t.kind !== m.kind) return false;
  if (m.counterpartyEquals !== undefined) {
    if ((t.counterparty ?? "").toLowerCase() !== m.counterpartyEquals.toLowerCase()) return false;
  }
  if (m.descriptionContains !== undefined) {
    if (!t.description.toLowerCase().includes(m.descriptionContains.toLowerCase())) return false;
  }
  const abs = t.amount.minorUnits < 0n ? -t.amount.minorUnits : t.amount.minorUnits;
  if (m.minAmountMinor !== undefined && abs < m.minAmountMinor) return false;
  if (m.maxAmountMinor !== undefined && abs > m.maxAmountMinor) return false;
  return true;
}

/** An ordered set of categorization rules. Pure + deterministic. */
export class RuleSet {
  private readonly rules: readonly CategoryRule[];

  constructor(rules: readonly CategoryRule[] = []) {
    // stable sort by priority (ascending), preserving insertion order on ties
    this.rules = [...rules]
      .map((r, i) => ({ r, i }))
      .sort((a, b) => a.r.priority - b.r.priority || a.i - b.i)
      .map(({ r }) => r);
  }

  /** The highest-priority rule that matches, or undefined. */
  match(t: CanonicalTransaction): CategoryRule | undefined {
    for (const rule of this.rules) {
      if (matches(rule, t)) return rule;
    }
    return undefined;
  }

  /** The account a transaction should code to per the rules, or undefined. */
  accountFor(t: CanonicalTransaction): AccountId | undefined {
    return this.match(t)?.accountId;
  }

  list(): readonly CategoryRule[] {
    return this.rules;
  }

  withRule(rule: CategoryRule): RuleSet {
    return new RuleSet([...this.rules, rule]);
  }
}
