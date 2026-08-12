/**
 * A normalized shape for a QuickBooks Online export. Real QBO exports arrive as
 * CSV/IIF/QBXML or via the API in several report shapes; a thin adapter maps any
 * of those into this DTO, and everything downstream (import + compare) works off
 * it. Amounts are decimal strings (e.g. "1234.56") — never floats — parsed into
 * integer-minor-unit `Money` at the boundary.
 */

/** QuickBooks' account-type vocabulary (the detail types collapse to these). */
export type QboAccountType =
  | "Bank"
  | "Accounts Receivable"
  | "Other Current Asset"
  | "Fixed Asset"
  | "Other Asset"
  | "Accounts Payable"
  | "Credit Card"
  | "Other Current Liability"
  | "Long Term Liability"
  | "Equity"
  | "Income"
  | "Other Income"
  | "Cost of Goods Sold"
  | "Expense"
  | "Other Expense";

export interface QboAccount {
  readonly name: string;
  /** QBO "account number" if the file has one; used as the ledger code. */
  readonly acctNum?: string;
  readonly type: QboAccountType;
}

export interface QboJournalLine {
  readonly account: string; // matches a QboAccount.name
  /** Exactly one of debit/credit is set (decimal string). */
  readonly debit?: string;
  readonly credit?: string;
  readonly memo?: string;
}

export interface QboJournalEntry {
  readonly id: string; // QBO txn id — becomes the idempotency key
  readonly date: string; // ISO date
  readonly lines: readonly QboJournalLine[];
  readonly memo?: string;
}

/** A row of QBO's own reported Trial Balance — the figures to diff against. */
export interface QboTrialBalanceRow {
  readonly account: string; // matches a QboAccount.name
  readonly debit?: string;
  readonly credit?: string;
}

export interface QboExport {
  readonly accounts: readonly QboAccount[];
  /** The general-journal detail to import into the ledger. */
  readonly entries: readonly QboJournalEntry[];
  /** QBO's reported trial balance (optional; enables parallel-close compare). */
  readonly reportedTrialBalance?: readonly QboTrialBalanceRow[];
}
