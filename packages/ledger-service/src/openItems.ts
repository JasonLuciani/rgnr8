/**
 * QuickBooks' open invoices and bills, kept as a non-posting copy.
 *
 * During a parallel run the books are a QuickBooks mirror: an opening entry
 * plus catch-ups, with no invoice or bill documents behind the A/R and A/P
 * balances. So Invoices and Collections showed $0 owed while A/R held $373.20
 * and QuickBooks had $389.95 open, and the forecast saw no receivables at all.
 *
 * Each Refresh now replaces this snapshot with what QuickBooks has open. It
 * posts nothing (the books stay an exact mirror), so it is allowed while
 * QuickBooks is the book of record. At cutover these become real documents,
 * tied out to the A/R and A/P balances (see the cutover plan, PR 12).
 */

import type { Pool, Queryable } from "@rgnr8/ledger-postgres";

export type OpenItemKind = "invoice" | "bill";

export interface OpenItem {
  readonly kind: OpenItemKind;
  /** QuickBooks' own id: stable across refreshes. */
  readonly qboId: string;
  /** The number people see (invoice number); may be empty, e.g. for bills. */
  readonly number: string;
  /** The customer or vendor name as QuickBooks shows it. */
  readonly party: string;
  readonly date: string;
  readonly dueDate: string;
  /** What is still open on it, in minor units. */
  readonly openMinor: bigint;
}

export interface OpenItemsSnapshot {
  /** The day QuickBooks was read; "" when no snapshot has been taken. */
  readonly asOf: string;
  readonly items: readonly OpenItem[];
}

export class OpenItemsError extends Error {}

export interface OpenItemsStore {
  migrate(): Promise<void>;
  get(tenant: string): Promise<OpenItemsSnapshot>;
  /** Replace the whole snapshot: what QuickBooks no longer has open goes. */
  replace(tenant: string, snapshot: OpenItemsSnapshot): Promise<void>;
}

const EMPTY: OpenItemsSnapshot = { asOf: "", items: [] };

export class InMemoryOpenItemsStore implements OpenItemsStore {
  private readonly rows = new Map<string, OpenItemsSnapshot>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  get(tenant: string): Promise<OpenItemsSnapshot> {
    return Promise.resolve(this.rows.get(tenant) ?? EMPTY);
  }

  replace(tenant: string, snapshot: OpenItemsSnapshot): Promise<void> {
    this.rows.set(tenant, { asOf: snapshot.asOf, items: [...snapshot.items] });
    return Promise.resolve();
  }
}

export const OPEN_ITEMS_DDL = `
CREATE TABLE IF NOT EXISTS mirror_open_item (
  tenant_id  text   NOT NULL,
  kind       text   NOT NULL CHECK (kind IN ('invoice', 'bill')),
  qbo_id     text   NOT NULL,
  number     text   NOT NULL DEFAULT '',
  party      text   NOT NULL DEFAULT '',
  txn_date   text   NOT NULL DEFAULT '',
  due_date   text   NOT NULL DEFAULT '',
  open_minor bigint NOT NULL,
  as_of      text   NOT NULL,
  PRIMARY KEY (tenant_id, kind, qbo_id)
);
`;

export class PgOpenItemsStore implements OpenItemsStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(OPEN_ITEMS_DDL);
  }

  private async tx<T>(tenant: string, fn: (db: Queryable) => Promise<T>): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [tenant]);
      const result = await fn(client);
      await client.query("COMMIT");
      return result;
    } catch (err) {
      try {
        await client.query("ROLLBACK");
      } catch {
        /* ignore */
      }
      throw err;
    } finally {
      client.release();
    }
  }

  async get(tenant: string): Promise<OpenItemsSnapshot> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT kind, qbo_id, number, party, txn_date, due_date, open_minor, as_of
         FROM mirror_open_item WHERE tenant_id=$1 ORDER BY kind, due_date, qbo_id`,
        [tenant],
      );
      if (res.rows.length === 0) return EMPTY;
      return {
        asOf: String(res.rows[0]!["as_of"] ?? ""),
        items: res.rows.map((r) => ({
          kind: String(r["kind"]) === "bill" ? "bill" : "invoice",
          qboId: String(r["qbo_id"]),
          number: String(r["number"] ?? ""),
          party: String(r["party"] ?? ""),
          date: String(r["txn_date"] ?? ""),
          dueDate: String(r["due_date"] ?? ""),
          openMinor: BigInt(String(r["open_minor"])),
        })),
      };
    });
  }

  async replace(tenant: string, snapshot: OpenItemsSnapshot): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query("DELETE FROM mirror_open_item WHERE tenant_id=$1", [tenant]);
      for (const i of snapshot.items) {
        await db.query(
          `INSERT INTO mirror_open_item
             (tenant_id, kind, qbo_id, number, party, txn_date, due_date, open_minor, as_of)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)`,
          [tenant, i.kind, i.qboId, i.number, i.party, i.date, i.dueDate,
            i.openMinor.toString(), snapshot.asOf],
        );
      }
    });
  }
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Parse a snapshot from the web app's request body, refusing anything malformed. */
export function snapshotFromJson(data: Record<string, unknown>): OpenItemsSnapshot {
  const asOf = String(data["as_of"] ?? "").trim();
  if (!DATE.test(asOf)) throw new OpenItemsError("as_of must be YYYY-MM-DD");
  const items: OpenItem[] = [];
  const seen = new Set<string>();
  for (const kind of ["invoice", "bill"] as const) {
    const raw = data[kind === "invoice" ? "invoices" : "bills"] ?? [];
    if (!Array.isArray(raw)) throw new OpenItemsError(`${kind}s must be an array`);
    for (const r of raw) {
      if (typeof r !== "object" || r === null) throw new OpenItemsError(`each ${kind} must be an object`);
      const o = r as Record<string, unknown>;
      const qboId = String(o["qbo_id"] ?? "").trim();
      if (!qboId) throw new OpenItemsError(`a ${kind} has no qbo_id`);
      if (seen.has(`${kind}:${qboId}`)) throw new OpenItemsError(`${kind} ${qboId} appears twice`);
      seen.add(`${kind}:${qboId}`);
      let openMinor: bigint;
      try {
        openMinor = BigInt(String(o["open_minor"] ?? ""));
      } catch {
        throw new OpenItemsError(`${kind} ${qboId}: open_minor must be an integer`);
      }
      if (openMinor <= 0n) throw new OpenItemsError(`${kind} ${qboId}: nothing open on it`);
      const date = String(o["date"] ?? "");
      const dueDate = String(o["due_date"] ?? "");
      if ((date && !DATE.test(date)) || (dueDate && !DATE.test(dueDate))) {
        throw new OpenItemsError(`${kind} ${qboId}: dates must be YYYY-MM-DD`);
      }
      items.push({
        kind, qboId, openMinor, date, dueDate,
        number: String(o["number"] ?? ""),
        party: String(o["party"] ?? ""),
      });
    }
  }
  return { asOf, items };
}

/** The snapshot as the web app reads it, with each side's total. */
export function snapshotJson(tenant: string, s: OpenItemsSnapshot): Record<string, unknown> {
  const side = (kind: OpenItemKind) => s.items.filter((i) => i.kind === kind);
  const total = (kind: OpenItemKind) =>
    side(kind).reduce((sum, i) => sum + i.openMinor, 0n).toString();
  const json = (i: OpenItem) => ({
    qbo_id: i.qboId, number: i.number, party: i.party,
    date: i.date, due_date: i.dueDate, open_minor: i.openMinor.toString(),
  });
  return {
    tenant,
    as_of: s.asOf,
    invoices: side("invoice").map(json),
    bills: side("bill").map(json),
    invoice_total_minor: total("invoice"),
    bill_total_minor: total("bill"),
  };
}
