/**
 * General-ledger detail (a.k.a. the transaction report) and single-account
 * drill-down.
 *
 * The statements roll every account down to one number; this is the opposite —
 * every posting that hit an account, in order, with a running balance. It is
 * what an owner clicks into from a statement line ("why is Office Expense
 * $4,210?") and what an auditor reads. Built directly from posted journal
 * entries, so it always ties to the ledger.
 *
 * Balances are signed debit-positive, the same convention the trial balance and
 * statements use: a debit raises the running balance, a credit lowers it.
 */

import { Money } from "@rgnr8/ledger-kernel";
import type {
  AccountId,
  AccountType,
  ChartOfAccounts,
  Currency,
  PostedEntry,
} from "@rgnr8/ledger-kernel";
import type { DateWindow } from "@rgnr8/ledger-kernel";

export interface GlDetailRow {
  readonly entryId: string;
  readonly sequence: number;
  readonly date: string;
  readonly memo: string;
  /** Debit amount on this account (zero if the line was a credit). */
  readonly debit: Money;
  /** Credit amount on this account (zero if the line was a debit). */
  readonly credit: Money;
  /** Signed change this line made to the account (debit +, credit −). */
  readonly change: Money;
  /** Signed running balance after this line, debit-positive. */
  readonly balance: Money;
}

export interface GlDetailAccount {
  readonly accountId: AccountId;
  readonly code: string;
  readonly name: string;
  readonly type: AccountType;
  /** Signed opening balance (everything strictly before the window). */
  readonly opening: Money;
  readonly rows: readonly GlDetailRow[];
  /** Signed closing balance (opening + the window's changes). */
  readonly closing: Money;
  readonly totalDebit: Money;
  readonly totalCredit: Money;
}

export interface GlDetailOptions {
  /** Restrict rows to this date window (opening is computed from before it). */
  readonly window?: DateWindow;
  /** Only report these accounts (default: every account with activity). */
  readonly accountIds?: readonly AccountId[];
}

function inWindow(date: string, window?: DateWindow): boolean {
  if (!window) return true;
  if (window.from !== undefined && date < window.from) return false;
  if (window.to !== undefined && date > window.to) return false;
  return true;
}

/** Entries sorted by posting sequence — the canonical ledger order. */
function bySequence(a: PostedEntry, b: PostedEntry): number {
  return a.sequence - b.sequence;
}

/**
 * Build the GL detail for every requested account. Opening balances fold in all
 * activity strictly before `window.from`; rows are the in-window postings with a
 * running signed balance; closing = opening + in-window change.
 */
export function glDetail(
  entries: readonly PostedEntry[],
  coa: ChartOfAccounts,
  currency: Currency,
  opts: GlDetailOptions = {},
): GlDetailAccount[] {
  const filter = opts.accountIds ? new Set(opts.accountIds) : undefined;
  const ordered = [...entries].sort(bySequence);

  // Opening (before the window) and in-window rows, per account.
  const opening = new Map<AccountId, Money>();
  const rowsByAccount = new Map<AccountId, GlDetailRow[]>();
  const running = new Map<AccountId, Money>();

  const zero = Money.zero(currency);
  const from = opts.window?.from;

  // First pass: opening balances from pre-window activity.
  if (from !== undefined) {
    for (const entry of ordered) {
      if (entry.entryDate >= from) continue;
      for (const line of entry.lines) {
        if (filter && !filter.has(line.accountId)) continue;
        const delta = line.side === "DEBIT" ? line.amount : line.amount.negate();
        opening.set(line.accountId, (opening.get(line.accountId) ?? zero).plus(delta));
      }
    }
  }

  // Seed running balances from opening.
  for (const [id, bal] of opening) running.set(id, bal);

  // Second pass: in-window rows with a running balance.
  for (const entry of ordered) {
    if (!inWindow(entry.entryDate, opts.window)) continue;
    for (const line of entry.lines) {
      if (filter && !filter.has(line.accountId)) continue;
      const isDebit = line.side === "DEBIT";
      const change = isDebit ? line.amount : line.amount.negate();
      const prev = running.get(line.accountId) ?? zero;
      const balance = prev.plus(change);
      running.set(line.accountId, balance);
      const list = rowsByAccount.get(line.accountId) ?? [];
      list.push({
        entryId: entry.id,
        sequence: entry.sequence,
        date: entry.entryDate,
        memo: line.memo ?? entry.memo ?? "",
        debit: isDebit ? line.amount : zero,
        credit: isDebit ? zero : line.amount,
        change,
        balance,
      });
      rowsByAccount.set(line.accountId, list);
    }
  }

  // Assemble per-account, over the union of accounts seen.
  const ids = new Set<AccountId>([...opening.keys(), ...rowsByAccount.keys()]);
  const out: GlDetailAccount[] = [];
  for (const id of ids) {
    const account = coa.get(id);
    if (!account) continue;
    const rows = rowsByAccount.get(id) ?? [];
    const open = opening.get(id) ?? zero;
    let totalDebit = zero;
    let totalCredit = zero;
    for (const r of rows) {
      totalDebit = totalDebit.plus(r.debit);
      totalCredit = totalCredit.plus(r.credit);
    }
    out.push({
      accountId: id,
      code: account.code,
      name: account.name,
      type: account.type,
      opening: open,
      rows,
      closing: running.get(id) ?? open,
      totalDebit,
      totalCredit,
    });
  }
  out.sort((a, b) => a.code.localeCompare(b.code));
  return out;
}

/**
 * Drill-down: the GL detail for a single account. This is what a statement line
 * links to. Returns undefined if the account isn't in the chart.
 */
export function accountLedger(
  entries: readonly PostedEntry[],
  accountId: AccountId,
  coa: ChartOfAccounts,
  currency: Currency,
  window?: DateWindow,
): GlDetailAccount | undefined {
  const opts: GlDetailOptions = { accountIds: [accountId], ...(window ? { window } : {}) };
  const [only] = glDetail(entries, coa, currency, opts);
  if (only) return only;
  // Account exists but had no activity → an empty, zeroed ledger.
  const account = coa.get(accountId);
  if (!account) return undefined;
  const zero = Money.zero(currency);
  return {
    accountId,
    code: account.code,
    name: account.name,
    type: account.type,
    opening: zero,
    rows: [],
    closing: zero,
    totalDebit: zero,
    totalCredit: zero,
  };
}
