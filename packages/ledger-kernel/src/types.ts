import type { Currency, Money } from "./money.js";

/** Branded id helpers keep account/entry/tenant ids from being mixed up. */
export type Brand<T, B extends string> = T & { readonly __brand: B };
export type TenantId = Brand<string, "TenantId">;
export type AccountId = Brand<string, "AccountId">;
export type EntryId = Brand<string, "EntryId">;
/** Idempotency key: a caller-supplied stable id for exactly-once posting. */
export type IdempotencyKey = Brand<string, "IdempotencyKey">;
/** Period key, e.g. "2026-08" — the accounting period an entry belongs to. */
export type PeriodKey = Brand<string, "PeriodKey">;

export const asTenantId = (s: string): TenantId => s as TenantId;
export const asAccountId = (s: string): AccountId => s as AccountId;
export const asIdempotencyKey = (s: string): IdempotencyKey => s as IdempotencyKey;
export const asPeriodKey = (s: string): PeriodKey => s as PeriodKey;

export enum AccountType {
  ASSET = "ASSET",
  LIABILITY = "LIABILITY",
  EQUITY = "EQUITY",
  REVENUE = "REVENUE",
  EXPENSE = "EXPENSE",
}

/** The side that increases an account's balance. */
export type NormalBalance = "DEBIT" | "CREDIT";

export function normalBalanceOf(type: AccountType): NormalBalance {
  switch (type) {
    case AccountType.ASSET:
    case AccountType.EXPENSE:
      return "DEBIT";
    case AccountType.LIABILITY:
    case AccountType.EQUITY:
    case AccountType.REVENUE:
      return "CREDIT";
  }
}

export interface Account {
  readonly id: AccountId;
  readonly code: string;
  readonly name: string;
  readonly type: AccountType;
  readonly currency: Currency;
  readonly parentId?: AccountId;
}

export type EntrySide = "DEBIT" | "CREDIT";

/**
 * Provenance — where a fact came from. Every posted journal retains this so
 * the audit trail can always be reconstructed. External deletion becomes a
 * tombstone; it never erases history.
 */
export interface Provenance {
  readonly sourceSystem: string;
  readonly sourceObject: string;
  readonly sourceVersion: string;
  /** Business effective date (ISO string), e.g. when the transaction occurred. */
  readonly effectiveDate: string;
  /** When the source system posted it (ISO string). */
  readonly postedDate: string;
  /** When RGNR8 ingested it (ISO string). */
  readonly ingestedAt: string;
  readonly normalizationVersion: string;
  readonly mappingVersion: string;
}

export interface JournalLineInput {
  readonly accountId: AccountId;
  readonly side: EntrySide;
  /** Always a positive amount; the side conveys direction. */
  readonly amount: Money;
  readonly memo?: string;
  readonly dimensions?: Readonly<Record<string, string>>;
}

/** A proposed entry — the input to the posting-command API, not yet validated/posted. */
export interface PostCommand {
  readonly tenantId: TenantId;
  readonly idempotencyKey: IdempotencyKey;
  readonly periodKey: PeriodKey;
  readonly currency: Currency;
  /** ISO string; supplied by the application clock, never generated in-core. */
  readonly entryDate: string;
  readonly lines: readonly JournalLineInput[];
  readonly provenance: Provenance;
  readonly memo?: string;
}

export type EntryStatus = "POSTED" | "REVERSAL";

/** A posted, immutable journal line. */
export interface PostedLine {
  readonly accountId: AccountId;
  readonly side: EntrySide;
  readonly amount: Money;
  readonly memo?: string;
  readonly dimensions?: Readonly<Record<string, string>>;
}

/**
 * A posted journal entry. Immutable and append-only: once posted it is frozen
 * and never edited. Corrections are made via a linked reversal (see reversalOf).
 */
export interface PostedEntry {
  readonly id: EntryId;
  readonly tenantId: TenantId;
  /** Monotonic per-tenant sequence assigned at posting time. */
  readonly sequence: number;
  readonly idempotencyKey: IdempotencyKey;
  readonly periodKey: PeriodKey;
  readonly currency: Currency;
  readonly entryDate: string;
  readonly status: EntryStatus;
  readonly lines: readonly PostedLine[];
  readonly provenance: Provenance;
  readonly memo?: string;
  /** Set when this entry reverses another; the reversed entry is never mutated. */
  readonly reversalOf?: EntryId;
  /** When the entry was appended (ISO string), supplied by the application clock. */
  readonly postedAt: string;
}

/**
 * A validated entry ready to append, before the store assigns its monotonic
 * per-tenant sequence and id. The store owns that assignment so it can be made
 * atomic (no gaps, no races) in a concurrent, persistent backend.
 */
export type DraftEntry = Omit<PostedEntry, "id" | "sequence">;
