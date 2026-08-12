import { RG, RG_BASE_CSS, RG_TOKENS_CSS, brandBar } from "@rgnr8/ledger-kernel";
import type { ConnectionStore } from "./store.js";
import type { ConnectionHealth } from "./types.js";

/**
 * A connection-health view: aggregate every connection by health, flag the ones
 * that need the owner's attention (auth dead) and the ones that have gone stale
 * (active but not synced recently), and render a self-contained HTML dashboard.
 * Deterministic — `now` is injected, never read from the system clock.
 */

export interface ConnectionHealthRow {
  readonly id: string;
  readonly provider: string;
  readonly tenantId: string;
  readonly health: ConnectionHealth;
  readonly lastSyncedAt: string | undefined;
  readonly lastError: string | undefined;
  readonly hoursSinceSync: number | undefined;
  readonly needsAttention: boolean;
  readonly stale: boolean;
}

export interface ConnectionHealthReport {
  readonly generatedAt: string;
  readonly total: number;
  readonly byHealth: Readonly<Record<ConnectionHealth, number>>;
  readonly needsAttention: readonly ConnectionHealthRow[];
  readonly stale: readonly ConnectionHealthRow[];
  readonly rows: readonly ConnectionHealthRow[];
}

export interface HealthReportOptions {
  /** A connection synced longer ago than this (or never) is "stale". */
  readonly staleAfterHours?: number;
  readonly tenantId?: string;
}

const ATTENTION: ReadonlySet<ConnectionHealth> = new Set(["EXPIRED", "REVOKED", "ERROR"]);

function hoursBetween(fromIso: string, toIso: string): number | undefined {
  const from = Date.parse(fromIso);
  const to = Date.parse(toIso);
  if (Number.isNaN(from) || Number.isNaN(to)) return undefined;
  return (to - from) / 3_600_000;
}

export function buildHealthReport(
  store: ConnectionStore,
  now: string,
  opts: HealthReportOptions = {},
): ConnectionHealthReport {
  const staleAfter = opts.staleAfterHours ?? 26; // a daily sync + slack
  const conns = store.list(opts.tenantId);

  const byHealth: Record<ConnectionHealth, number> = {
    NEW: 0,
    ACTIVE: 0,
    EXPIRED: 0,
    REVOKED: 0,
    ERROR: 0,
  };

  const rows: ConnectionHealthRow[] = conns.map((c) => {
    byHealth[c.health] += 1;
    const hoursSinceSync =
      c.lastSyncedAt !== undefined ? hoursBetween(c.lastSyncedAt, now) : undefined;
    const needsAttention = ATTENTION.has(c.health);
    // stale = healthy-looking but the data is old (or it has never synced)
    const stale =
      !needsAttention &&
      (c.lastSyncedAt === undefined || (hoursSinceSync !== undefined && hoursSinceSync > staleAfter));
    return {
      id: c.id,
      provider: c.provider,
      tenantId: c.tenantId,
      health: c.health,
      lastSyncedAt: c.lastSyncedAt,
      lastError: c.lastError,
      hoursSinceSync,
      needsAttention,
      stale,
    };
  });

  return {
    generatedAt: now,
    total: rows.length,
    byHealth,
    needsAttention: rows.filter((r) => r.needsAttention),
    stale: rows.filter((r) => r.stale),
    rows,
  };
}

// --- ops-status/1 serializer (closes the cross-language ops loop) -------------

/** The connector fragment of a tenant's operational status, as the Python
 * `rgnr8_ops.ConnectorHealth` consumes it. `last_sync` is the most recent sync
 * across the tenant's connections (null if none has ever synced). Pair with the
 * close fragment (`opsCloseStatus` in @rgnr8/close) under `{connectors, close}`
 * and hand to `Fleet.set_status_from_json`. */
export interface OpsConnectorStatusJson {
  readonly total: number;
  readonly needs_attention: number;
  readonly stale: number;
  readonly last_sync: string | null;
}

export function opsConnectorStatus(report: ConnectionHealthReport): OpsConnectorStatusJson {
  let lastSync: string | null = null;
  for (const r of report.rows) {
    if (r.lastSyncedAt !== undefined && (lastSync === null || r.lastSyncedAt > lastSync)) {
      lastSync = r.lastSyncedAt;
    }
  }
  return {
    total: report.total,
    needs_attention: report.needsAttention.length,
    stale: report.stale.length,
    last_sync: lastSync,
  };
}

// --- HTML dashboard ----------------------------------------------------------

function esc(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

const HEALTH_COLOR: Readonly<Record<ConnectionHealth, string>> = {
  ACTIVE: RG.positive,
  NEW: RG.accent,
  EXPIRED: RG.watch,
  REVOKED: RG.risk,
  ERROR: RG.risk,
};

function row(r: ConnectionHealthRow): string {
  const age =
    r.hoursSinceSync === undefined ? "never" : `${r.hoursSinceSync.toFixed(1)}h ago`;
  const flag = r.needsAttention ? "⚠ reconnect" : r.stale ? "• stale" : "ok";
  return `<tr>
    <td>${esc(r.tenantId)}</td>
    <td>${esc(r.provider)}</td>
    <td><span class="dot" style="background:${HEALTH_COLOR[r.health]}"></span>${esc(r.health)}</td>
    <td>${esc(age)}</td>
    <td>${esc(flag)}</td>
    <td class="err">${esc(r.lastError ?? "")}</td>
  </tr>`;
}

export function renderHealthHtml(report: ConnectionHealthReport): string {
  const summary = (Object.keys(report.byHealth) as ConnectionHealth[])
    .map(
      (h) =>
        `<span class="pill"><span class="dot" style="background:${HEALTH_COLOR[h]}"></span>${h}: ${report.byHealth[h]}</span>`,
    )
    .join(" ");
  const rowsHtml = report.rows.map(row).join("\n");
  const attention = report.needsAttention.length;
  const stale = report.stale.length;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connection health</title>
<style>
${RG_TOKENS_CSS}
${RG_BASE_CSS}
  .wrap{max-width:960px;margin:0 auto;padding:24px 20px 56px}
  h1{font-size:22px;margin:0 0 2px;font-weight:800;letter-spacing:-.01em}
  .sub{color:var(--rg-muted);margin:0 0 16px;font-size:13px}
  .pill{display:inline-block;background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:var(--rg-pill);padding:5px 12px;margin:0 6px 6px 0;font-size:12px;font-weight:600}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
  table{border-collapse:separate;border-spacing:0;width:100%;background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:var(--rg-radius);overflow:hidden;margin-top:12px;box-shadow:var(--rg-shadow)}
  th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--rg-line);font-size:13.5px}
  thead th{background:var(--rg-surface);font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-muted)}
  tbody tr:last-child td{border-bottom:none}
  .err{color:var(--rg-risk);font-size:12px}
  .banner{padding:12px 16px;border-radius:var(--rg-radius);margin-bottom:12px;font-weight:700;font-size:13px;border:1px solid transparent}
  .warn{background:#FDF3E4;color:#8A5A00;border-color:#F3DCA6}
  .good{background:#E7F6EF;color:#0A6E4B;border-color:#BFE7D6}
</style></head>
<body>
${brandBar("Connection health")}
<div class="wrap">
  <h1>Connection health</h1>
  <p class="sub">${report.total} connection(s) · generated ${esc(report.generatedAt)}</p>
  ${
    attention > 0
      ? `<div class="banner warn">${attention} connection(s) need the owner to reconnect${stale ? `; ${stale} stale` : ""}.</div>`
      : stale > 0
        ? `<div class="banner warn">${stale} connection(s) are stale (no recent sync).</div>`
        : `<div class="banner good">All connections healthy.</div>`
  }
  <div>${summary}</div>
  <table>
    <thead><tr><th>Tenant</th><th>Provider</th><th>Health</th><th>Last sync</th><th>Flag</th><th>Last error</th></tr></thead>
    <tbody>
${rowsHtml}
    </tbody>
  </table>
</div>
</body></html>`;
}
