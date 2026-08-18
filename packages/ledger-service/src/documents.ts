import type { Pool, Queryable } from "@rgnr8/ledger-postgres";

/**
 * Durable storage for AR/AP source documents — invoices, bills, the parties they
 * name, and the payments applied to them.
 *
 * The journal remains the system of record for *the books*; this holds the
 * **open-item detail** the journal can't express on its own: which invoice is
 * still owed, by whom, and when it was due. Without it, "who owes me?" and an
 * aging report are unanswerable — which is exactly the gap between a general
 * ledger and a business's day-to-day accounting.
 *
 * Money is integer minor units throughout. Everything is tenant-scoped.
 */

export type DocKind = "invoice" | "bill";
export type PartyKind = "customer" | "vendor";
export type DocStatus = "OPEN" | "PARTIAL" | "PAID";

export interface PartyRecord {
  readonly id: string;
  readonly name: string;
  readonly email?: string;
  readonly termsDays?: number;
}

export interface DocLineRecord {
  readonly description: string;
  readonly quantity: number;
  readonly unitAmountMinor: string;
  readonly accountCode: string;
  readonly amountMinor: string;
}

export interface DocRecord {
  readonly id: string;
  readonly kind: DocKind;
  readonly partyId: string;
  readonly date: string;
  readonly dueDate: string;
  readonly totalMinor: string;
  readonly openMinor: string;
  readonly status: DocStatus;
  readonly memo: string;
  readonly lines: readonly DocLineRecord[];
}

export interface PaymentRecord {
  readonly id: string;
  readonly docKind: DocKind;
  readonly docId: string;
  readonly date: string;
  readonly amountMinor: string;
  readonly bankCode: string;
}

export interface DocumentStore {
  migrate(): Promise<void>;
  upsertParty(tenant: string, kind: PartyKind, party: PartyRecord): Promise<void>;
  getParty(tenant: string, kind: PartyKind, id: string): Promise<PartyRecord | undefined>;
  listParties(tenant: string, kind: PartyKind): Promise<PartyRecord[]>;
  saveDoc(tenant: string, doc: DocRecord): Promise<void>;
  getDoc(tenant: string, kind: DocKind, id: string): Promise<DocRecord | undefined>;
  listDocs(tenant: string, kind: DocKind): Promise<DocRecord[]>;
  setOpen(
    tenant: string,
    kind: DocKind,
    id: string,
    openMinor: string,
    status: DocStatus,
  ): Promise<void>;
  addPayment(tenant: string, payment: PaymentRecord): Promise<void>;
  listPayments(tenant: string, kind: DocKind, docId: string): Promise<PaymentRecord[]>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryDocumentStore implements DocumentStore {
  private readonly parties = new Map<string, PartyRecord>();
  private readonly docs = new Map<string, DocRecord>();
  private readonly payments = new Map<string, PaymentRecord>();

  private pk(tenant: string, kind: string, id: string): string {
    return `${tenant} ${kind} ${id}`;
  }

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  upsertParty(tenant: string, kind: PartyKind, party: PartyRecord): Promise<void> {
    this.parties.set(this.pk(tenant, kind, party.id), party);
    return Promise.resolve();
  }

  getParty(tenant: string, kind: PartyKind, id: string): Promise<PartyRecord | undefined> {
    return Promise.resolve(this.parties.get(this.pk(tenant, kind, id)));
  }

  listParties(tenant: string, kind: PartyKind): Promise<PartyRecord[]> {
    const prefix = `${tenant} ${kind} `;
    const out: PartyRecord[] = [];
    for (const [k, v] of this.parties) if (k.startsWith(prefix)) out.push(v);
    out.sort((a, b) => a.name.localeCompare(b.name));
    return Promise.resolve(out);
  }

  saveDoc(tenant: string, doc: DocRecord): Promise<void> {
    this.docs.set(this.pk(tenant, doc.kind, doc.id), doc);
    return Promise.resolve();
  }

  getDoc(tenant: string, kind: DocKind, id: string): Promise<DocRecord | undefined> {
    return Promise.resolve(this.docs.get(this.pk(tenant, kind, id)));
  }

  listDocs(tenant: string, kind: DocKind): Promise<DocRecord[]> {
    const prefix = `${tenant} ${kind} `;
    const out: DocRecord[] = [];
    for (const [k, v] of this.docs) if (k.startsWith(prefix)) out.push(v);
    out.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : a.id.localeCompare(b.id)));
    return Promise.resolve(out);
  }

  setOpen(
    tenant: string,
    kind: DocKind,
    id: string,
    openMinor: string,
    status: DocStatus,
  ): Promise<void> {
    const key = this.pk(tenant, kind, id);
    const existing = this.docs.get(key);
    if (existing) this.docs.set(key, { ...existing, openMinor, status });
    return Promise.resolve();
  }

  addPayment(tenant: string, payment: PaymentRecord): Promise<void> {
    this.payments.set(this.pk(tenant, payment.docKind, payment.id), payment);
    return Promise.resolve();
  }

  listPayments(tenant: string, kind: DocKind, docId: string): Promise<PaymentRecord[]> {
    const prefix = `${tenant} ${kind} `;
    const out: PaymentRecord[] = [];
    for (const [k, v] of this.payments) {
      if (k.startsWith(prefix) && v.docId === docId) out.push(v);
    }
    out.sort((a, b) => a.date.localeCompare(b.date));
    return Promise.resolve(out);
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const DOCUMENT_DDL = `
CREATE TABLE IF NOT EXISTS party (
  tenant_id  text NOT NULL,
  kind       text NOT NULL,
  id         text NOT NULL,
  name       text NOT NULL,
  email      text,
  terms_days integer,
  CONSTRAINT party_pk PRIMARY KEY (tenant_id, kind, id)
);

CREATE TABLE IF NOT EXISTS doc (
  tenant_id    text NOT NULL,
  kind         text NOT NULL,
  id           text NOT NULL,
  party_id     text NOT NULL,
  doc_date     text NOT NULL,
  due_date     text NOT NULL,
  total_minor  numeric(38,0) NOT NULL,
  open_minor   numeric(38,0) NOT NULL,
  status       text NOT NULL,
  memo         text,
  CONSTRAINT doc_pk PRIMARY KEY (tenant_id, kind, id)
);

CREATE TABLE IF NOT EXISTS doc_line (
  tenant_id         text NOT NULL,
  kind              text NOT NULL,
  doc_id            text NOT NULL,
  line_index        integer NOT NULL,
  description       text,
  quantity          numeric(20,4) NOT NULL,
  unit_amount_minor numeric(38,0) NOT NULL,
  account_code      text NOT NULL,
  amount_minor      numeric(38,0) NOT NULL,
  CONSTRAINT doc_line_pk PRIMARY KEY (tenant_id, kind, doc_id, line_index)
);

CREATE TABLE IF NOT EXISTS doc_payment (
  tenant_id    text NOT NULL,
  kind         text NOT NULL,
  id           text NOT NULL,
  doc_id       text NOT NULL,
  pay_date     text NOT NULL,
  amount_minor numeric(38,0) NOT NULL,
  bank_code    text NOT NULL,
  CONSTRAINT doc_payment_pk PRIMARY KEY (tenant_id, kind, id)
);
`;

function str(v: unknown): string {
  return v === null || v === undefined ? "" : String(v);
}

export class PgDocumentStore implements DocumentStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(DOCUMENT_DDL);
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
        /* ignore rollback failure */
      }
      throw err;
    } finally {
      client.release();
    }
  }

  async upsertParty(tenant: string, kind: PartyKind, p: PartyRecord): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO party (tenant_id, kind, id, name, email, terms_days)
         VALUES ($1,$2,$3,$4,$5,$6)
         ON CONFLICT (tenant_id, kind, id) DO UPDATE SET
           name = EXCLUDED.name, email = EXCLUDED.email, terms_days = EXCLUDED.terms_days`,
        [tenant, kind, p.id, p.name, p.email ?? null, p.termsDays ?? null],
      ),
    );
  }

  async getParty(tenant: string, kind: PartyKind, id: string): Promise<PartyRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT id, name, email, terms_days FROM party WHERE tenant_id=$1 AND kind=$2 AND id=$3",
        [tenant, kind, id],
      );
      const row = res.rows[0];
      return row ? rowToParty(row) : undefined;
    });
  }

  async listParties(tenant: string, kind: PartyKind): Promise<PartyRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT id, name, email, terms_days FROM party WHERE tenant_id=$1 AND kind=$2",
        [tenant, kind],
      );
      return res.rows.map(rowToParty).sort((a, b) => a.name.localeCompare(b.name));
    });
  }

  async saveDoc(tenant: string, doc: DocRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO doc (tenant_id, kind, id, party_id, doc_date, due_date,
                          total_minor, open_minor, status, memo)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
         ON CONFLICT (tenant_id, kind, id) DO UPDATE SET
           party_id=EXCLUDED.party_id, doc_date=EXCLUDED.doc_date, due_date=EXCLUDED.due_date,
           total_minor=EXCLUDED.total_minor, open_minor=EXCLUDED.open_minor,
           status=EXCLUDED.status, memo=EXCLUDED.memo`,
        [
          tenant, doc.kind, doc.id, doc.partyId, doc.date, doc.dueDate,
          doc.totalMinor, doc.openMinor, doc.status, doc.memo,
        ],
      );
      for (let i = 0; i < doc.lines.length; i++) {
        const l = doc.lines[i]!;
        await db.query(
          `INSERT INTO doc_line (tenant_id, kind, doc_id, line_index, description,
                                 quantity, unit_amount_minor, account_code, amount_minor)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
           ON CONFLICT (tenant_id, kind, doc_id, line_index) DO UPDATE SET
             description=EXCLUDED.description, quantity=EXCLUDED.quantity,
             unit_amount_minor=EXCLUDED.unit_amount_minor,
             account_code=EXCLUDED.account_code, amount_minor=EXCLUDED.amount_minor`,
          [
            tenant, doc.kind, doc.id, i, l.description, l.quantity,
            l.unitAmountMinor, l.accountCode, l.amountMinor,
          ],
        );
      }
    });
  }

  async getDoc(tenant: string, kind: DocKind, id: string): Promise<DocRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT id, party_id, doc_date, due_date, total_minor, open_minor, status, memo
         FROM doc WHERE tenant_id=$1 AND kind=$2 AND id=$3`,
        [tenant, kind, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      const lines = await db.query(
        `SELECT line_index, description, quantity, unit_amount_minor, account_code, amount_minor
         FROM doc_line WHERE tenant_id=$1 AND kind=$2 AND doc_id=$3`,
        [tenant, kind, id],
      );
      return rowToDoc(row, kind, lines.rows);
    });
  }

  async listDocs(tenant: string, kind: DocKind): Promise<DocRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT id, party_id, doc_date, due_date, total_minor, open_minor, status, memo
         FROM doc WHERE tenant_id=$1 AND kind=$2`,
        [tenant, kind],
      );
      const lines = await db.query(
        `SELECT doc_id, line_index, description, quantity, unit_amount_minor,
                account_code, amount_minor
         FROM doc_line WHERE tenant_id=$1 AND kind=$2`,
        [tenant, kind],
      );
      const byDoc = new Map<string, Record<string, unknown>[]>();
      for (const l of lines.rows) {
        const key = str(l["doc_id"]);
        const list = byDoc.get(key) ?? [];
        list.push(l);
        byDoc.set(key, list);
      }
      return res.rows
        .map((r) => rowToDoc(r, kind, byDoc.get(str(r["id"])) ?? []))
        .sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : a.id.localeCompare(b.id)));
    });
  }

  async setOpen(
    tenant: string,
    kind: DocKind,
    id: string,
    openMinor: string,
    status: DocStatus,
  ): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        "UPDATE doc SET open_minor=$4, status=$5 WHERE tenant_id=$1 AND kind=$2 AND id=$3",
        [tenant, kind, id, openMinor, status],
      ),
    );
  }

  async addPayment(tenant: string, p: PaymentRecord): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO doc_payment (tenant_id, kind, id, doc_id, pay_date, amount_minor, bank_code)
         VALUES ($1,$2,$3,$4,$5,$6,$7)
         ON CONFLICT (tenant_id, kind, id) DO NOTHING`,
        [tenant, p.docKind, p.id, p.docId, p.date, p.amountMinor, p.bankCode],
      ),
    );
  }

  async listPayments(tenant: string, kind: DocKind, docId: string): Promise<PaymentRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT id, doc_id, pay_date, amount_minor, bank_code
         FROM doc_payment WHERE tenant_id=$1 AND kind=$2 AND doc_id=$3`,
        [tenant, kind, docId],
      );
      return res.rows
        .map((r) => ({
          id: str(r["id"]),
          docKind: kind,
          docId: str(r["doc_id"]),
          date: str(r["pay_date"]),
          amountMinor: str(r["amount_minor"]),
          bankCode: str(r["bank_code"]),
        }))
        .sort((a, b) => a.date.localeCompare(b.date));
    });
  }
}

function rowToParty(r: Record<string, unknown>): PartyRecord {
  const email = r["email"];
  const terms = r["terms_days"];
  return {
    id: str(r["id"]),
    name: str(r["name"]),
    ...(email ? { email: str(email) } : {}),
    ...(terms !== null && terms !== undefined ? { termsDays: Number(terms) } : {}),
  };
}

function rowToDoc(
  r: Record<string, unknown>,
  kind: DocKind,
  lines: Record<string, unknown>[],
): DocRecord {
  return {
    id: str(r["id"]),
    kind,
    partyId: str(r["party_id"]),
    date: str(r["doc_date"]),
    dueDate: str(r["due_date"]),
    totalMinor: str(r["total_minor"]),
    openMinor: str(r["open_minor"]),
    status: str(r["status"]) as DocStatus,
    memo: str(r["memo"]),
    lines: [...lines]
      .sort((a, b) => Number(a["line_index"]) - Number(b["line_index"]))
      .map((l) => ({
        description: str(l["description"]),
        quantity: Number(l["quantity"]),
        unitAmountMinor: str(l["unit_amount_minor"]),
        accountCode: str(l["account_code"]),
        amountMinor: str(l["amount_minor"]),
      })),
  };
}
