import { Money, USD, defineCurrency } from "@rgnr8/ledger-kernel";
import type { NormalizedInput, ProviderAdapter, RawRecord } from "../types.js";
import { TransactionKind } from "../types.js";

/**
 * Normalizes the change-data-capture records the `QboSyncConnector` emits into
 * the same canonical shape as the bank/payroll adapters. Each CDC record is one
 * QBO cash-affecting entity:
 *
 *   Purchase → money OUT of a bank/card account (expense, check, CC charge)
 *   Deposit  → money INTO a bank account
 *   Payment  → a customer payment received (money in)
 *
 * QBO reports `TotalAmt` as a positive number regardless of direction, so the
 * adapter applies the sign from the entity type to reach RGNR8's convention
 * (positive = into the account). Ids are namespaced by entity because QBO ids
 * are only unique per entity type; the connector already prefixes the
 * externalId the same way (`Purchase:123`), and we key provenance off it.
 */

interface QboRef {
  value?: string;
  name?: string;
}
interface QboCdcPayload {
  entity: "Purchase" | "Deposit" | "Payment";
  Id?: string;
  TxnDate?: string;
  TotalAmt?: number;
  CurrencyRef?: QboRef;
  AccountRef?: QboRef;
  DepositToAccountRef?: QboRef;
  EntityRef?: QboRef;
  CustomerRef?: QboRef;
  PaymentType?: string;
  PrivateNote?: string;
  [k: string]: unknown;
}

function currencyFor(code: string | undefined): ReturnType<typeof defineCurrency> {
  if (!code || code === "USD") return USD;
  return defineCurrency(code, 2);
}

export class QboLikeAdapter implements ProviderAdapter {
  readonly provider = "qbo";
  constructor(private readonly tenantId: string) {}

  normalize(raw: RawRecord): NormalizedInput[] {
    const p = raw.payload as QboCdcPayload;
    const currency = currencyFor(p.CurrencyRef?.value);
    const total = Money.fromDecimal(String(p.TotalAmt ?? 0), currency);

    const inflow = p.entity === "Deposit" || p.entity === "Payment";
    const amount = inflow ? total : total.negate();
    const date = p.TxnDate ?? raw.fetchedAt.slice(0, 10);

    const counterparty =
      p.EntityRef?.name ?? p.CustomerRef?.name ?? (p.entity === "Payment" ? "Customer" : undefined);
    const description =
      p.PrivateNote ??
      (p.entity === "Purchase"
        ? `QBO purchase${counterparty ? ` — ${counterparty}` : ""}`
        : p.entity === "Payment"
          ? "Customer payment"
          : "Deposit");

    return [
      {
        tenantId: this.tenantId,
        accountId: raw.accountId,
        externalId: raw.externalId,
        status: "POSTED",
        date,
        amount,
        description,
        ...(counterparty ? { counterparty } : {}),
        kind: kindFor(p.entity),
        source: {
          provider: this.provider,
          sourceType: `qbo.${p.entity.toLowerCase()}`,
          sourceId: raw.externalId,
          sourceVersion: raw.sourceVersion,
          fetchedAt: raw.fetchedAt,
        },
      },
    ];
  }
}

function kindFor(entity: QboCdcPayload["entity"]): TransactionKind {
  switch (entity) {
    case "Deposit":
    case "Payment":
      return TransactionKind.DEPOSIT;
    case "Purchase":
      return TransactionKind.PURCHASE;
  }
}
