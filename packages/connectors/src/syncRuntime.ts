import type { ConnectionStore } from "./store.js";
import type { ConnectorRunner } from "./runner.js";
import type { Connection, SyncReport } from "./types.js";

/**
 * The inbound companion to the delivery runtime: a periodic loop that pulls bank
 * and payroll data for connections that don't (or can't only) rely on webhooks.
 * Each tick decides which connections are due, runs `ConnectorRunner.sync` on
 * them, hands the RawRecords to a sink (the ingestion pipeline), and schedules
 * the next attempt — backing off failing connections and skipping ones that need
 * the owner to reconnect.
 *
 * Deterministic: the clock (`nowMs`) is injected, never read here, so a tick is
 * fully testable. Backoff/scheduling state lives in the runtime (a production
 * deployment persists it); connection health lives on the connection.
 */

/** What to do with a successful sync's records. */
export type SyncSink = (report: SyncReport) => void | Promise<void>;

/** The OAuth token-refresh seam. Given a connection whose access token is
 * expired (or about to be), return refreshed credentials — or `null` when the
 * refresh token itself is dead and the owner must reconnect. Production wraps a
 * real Plaid/Gusto/QBO token endpoint; tests use a fake. */
export interface TokenRefreshResult {
  readonly accessToken: string;
  readonly refreshToken?: string;
  /** ISO instant the new access token expires (for pre-emptive refresh). */
  readonly accessTokenExpiresAt?: string;
}
export interface TokenRefresher {
  refresh(conn: Connection): Promise<TokenRefreshResult | null>;
}

/** Backoff/scheduling state per connection — persist this so retry cadence and
 * failure counts survive a restart (an in-memory default is used otherwise). */
export interface SyncSchedule {
  failures: number;
  nextAttemptMs: number;
}
export interface SyncScheduleStore {
  get(connectionId: string): SyncSchedule | undefined;
  set(connectionId: string, schedule: SyncSchedule): void;
  delete(connectionId: string): void;
}
export class InMemorySyncScheduleStore implements SyncScheduleStore {
  private readonly m = new Map<string, SyncSchedule>();
  get(id: string): SyncSchedule | undefined {
    return this.m.get(id);
  }
  set(id: string, s: SyncSchedule): void {
    this.m.set(id, s);
  }
  delete(id: string): void {
    this.m.delete(id);
  }
}

export interface SyncRuntimeOptions {
  /** Normal cadence between successful syncs of a connection. */
  readonly intervalMs?: number;
  /** First retry delay after an error; doubles each consecutive failure. */
  readonly backoffBaseMs?: number;
  /** Cap on the backoff delay. */
  readonly backoffMaxMs?: number;
  /** Persist backoff state here (defaults to in-memory, lost on restart). */
  readonly schedules?: SyncScheduleStore;
  /** If set, expired connections are auto-refreshed before they're skipped. */
  readonly refresher?: TokenRefresher;
}

export interface ConnectionSyncOutcome {
  readonly connectionId: string;
  readonly synced: boolean;
  readonly health: SyncReport["health"] | "SKIPPED";
  readonly reason?: string;
  readonly records: number;
}

export interface SyncTickReport {
  readonly synced: number;
  readonly skipped: number;
  readonly outcomes: readonly ConnectionSyncOutcome[];
}

export class SyncRuntime {
  private readonly intervalMs: number;
  private readonly backoffBaseMs: number;
  private readonly backoffMaxMs: number;
  private readonly schedules: SyncScheduleStore;
  private readonly refresher: TokenRefresher | undefined;

  constructor(
    private readonly store: ConnectionStore,
    private readonly runner: ConnectorRunner,
    private readonly sink: SyncSink,
    opts: SyncRuntimeOptions = {},
  ) {
    this.intervalMs = opts.intervalMs ?? 3_600_000; // hourly
    this.backoffBaseMs = opts.backoffBaseMs ?? 60_000; // 1 min
    this.backoffMaxMs = opts.backoffMaxMs ?? 21_600_000; // 6 h
    this.schedules = opts.schedules ?? new InMemorySyncScheduleStore();
    this.refresher = opts.refresher;
  }

  /** Attempt an OAuth token refresh for an expired connection. Returns true and
   * updates the stored connection to ACTIVE on success; false → owner must
   * reconnect (health left EXPIRED/REVOKED). No-op without a refresher. */
  private async tryRefresh(conn: Connection): Promise<boolean> {
    if (this.refresher === undefined) return false;
    const result = await this.refresher.refresh(conn);
    if (result === null) return false;
    conn.accessToken = result.accessToken;
    if (result.refreshToken !== undefined) conn.refreshToken = result.refreshToken;
    if (result.accessTokenExpiresAt !== undefined) {
      conn.accessTokenExpiresAt = result.accessTokenExpiresAt;
    }
    conn.health = "ACTIVE";
    this.store.put(conn);
    return true;
  }

  private backoff(failures: number): number {
    const delay = this.backoffBaseMs * 2 ** Math.max(0, failures - 1);
    return Math.min(delay, this.backoffMaxMs);
  }

  /** When a connection is next eligible, absent runtime backoff state. */
  private baseNextAttempt(lastSyncedAt: string | undefined): number {
    if (lastSyncedAt === undefined) return 0; // never synced → due now
    const t = Date.parse(lastSyncedAt);
    return Number.isNaN(t) ? 0 : t + this.intervalMs;
  }

  async tick(nowMs: number, fetchedAt: string): Promise<SyncTickReport> {
    const outcomes: ConnectionSyncOutcome[] = [];
    for (const conn of this.store.list()) {
      // A REVOKED connection needs a human. An EXPIRED one gets one automatic
      // token-refresh attempt (if a refresher is configured) before we give up.
      if (conn.health === "REVOKED") {
        outcomes.push({ connectionId: conn.id, synced: false, health: "SKIPPED", reason: "revoked", records: 0 });
        continue;
      }
      if (conn.health === "EXPIRED") {
        const refreshed = await this.tryRefresh(conn);
        if (!refreshed) {
          outcomes.push({ connectionId: conn.id, synced: false, health: "SKIPPED", reason: "expired", records: 0 });
          continue;
        }
        // token refreshed → fall through and sync now
        this.schedules.delete(conn.id);
      }
      const sched = this.schedules.get(conn.id);
      const nextAttempt = sched ? sched.nextAttemptMs : this.baseNextAttempt(conn.lastSyncedAt);
      if (nowMs < nextAttempt) {
        outcomes.push({ connectionId: conn.id, synced: false, health: "SKIPPED", reason: "not_due", records: 0 });
        continue;
      }

      const report = await this.runner.sync(conn.id, fetchedAt);
      if (report.health === "ACTIVE") {
        this.schedules.set(conn.id, { failures: 0, nextAttemptMs: nowMs + this.intervalMs });
        await this.sink(report);
        outcomes.push({ connectionId: conn.id, synced: true, health: "ACTIVE", records: report.rawRecords.length });
      } else if (report.health === "ERROR") {
        const failures = (sched?.failures ?? 0) + 1;
        this.schedules.set(conn.id, { failures, nextAttemptMs: nowMs + this.backoff(failures) });
        outcomes.push({ connectionId: conn.id, synced: false, health: "ERROR", reason: "retry_scheduled", records: 0 });
      } else {
        // discovered EXPIRED/REVOKED during the sync — stop auto-retrying
        this.schedules.delete(conn.id);
        outcomes.push({ connectionId: conn.id, synced: false, health: report.health, reason: "needs_reconnect", records: 0 });
      }
    }
    const synced = outcomes.filter((o) => o.synced).length;
    return { synced, skipped: outcomes.length - synced, outcomes };
  }
}

export interface ServeSyncOptions {
  readonly intervalMs?: number;
  /** Injected clock returning epoch ms; defaults to Date.now. */
  readonly now?: () => number;
  readonly sleep?: (ms: number) => Promise<void>;
  readonly maxTicks?: number;
}

/** The thin real-clock loop around `SyncRuntime.tick`. `now`/`sleep` are
 * injectable so a harness drives it without real time. */
export async function serveSync(runtime: SyncRuntime, opts: ServeSyncOptions = {}): Promise<void> {
  const interval = opts.intervalMs ?? 300_000; // 5 min tick
  const now = opts.now ?? (() => Date.now());
  const sleep = opts.sleep ?? ((ms: number) => new Promise<void>((r) => setTimeout(r, ms)));
  let ticks = 0;
  while (opts.maxTicks === undefined || ticks < opts.maxTicks) {
    const t = now();
    await runtime.tick(t, new Date(t).toISOString());
    ticks++;
    if (opts.maxTicks !== undefined && ticks >= opts.maxTicks) break;
    await sleep(interval);
  }
}
