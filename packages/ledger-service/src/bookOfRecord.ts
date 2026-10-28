/**
 * The parallel-run guard: while QuickBooks is the book of record, the ledger is
 * a copy of it, and nothing may write to it except the copying.
 *
 * During a parallel run RGNR8 mirrors one QuickBooks company. A Refresh brings
 * the books level with QuickBooks by appending one catch-up entry, and it
 * refuses to run at all once any entry from anywhere else is in the journal: it
 * could not tell a correction from a difference. So a single invoice, bill or
 * journal entry typed into RGNR8 used to stop every later Refresh, and around
 * thirty posting routes, the public API and MCP could each do it.
 *
 * Two layers, so a route nobody listed still can't slip through:
 *  - {@link isPostingRoute} refuses the known posting routes with a 409 before
 *    they read or write anything (no half-saved document behind the refusal);
 *  - {@link guardBackend} wraps the one write every posting goes through,
 *    `LedgerStore.append`, and refuses there too.
 *
 * Allowed while QuickBooks is the book of record: the go-live opening entry and
 * the mirror's own catch-up entries, told apart by the idempotency key their
 * writers mint AND a QuickBooks source. Neither is something a person or an API
 * caller can produce: the web app stamps its own source on every entry it
 * forwards and refuses "quickbooks" from callers.
 */

import type { DraftEntry, LedgerStore, PostedEntry, TenantId } from "@rgnr8/ledger-kernel";
import type { LedgerBackend } from "./backend.js";

/** The idempotency-key prefix of a QuickBooks mirror catch-up entry (qbo_mirror.py). */
export const MIRROR_KEY_PREFIX = "qbo-mirror:";
/** The idempotency-key prefix of the go-live opening-balance entry (cutover.ts). */
export const OPENING_KEY_PREFIX = "cutover:opening:";
/** The one source the mirror writes under, and that no other caller may claim. */
export const QUICKBOOKS_SOURCE = "quickbooks";

/** What the owner is told, wherever the refusal surfaces. */
export const PARALLEL_RUN_MESSAGE =
  "QuickBooks is the book of record for this account during the parallel run. "
  + "Add this in QuickBooks, then press Refresh to bring it across.";

/** A write refused because QuickBooks is the book of record. Maps to 409. */
export class BookOfRecordError extends Error {
  readonly code = "book_of_record";
  constructor() {
    super(PARALLEL_RUN_MESSAGE);
  }
}

/** True for the two entries a parallel run is made of: the opening and the catch-ups. */
export function isQuickBooksCopy(entry: Pick<DraftEntry, "idempotencyKey" | "provenance">): boolean {
  const key = String(entry.idempotencyKey);
  return entry.provenance.sourceSystem === QUICKBOOKS_SOURCE
    && (key.startsWith(MIRROR_KEY_PREFIX) || key.startsWith(OPENING_KEY_PREFIX));
}

/** Whether this account's books are a QuickBooks copy right now. */
export async function inParallelRun(backend: LedgerBackend, tenant: TenantId): Promise<boolean> {
  return (await backend.settings().get(String(tenant))).bookOfRecord === "quickbooks";
}

/**
 * The tenant routes (the path after `/t/<tenant>/`) that post or reverse a
 * journal entry. `POST entries` is not here: it is the mirror's own route, and
 * the store guard decides it entry by entry.
 */
export function isPostingRoute(method: string, rest: readonly string[]): boolean {
  if (method !== "POST") return false;
  const [head, , action] = rest;
  switch (head) {
    case "entries": // only /entries/<id>/reverse; a plain POST /entries is the mirror's
      return action === "reverse";
    case "ingest":
    case "invoices":
    case "bills":
    case "feed":
    case "debt":
    case "assets":
      return true;
    case "purchase-orders":
      return action === "bill" || action === "receipts";
    case "sales-orders":
    case "estimates":
      return action === "invoice";
    case "jobs":
      return action === "bill" || action === "retainage" || action === "deposits";
    case "inventory":
      return rest[1] === "receipts" || rest[1] === "issues" || rest[1] === "counts";
    case "wip":
      return rest[1] === "post";
    case "work-orders":
      return action === "entries";
    case "recurring":
      return rest[1] === "run";
    case "payroll":
      return rest[1] === "remit" || (rest[1] === "runs" && (rest[3] === "post" || rest[3] === "void"));
    default:
      return false;
  }
}

/** Wrap a store so `append` refuses anything but a QuickBooks copy during a parallel run. */
function guardStore(store: LedgerStore, backend: LedgerBackend): LedgerStore {
  return new Proxy(store, {
    get(target, prop, receiver) {
      if (prop === "append") {
        return async (draft: DraftEntry): Promise<PostedEntry> => {
          if (!isQuickBooksCopy(draft) && await inParallelRun(backend, draft.tenantId)) {
            throw new BookOfRecordError();
          }
          return target.append(draft);
        };
      }
      const value = Reflect.get(target, prop, receiver) as unknown;
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}

/**
 * The backend every handler uses: identical, except that each tenant's store is
 * guarded. The go-live transaction's own stores aren't wrapped; go-live only
 * ever writes the opening entry, which the guard would allow anyway.
 */
export function guardBackend(backend: LedgerBackend): LedgerBackend {
  return new Proxy(backend, {
    get(target, prop, receiver) {
      if (prop === "store") {
        return (tenant: TenantId) => guardStore(target.store(tenant), target);
      }
      const value = Reflect.get(target, prop, receiver) as unknown;
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
}
