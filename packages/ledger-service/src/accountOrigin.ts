/**
 * Where an account came from, and the one-time backfill that records it for
 * tenants that went live from QuickBooks before origins were recorded.
 *
 * Accounts brought over at a QuickBooks go-live carry origin "quickbooks", which
 * keeps them read-only while QuickBooks is the book of record. The go-live sets
 * that itself. A tenant that went live before the origin column existed has none
 * of its accounts marked, so the first chart read or write for it runs
 * {@link backfillAccountOrigin} once. It marks every account the tenant has at
 * that moment, then sets a flag on the tenant's settings so it never runs again.
 *
 * It must run under the tenant's own RLS context. A schema migration has no tenant
 * bound, and under FORCE row-level security it would see no rows at all. So the
 * backend runs this per tenant, inside one transaction holding the tenant's
 * settings row lock (see PostgresBackend.ensureAccountOrigin).
 */

import type { LedgerStore, TenantId } from "@rgnr8/ledger-kernel";
import { OPENING_KEY_PREFIX, QUICKBOOKS_SOURCE } from "./bookOfRecord.js";
import type { SettingsStore } from "./settings.js";

/** The stores and the one write the backfill needs, bound to one transaction when run on Postgres. */
export interface AccountOriginParts {
  readonly settings: SettingsStore;
  readonly store: LedgerStore;
  /** Mark every account the tenant has now as QuickBooks-origin. */
  markAllQuickBooks(): Promise<void>;
}

/**
 * Whether the tenant's books were brought over from QuickBooks: its opening
 * entry carries QuickBooks provenance under the go-live's key. Read once per
 * tenant, by the backfill, so the scan of the journal is a one-time cost.
 */
export async function wentLiveFromQuickBooks(store: LedgerStore, tenant: TenantId): Promise<boolean> {
  for (const entry of await store.list(tenant)) {
    if (entry.provenance.sourceSystem === QUICKBOOKS_SOURCE
      && String(entry.idempotencyKey).startsWith(OPENING_KEY_PREFIX)) {
      return true;
    }
  }
  return false;
}

/**
 * Run the backfill for one tenant if it has not run yet. Idempotent: once the
 * flag is set it returns at once, and the flag is carried forward by every
 * settings save, so an unrelated settings change can't re-run it.
 */
export async function backfillAccountOrigin(parts: AccountOriginParts, tenant: TenantId): Promise<void> {
  const current = await parts.settings.get(tenant);
  if (current.accountOriginBackfilled) return;
  if (await wentLiveFromQuickBooks(parts.store, tenant)) {
    await parts.markAllQuickBooks();
  }
  await parts.settings.save(tenant, { ...current, accountOriginBackfilled: true });
}

/**
 * Record that a go-live has run, so the backfill never treats the accounts it
 * just created as pre-existing. The go-live already marked its own accounts.
 */
export async function settleAccountOrigin(settings: SettingsStore, tenant: TenantId): Promise<void> {
  const current = await settings.get(tenant);
  if (!current.accountOriginBackfilled) {
    await settings.save(tenant, { ...current, accountOriginBackfilled: true });
  }
}
