import type { AccountId, IdempotencyKey, PeriodKey } from "./types.js";

export class LedgerError extends Error {}

export class EmptyEntryError extends LedgerError {
  override readonly name = "EmptyEntryError";
  constructor() {
    super("A journal entry must have at least two lines");
  }
}

export class UnbalancedEntryError extends LedgerError {
  override readonly name = "UnbalancedEntryError";
  constructor(readonly debitMinor: bigint, readonly creditMinor: bigint) {
    super(`Journal entry is unbalanced: debits ${debitMinor} != credits ${creditMinor} (minor units)`);
  }
}

export class NonPositiveAmountError extends LedgerError {
  override readonly name = "NonPositiveAmountError";
  constructor(readonly accountId: AccountId) {
    super(`Journal line for account ${accountId} must have a positive amount`);
  }
}

export class UnknownAccountError extends LedgerError {
  override readonly name = "UnknownAccountError";
  constructor(readonly accountId: AccountId) {
    super(`Unknown account: ${accountId}`);
  }
}

export class LineCurrencyError extends LedgerError {
  override readonly name = "LineCurrencyError";
  constructor(readonly accountId: AccountId, message: string) {
    super(message);
  }
}

export class PeriodClosedError extends LedgerError {
  override readonly name = "PeriodClosedError";
  constructor(readonly periodKey: PeriodKey) {
    super(`Accounting period ${periodKey} is closed; postings are not permitted`);
  }
}

export class DuplicateIdempotencyKeyError extends LedgerError {
  override readonly name = "DuplicateIdempotencyKeyError";
  constructor(readonly key: IdempotencyKey) {
    super(`Idempotency key already used with a different command payload: ${key}`);
  }
}

export class UnknownEntryError extends LedgerError {
  override readonly name = "UnknownEntryError";
  constructor(readonly id: string) {
    super(`Unknown entry: ${id}`);
  }
}
