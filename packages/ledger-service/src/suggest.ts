import type { FeedRuleRecord, FeedTxnRecord } from "./feed.js";

/**
 * What account should this bank line be categorized to?
 *
 * Two layers, in strict precedence:
 *
 * 1. **Rules** — hand-authored by the owner or their bookkeeper. A rule hit is
 *    treated as certain (confidence 1.0), because somebody stated it outright.
 * 2. **Learned history** — what this tenant actually chose the last N times a
 *    line like this came through. Confidence is the empirical share of the
 *    winning account for that key, so eight-of-eight reads 1.0 and six-of-ten
 *    reads 0.6.
 *
 * With neither, the answer is an explicit *no suggestion* at confidence 0 — the
 * signal that a human still has to look at it. Never a silent default: guessing
 * "Office Supplies" because nothing matched is how books end up meaningless.
 *
 * The description normalizer strips digits and punctuation so that "SQ *COFFEE
 * 1234" and "SQ *COFFEE 9987" collapse to one key and reinforce a single signal
 * instead of splintering. This mirrors `rgnr8_categorize.learn` on the Python
 * side; the parity is asserted in tests.
 */

export type SuggestionSource = "rule" | "learned" | "none";

export interface Suggestion {
  readonly accountCode: string;
  readonly confidence: number;
  readonly reason: string;
  readonly source: SuggestionSource;
  /** The rule that fired, when one did — so the UI can offer to edit it. */
  readonly ruleId: string;
  /** Whether that rule says to post without review. */
  readonly autoPost: boolean;
}

export const NO_SUGGESTION: Suggestion = Object.freeze({
  accountCode: "",
  confidence: 0,
  reason: "No rule matched and nothing like this has been categorized before",
  source: "none",
  ruleId: "",
  autoPost: false,
});

/** Lowercase, drop digits and punctuation, collapse whitespace. */
export function normalizeDescription(text: string): string {
  return text
    .toLowerCase()
    .replace(/[0-9]+/g, " ")
    .replace(/[^a-z\s]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function normalizeParty(text: string): string {
  return text.toLowerCase().trim();
}

function isInflow(amountMinor: string): boolean {
  return !amountMinor.trim().startsWith("-");
}

/** Does this rule match this line? Every predicate it declares must hold. */
export function ruleMatches(rule: FeedRuleRecord, txn: FeedTxnRecord): boolean {
  if (rule.descriptionContains) {
    const needle = rule.descriptionContains.toLowerCase();
    if (!txn.description.toLowerCase().includes(needle)) return false;
  }
  if (rule.counterpartyEquals) {
    if (normalizeParty(txn.counterparty) !== normalizeParty(rule.counterpartyEquals)) return false;
  }
  if (rule.sign === "in" && !isInflow(txn.amountMinor)) return false;
  if (rule.sign === "out" && isInflow(txn.amountMinor)) return false;
  // A rule with no predicates at all would match everything — refuse to honour it.
  return Boolean(rule.descriptionContains || rule.counterpartyEquals || rule.sign);
}

interface Tally {
  readonly counts: Map<string, number>;
  total: number;
}

/**
 * What the tenant has already decided, indexed two ways: by counterparty (a
 * named vendor is a strong signal) and by normalized description (the fallback
 * when the feed gives no counterparty).
 */
export class LearnedModel {
  private readonly byParty = new Map<string, Tally>();
  private readonly byDescription = new Map<string, Tally>();

  constructor(history: readonly FeedTxnRecord[]) {
    for (const t of history) {
      if (!t.categoryCode) continue;
      if (t.status !== "POSTED" && t.status !== "MATCHED") continue;
      const party = normalizeParty(t.counterparty);
      if (party) this.record(this.byParty, party, t.categoryCode);
      const desc = normalizeDescription(t.description);
      if (desc) this.record(this.byDescription, desc, t.categoryCode);
    }
  }

  private record(index: Map<string, Tally>, key: string, category: string): void {
    let tally = index.get(key);
    if (!tally) {
      tally = { counts: new Map(), total: 0 };
      index.set(key, tally);
    }
    tally.counts.set(category, (tally.counts.get(category) ?? 0) + 1);
    tally.total += 1;
  }

  /** Winner and its empirical share. Ties break by account code, ascending. */
  private best(tally: Tally | undefined): { code: string; share: number; seen: number } | undefined {
    if (!tally || tally.total === 0) return undefined;
    let bestCode = "";
    let bestCount = -1;
    for (const [code, count] of [...tally.counts].sort((a, b) => a[0].localeCompare(b[0]))) {
      if (count > bestCount) {
        bestCode = code;
        bestCount = count;
      }
    }
    return { code: bestCode, share: bestCount / tally.total, seen: tally.total };
  }

  suggest(txn: FeedTxnRecord): Suggestion | undefined {
    const party = normalizeParty(txn.counterparty);
    const partyHit = party ? this.best(this.byParty.get(party)) : undefined;
    const hit = partyHit ?? this.best(this.byDescription.get(normalizeDescription(txn.description)));
    if (!hit || !hit.code) return undefined;
    const what = partyHit ? txn.counterparty : "lines like this";
    const times = hit.seen === 1 ? "once" : `${hit.seen} times`;
    return {
      accountCode: hit.code,
      confidence: hit.share,
      reason: `You've categorized ${what} this way before (${times})`,
      source: "learned",
      ruleId: "",
      autoPost: false,
    };
  }
}

/**
 * The suggestion for one line: rules first (ordered by priority), then learned
 * history, then nothing.
 */
export function suggestFor(
  txn: FeedTxnRecord,
  rules: readonly FeedRuleRecord[],
  learned: LearnedModel,
): Suggestion {
  for (const rule of rules) {
    if (!ruleMatches(rule, txn)) continue;
    return {
      accountCode: rule.accountCode,
      confidence: 1,
      reason: `Rule "${rule.id}" matched`,
      source: "rule",
      ruleId: rule.id,
      autoPost: rule.autoPost,
    };
  }
  return learned.suggest(txn) ?? NO_SUGGESTION;
}
