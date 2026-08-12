import type { RawRecord } from "@rgnr8/ingestion";
import type { ConnectionStore } from "./store.js";
import {
  AuthExpiredError,
  AuthRevokedError,
  ProviderHttpError,
  RateLimitError,
  type Connector,
  type HttpClient,
  type SyncIssue,
  type SyncReport,
} from "./types.js";

export interface RunnerOptions {
  readonly maxPages?: number;
  readonly maxRetries?: number;
  /** Injected so tests are deterministic; defaults to a real timer. */
  readonly sleep?: (ms: number) => Promise<void>;
}

const realSleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/**
 * Drives incremental sync for a connection: paginates via the connector's
 * cursor, retries rate limits with backoff, updates connection health on auth
 * failures, and returns the RawRecords for the ingestion pipeline. Bounded by
 * maxPages so one run can't loop forever.
 */
export class ConnectorRunner {
  private readonly connectors = new Map<string, Connector>();
  private readonly maxPages: number;
  private readonly maxRetries: number;
  private readonly sleep: (ms: number) => Promise<void>;

  constructor(
    private readonly store: ConnectionStore,
    private readonly http: HttpClient,
    connectors: readonly Connector[],
    opts: RunnerOptions = {},
  ) {
    for (const c of connectors) this.connectors.set(c.provider, c);
    this.maxPages = opts.maxPages ?? 20;
    this.maxRetries = opts.maxRetries ?? 3;
    this.sleep = opts.sleep ?? realSleep;
  }

  async sync(connectionId: string, fetchedAt: string): Promise<SyncReport> {
    const conn = this.store.get(connectionId);
    const issues: SyncIssue[] = [];
    if (!conn) {
      return { connectionId, rawRecords: [], pages: 0, removed: 0, health: "ERROR", truncated: false, issues: [{ kind: "unknown_connection", message: connectionId }] };
    }
    const connector = this.connectors.get(conn.provider);
    if (!connector) {
      conn.health = "ERROR";
      this.store.put(conn);
      return { connectionId, rawRecords: [], pages: 0, removed: 0, health: "ERROR", truncated: false, issues: [{ kind: "no_connector", message: conn.provider }] };
    }
    if (conn.health === "REVOKED") {
      return { connectionId, rawRecords: [], pages: 0, removed: 0, health: "REVOKED", truncated: false, issues: [{ kind: "revoked", message: "reconnect required" }] };
    }

    const raws: RawRecord[] = [];
    let pages = 0;
    let removed = 0;
    let truncated = false;
    let terminalHealth: SyncReport["health"] = "ACTIVE";

    while (pages < this.maxPages) {
      let retries = 0;
      let advanced = false;
      // retry loop for a single page
      // eslint-disable-next-line no-constant-condition
      while (true) {
        try {
          const page = await connector.syncPage(conn, this.http, fetchedAt);
          raws.push(...page.rawRecords);
          removed += page.removed;
          if (page.nextCursor !== undefined) conn.cursor = page.nextCursor;
          pages++;
          advanced = true;
          if (!page.hasMore) {
            terminalHealth = "ACTIVE";
            this.finish(conn, terminalHealth, fetchedAt, undefined);
            return { connectionId, rawRecords: raws, pages, removed, health: terminalHealth, truncated, issues };
          }
          break; // page done, continue outer pagination
        } catch (err) {
          if (err instanceof RateLimitError && retries < this.maxRetries) {
            retries++;
            issues.push({ kind: "rate_limited", message: `retry ${retries} after ${err.retryAfterMs}ms` });
            await this.sleep(err.retryAfterMs);
            continue; // retry same page
          }
          terminalHealth = this.healthFor(err);
          issues.push({ kind: terminalHealth.toLowerCase(), message: err instanceof Error ? err.message : String(err) });
          this.finish(conn, terminalHealth, fetchedAt, err instanceof Error ? err.message : String(err));
          return { connectionId, rawRecords: raws, pages, removed, health: terminalHealth, truncated, issues };
        }
      }
      if (!advanced) break;
    }

    // reached the page cap with more still available
    truncated = true;
    issues.push({ kind: "truncated", message: `stopped after ${this.maxPages} pages; more remain` });
    this.finish(conn, "ACTIVE", fetchedAt, undefined);
    return { connectionId, rawRecords: raws, pages, removed, health: "ACTIVE", truncated, issues };
  }

  private healthFor(err: unknown): SyncReport["health"] {
    if (err instanceof AuthExpiredError) return "EXPIRED";
    if (err instanceof AuthRevokedError) return "REVOKED";
    if (err instanceof ProviderHttpError || err instanceof RateLimitError) return "ERROR";
    return "ERROR";
  }

  private finish(
    conn: NonNullable<ReturnType<ConnectionStore["get"]>>,
    health: SyncReport["health"],
    fetchedAt: string,
    error: string | undefined,
  ): void {
    conn.health = health;
    if (health === "ACTIVE") conn.lastSyncedAt = fetchedAt;
    if (error !== undefined) conn.lastError = error;
    this.store.put(conn);
  }
}
