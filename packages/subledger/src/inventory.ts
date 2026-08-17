import {
  Money,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  type AccountId,
  type JournalLineInput,
  type PostCommand,
} from "@rgnr8/ledger-kernel";
import type { DocPostContext } from "./documents.js";

/**
 * Inventory adjustments — the value correction posted when a physical count,
 * shrinkage, or write-down changes what inventory is worth. A positive delta
 * (found stock / write-up) debits the inventory asset and credits the
 * adjustment account; a negative delta (shrinkage / write-down) does the
 * reverse, hitting the adjustment/expense account. Quantity is carried for the
 * record; the *value* delta is what posts (the ledger is in money, not units).
 */

export interface InventoryAdjustment {
  readonly id: string;
  readonly date: string; // ISO
  readonly itemId: string;
  /** Signed change in units (informational; the value delta drives the JE). */
  readonly quantityDelta: number;
  /** Signed change in inventory value; + = increase, − = decrease. */
  readonly valueDelta: Money;
  readonly memo?: string;
}

export interface InventoryAccounts {
  /** Inventory asset account. */
  readonly inventoryAccountId: AccountId;
  /** Adjustment / shrinkage expense account (or COGS). */
  readonly adjustmentAccountId: AccountId;
}

export class InventoryError extends Error {}

/**
 * Build the balanced journal for an inventory value adjustment. Returns
 * undefined for a zero-value adjustment (a pure quantity reclass with no money
 * impact posts nothing).
 */
export function inventoryAdjustmentCommand(
  adj: InventoryAdjustment,
  accounts: InventoryAccounts,
  ctx: DocPostContext,
): PostCommand | undefined {
  if (adj.valueDelta.currency.code !== ctx.currency.code) {
    throw new InventoryError(
      `adjustment currency ${adj.valueDelta.currency.code} != context ${ctx.currency.code}`,
    );
  }
  if (adj.valueDelta.isZero()) return undefined;

  const increase = !adj.valueDelta.isNegative();
  const amount = increase ? adj.valueDelta : adj.valueDelta.negate();
  const lines: JournalLineInput[] = increase
    ? [
        { accountId: accounts.inventoryAccountId, side: "DEBIT", amount, memo: "Inventory increase" },
        { accountId: accounts.adjustmentAccountId, side: "CREDIT", amount, memo: "Inventory adjustment" },
      ]
    : [
        { accountId: accounts.adjustmentAccountId, side: "DEBIT", amount, memo: "Inventory shrinkage/write-down" },
        { accountId: accounts.inventoryAccountId, side: "CREDIT", amount, memo: "Inventory decrease" },
      ];

  return {
    tenantId: asTenantId(ctx.tenantId),
    idempotencyKey: asIdempotencyKey(`invadj:${adj.id}`),
    periodKey: asPeriodKey(adj.date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: adj.date,
    memo: adj.memo ?? `Inventory adjustment ${adj.id}`,
    provenance: { ...ctx.provenance, mappingVersion: ctx.mappingVersion ?? ctx.provenance.mappingVersion },
    lines,
  };
}

/** Running inventory position for one item (units + value), updated by adjustments. */
export interface InventoryPosition {
  readonly itemId: string;
  readonly units: number;
  readonly value: Money;
}

/** Apply an adjustment to a position (pure). */
export function applyAdjustment(pos: InventoryPosition, adj: InventoryAdjustment): InventoryPosition {
  return {
    itemId: pos.itemId,
    units: pos.units + adj.quantityDelta,
    value: pos.value.plus(adj.valueDelta),
  };
}
