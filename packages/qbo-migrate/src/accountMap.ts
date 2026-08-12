import {
  AccountType,
  ChartOfAccounts,
  asAccountId,
  type Account,
  type AccountId,
  type Currency,
} from "@rgnr8/ledger-kernel";
import type { QboAccount, QboAccountType } from "./types.js";

/** The mapping version stamped into provenance so a re-map is auditable. */
export const QBO_MAPPING_VERSION = "qbo-map/1";

/** Collapse QuickBooks' account types onto the kernel's five. */
export function mapQboType(t: QboAccountType): AccountType {
  switch (t) {
    case "Bank":
    case "Accounts Receivable":
    case "Other Current Asset":
    case "Fixed Asset":
    case "Other Asset":
      return AccountType.ASSET;
    case "Accounts Payable":
    case "Credit Card":
    case "Other Current Liability":
    case "Long Term Liability":
      return AccountType.LIABILITY;
    case "Equity":
      return AccountType.EQUITY;
    case "Income":
    case "Other Income":
      return AccountType.REVENUE;
    case "Cost of Goods Sold":
    case "Expense":
    case "Other Expense":
      return AccountType.EXPENSE;
  }
}

export interface QboChart {
  readonly coa: ChartOfAccounts;
  /** QBO account name → kernel AccountId, for wiring journal lines. */
  readonly byName: ReadonlyMap<string, AccountId>;
}

function slug(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/**
 * Build a kernel ChartOfAccounts from a QBO account list. The ledger `code` is
 * the QBO account number when present, else a stable slug of the name; the
 * account id is `qbo:<code>`. Deterministic — same export in, same ids out.
 * Duplicate codes are disambiguated by appending the mapped type.
 */
export function buildChartFromQbo(accounts: readonly QboAccount[], currency: Currency): QboChart {
  const coa = new ChartOfAccounts();
  const byName = new Map<string, AccountId>();
  const usedCodes = new Set<string>();

  for (const a of accounts) {
    const type = mapQboType(a.type);
    let code = a.acctNum?.trim() || slug(a.name);
    if (usedCodes.has(code)) code = `${code}-${type.toLowerCase()}`;
    // last resort: suffix a counter (keeps construction total)
    let n = 2;
    while (usedCodes.has(code)) code = `${code}-${n++}`;
    usedCodes.add(code);

    const id = asAccountId(`qbo:${code}`);
    const account: Account = { id, code, name: a.name, type, currency };
    coa.add(account);
    byName.set(a.name, id);
  }

  return { coa, byName };
}
