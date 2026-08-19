import { Money, getCurrency } from "@rgnr8/ledger-kernel";
import type {
  AccountId,
  DraftEntry,
  EntryId,
  EntrySide,
  EntryStatus,
  IdempotencyKey,
  LedgerStore,
  PostedEntry,
  PostedLine,
  Provenance,
  TenantId,
} from "@rgnr8/ledger-kernel";
import { CORE_DDL } from "./schema.js";

/** Minimal structural view of a node-postgres Pool (also satisfied by pg-mem). */
export interface QueryResult {
  rows: Record<string, unknown>[];
}
export interface Queryable {
  query(text: string, values?: unknown[]): Promise<QueryResult>;
}
export interface PoolClient extends Queryable {
  release(): void;
}
export interface Pool extends Queryable {
  connect(): Promise<PoolClient>;
}

/**
 * PostgreSQL implementation of the kernel's append-only LedgerStore.
 *
 * Guarantees:
 * - Atomic, gap-free per-tenant sequencing via a counter row bumped inside the
 *   same transaction as the insert (appends for a tenant serialize on it).
 * - Idempotency via UNIQUE (tenant_id, idempotency_key); a concurrent duplicate
 *   rolls back its own sequence bump and returns the winning entry — no gap.
 * - Append-only: this class issues no UPDATE or DELETE against posted rows.
 * - Tenant isolation: every query filters by tenant_id (RLS is the second layer).
 */
export class PgLedgerStore implements LedgerStore {
  constructor(private readonly pool: Pool) {}

  /** Create tables and indexes if absent. Safe to run repeatedly. */
  async migrate(): Promise<void> {
    await this.pool.query(CORE_DDL);
  }

  async append(draft: DraftEntry): Promise<PostedEntry> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      await client.query(SET_TENANT_GUC, [draft.tenantId]);

      const pre = await this.loadByIdempotencyKey(client, draft.tenantId, draft.idempotencyKey);
      if (pre) {
        await client.query("COMMIT");
        return pre;
      }

      const seqRes = await client.query(
        `INSERT INTO ledger_tenant_seq (tenant_id, last_seq) VALUES ($1, 1)
         ON CONFLICT (tenant_id) DO UPDATE SET last_seq = ledger_tenant_seq.last_seq + 1
         RETURNING last_seq`,
        [draft.tenantId],
      );
      const sequence = Number((seqRes.rows[0] as { last_seq: string | number }).last_seq);
      const id = `${draft.tenantId}:${sequence}` as EntryId;

      await client.query(
        `INSERT INTO journal_entry (
           tenant_id, sequence, id, idempotency_key, period_key, currency_code,
           entry_date, status, memo, reversal_of, posted_at,
           prov_source_system, prov_source_object, prov_source_version,
           prov_effective_date, prov_posted_date, prov_ingested_at,
           prov_normalization_version, prov_mapping_version
         ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)`,
        [
          draft.tenantId,
          sequence,
          id,
          draft.idempotencyKey,
          draft.periodKey,
          draft.currency.code,
          draft.entryDate,
          draft.status,
          draft.memo ?? null,
          draft.reversalOf ?? null,
          draft.postedAt,
          draft.provenance.sourceSystem,
          draft.provenance.sourceObject,
          draft.provenance.sourceVersion,
          draft.provenance.effectiveDate,
          draft.provenance.postedDate,
          draft.provenance.ingestedAt,
          draft.provenance.normalizationVersion,
          draft.provenance.mappingVersion,
        ],
      );

      let lineIndex = 0;
      for (const line of draft.lines) {
        await client.query(
          `INSERT INTO journal_line (
             tenant_id, entry_sequence, line_index, account_id, side,
             amount_minor, currency_code, memo, dimensions
           ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)`,
          [
            draft.tenantId,
            sequence,
            lineIndex++,
            line.accountId,
            line.side,
            line.amount.minorUnits.toString(),
            line.amount.currency.code,
            line.memo ?? null,
            line.dimensions ? JSON.stringify(line.dimensions) : null,
          ],
        );
      }

      await client.query("COMMIT");
      return this.hydrate(draft.tenantId, {
        sequence,
        id,
        idempotency_key: draft.idempotencyKey,
        period_key: draft.periodKey,
        currency_code: draft.currency.code,
        entry_date: draft.entryDate,
        status: draft.status,
        memo: draft.memo ?? null,
        reversal_of: draft.reversalOf ?? null,
        posted_at: draft.postedAt,
        prov_source_system: draft.provenance.sourceSystem,
        prov_source_object: draft.provenance.sourceObject,
        prov_source_version: draft.provenance.sourceVersion,
        prov_effective_date: draft.provenance.effectiveDate,
        prov_posted_date: draft.provenance.postedDate,
        prov_ingested_at: draft.provenance.ingestedAt,
        prov_normalization_version: draft.provenance.normalizationVersion,
        prov_mapping_version: draft.provenance.mappingVersion,
      }, draft.lines);
    } catch (err) {
      await safeRollback(client);
      // A concurrent duplicate lost the race on the idempotency constraint:
      // its sequence bump rolled back (no gap); return the winning entry.
      if (isIdempotencyConflict(err)) {
        const winner = await this.getByIdempotencyKey(draft.tenantId, draft.idempotencyKey);
        if (winner) return winner;
      }
      throw err;
    } finally {
      client.release();
    }
  }

  async getByIdempotencyKey(tenant: TenantId, key: IdempotencyKey): Promise<PostedEntry | undefined> {
    return this.withTenant(tenant, (q) => this.loadByIdempotencyKey(q, tenant, key));
  }

  async getById(tenant: TenantId, id: EntryId): Promise<PostedEntry | undefined> {
    return this.withTenant(tenant, async (q) => {
      const res = await q.query(
        `SELECT * FROM journal_entry WHERE tenant_id = $1 AND id = $2`,
        [tenant, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      return this.hydrate(tenant, row, await this.loadLines(q, tenant, Number(row["sequence"])));
    });
  }

  async getBySequence(tenant: TenantId, sequence: number): Promise<PostedEntry | undefined> {
    return this.withTenant(tenant, async (q) => {
      const res = await q.query(
        `SELECT * FROM journal_entry WHERE tenant_id = $1 AND sequence = $2`,
        [tenant, sequence],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      return this.hydrate(tenant, row, await this.loadLines(q, tenant, sequence));
    });
  }

  async list(tenant: TenantId): Promise<readonly PostedEntry[]> {
    return this.withTenant(tenant, async (q) => {
      const entries = await q.query(
        `SELECT * FROM journal_entry WHERE tenant_id = $1 ORDER BY sequence ASC`,
        [tenant],
      );
      const out: PostedEntry[] = [];
      for (const row of entries.rows) {
        out.push(this.hydrate(tenant, row, await this.loadLines(q, tenant, Number(row["sequence"]))));
      }
      return out;
    });
  }

  /**
   * Net debit position per account over an `entryDate` window, aggregated in the
   * database. This is the pushdown that keeps a trial balance or statement from
   * streaming a tenant's entire journal into memory: `SUM(...) GROUP BY account`
   * against the numeric amount column, filtered by the entry date on the parent
   * entry. Debit is positive, credit negative — identical to summing every line
   * of `list()` in the same window, which the kernel's parity test asserts.
   */
  async netByAccount(
    tenant: TenantId, window?: { from?: string; to?: string },
  ): Promise<Map<AccountId, bigint>> {
    return this.withTenant(tenant, async (q) => {
      const params: unknown[] = [tenant];
      const clauses: string[] = ["l.tenant_id = $1"];
      if (window?.from !== undefined) {
        params.push(window.from);
        clauses.push(`e.entry_date >= $${params.length}`);
      }
      if (window?.to !== undefined) {
        params.push(window.to);
        clauses.push(`e.entry_date <= $${params.length}`);
      }
      const res = await q.query(
        `SELECT l.account_id AS account_id,
                SUM(CASE WHEN l.side = 'DEBIT' THEN l.amount_minor ELSE -l.amount_minor END) AS net
           FROM journal_line l
           JOIN journal_entry e
             ON e.tenant_id = l.tenant_id AND e.sequence = l.entry_sequence
          WHERE ${clauses.join(" AND ")}
          GROUP BY l.account_id`,
        params,
      );
      // Include every account with any line, even a net of zero — matching the
      // in-memory reference, which keeps a touched-but-balanced account so it can
      // still appear on the trial balance as a zero row.
      const out = new Map<AccountId, bigint>();
      for (const row of res.rows) {
        out.set(String(row["account_id"]) as AccountId, BigInt(String(row["net"] ?? "0")));
      }
      return out;
    });
  }

  // --- internals ---------------------------------------------------------------

  /**
   * Run reads inside a transaction that first binds the tenant GUC
   * (`app.tenant_id`) so Postgres RLS policies engage. `is_local = true` scopes
   * the binding to this transaction, so a pooled connection never leaks one
   * tenant's binding to the next caller.
   */
  private async withTenant<T>(tenant: TenantId, fn: (q: Queryable) => Promise<T>): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      await client.query(SET_TENANT_GUC, [tenant]);
      const result = await fn(client);
      await client.query("COMMIT");
      return result;
    } catch (err) {
      await safeRollback(client);
      throw err;
    } finally {
      client.release();
    }
  }

  private async loadByIdempotencyKey(
    q: Queryable,
    tenant: TenantId,
    key: IdempotencyKey,
  ): Promise<PostedEntry | undefined> {
    const res = await q.query(
      `SELECT * FROM journal_entry WHERE tenant_id = $1 AND idempotency_key = $2`,
      [tenant, key],
    );
    const row = res.rows[0];
    if (!row) return undefined;
    return this.hydrate(tenant, row, await this.loadLines(q, tenant, Number(row["sequence"])));
  }

  private async loadLines(
    q: Queryable,
    tenant: TenantId,
    sequence: number,
  ): Promise<readonly RawLine[]> {
    const res = await q.query(
      `SELECT account_id, side, amount_minor, currency_code, memo, dimensions
         FROM journal_line
        WHERE tenant_id = $1 AND entry_sequence = $2
        ORDER BY line_index ASC`,
      [tenant, sequence],
    );
    return res.rows as unknown as RawLine[];
  }

  private hydrate(
    tenant: TenantId,
    row: Record<string, unknown> | EntryRowLike,
    lines: readonly (RawLine | PostedLine)[],
  ): PostedEntry {
    const r = row as EntryRowLike;
    const currency = getCurrency(String(r.currency_code));

    const postedLines: PostedLine[] = lines.map((l) => {
      if ("amount" in l) return l; // already a PostedLine (write path)
      const raw = l as RawLine;
      const lineCurrency = getCurrency(String(raw.currency_code));
      const dims = parseDimensions(raw.dimensions);
      return Object.freeze({
        accountId: String(raw.account_id) as AccountId,
        side: String(raw.side) as EntrySide,
        amount: Money.fromMinorUnits(BigInt(String(raw.amount_minor)), lineCurrency),
        ...(raw.memo != null ? { memo: String(raw.memo) } : {}),
        ...(dims ? { dimensions: dims } : {}),
      });
    });

    const provenance: Provenance = Object.freeze({
      sourceSystem: String(r.prov_source_system),
      sourceObject: String(r.prov_source_object),
      sourceVersion: String(r.prov_source_version),
      effectiveDate: String(r.prov_effective_date),
      postedDate: String(r.prov_posted_date),
      ingestedAt: String(r.prov_ingested_at),
      normalizationVersion: String(r.prov_normalization_version),
      mappingVersion: String(r.prov_mapping_version),
    });

    return Object.freeze({
      id: String(r.id) as EntryId,
      tenantId: tenant,
      sequence: Number(r.sequence),
      idempotencyKey: String(r.idempotency_key) as IdempotencyKey,
      periodKey: String(r.period_key) as PostedEntry["periodKey"],
      currency,
      entryDate: String(r.entry_date),
      status: String(r.status) as EntryStatus,
      lines: Object.freeze(postedLines),
      provenance,
      postedAt: String(r.posted_at),
      ...(r.memo != null ? { memo: String(r.memo) } : {}),
      ...(r.reversal_of != null ? { reversalOf: String(r.reversal_of) as EntryId } : {}),
    });
  }
}

interface EntryRowLike {
  sequence: number | string;
  id: string;
  idempotency_key: string;
  period_key: string;
  currency_code: string;
  entry_date: string;
  status: string;
  memo: string | null;
  reversal_of: string | null;
  posted_at: string;
  prov_source_system: string;
  prov_source_object: string;
  prov_source_version: string;
  prov_effective_date: string;
  prov_posted_date: string;
  prov_ingested_at: string;
  prov_normalization_version: string;
  prov_mapping_version: string;
}

interface RawLine {
  account_id: string;
  side: string;
  amount_minor: string | number;
  currency_code: string;
  memo: string | null;
  dimensions: unknown;
}

function parseDimensions(value: unknown): Readonly<Record<string, string>> | undefined {
  if (value == null) return undefined;
  const obj = typeof value === "string" ? JSON.parse(value) : value;
  if (obj && typeof obj === "object") return Object.freeze({ ...(obj as Record<string, string>) });
  return undefined;
}

/**
 * Bind the tenant GUC that the RLS policies key on (see `RLS_DDL` in schema.ts,
 * which reads `current_setting('app.tenant_id')`). Issued inside every read and
 * write transaction so row-level security actually engages; `is_local = true`
 * keeps it transaction-scoped on pooled connections.
 */
const SET_TENANT_GUC = `SELECT set_config('app.tenant_id', $1, true)`;

async function safeRollback(client: PoolClient): Promise<void> {
  try {
    await client.query("ROLLBACK");
  } catch {
    /* connection may already be aborted */
  }
}

function isIdempotencyConflict(err: unknown): boolean {
  const e = err as { code?: string; constraint?: string; message?: string };
  const haystack = `${e?.constraint ?? ""} ${e?.message ?? ""}`.toLowerCase();
  if (haystack.includes("idem")) return true;
  // During append the counter serializes sequence/id, so the only unique
  // violation a concurrent caller can hit is the idempotency key.
  return e?.code === "23505";
}
