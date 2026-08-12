import type { BookItem, MatchPair, StatementLine } from "./types.js";

function dayNumber(iso: string): number {
  return Math.floor(Date.parse(iso.slice(0, 10) + "T00:00:00Z") / 86_400_000);
}

function sameAmount(a: StatementLine, b: BookItem): boolean {
  return a.amount.currency.code === b.amount.currency.code && a.amount.minorUnits === b.amount.minorUnits;
}

export interface MatchResult {
  matched: MatchPair[];
  unmatchedStatement: StatementLine[];
  unmatchedBook: BookItem[];
}

/**
 * Match statement lines to book items. First by provider external id (exact),
 * then greedily by equal amount within a date window. Deterministic: candidates
 * are considered in ascending date, then id order.
 */
export function matchItems(
  statementLines: readonly StatementLine[],
  bookItems: readonly BookItem[],
  windowDays = 4,
): MatchResult {
  const stmt = [...statementLines].sort(sortByDateId);
  const book = [...bookItems].sort(sortByDateId);
  const matched: MatchPair[] = [];
  const bookUsed = new Set<string>();

  const bookByExt = new Map<string, BookItem>();
  for (const b of book) if (b.externalId) bookByExt.set(b.externalId, b);

  const stmtRemaining: StatementLine[] = [];

  // Pass 1: external id.
  for (const s of stmt) {
    if (s.externalId) {
      const b = bookByExt.get(s.externalId);
      if (b && !bookUsed.has(b.id)) {
        matched.push({ statement: s, book: b, method: "external_id" });
        bookUsed.add(b.id);
        continue;
      }
    }
    stmtRemaining.push(s);
  }

  // Pass 2: equal amount within date window.
  const stmtUnmatched: StatementLine[] = [];
  for (const s of stmtRemaining) {
    let picked: BookItem | undefined;
    for (const b of book) {
      if (bookUsed.has(b.id)) continue;
      if (!sameAmount(s, b)) continue;
      if (Math.abs(dayNumber(s.date) - dayNumber(b.date)) > windowDays) continue;
      picked = b;
      break;
    }
    if (picked) {
      matched.push({ statement: s, book: picked, method: "amount_date" });
      bookUsed.add(picked.id);
    } else {
      stmtUnmatched.push(s);
    }
  }

  const bookUnmatched = book.filter((b) => !bookUsed.has(b.id));
  return { matched, unmatchedStatement: stmtUnmatched, unmatchedBook: bookUnmatched };
}

function sortByDateId(a: { date: string; id: string }, b: { date: string; id: string }): number {
  return a.date < b.date ? -1 : a.date > b.date ? 1 : a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
}
