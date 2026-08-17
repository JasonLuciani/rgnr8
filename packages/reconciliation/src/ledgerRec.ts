import {
  Money,
  sumMoney,
  type AccountId,
  type Currency,
  type PostedEntry,
} from "@rgnr8/ledger-kernel";
import { matchItems } from "./match.js";
import type { BookItem, MatchPair, StatementLine, Statement } from "./types.js";

/**
 * Ledger-based bank reconciliation with a persistent cleared/uncleared status,
 * the QBO-style register reconcile that the abstract {@link reconcileStatement}
 * flow didn't cover.
 *
 * Where does "cleared" live? The journal is append-only and its entries are
 * frozen, so a mutable per-entry flag can't sit on the entry. Instead the
 * status lives beside the ledger in a {@link ClearedRegister} keyed by
 * (account, entry). Each posted entry that touches the bank account contributes
 * one register line — its *net* effect on that account — and carries one of
 * three statuses:
 *
 *   UNCLEARED  → posted in our books, not yet seen on a bank statement
 *   CLEARED    → checked off against the statement under review (tentative)
 *   RECONCILED → locked in by a finished, balanced reconciliation
 *
 * The cleared balance is the statement opening balance plus every line marked
 * cleared/reconciled this session; when it equals the statement's closing
 * balance the reconciliation is finishable, at which point the cleared lines
 * are promoted to RECONCILED and the register's reconciled-through advances.
 */

export type ClearStatus = "UNCLEARED" | "CLEARED" | "RECONCILED";

/** One posted entry's net effect on a single bank/asset account. */
export interface RegisterLine {
  readonly entryId: string;
  readonly accountId: string;
  readonly date: string; // entryDate (ISO)
  /** Signed: + = into the account (net debit for an asset), − = out. */
  readonly amount: Money;
  readonly memo: string;
  readonly status: ClearStatus;
}

/**
 * Persistent cleared/reconciled status keyed by (account, entry). In-memory
 * reference implementation; a SQL adapter implements the same shape. Absent =
 * UNCLEARED, so a fresh register treats every entry as uncleared.
 */
export class ClearedRegister {
  private readonly byAccount = new Map<string, Map<string, ClearStatus>>();
  /** Reconciled-through statement closing date per account (ISO). */
  private readonly through = new Map<string, string>();

  private acct(accountId: string): Map<string, ClearStatus> {
    let m = this.byAccount.get(accountId);
    if (!m) {
      m = new Map();
      this.byAccount.set(accountId, m);
    }
    return m;
  }

  statusOf(accountId: string, entryId: string): ClearStatus {
    return this.acct(accountId).get(entryId) ?? "UNCLEARED";
  }

  setStatus(accountId: string, entryId: string, status: ClearStatus): void {
    this.acct(accountId).set(entryId, status);
  }

  /** Promote a set of entries to RECONCILED and advance the reconciled-through date. */
  markReconciled(accountId: string, entryIds: readonly string[], throughDate: string): void {
    const m = this.acct(accountId);
    for (const id of entryIds) m.set(id, "RECONCILED");
    const prev = this.through.get(accountId);
    if (!prev || throughDate > prev) this.through.set(accountId, throughDate);
  }

  reconciledThrough(accountId: string): string | undefined {
    return this.through.get(accountId);
  }
}

/** Net effect (signed, + = debit) of one entry on one account. */
function netOnAccount(entry: PostedEntry, accountId: AccountId, currency: Currency): Money {
  let net = Money.zero(currency);
  for (const line of entry.lines) {
    if (line.accountId !== accountId) continue;
    net = line.side === "DEBIT" ? net.plus(line.amount) : net.minus(line.amount);
  }
  return net;
}

/**
 * Build the bank register for one account from posted ledger entries: one line
 * per entry that has a non-zero net effect on the account, tagged with its
 * cleared status from the register. Sorted by (date, entryId) for determinism.
 */
export function bankRegister(
  entries: readonly PostedEntry[],
  accountId: AccountId,
  currency: Currency,
  register: ClearedRegister,
): RegisterLine[] {
  const lines: RegisterLine[] = [];
  for (const entry of entries) {
    const net = netOnAccount(entry, accountId, currency);
    if (net.isZero()) continue;
    lines.push({
      entryId: entry.id,
      accountId,
      date: entry.entryDate,
      amount: net,
      memo: entry.memo ?? "",
      status: register.statusOf(accountId, entry.id),
    });
  }
  lines.sort((a, b) =>
    a.date < b.date ? -1 : a.date > b.date ? 1 : a.entryId < b.entryId ? -1 : a.entryId > b.entryId ? 1 : 0,
  );
  return lines;
}

export type BankReconStatus = "BALANCED" | "OUT_OF_BALANCE";

export interface BankReconciliation {
  readonly accountId: string;
  readonly periodStart: string;
  readonly periodEnd: string;
  readonly status: BankReconStatus;
  readonly statementOpening: Money;
  readonly statementClosing: Money;
  /** Opening + every cleared/reconciled register line = the cleared balance. */
  readonly clearedBalance: Money;
  /** statementClosing − clearedBalance; zero when the reconciliation ties out. */
  readonly difference: Money;
  /** Statement lines matched to register lines (these become cleared). */
  readonly matched: readonly MatchPair[];
  /** Register lines newly cleared by this reconciliation (entry ids). */
  readonly newlyClearedEntryIds: readonly string[];
  /** Register lines still uncleared after matching. */
  readonly unclearedLines: readonly RegisterLine[];
  /** Statement lines with no matching book entry (missing from the books). */
  readonly unmatchedStatement: readonly StatementLine[];
  readonly statementConsistent: boolean;
  /** True when the reconciliation is BALANCED and can be finished/locked. */
  readonly canFinish: boolean;
  readonly notes: readonly string[];
}

export interface ReconcileBankOptions {
  readonly windowDays?: number;
}

function lineToBookItem(l: RegisterLine): BookItem {
  return { id: l.entryId, date: l.date, amount: l.amount, description: l.memo };
}

/**
 * Reconcile a bank/card account's ledger register against a statement.
 *
 * Already-RECONCILED lines are folded into the opening balance (they were
 * locked by a prior reconciliation) and are not re-matched. The remaining
 * lines are matched to statement lines; matches are the entries that clear.
 * The account balances when the statement is internally consistent and the
 * cleared balance equals the statement's closing balance.
 */
export function reconcileBankAccount(
  entries: readonly PostedEntry[],
  accountId: AccountId,
  statement: Statement,
  register: ClearedRegister,
  opts: ReconcileBankOptions = {},
): BankReconciliation {
  const currency = statement.openingBalance.currency;
  const allLines = bankRegister(entries, accountId, currency, register);

  const reconciledLines = allLines.filter((l) => l.status === "RECONCILED");
  const openLines = allLines.filter((l) => l.status !== "RECONCILED");

  const { matched, unmatchedBook } = matchItems(
    statement.lines,
    openLines.map(lineToBookItem),
    opts.windowDays ?? 4,
  );
  const matchedIds = new Set(matched.map((m) => m.book.id));

  // Statement consistency: opening + statement lines == stated closing.
  const sumStatement = sumMoney(statement.lines.map((l) => l.amount), currency);
  const derivedClosing = statement.openingBalance.plus(sumStatement);
  const statementConsistent = derivedClosing.equals(statement.closingBalance);

  // Cleared balance = opening + the newly cleared (matched) lines this session.
  // (Prior RECONCILED lines already sit inside the statement opening balance.)
  const clearedSum = sumMoney(
    matched.map((m) => m.book.amount),
    currency,
  );
  const clearedBalance = statement.openingBalance.plus(clearedSum);
  const difference = statement.closingBalance.minus(clearedBalance);

  const newlyClearedEntryIds = openLines.filter((l) => matchedIds.has(l.entryId)).map((l) => l.entryId);
  const unclearedLines = openLines
    .filter((l) => !matchedIds.has(l.entryId))
    .map((l) => ({ ...l, status: "UNCLEARED" as ClearStatus }));

  const unmatchedStatement = statement.lines.filter(
    (s) => !matched.some((m) => m.statement.id === s.id),
  );

  const notes: string[] = [];
  if (!statementConsistent) {
    notes.push(
      `statement is internally inconsistent: opening + lines = ${derivedClosing.toDecimalString()} ` +
        `but closing balance is ${statement.closingBalance.toDecimalString()}`,
    );
  }
  if (unmatchedStatement.length > 0) {
    notes.push(`${unmatchedStatement.length} statement line(s) not found in the books`);
  }
  if (!difference.isZero()) {
    notes.push(`cleared balance is off by ${difference.toDecimalString()}`);
  }
  if (reconciledLines.length > 0) {
    notes.push(`${reconciledLines.length} line(s) already reconciled in a prior period`);
  }
  // unmatchedBook are open ledger lines not on this statement — in-transit; benign.
  void unmatchedBook;

  const balanced = statementConsistent && difference.isZero() && unmatchedStatement.length === 0;

  return {
    accountId,
    periodStart: statement.periodStart,
    periodEnd: statement.periodEnd,
    status: balanced ? "BALANCED" : "OUT_OF_BALANCE",
    statementOpening: statement.openingBalance,
    statementClosing: statement.closingBalance,
    clearedBalance,
    difference,
    matched,
    newlyClearedEntryIds,
    unclearedLines,
    unmatchedStatement,
    statementConsistent,
    canFinish: balanced,
    notes,
  };
}

/**
 * Finish a balanced reconciliation: promote every cleared line to RECONCILED
 * in the register and advance the account's reconciled-through date. Throws if
 * the reconciliation isn't finishable (out of balance).
 */
export function finishBankReconciliation(
  recon: BankReconciliation,
  register: ClearedRegister,
): void {
  if (!recon.canFinish) {
    throw new Error(
      `cannot finish an out-of-balance reconciliation for ${recon.accountId} ` +
        `(off by ${recon.difference.toDecimalString()})`,
    );
  }
  register.markReconciled(recon.accountId, recon.newlyClearedEntryIds, recon.periodEnd);
}
