import type { RawArchive } from "./archive.js";
import { InMemoryRawArchive } from "./archive.js";
import { dedupeKey, directionOf } from "./dedupe.js";
import { detectInternalTransfers, type TransferCandidate } from "./transfers.js";
import type {
  CanonicalTransaction,
  IngestIssue,
  IngestResult,
  NormalizedInput,
  ProviderAdapter,
  RawRecord,
  TransactionKind,
} from "./types.js";

/** Internal mutable row; the public type is the readonly CanonicalTransaction. */
interface MutableCanonical extends TransferCandidate {
  tenantId: string;
  externalId: string;
  direction: "INFLOW" | "OUTFLOW";
  description: string;
  counterparty?: string;
  dedupeKey: string;
  source: NormalizedInput["source"];
  supersedes?: string;
  supersededBy?: string;
  supersedesExternalId?: string;
}

export interface PipelineOptions {
  readonly transferWindowDays?: number;
}

/**
 * The ingestion pipeline. Stateful and idempotent: archives raw payloads,
 * normalizes via provider adapters, deduplicates, resolves pending→posted,
 * detects internal transfers, and exposes canonical transactions to downstream
 * services (ledger mapping, forecast inputs). Re-ingesting the same feed adds
 * nothing.
 */
export class IngestionPipeline {
  private readonly adapters = new Map<string, ProviderAdapter>();
  private readonly byId = new Map<string, MutableCanonical>();
  private readonly seen = new Set<string>();
  private readonly transferWindow: number;

  constructor(
    readonly archive: RawArchive = new InMemoryRawArchive(),
    opts: PipelineOptions = {},
  ) {
    this.transferWindow = opts.transferWindowDays ?? 3;
  }

  register(adapter: ProviderAdapter): this {
    this.adapters.set(adapter.provider, adapter);
    return this;
  }

  ingest(raws: readonly RawRecord[]): IngestResult {
    const issues: IngestIssue[] = [];
    const added: string[] = [];
    let archived = 0;
    let duplicates = 0;

    for (const raw of raws) {
      if (!this.archive.has(raw.provider, raw.accountId, raw.externalId)) {
        this.archive.append(raw);
        archived++;
      }
      const adapter = this.adapters.get(raw.provider);
      if (!adapter) {
        issues.push({
          kind: "no_adapter",
          message: `no adapter registered for provider "${raw.provider}"`,
          accountId: raw.accountId,
          externalId: raw.externalId,
        });
        continue;
      }

      let normalized: NormalizedInput[];
      try {
        normalized = adapter.normalize(raw);
      } catch (err) {
        issues.push({
          kind: "normalize_failed",
          message: err instanceof Error ? err.message : String(err),
          accountId: raw.accountId,
          externalId: raw.externalId,
        });
        continue;
      }

      for (const n of normalized) {
        const key = dedupeKey(n);
        if (this.seen.has(key)) {
          duplicates++;
          continue;
        }
        const row: MutableCanonical = {
          id: key,
          tenantId: n.tenantId,
          accountId: n.accountId,
          externalId: n.externalId,
          status: n.status,
          date: n.date,
          amount: n.amount,
          direction: directionOf(n),
          description: n.description,
          ...(n.counterparty ? { counterparty: n.counterparty } : {}),
          kind: n.kind,
          isInternalTransfer: false,
          dedupeKey: key,
          source: n.source,
          ...(n.supersedesExternalId ? { supersedesExternalId: n.supersedesExternalId } : {}),
        };
        this.byId.set(key, row);
        this.seen.add(key);
        added.push(key);
      }
    }

    const pendingSuperseded = this.resolvePendingPosted(added);
    // Single transfer pass over the live rows (mutates isInternalTransfer/kind in place).
    const transfersDetected = detectInternalTransfers([...this.byId.values()], this.transferWindow);

    return {
      added: added.map((id) => this.toPublic(this.byId.get(id)!)),
      archived,
      duplicates,
      pendingSuperseded,
      transfersDetected,
      issues,
    };
  }

  /** Active transactions (superseded pending rows excluded). */
  active(): readonly CanonicalTransaction[] {
    return [...this.byId.values()].filter((r) => !r.supersededBy).map((r) => this.toPublic(r));
  }

  all(): readonly CanonicalTransaction[] {
    return [...this.byId.values()].map((r) => this.toPublic(r));
  }

  private resolvePendingPosted(addedIds: string[]): number {
    let count = 0;
    for (const id of addedIds) {
      const posted = this.byId.get(id);
      if (!posted || posted.status !== "POSTED" || !posted.supersedesExternalId) continue;
      const pendingKey = `${posted.source.provider}:${posted.accountId}:${posted.supersedesExternalId}`;
      const pending = this.byId.get(pendingKey);
      if (pending && pending.status === "PENDING" && !pending.supersededBy) {
        pending.supersededBy = posted.id;
        pending.superseded = true;
        posted.supersedes = pending.id;
        count++;
      }
    }
    return count;
  }

  private toPublic(r: MutableCanonical): CanonicalTransaction {
    const {
      superseded: _superseded,
      supersedesExternalId: _sxid,
      ...rest
    } = r as MutableCanonical & Record<string, unknown>;
    return Object.freeze(rest) as CanonicalTransaction;
  }
}
