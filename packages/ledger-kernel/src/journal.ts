import type { ChartOfAccounts } from "./chartOfAccounts.js";
import type { JournalLineInput, PostedLine } from "./types.js";
import {
  EmptyEntryError,
  LineCurrencyError,
  NonPositiveAmountError,
  UnbalancedEntryError,
  UnknownAccountError,
} from "./errors.js";
import type { Currency } from "./money.js";

/**
 * Validate a proposed set of lines and return frozen, validated lines.
 *
 * "Balanced by construction": this is the ONLY path to a set of postable lines,
 * and it refuses to return unless the entry is well-formed and balanced. There
 * is no way to construct a posted entry that skips these checks.
 */
export function validateAndBuildLines(
  lines: readonly JournalLineInput[],
  currency: Currency,
  coa: ChartOfAccounts,
): readonly PostedLine[] {
  if (lines.length < 2) throw new EmptyEntryError();

  let debitMinor = 0n;
  let creditMinor = 0n;
  const built: PostedLine[] = [];

  for (const line of lines) {
    const account = coa.get(line.accountId);
    if (!account) throw new UnknownAccountError(line.accountId);

    if (line.amount.currency.code !== currency.code) {
      throw new LineCurrencyError(
        line.accountId,
        `Line amount currency ${line.amount.currency.code} != entry currency ${currency.code}`,
      );
    }
    if (account.currency.code !== currency.code) {
      throw new LineCurrencyError(
        line.accountId,
        `Account currency ${account.currency.code} != entry currency ${currency.code}`,
      );
    }
    if (!line.amount.isPositive()) {
      throw new NonPositiveAmountError(line.accountId);
    }

    if (line.side === "DEBIT") debitMinor += line.amount.minorUnits;
    else creditMinor += line.amount.minorUnits;

    built.push(
      Object.freeze({
        accountId: line.accountId,
        side: line.side,
        amount: line.amount,
        ...(line.memo !== undefined ? { memo: line.memo } : {}),
        ...(line.dimensions !== undefined
          ? { dimensions: Object.freeze({ ...line.dimensions }) }
          : {}),
      }),
    );
  }

  if (debitMinor !== creditMinor) {
    throw new UnbalancedEntryError(debitMinor, creditMinor);
  }

  return Object.freeze(built);
}
