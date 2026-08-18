import type { PeriodStore } from "./periods.js";
import type { LedgerStore } from "./ledgerStore.js";
import { executeGoLive, goLiveFromDto, type GoLiveDto } from "./goLive.js";

/**
 * The runtime transport for go-live: a queue of `go-live/1` requests the Python
 * operator control-plane enqueues, and a worker that drains them into the TS
 * accounting core (running {@link executeGoLive} per request).
 *
 * The queue is a seam — an in-memory reference here; production backs it with a
 * table or message queue. Processing is idempotent (executeGoLive is idempotent
 * through the cutover's idempotency key) and failure-isolated (one bad request
 * is recorded and skipped, the rest still run). The per-tenant ledger is
 * supplied by a factory so each tenant posts into its own store.
 */

export interface GoLiveJob {
  readonly id: string;
  readonly dto: GoLiveDto;
}

export interface GoLiveJobResult {
  readonly id: string;
  readonly tenantId: string;
  readonly ok: boolean;
  readonly openingEntryId?: string;
  readonly createdAccounts?: number;
  readonly error?: string;
}

export interface GoLiveQueue {
  /** Jobs not yet processed, oldest first. */
  pending(): Promise<readonly GoLiveJob[]>;
  /** Mark a job finished with its result (removes it from `pending`). */
  markDone(result: GoLiveJobResult): Promise<void>;
}

export class InMemoryGoLiveQueue implements GoLiveQueue {
  private readonly jobs: GoLiveJob[] = [];
  private readonly done = new Map<string, GoLiveJobResult>();

  enqueue(job: GoLiveJob): void {
    this.jobs.push(job);
  }

  pending(): Promise<readonly GoLiveJob[]> {
    return Promise.resolve(this.jobs.filter((j) => !this.done.has(j.id)));
  }

  markDone(result: GoLiveJobResult): Promise<void> {
    this.done.set(result.id, result);
    return Promise.resolve();
  }

  result(id: string): GoLiveJobResult | undefined {
    return this.done.get(id);
  }
}

/** Supplies the (store, periods) a tenant posts its go-live into. */
export type LedgerFor = (tenantId: string) => { store: LedgerStore; periods: PeriodStore };

export class GoLiveWorker {
  constructor(
    private readonly queue: GoLiveQueue,
    private readonly ledgerFor: LedgerFor,
  ) {}

  /**
   * Process every pending go-live job. Returns the per-job results. A job whose
   * DTO is malformed or whose posting fails is recorded as `ok: false` with the
   * error and skipped — it never blocks the others. `postedAt` is injected.
   */
  async drain(postedAt: string): Promise<GoLiveJobResult[]> {
    const results: GoLiveJobResult[] = [];
    for (const job of await this.queue.pending()) {
      const tenantId = job.dto.tenant_id;
      let result: GoLiveJobResult;
      try {
        const request = goLiveFromDto(job.dto);
        const { store, periods } = this.ledgerFor(tenantId);
        const outcome = await executeGoLive(store, periods, request, postedAt);
        result = {
          id: job.id,
          tenantId,
          ok: true,
          openingEntryId: outcome.cutover.entry.id,
          createdAccounts: outcome.createdAccounts.length,
        };
      } catch (err) {
        result = {
          id: job.id,
          tenantId,
          ok: false,
          error: err instanceof Error ? err.message : String(err),
        };
      }
      await this.queue.markDone(result);
      results.push(result);
    }
    return results;
  }
}
