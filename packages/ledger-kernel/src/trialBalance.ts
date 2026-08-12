import type { ChartOfAccounts } from "./chartOfAccounts.js";
import type { LedgerStore } from "./ledgerStore.js";
import { Money, type Currency } from "./money.js";
import { normalBalanceOf, type AccountId, type AccountType, type TenantId } from "./types.js";

export interface TrialBalanceRow {
  readonly accountId: AccountId;
  readonly code: string;
  readonly name: string;
  readonly type: AccountType;
  /** Debit-column amount (zero if the account carries a credit balance). */
  readonly debit: Money;
  /** Credit-column amount (zero if the account carries a debit balance). */
  readonly credit: Money;
}

export interface TrialBalance {
  readonly currency: Currency;
  readonly rows: readonly TrialBalanceRow[];
  readonly totalDebit: Money;
  readonly totalCredit: Money;
  /** True when total debits equal total credits — always true for a sound ledger. */
  readonly inBalance: boolean;
}

/**
 * Derive a trial balance purely from posted ledger facts. All financial
 * statements are generated from the same ledger facts; nothing is stored
 * as a mutable running total.
 */
export async function computeTrialBalance(
  store: LedgerStore,
  tenant: TenantId,
  coa: ChartOfAccounts,
  currency: Currency,
): Promise<TrialBalance> {
  // Net debit position per account, in minor units (debit positive).
  const net = new Map<AccountId, bigint>();

  for (const entry of await store.list(tenant)) {
    for (const line of entry.lines) {
      const delta = line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
      net.set(line.accountId, (net.get(line.accountId) ?? 0n) + delta);
    }
  }

  const rows: TrialBalanceRow[] = [];
  let totalDebitMinor = 0n;
  let totalCreditMinor = 0n;

  const accountsWithActivity = [...net.keys()]
    .map((id) => coa.get(id))
    .filter((a): a is NonNullable<typeof a> => a !== undefined)
    .sort((a, b) => a.code.localeCompare(b.code));

  for (const account of accountsWithActivity) {
    const n = net.get(account.id) ?? 0n;
    const debitMinor = n > 0n ? n : 0n;
    const creditMinor = n < 0n ? -n : 0n;
    totalDebitMinor += debitMinor;
    totalCreditMinor += creditMinor;
    rows.push({
      accountId: account.id,
      code: account.code,
      name: account.name,
      type: account.type,
      debit: Money.fromMinorUnits(debitMinor, currency),
      credit: Money.fromMinorUnits(creditMinor, currency),
    });
  }

  return {
    currency,
    rows,
    totalDebit: Money.fromMinorUnits(totalDebitMinor, currency),
    totalCredit: Money.fromMinorUnits(totalCreditMinor, currency),
    inBalance: totalDebitMinor === totalCreditMinor,
  };
}

/**
 * Balances per account expressed in normal-balance orientation (a positive
 * result means the account's normal side). Useful for building statements.
 */
export async function accountBalances(
  store: LedgerStore,
  tenant: TenantId,
  coa: ChartOfAccounts,
  currency: Currency,
): Promise<Map<AccountId, Money>> {
  const net = new Map<AccountId, bigint>();
  for (const entry of await store.list(tenant)) {
    for (const line of entry.lines) {
      const delta = line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
      net.set(line.accountId, (net.get(line.accountId) ?? 0n) + delta);
    }
  }
  const out = new Map<AccountId, Money>();
  for (const [id, n] of net) {
    const account = coa.get(id);
    if (!account) continue;
    const oriented = normalBalanceOf(account.type) === "DEBIT" ? n : -n;
    out.set(id, Money.fromMinorUnits(oriented, currency));
  }
  return out;
}
