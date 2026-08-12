import { asAccountId, asIdempotencyKey, asPeriodKey, asTenantId } from "@rgnr8/ledger-kernel";
import type { AccountId, JournalLineInput, PostCommand, Provenance } from "@rgnr8/ledger-kernel";
import type { CanonicalTransaction } from "./types.js";
import { TransactionKind } from "./types.js";

/** GL accounts a mapping targets. A bank/card account maps to one cash GL account. */
export interface AccountMap {
  readonly cash: AccountId;
  readonly income: AccountId;
  readonly expense: AccountId;
  readonly fees: AccountId;
  readonly payrollExpense: AccountId;
  readonly payrollTaxExpense: AccountId;
  readonly interestIncome: AccountId;
  readonly mappingVersion?: string;
}

export interface MappedCommands {
  readonly commands: readonly PostCommand[];
  readonly skippedTransfers: number;
  readonly skippedZero: number;
}

/** Convenience: build an AccountMap from a code prefix (e.g. "gl."). */
export function defaultAccountMap(prefix = "gl."): AccountMap {
  return {
    cash: asAccountId(`${prefix}cash`),
    income: asAccountId(`${prefix}income`),
    expense: asAccountId(`${prefix}expense`),
    fees: asAccountId(`${prefix}fees`),
    payrollExpense: asAccountId(`${prefix}payroll_expense`),
    payrollTaxExpense: asAccountId(`${prefix}payroll_tax`),
    interestIncome: asAccountId(`${prefix}interest_income`),
    mappingVersion: "map-1",
  };
}

/**
 * Map canonical transactions to balanced kernel posting commands.
 *
 * Each cash movement becomes a two-line journal against the cash account and a
 * counter account chosen by kind/direction. Internal transfers are skipped
 * (they net within cash). The idempotency key is derived from the transaction
 * id, so re-mapping and re-posting is safe.
 */
export function toPostingCommands(
  txns: readonly CanonicalTransaction[],
  map: AccountMap,
): MappedCommands {
  const commands: PostCommand[] = [];
  let skippedTransfers = 0;
  let skippedZero = 0;

  for (const t of txns) {
    if (t.isInternalTransfer) {
      skippedTransfers++;
      continue;
    }
    if (t.amount.minorUnits === 0n) {
      skippedZero++;
      continue;
    }

    const magnitude = t.amount.abs();
    const inflow = t.direction === "INFLOW";
    const counter = counterAccount(t.kind, inflow, map);

    // Cash rises on an inflow (debit cash) and falls on an outflow (credit cash).
    const lines: JournalLineInput[] = inflow
      ? [
          { accountId: map.cash, side: "DEBIT", amount: magnitude },
          { accountId: counter, side: "CREDIT", amount: magnitude },
        ]
      : [
          { accountId: counter, side: "DEBIT", amount: magnitude },
          { accountId: map.cash, side: "CREDIT", amount: magnitude },
        ];

    const provenance: Provenance = {
      sourceSystem: t.source.provider,
      sourceObject: t.source.sourceType,
      sourceVersion: t.source.sourceVersion,
      effectiveDate: t.date,
      postedDate: t.date,
      ingestedAt: t.source.fetchedAt,
      normalizationVersion: "ingest-1",
      mappingVersion: map.mappingVersion ?? "map-1",
    };

    commands.push({
      tenantId: asTenantId(t.tenantId),
      idempotencyKey: asIdempotencyKey(`ingest:${t.id}`),
      periodKey: asPeriodKey(t.date.slice(0, 7)),
      currency: t.amount.currency,
      entryDate: t.date,
      memo: t.description,
      provenance,
      lines,
    });
  }

  return { commands, skippedTransfers, skippedZero };
}

function counterAccount(kind: TransactionKind, inflow: boolean, map: AccountMap): AccountId {
  switch (kind) {
    case TransactionKind.DEPOSIT:
      return map.income;
    case TransactionKind.REFUND:
      return map.expense; // a refund reduces the expense it came from
    case TransactionKind.INTEREST:
      return map.interestIncome;
    case TransactionKind.FEE:
      return map.fees;
    case TransactionKind.PAYROLL_NET:
      return map.payrollExpense;
    case TransactionKind.PAYROLL_TAX:
      return map.payrollTaxExpense;
    case TransactionKind.PURCHASE:
      return map.expense;
    case TransactionKind.TRANSFER:
    case TransactionKind.OTHER:
      return inflow ? map.income : map.expense;
  }
}
