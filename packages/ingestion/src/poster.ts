import {
  PeriodClosedError,
  asTenantId,
  type IdempotencyKey,
  type LedgerStore,
  type PostCommand,
  type PostedEntry,
  type PostingEngine,
} from "@rgnr8/ledger-kernel";

import type { CanonicalTransaction } from "./types.js";
import { toPostingCommands, type AccountMap } from "./mapping.js";

/**
 * The wire from the ingested feed to the double-entry ledger.
 *
 * `mapping.toPostingCommands` turns canonical transactions into balanced posting
 * commands, but on its own it does nothing — this is the piece that actually
 * drives them into the `PostingEngine`, which is what keeps the ledger *current*
 * (not just seeded once at migration). It is:
 *
 * - **idempotent** — each command carries `ingest:<txnId>` as its idempotency key,
 *   so re-running over the same feed never double-posts; already-posted
 *   transactions are counted, not re-appended;
 * - **period-safe** — a transaction dated into a *closed* period is reported as
 *   `blockedByClosedPeriod` rather than throwing, so one locked month can't wedge
 *   the batch;
 * - **failure-isolated** — a single bad command is captured in `failed` and the
 *   rest still post.
 *
 * This closes the "it's only an overlay" gap: with this running on each ingest
 * tick, the RGNR8 ledger stays the system of record for ongoing bank/payroll
 * activity.
 */
export interface PostingReport {
  /** Newly posted this run. */
  readonly posted: number;
  /** Already in the ledger (idempotent replay) — not re-posted. */
  readonly alreadyPosted: number;
  /** Internal transfers skipped by the mapping (not income/expense). */
  readonly skippedTransfers: number;
  /** Zero-amount transactions skipped by the mapping. */
  readonly skippedZero: number;
  /** Transactions dated into a locked period — deferred, not posted. */
  readonly blockedByClosedPeriod: number;
  /** Commands that errored (kept isolated so the batch continues). */
  readonly failed: readonly { readonly key: string; readonly error: string }[];
  /** The posted entries produced this run (in order). */
  readonly entries: readonly PostedEntry[];
}

export interface PostOptions {
  /** The post timestamp stamped on new entries (injected — no wall clock). */
  readonly postedAt: string;
}

/**
 * Post the given canonical transactions to the ledger, idempotently. `store` must
 * be the same store the `engine` writes to (used to detect already-posted keys).
 */
export async function postCanonicalToLedger(
  txns: readonly CanonicalTransaction[],
  map: AccountMap,
  engine: PostingEngine,
  store: LedgerStore,
  opts: PostOptions,
): Promise<PostingReport> {
  const { commands, skippedTransfers, skippedZero } = toPostingCommands(txns, map);

  let posted = 0;
  let alreadyPosted = 0;
  let blockedByClosedPeriod = 0;
  const failed: { key: string; error: string }[] = [];
  const entries: PostedEntry[] = [];

  for (const command of commands) {
    const existing = await alreadyInLedger(store, command);
    if (existing !== undefined) {
      alreadyPosted++;
      continue;
    }
    try {
      const entry = await engine.post(command, { postedAt: opts.postedAt });
      entries.push(entry);
      posted++;
    } catch (err) {
      if (err instanceof PeriodClosedError) {
        blockedByClosedPeriod++;
        continue;
      }
      failed.push({ key: String(command.idempotencyKey), error: errorMessage(err) });
    }
  }

  return {
    posted,
    alreadyPosted,
    skippedTransfers,
    skippedZero,
    blockedByClosedPeriod,
    failed,
    entries,
  };
}

async function alreadyInLedger(
  store: LedgerStore,
  command: PostCommand,
): Promise<PostedEntry | undefined> {
  return store.getByIdempotencyKey(
    asTenantId(String(command.tenantId)),
    command.idempotencyKey as IdempotencyKey,
  );
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? `${err.name}: ${err.message}` : String(err);
}
