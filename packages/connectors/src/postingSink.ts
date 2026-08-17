import {
  IngestionPipeline,
  postCanonicalToLedger,
  type AccountMap,
  type IngestResult,
  type PostingReport,
  type ProviderAdapter,
  type RuleSet,
} from "@rgnr8/ingestion";
import type { LedgerStore, PostingEngine } from "@rgnr8/ledger-kernel";
import type { SyncReport } from "./types.js";
import type { SyncSink } from "./syncRuntime.js";

/**
 * The missing wire between continuous sync and the ledger. `SyncRuntime` pulls
 * provider records and hands each successful `SyncReport` to a `SyncSink`, but
 * on its own a sink does nothing with them. This sink is the real one: it feeds
 * the raw records through the ingestion pipeline (adapters → dedupe → transfer
 * detection) and then posts the resulting canonical transactions into the
 * double-entry ledger via `postCanonicalToLedger` — idempotently, so replays
 * and overlapping sync pages never double-post.
 *
 * With a `QboLikeAdapter` registered, this closes the QBO loop end to end:
 * QBO CDC → RawRecords → canonical transactions → posted journal entries. The
 * same sink handles Plaid/Gusto records too — whatever adapters are registered.
 */

export interface LedgerSinkOptions {
  /** The provider adapters that turn RawRecords into canonical transactions. */
  readonly adapters: readonly ProviderAdapter[];
  readonly engine: PostingEngine;
  readonly store: LedgerStore;
  readonly accountMap: AccountMap;
  /** Categorization rules applied while posting (optional). */
  readonly rules?: RuleSet;
  /** Reuse one pipeline across ticks so dedupe/supersession state persists. */
  readonly pipeline?: IngestionPipeline;
}

export interface LedgerSinkOutcome {
  readonly connectionId: string;
  readonly ingest: IngestResult;
  readonly posting: PostingReport;
}

/**
 * A `SyncSink` that ingests a report's raw records and posts them to the ledger.
 * `onOutcome` receives the ingest + posting result for observability/tests.
 * `postedAt` is injected (no wall clock) so runs are deterministic; it defaults
 * to the report's implied fetch instant via the raw records.
 */
export class LedgerPostingSink {
  private readonly pipeline: IngestionPipeline;

  constructor(
    private readonly opts: LedgerSinkOptions,
    private readonly onOutcome?: (outcome: LedgerSinkOutcome) => void,
  ) {
    this.pipeline = opts.pipeline ?? new IngestionPipeline();
    for (const adapter of opts.adapters) this.pipeline.register(adapter);
  }

  /** Bind as a `SyncSink`. `postedAt` supplies the ledger post timestamp. */
  asSink(postedAt: string): SyncSink {
    return async (report: SyncReport): Promise<void> => {
      await this.consume(report, postedAt);
    };
  }

  async consume(report: SyncReport, postedAt: string): Promise<LedgerSinkOutcome> {
    const ingest = this.pipeline.ingest(report.rawRecords);
    const posting = await postCanonicalToLedger(
      this.pipeline.active(),
      this.opts.accountMap,
      this.opts.engine,
      this.opts.store,
      { postedAt, ...(this.opts.rules ? { rules: this.opts.rules } : {}) },
    );
    const outcome: LedgerSinkOutcome = { connectionId: report.connectionId, ingest, posting };
    this.onOutcome?.(outcome);
    return outcome;
  }
}
