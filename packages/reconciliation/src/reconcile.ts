import { Money, sumMoney } from "@rgnr8/ledger-kernel";
import { matchItems } from "./match.js";
import {
  DifferenceCategory,
  type BookItem,
  type ClassifiedItem,
  type Reconciliation,
  type SignOff,
  type Statement,
  type StatementLine,
} from "./types.js";

export interface ReconcileOptions {
  readonly windowDays?: number;
}

/**
 * Reconcile a statement against the book items for the same account.
 *
 * Matched items cancel. Unmatched BOOK items are treated as in-transit TIMING
 * differences (benign). Unmatched STATEMENT lines are MISSING_SOURCE — bank
 * activity not yet in the books, which must be resolved before the account is
 * clean. The reconciliation is BALANCED only when the statement is internally
 * consistent, nothing is missing from the books, and the residual is zero.
 */
export function reconcileStatement(
  statement: Statement,
  bookItems: readonly BookItem[],
  opts: ReconcileOptions = {},
): Reconciliation {
  const currency = statement.openingBalance.currency;
  const { matched, unmatchedStatement, unmatchedBook } = matchItems(
    statement.lines,
    bookItems,
    opts.windowDays ?? 4,
  );

  const sumStatement = sumMoney(
    statement.lines.map((l) => l.amount),
    currency,
  );
  const statementDerivedClosing = statement.openingBalance.plus(sumStatement);
  const statementConsistent = statementDerivedClosing.equals(statement.closingBalance);

  const sumBook = sumMoney(
    bookItems.map((b) => b.amount),
    currency,
  );
  const bookClosing = statement.openingBalance.plus(sumBook);
  const difference = bookClosing.minus(statement.closingBalance);

  const timingSum = sumMoney(
    unmatchedBook.map((b) => b.amount),
    currency,
  );
  const unreconciledResidual = difference.minus(timingSum);

  const classifiedBook: ClassifiedItem[] = unmatchedBook.map((b) =>
    classify(b, "BOOK", DifferenceCategory.TIMING, "in transit — in books, not yet on the statement"),
  );
  const classifiedStatement: ClassifiedItem[] = unmatchedStatement.map((s) =>
    classify(s, "STATEMENT", DifferenceCategory.MISSING_SOURCE, "on the statement but not in the books"),
  );

  const notes: string[] = [];
  if (!statementConsistent) {
    notes.push(
      `statement is internally inconsistent: opening + lines = ${statementDerivedClosing.toDecimalString()} ` +
        `but closing balance is ${statement.closingBalance.toDecimalString()}`,
    );
  }
  if (classifiedStatement.length > 0) {
    notes.push(`${classifiedStatement.length} statement line(s) missing from the books`);
  }
  if (classifiedBook.length > 0) {
    notes.push(`${classifiedBook.length} in-transit item(s) not yet on the statement`);
  }

  const status =
    statementConsistent && classifiedStatement.length === 0 && unreconciledResidual.isZero()
      ? "BALANCED"
      : "OUT_OF_BALANCE";

  return {
    accountId: statement.accountId,
    periodStart: statement.periodStart,
    periodEnd: statement.periodEnd,
    status,
    statementOpening: statement.openingBalance,
    statementClosing: statement.closingBalance,
    bookClosing,
    difference,
    unreconciledResidual,
    matched,
    unmatchedStatement: classifiedStatement,
    unmatchedBook: classifiedBook,
    statementConsistent,
    notes,
  };
}

/**
 * The publish gate: owner-facing numbers for an account may only be shown when
 * its reconciliation is BALANCED. Missing-source items or a nonzero residual
 * block it.
 */
export function isCleanForPublish(recon: Reconciliation): boolean {
  return recon.status === "BALANCED";
}

/** Record reviewer sign-off. Only a BALANCED reconciliation can be signed. */
export function signReconciliation(recon: Reconciliation, signOff: SignOff): Reconciliation {
  if (!isCleanForPublish(recon)) {
    throw new Error(
      `cannot sign off an out-of-balance reconciliation for ${recon.accountId} ` +
        `(residual ${recon.unreconciledResidual.toDecimalString()})`,
    );
  }
  return { ...recon, signOff };
}

function classify(
  item: StatementLine | BookItem,
  side: "STATEMENT" | "BOOK",
  category: DifferenceCategory,
  note: string,
): ClassifiedItem {
  return Object.freeze({
    side,
    id: item.id,
    date: item.date,
    amount: item.amount,
    description: item.description,
    category,
    note,
  });
}
