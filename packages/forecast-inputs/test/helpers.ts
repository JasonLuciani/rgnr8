import { Money, USD } from "@rgnr8/ledger-kernel";
import { TransactionKind, type CanonicalTransaction } from "@rgnr8/ingestion";

let seq = 0;

export function txn(
  accountId: string,
  date: string,
  amount: string,
  counterparty: string,
  kind: TransactionKind,
): CanonicalTransaction {
  const money = Money.fromDecimal(amount, USD);
  const id = `t${++seq}`;
  return {
    id,
    tenantId: "acme",
    accountId,
    externalId: id,
    status: "POSTED",
    date,
    amount: money,
    direction: money.minorUnits >= 0n ? "INFLOW" : "OUTFLOW",
    description: counterparty,
    counterparty,
    kind,
    isInternalTransfer: false,
    dedupeKey: id,
    source: {
      provider: "test",
      sourceType: "bank.transaction",
      sourceId: id,
      sourceVersion: "1",
      fetchedAt: "2026-08-31T00:00:00Z",
    },
  };
}
