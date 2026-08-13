/**
 * Account classification and the trial-balance shape the statements are built
 * from.
 *
 * ledger-kernel accounts carry a structured `type` (AccountType) plus a `code`
 * and `name`. We classify primarily off `type` (authoritative), with a
 * code-prefix fallback for the rare case where only a code is on hand.
 *
 * NOTE on trial balances: the kernel already exposes `computeTrialBalance`
 * (debit/credit columns) and `accountBalances` (normal-oriented balances). We do
 * NOT recompute from posted entries here — we adapt those kernel outputs into a
 * single *signed, debit-positive* balance per account, which is the natural form
 * for driving every statement. Debit balances are positive, credit balances are
 * negative, so a sound ledger sums to exactly zero.
 */

import { AccountType, Money, normalBalanceOf } from "@rgnr8/ledger-kernel";
import type {
  Account,
  AccountId,
  Currency,
  TrialBalance as KernelTrialBalance,
} from "@rgnr8/ledger-kernel";
import type { ChartOfAccounts } from "@rgnr8/ledger-kernel";

/** The five statement classes an account rolls up into. */
export type AccountClass = "asset" | "liability" | "equity" | "revenue" | "expense";

/** Authoritative classification from the kernel's structured account type. */
export function classifyByType(type: AccountType): AccountClass {
  switch (type) {
    case AccountType.ASSET:
      return "asset";
    case AccountType.LIABILITY:
      return "liability";
    case AccountType.EQUITY:
      return "equity";
    case AccountType.REVENUE:
      return "revenue";
    case AccountType.EXPENSE:
      return "expense";
  }
}

/**
 * Fallback classification from a standard chart-of-accounts code range:
 * 1xxx asset, 2xxx liability, 3xxx equity, 4xxx revenue, 5xxx+ expense.
 * Used only when a structured type is unavailable.
 */
export function classifyByCode(code: string): AccountClass {
  switch (code.trim().charAt(0)) {
    case "1":
      return "asset";
    case "2":
      return "liability";
    case "3":
      return "equity";
    case "4":
      return "revenue";
    default:
      return "expense";
  }
}

/** Classify an account from the chart of accounts (by its structured type). */
export function classify(account: Account): AccountClass {
  return classifyByType(account.type);
}

/** One account's signed net balance for a period. */
export interface TrialBalanceEntry {
  readonly accountId: AccountId;
  readonly code: string;
  readonly name: string;
  readonly accountClass: AccountClass;
  /**
   * Signed net balance, debit-positive: a debit balance is positive, a credit
   * balance is negative. (Asset/expense normal-debit accounts read positive
   * when they carry their normal balance; liability/equity/revenue read
   * negative.)
   */
  readonly signed: Money;
}

/** A period's trial balance: account -> signed balance, in one currency. */
export interface TrialBalance {
  readonly currency: Currency;
  readonly entries: readonly TrialBalanceEntry[];
}

/** Construct a trial balance directly (e.g. from fixtures or an external source). */
export function makeTrialBalance(
  currency: Currency,
  entries: readonly TrialBalanceEntry[],
): TrialBalance {
  return { currency, entries: [...entries].sort(byCode) };
}

/**
 * Adapt the kernel's `computeTrialBalance` output into the signed shape.
 * signed = debit column - credit column.
 */
export function fromKernelTrialBalance(ktb: KernelTrialBalance): TrialBalance {
  const entries: TrialBalanceEntry[] = ktb.rows.map((r) => ({
    accountId: r.accountId,
    code: r.code,
    name: r.name,
    accountClass: classifyByType(r.type),
    signed: r.debit.minus(r.credit),
  }));
  return { currency: ktb.currency, entries: entries.sort(byCode) };
}

/**
 * Adapt the kernel's `accountBalances` output (normal-oriented Money per
 * account) into the signed, debit-positive shape. For a normal-debit account
 * the oriented value already is the signed value; for a normal-credit account
 * it is negated.
 */
export function fromAccountBalances(
  balances: ReadonlyMap<AccountId, Money>,
  coa: ChartOfAccounts,
  currency: Currency,
): TrialBalance {
  const entries: TrialBalanceEntry[] = [];
  for (const [id, oriented] of balances) {
    const account = coa.get(id);
    if (!account) continue;
    const signed = normalBalanceOf(account.type) === "DEBIT" ? oriented : oriented.negate();
    entries.push({
      accountId: id,
      code: account.code,
      name: account.name,
      accountClass: classifyByType(account.type),
      signed,
    });
  }
  return { currency, entries: entries.sort(byCode) };
}

/**
 * The positive-natural presentation amount for an account: what it contributes
 * to its statement total. Asset/expense use the signed balance directly;
 * liability/equity/revenue flip sign so a normal credit balance reads positive.
 */
export function naturalAmount(entry: TrialBalanceEntry): Money {
  switch (entry.accountClass) {
    case "asset":
    case "expense":
      return entry.signed;
    case "liability":
    case "equity":
    case "revenue":
      return entry.signed.negate();
  }
}

/** All entries of one class, in code order. */
export function entriesOfClass(
  tb: TrialBalance,
  cls: AccountClass,
): readonly TrialBalanceEntry[] {
  return tb.entries.filter((e) => e.accountClass === cls);
}

/** Sum the natural amounts of a set of entries, in the given currency. */
export function sumNatural(entries: readonly TrialBalanceEntry[], currency: Currency): Money {
  return entries.reduce((acc, e) => acc.plus(naturalAmount(e)), Money.zero(currency));
}

/** Index a trial balance by account id, for cross-period alignment. */
export function indexById(tb: TrialBalance): ReadonlyMap<AccountId, TrialBalanceEntry> {
  const m = new Map<AccountId, TrialBalanceEntry>();
  for (const e of tb.entries) m.set(e.accountId, e);
  return m;
}

/** Stable sort comparator by account code. */
export function byCode(a: TrialBalanceEntry, b: TrialBalanceEntry): number {
  return a.code.localeCompare(b.code);
}
