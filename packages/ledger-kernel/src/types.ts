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

/**
 * QBO-style detail subtypes. Each maps to exactly one {@link AccountType}
 * (see {@link accountTypeOfSubtype}) and drives statement classification and
 * behavior (Bank/AR/AP/Undeposited-Funds/Sales-Tax-Payable are special).
 */
export enum AccountSubtype {
  // ASSET
  BANK = "BANK",
  ACCOUNTS_RECEIVABLE = "ACCOUNTS_RECEIVABLE",
  UNDEPOSITED_FUNDS = "UNDEPOSITED_FUNDS",
  INVENTORY = "INVENTORY",
  OTHER_CURRENT_ASSET = "OTHER_CURRENT_ASSET",
  FIXED_ASSET = "FIXED_ASSET",
  /** Contra-asset: accumulated depreciation. Its movement is a non-cash
   * depreciation add-back (operating), NOT a capex flow (investing). */
  ACCUMULATED_DEPRECIATION = "ACCUMULATED_DEPRECIATION",
  OTHER_ASSET = "OTHER_ASSET",
  // LIABILITY
  ACCOUNTS_PAYABLE = "ACCOUNTS_PAYABLE",
  CREDIT_CARD = "CREDIT_CARD",
  SALES_TAX_PAYABLE = "SALES_TAX_PAYABLE",
  OTHER_CURRENT_LIABILITY = "OTHER_CURRENT_LIABILITY",
  LONG_TERM_LIABILITY = "LONG_TERM_LIABILITY",
  // EQUITY
  EQUITY = "EQUITY",
  RETAINED_EARNINGS = "RETAINED_EARNINGS",
  // REVENUE
  INCOME = "INCOME",
  OTHER_INCOME = "OTHER_INCOME",
  // EXPENSE
  EXPENSE = "EXPENSE",
  COST_OF_GOODS_SOLD = "COST_OF_GOODS_SOLD",
  OTHER_EXPENSE = "OTHER_EXPENSE",
}

const SUBTYPE_TYPE: Readonly<Record<AccountSubtype, AccountType>> = {
  [AccountSubtype.BANK]: AccountType.ASSET,
  [AccountSubtype.ACCOUNTS_RECEIVABLE]: AccountType.ASSET,
  [AccountSubtype.UNDEPOSITED_FUNDS]: AccountType.ASSET,
  [AccountSubtype.INVENTORY]: AccountType.ASSET,
  [AccountSubtype.OTHER_CURRENT_ASSET]: AccountType.ASSET,
  [AccountSubtype.FIXED_ASSET]: AccountType.ASSET,
  [AccountSubtype.ACCUMULATED_DEPRECIATION]: AccountType.ASSET,
  [AccountSubtype.OTHER_ASSET]: AccountType.ASSET,
  [AccountSubtype.ACCOUNTS_PAYABLE]: AccountType.LIABILITY,
  [AccountSubtype.CREDIT_CARD]: AccountType.LIABILITY,
  [AccountSubtype.SALES_TAX_PAYABLE]: AccountType.LIABILITY,
  [AccountSubtype.OTHER_CURRENT_LIABILITY]: AccountType.LIABILITY,
  [AccountSubtype.LONG_TERM_LIABILITY]: AccountType.LIABILITY,
  [AccountSubtype.EQUITY]: AccountType.EQUITY,
  [AccountSubtype.RETAINED_EARNINGS]: AccountType.EQUITY,
  [AccountSubtype.INCOME]: AccountType.REVENUE,
  [AccountSubtype.OTHER_INCOME]: AccountType.REVENUE,
  [AccountSubtype.EXPENSE]: AccountType.EXPENSE,
  [AccountSubtype.COST_OF_GOODS_SOLD]: AccountType.EXPENSE,
  [AccountSubtype.OTHER_EXPENSE]: AccountType.EXPENSE,
};

/** The {@link AccountType} a subtype belongs to. */
export function accountTypeOfSubtype(subtype: AccountSubtype): AccountType {
  return SUBTYPE_TYPE[subtype];
}

/**
 * Where an account came from. `quickbooks` marks an account brought over at
 * go-live from QuickBooks, whose chart is the system of record until cutover.
 * Absent means RGNR8 made it.
 */
export type AccountOrigin = "rgnr8" | "quickbooks";

/**
 * The detail subtypes whose accounts the books depend on: the control accounts
 * the AR/AP and payment flows post to, the sales-tax liability, undeposited funds
 * and retained earnings. A system account can be renamed but not inactivated and
 * not retyped (see {@link isSystemAccount}).
 */
export const SYSTEM_SUBTYPES: readonly AccountSubtype[] = [
  AccountSubtype.ACCOUNTS_RECEIVABLE,
  AccountSubtype.ACCOUNTS_PAYABLE,
  AccountSubtype.UNDEPOSITED_FUNDS,
  AccountSubtype.SALES_TAX_PAYABLE,
  AccountSubtype.RETAINED_EARNINGS,
];

/** The name the go-live gives the account that absorbs the conversion residual. */
export const OPENING_BALANCE_EQUITY_NAME = "Opening Balance Equity";
/**
 * The code the go-live's opening-balance equity account carries by default (the
 * web app always sends it). System accounts cannot be renumbered, so this code
 * is a stable marker even if the account is renamed.
 */
export const OPENING_BALANCE_EQUITY_CODE = "3010";

/**
 * Whether an account is a system account. There is no stored flag: an account is
 * system when its subtype is in {@link SYSTEM_SUBTYPES}, or when it is the
 * Opening Balance Equity account the go-live creates. That account is an EQUITY
 * account carrying {@link OPENING_BALANCE_EQUITY_CODE} (stable, because system
 * accounts cannot be renumbered) or, for a client that chose another code, the
 * name {@link OPENING_BALANCE_EQUITY_NAME}.
 */
export function isSystemAccount(account: Pick<Account, "subtype" | "name" | "code">): boolean {
  if (account.subtype !== undefined && SYSTEM_SUBTYPES.includes(account.subtype)) return true;
  if (account.subtype !== AccountSubtype.EQUITY) return false;
  return account.code === OPENING_BALANCE_EQUITY_CODE || account.name === OPENING_BALANCE_EQUITY_NAME;
}

export interface Account {
  readonly id: AccountId;
  readonly code: string;
  readonly name: string;
  readonly type: AccountType;
  readonly currency: Currency;
  readonly parentId?: AccountId;
  /** QBO-style detail subtype (must belong to `type`). */
  readonly subtype?: AccountSubtype;
  /** Inactive accounts are hidden from pickers but keep their history. Default true. */
  readonly active?: boolean;
  /** Where the account came from. Default `rgnr8`. */
  readonly origin?: AccountOrigin;
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
