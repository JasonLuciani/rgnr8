import type { Money } from "@rgnr8/ledger-kernel";

/**
 * A raw payload exactly as received from a provider, archived append-only.
 * External deletion becomes a tombstone; the raw record is never mutated.
 */
export interface RawRecord {
  readonly provider: string;
  /** The financial account this payload concerns (bank/card/payroll account). */
  readonly accountId: string;
  /** Provider's stable id for the record, if any (used for idempotency). */
  readonly externalId: string;
  /** Opaque provider payload. */
  readonly payload: unknown;
  /** When RGNR8 fetched it (ISO string). Supplied by the caller, not generated. */
  readonly fetchedAt: string;
  readonly sourceVersion: string;
}

export type TransactionStatus = "PENDING" | "POSTED";

export type Direction = "INFLOW" | "OUTFLOW";

export enum TransactionKind {
  DEPOSIT = "DEPOSIT",
  PURCHASE = "PURCHASE",
  FEE = "FEE",
  INTEREST = "INTEREST",
  REFUND = "REFUND",
  PAYROLL_NET = "PAYROLL_NET",
  PAYROLL_TAX = "PAYROLL_TAX",
  TRANSFER = "TRANSFER",
  OTHER = "OTHER",
}

/** Provenance carried on every normalized/canonical record. */
export interface SourceRef {
  readonly provider: string;
  readonly sourceType: string; // e.g. "bank.transaction", "payroll.run"
  readonly sourceId: string; // provider externalId
  readonly sourceVersion: string;
  readonly fetchedAt: string;
}

/**
 * Adapter output: a provider-agnostic record, before the pipeline assigns an id,
 * dedupe key, transfer flag, or supersession link.
 */
export interface NormalizedInput {
  readonly tenantId: string;
  readonly accountId: string;
  readonly externalId: string;
  readonly status: TransactionStatus;
  /** Effective date (posted date if posted, else authorized/pending date), ISO. */
  readonly date: string;
  /** Signed amount: positive = money into the account, negative = money out. */
  readonly amount: Money;
  readonly description: string;
  readonly counterparty?: string;
  readonly kind: TransactionKind;
  /** Provider's id of the PENDING record this POSTED record replaces, if any. */
  readonly supersedesExternalId?: string;
  readonly source: SourceRef;
}

/**
 * The normalized, deduplicated, classified transaction — the unit downstream
 * services (ledger, forecast) consume.
 */
export interface CanonicalTransaction {
  readonly id: string;
  readonly tenantId: string;
  readonly accountId: string;
  readonly externalId: string;
  readonly status: TransactionStatus;
  readonly date: string;
  readonly amount: Money; // signed (negative = out of account)
  readonly direction: Direction;
  readonly description: string;
  readonly counterparty?: string;
  readonly kind: TransactionKind;
  readonly isInternalTransfer: boolean;
  readonly dedupeKey: string;
  readonly source: SourceRef;
  /** Set when a POSTED record replaced an earlier PENDING one. */
  readonly supersedes?: string;
  /** Set on a PENDING record once a POSTED record supersedes it. */
  readonly supersededBy?: string;
}

/** An adapter turns a raw provider payload into one or more normalized records. */
export interface ProviderAdapter {
  readonly provider: string;
  normalize(raw: RawRecord): NormalizedInput[];
}

export interface IngestIssue {
  readonly kind: string;
  readonly message: string;
  readonly accountId?: string;
  readonly externalId?: string;
}

export interface IngestResult {
  /** Newly added canonical transactions (not counting idempotent duplicates). */
  readonly added: readonly CanonicalTransaction[];
  readonly archived: number;
  readonly duplicates: number;
  readonly pendingSuperseded: number;
  readonly transfersDetected: number;
  readonly issues: readonly IngestIssue[];
}
