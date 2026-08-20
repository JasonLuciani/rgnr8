import type { ChartOfAccounts } from "./chartOfAccounts.js";
import type { LedgerStore } from "./ledgerStore.js";
import type { PeriodStore } from "./periods.js";
import { InMemoryPeriodStore } from "./periods.js";
import { validateAndBuildLines } from "./journal.js";
import {
  DuplicateIdempotencyKeyError,
  PeriodClosedError,
  UnknownEntryError,
} from "./errors.js";
import type {
  DraftEntry,
  EntryId,
  IdempotencyKey,
  PeriodKey,
  PostCommand,
  PostedEntry,
  PostedLine,
  Provenance,
  TenantId,
} from "./types.js";

export interface PostOptions {
  /** When the entry is appended (ISO string). Supplied by the application clock. */
  readonly postedAt: string;
}

export interface ReverseOptions {
  readonly idempotencyKey: IdempotencyKey;
  readonly periodKey: PeriodKey;
  readonly entryDate: string;
  readonly postedAt: string;
  readonly provenance: Provenance;
  readonly memo?: string;
}

/**
 * The posting engine — the single typed command API for changing the ledger.
 * Only this service can produce a PostedEntry, and it enforces every invariant:
 * balance, currency, account existence, period locks, idempotency, and
 * append-only immutability. Other modules must go through it; no direct writes.
 *
 * The store owns atomic sequence/id assignment, so this engine is correct under
 * a concurrent, persistent backend as well as the in-memory one.
 */
export class PostingEngine {
  constructor(
    private readonly coa: ChartOfAccounts,
    private readonly store: LedgerStore,
    /**
     * Period-lock seam. Consulted per (tenant, period) before every write.
     * Back-compat: omitting it defaults to a process-local InMemoryPeriodStore,
     * so existing call sites keep their default-OPEN behavior. Production passes
     * a durable `SqlPeriodStore` so sealed periods survive a restart.
     */
    private readonly periods: PeriodStore = new InMemoryPeriodStore(),
  ) {}

  async post(command: PostCommand, opts: PostOptions): Promise<PostedEntry> {
    // Idempotent replay: a matching key returns the original; a divergent
    // payload on the same key is a caller bug and is rejected.
    const existing = await this.store.getByIdempotencyKey(command.tenantId, command.idempotencyKey);
    if (existing) {
      if (fingerprintOfEntry(existing) !== fingerprintOfCommand(command)) {
        throw new DuplicateIdempotencyKeyError(command.idempotencyKey);
      }
      return existing;
    }

    if ((await this.periods.status(command.tenantId, command.periodKey)) === "LOCKED") {
      throw new PeriodClosedError(command.periodKey);
    }

    const lines = validateAndBuildLines(command.lines, command.currency, this.coa);

    const draft: DraftEntry = {
      tenantId: command.tenantId,
      idempotencyKey: command.idempotencyKey,
      periodKey: command.periodKey,
      currency: command.currency,
      entryDate: command.entryDate,
      status: "POSTED",
      lines,
      provenance: Object.freeze({ ...command.provenance }),
      postedAt: opts.postedAt,
      ...(command.memo !== undefined ? { memo: command.memo } : {}),
    };

    return this.store.append(draft);
  }

  /**
   * Reverse a posted entry by appending a NEW entry with the sides swapped.
   * The original is never mutated — correction is by reversal, not edit.
   */
  async reverse(tenant: TenantId, originalId: EntryId, opts: ReverseOptions): Promise<PostedEntry> {
    const original = await this.store.getById(tenant, originalId);
    if (!original) throw new UnknownEntryError(originalId);

    const swapped: PostedLine[] = original.lines.map((l) =>
      Object.freeze({
        accountId: l.accountId,
        side: l.side === "DEBIT" ? "CREDIT" : "DEBIT",
        amount: l.amount,
        ...(l.memo !== undefined ? { memo: l.memo } : {}),
        ...(l.dimensions !== undefined ? { dimensions: l.dimensions } : {}),
      } as PostedLine),
    );
    const memo = opts.memo ?? `Reversal of ${original.id}`;

    // Idempotent replay: a matching key returns the original reversal; a divergent
    // payload on the same key is a caller bug and is rejected — the same guard
    // `post()` applies, so reversal replays can't silently return a different entry.
    const existing = await this.store.getByIdempotencyKey(tenant, opts.idempotencyKey);
    if (existing) {
      const intended = fingerprintOfReversal(
        original.currency.code,
        opts.periodKey,
        opts.entryDate,
        memo,
        opts.provenance,
        swapped,
      );
      if (fingerprintOfEntry(existing) !== intended) {
        throw new DuplicateIdempotencyKeyError(opts.idempotencyKey);
      }
      return existing;
    }

    if ((await this.periods.status(tenant, opts.periodKey)) === "LOCKED") {
      throw new PeriodClosedError(opts.periodKey);
    }

    const draft: DraftEntry = {
      tenantId: tenant,
      idempotencyKey: opts.idempotencyKey,
      periodKey: opts.periodKey,
      currency: original.currency,
      entryDate: opts.entryDate,
      status: "REVERSAL",
      lines: Object.freeze(swapped),
      provenance: Object.freeze({ ...opts.provenance }),
      postedAt: opts.postedAt,
      reversalOf: original.id,
      memo,
    };

    return this.store.append(draft);
  }
}

// --- idempotency fingerprinting ------------------------------------------------
//
// The fingerprint is the full economic identity of an entry: reusing an
// idempotency key with a MATERIALLY different payload must raise, not silently
// return the original. So the fingerprint covers everything that changes what
// the entry means — currency, period, date, entry + line memo, per-line
// dimensional coding, and the provenance record — not just accounts and amounts.
// A genuine idempotent retry re-sends the identical command (including its
// provenance snapshot), so it still dedupes.

/** Deterministic serialization of a line's dimension tags (key-sorted). */
function fingerprintDimensions(dims?: Readonly<Record<string, string>>): string {
  if (!dims) return "";
  return Object.keys(dims)
    .sort()
    .map((k) => `${k}=${dims[k]}`)
    .join(",");
}

function fingerprintProvenance(p: Provenance): string {
  return [
    p.sourceSystem,
    p.sourceObject,
    p.sourceVersion,
    p.effectiveDate,
    p.postedDate,
    p.ingestedAt,
    p.normalizationVersion,
    p.mappingVersion,
  ].join("|");
}

function fingerprintLines(lines: readonly PostedLine[]): string {
  return lines
    .map((l) =>
      [
        l.accountId,
        l.side,
        l.amount.currency.code,
        l.amount.minorUnits,
        l.memo ?? "",
        fingerprintDimensions(l.dimensions),
      ].join("|"),
    )
    .join(";");
}

function fingerprintOfCommand(c: PostCommand): string {
  return [
    c.currency.code,
    c.periodKey,
    c.entryDate,
    c.memo ?? "",
    fingerprintProvenance(c.provenance),
    fingerprintLines(c.lines as readonly PostedLine[]),
  ].join("::");
}

function fingerprintOfEntry(e: PostedEntry): string {
  return [
    e.currency.code,
    e.periodKey,
    e.entryDate,
    e.memo ?? "",
    fingerprintProvenance(e.provenance),
    fingerprintLines(e.lines),
  ].join("::");
}

/** Fingerprint of the reversal a `reverse()` call would produce, for the same
 * idempotency divergence check `post()` runs against a duplicate key. */
function fingerprintOfReversal(
  currencyCode: string,
  periodKey: PeriodKey,
  entryDate: string,
  memo: string,
  provenance: Provenance,
  lines: readonly PostedLine[],
): string {
  return [
    currencyCode,
    periodKey,
    entryDate,
    memo,
    fingerprintProvenance(provenance),
    fingerprintLines(lines),
  ].join("::");
}
