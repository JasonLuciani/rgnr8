import { Money, defineCurrency, USD } from "@rgnr8/ledger-kernel";
import type { NormalizedInput, ProviderAdapter, RawRecord } from "../types.js";
import { TransactionKind } from "../types.js";

/**
 * A single transaction in a Plaid-like payload.
 * NOTE the provider convention: a POSITIVE `amount` means money LEAVING the
 * account (a debit); NEGATIVE means money coming in. The adapter flips this to
 * RGNR8's convention (positive = into the account) — real normalization work.
 */
interface PlaidTxn {
  transaction_id: string;
  account_id: string;
  date: string;
  authorized_date?: string;
  pending: boolean;
  amount: string; // decimal string, provider sign convention
  name: string;
  merchant_name?: string;
  iso_currency_code?: string;
  category?: string[];
  /** Provider's id of the pending txn this posted txn replaces. */
  pending_transaction_id?: string;
}

function currencyFor(code: string | undefined): ReturnType<typeof defineCurrency> {
  if (!code || code === "USD") return USD;
  return defineCurrency(code, 2);
}

function classify(ourAmountIsInflow: boolean, name: string, category: string[]): TransactionKind {
  const cats = category.map((c) => c.toLowerCase());
  const n = name.toLowerCase();
  if (cats.includes("transfer") || n.includes("transfer")) return TransactionKind.TRANSFER;
  if (ourAmountIsInflow) {
    if (n.includes("refund")) return TransactionKind.REFUND;
    if (n.includes("interest")) return TransactionKind.INTEREST;
    return TransactionKind.DEPOSIT;
  }
  if (cats.some((c) => c.includes("bank fee")) || n.includes("fee")) return TransactionKind.FEE;
  return TransactionKind.PURCHASE;
}

export class BankPlaidLikeAdapter implements ProviderAdapter {
  readonly provider = "plaid";
  constructor(private readonly tenantId: string) {}

  normalize(raw: RawRecord): NormalizedInput[] {
    const t = raw.payload as PlaidTxn;
    const currency = currencyFor(t.iso_currency_code);
    // Flip provider sign: provider positive = outflow -> our negative.
    const ourAmount = Money.fromDecimal(t.amount, currency).negate();
    const inflow = ourAmount.minorUnits >= 0n;
    const status = t.pending ? "PENDING" : "POSTED";
    const date = t.pending ? (t.authorized_date ?? t.date) : t.date;

    return [
      {
        tenantId: this.tenantId,
        accountId: t.account_id,
        externalId: t.transaction_id,
        status,
        date,
        amount: ourAmount,
        description: t.name,
        ...(t.merchant_name ? { counterparty: t.merchant_name } : {}),
        ...(status === "POSTED" && t.pending_transaction_id
          ? { supersedesExternalId: t.pending_transaction_id }
          : {}),
        kind: classify(inflow, t.name, t.category ?? []),
        source: {
          provider: this.provider,
          sourceType: "bank.transaction",
          sourceId: t.transaction_id,
          sourceVersion: raw.sourceVersion,
          fetchedAt: raw.fetchedAt,
        },
      },
    ];
  }
}
