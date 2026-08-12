import type { Money } from "@rgnr8/ledger-kernel";

/** One line on a bank/card statement. Amount is signed: + = into the account. */
export interface StatementLine {
  readonly id: string;
  readonly date: string; // ISO
  readonly amount: Money; // signed
  readonly description: string;
  readonly externalId?: string;
}

/** A statement to reconcile against — with its own opening/closing balances. */
export interface Statement {
  readonly accountId: string;
  readonly periodStart: string;
  readonly periodEnd: string;
  readonly openingBalance: Money;
  readonly closingBalance: Money;
  readonly lines: readonly StatementLine[];
}

/** A book-side item (from the ledger / ingested feed) for the same account. */
export interface BookItem {
  readonly id: string;
  readonly date: string;
  readonly amount: Money; // signed, same convention as StatementLine
  readonly description: string;
  readonly externalId?: string;
}

export type MatchMethod = "external_id" | "amount_date";

export interface MatchPair {
  readonly statement: StatementLine;
  readonly book: BookItem;
  readonly method: MatchMethod;
}

/**
 * How an unmatched item is explained. TIMING (in-transit) is benign; the rest
 * are reconciling problems that must be resolved before the account is clean.
 */
export enum DifferenceCategory {
  TIMING = "TIMING", // in transit — booked but not yet on statement, or vice-versa
  MISSING_SOURCE = "MISSING_SOURCE", // on the bank but not in our books — must be booked
  MAPPING = "MAPPING", // booked to the wrong account
  POSTING_ERROR = "POSTING_ERROR", // a book entry that shouldn't exist
  LEGACY_ERROR = "LEGACY_ERROR", // pre-existing error in migrated books
  RGNR8_DEFECT = "RGNR8_DEFECT", // our own bug
}

export type ItemSide = "STATEMENT" | "BOOK";

export interface ClassifiedItem {
  readonly side: ItemSide;
  readonly id: string;
  readonly date: string;
  readonly amount: Money;
  readonly description: string;
  readonly category: DifferenceCategory;
  readonly note: string;
}

export type ReconStatus = "BALANCED" | "OUT_OF_BALANCE";

export interface SignOff {
  readonly owner: string;
  readonly reviewer: string;
  readonly completedAt: string; // ISO, supplied by the caller
}

export interface Reconciliation {
  readonly accountId: string;
  readonly periodStart: string;
  readonly periodEnd: string;
  readonly status: ReconStatus;
  readonly statementOpening: Money;
  readonly statementClosing: Money;
  /** Statement opening + all book items (our books' ending cash for the account). */
  readonly bookClosing: Money;
  /** bookClosing − statementClosing (0 when fully reconciled excluding timing). */
  readonly difference: Money;
  /** Residual after removing benign in-transit timing items; 0 when clean. */
  readonly unreconciledResidual: Money;
  readonly matched: readonly MatchPair[];
  readonly unmatchedStatement: readonly ClassifiedItem[];
  readonly unmatchedBook: readonly ClassifiedItem[];
  readonly statementConsistent: boolean;
  readonly notes: readonly string[];
  readonly signOff?: SignOff;
}
