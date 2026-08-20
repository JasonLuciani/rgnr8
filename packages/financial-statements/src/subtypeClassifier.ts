/**
 * A cash-flow classifier driven by account *subtypes* rather than code-prefix
 * regexes.
 *
 * The default classifier guesses sections from account-code ranges (10xx = cash,
 * 15–19xx = PP&E, …). That only works on a chart that happens to follow that
 * numbering. Now that accounts carry a structured {@link AccountSubtype}, the
 * classification can be exact: a BANK account is cash, FIXED_ASSET/OTHER_ASSET
 * movements are investing, long-term debt and equity are financing, and the
 * working-capital accounts (AR, AP, credit cards, sales-tax payable, inventory,
 * other current items) are operating — regardless of how the account is coded.
 */

import { AccountSubtype } from "@rgnr8/ledger-kernel";
import type { AccountId, ChartOfAccounts } from "@rgnr8/ledger-kernel";
import type { CashFlowClassifier, CashFlowSection } from "./statements.js";
import type { TrialBalanceEntry } from "./accounts.js";

function sectionForSubtype(subtype: AccountSubtype): CashFlowSection {
  switch (subtype) {
    // Accumulated depreciation is a contra-asset: its period movement IS the
    // depreciation expense, a non-cash charge already inside net income. It must
    // land in operating as the add-back — not investing, which is reserved for
    // actual capex (the gross fixed-asset accounts).
    case AccountSubtype.ACCUMULATED_DEPRECIATION:
      return "operating";
    case AccountSubtype.FIXED_ASSET:
    case AccountSubtype.OTHER_ASSET:
      return "investing";
    case AccountSubtype.LONG_TERM_LIABILITY:
    case AccountSubtype.EQUITY:
    case AccountSubtype.RETAINED_EARNINGS:
      return "financing";
    default:
      return "operating";
  }
}

/**
 * Build a subtype-aware cash-flow classifier over a chart of accounts. Accounts
 * whose subtype is unset fall back to their statement class (equity → financing,
 * everything else → operating), and no account is ever treated as cash without
 * an explicit BANK subtype.
 */
export function subtypeCashFlowClassifier(coa: ChartOfAccounts): CashFlowClassifier {
  const subtypeOf = (id: AccountId): AccountSubtype | undefined => coa.get(id)?.subtype;
  return {
    isCash(entry: TrialBalanceEntry): boolean {
      return subtypeOf(entry.accountId) === AccountSubtype.BANK;
    },
    section(entry: TrialBalanceEntry): CashFlowSection {
      const st = subtypeOf(entry.accountId);
      if (st !== undefined) return sectionForSubtype(st);
      // No subtype on the account → fall back to class.
      return entry.accountClass === "equity" ? "financing" : "operating";
    },
  };
}
