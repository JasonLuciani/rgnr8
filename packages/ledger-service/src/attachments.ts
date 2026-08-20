import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import type { TenantId } from "@rgnr8/ledger-kernel";

/**
 * Attachments — the receipt behind the number.
 *
 * A journal entry says $249 went to Software & Subscriptions on the 21st. It
 * does not say *what* was bought, and in an audit, a deduction dispute, or a
 * conversation with an accountant eleven months later, that is the only thing
 * anyone wants. The receipt is the evidence; the entry is the claim.
 *
 * So a document can be attached to whatever it evidences: a bank line, a bill,
 * an invoice, or a journal entry. The link is by subject, not by upload order,
 * so "show me the receipt for this" is a lookup rather than a search through a
 * folder named after a month.
 *
 * Bytes live in the database beside the books. That is a deliberate trade: an
 * object store would scale further, but it introduces a second system that can
 * be backed up separately, restored to a different point in time, or lose a
 * file while the ledger still points at it. Books whose evidence has silently
 * gone missing are worse than books with no evidence at all.
 */

export class AttachmentError extends Error {}

/** What an attachment can be evidence for. */
export type SubjectKind = "entry" | "feed" | "invoice" | "bill" | "payroll";

const SUBJECT_KINDS: readonly SubjectKind[] = [
  "entry", "feed", "invoice", "bill", "payroll",
];

export interface AttachmentRecord {
  readonly id: string;
  readonly subjectKind: SubjectKind;
  readonly subjectId: string;
  readonly filename: string;
  readonly contentType: string;
  readonly bytes: number;
  readonly uploadedAt: string;
  readonly note: string;
}

export interface AttachmentStore {
  migrate(): Promise<void>;
  save(tenant: string, record: AttachmentRecord, content: Buffer): Promise<void>;
  list(
    tenant: string, subject?: { kind: SubjectKind; id: string },
  ): Promise<AttachmentRecord[]>;
  get(tenant: string, id: string): Promise<{ record: AttachmentRecord; content: Buffer } | undefined>;
  remove(tenant: string, id: string): Promise<void>;
  /** How many attachments each of these subjects has — for a list view. */
  counts(tenant: string, kind: SubjectKind): Promise<Map<string, number>>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryAttachmentStore implements AttachmentStore {
  private readonly items = new Map<string, { record: AttachmentRecord; content: Buffer }>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  save(tenant: string, record: AttachmentRecord, content: Buffer): Promise<void> {
    this.items.set(`${tenant}::${record.id}`, { record, content });
    return Promise.resolve();
  }

  list(
    tenant: string, subject?: { kind: SubjectKind; id: string },
  ): Promise<AttachmentRecord[]> {
    const prefix = `${tenant}::`;
    const out: AttachmentRecord[] = [];
    for (const [k, v] of this.items) {
      if (!k.startsWith(prefix)) continue;
      if (subject && (v.record.subjectKind !== subject.kind || v.record.subjectId !== subject.id)) {
        continue;
      }
      out.push(v.record);
    }
    return Promise.resolve(out.sort((a, b) => a.uploadedAt.localeCompare(b.uploadedAt)));
  }

  get(
    tenant: string, id: string,
  ): Promise<{ record: AttachmentRecord; content: Buffer } | undefined> {
    return Promise.resolve(this.items.get(`${tenant}::${id}`));
  }

  remove(tenant: string, id: string): Promise<void> {
    this.items.delete(`${tenant}::${id}`);
    return Promise.resolve();
  }

  async counts(tenant: string, kind: SubjectKind): Promise<Map<string, number>> {
    const out = new Map<string, number>();
    for (const record of await this.list(tenant)) {
      if (record.subjectKind !== kind) continue;
      out.set(record.subjectId, (out.get(record.subjectId) ?? 0) + 1);
    }
    return out;
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const ATTACHMENT_DDL = `
CREATE TABLE IF NOT EXISTS attachment (
  tenant_id    text NOT NULL,
  id           text NOT NULL,
  subject_kind text NOT NULL,
  subject_id   text NOT NULL,
  filename     text NOT NULL,
  content_type text NOT NULL,
  byte_size    integer NOT NULL,
  uploaded_at  text NOT NULL,
  note         text NOT NULL DEFAULT '',
  content      bytea NOT NULL,
  CONSTRAINT attachment_pk PRIMARY KEY (tenant_id, id)
);

CREATE INDEX IF NOT EXISTS attachment_subject_idx
  ON attachment (tenant_id, subject_kind, subject_id);
`;

function recordFromRow(r: Record<string, unknown>): AttachmentRecord {
  return {
    id: String(r["id"]),
    subjectKind: String(r["subject_kind"]) as SubjectKind,
    subjectId: String(r["subject_id"]),
    filename: String(r["filename"]),
    contentType: String(r["content_type"]),
    bytes: Number(r["byte_size"]),
    uploadedAt: String(r["uploaded_at"]),
    note: String(r["note"] ?? ""),
  };
}

export class PgAttachmentStore implements AttachmentStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(ATTACHMENT_DDL);
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

  async save(tenant: string, record: AttachmentRecord, content: Buffer): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO attachment (tenant_id, id, subject_kind, subject_id, filename,
           content_type, byte_size, uploaded_at, note, content)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           subject_kind=EXCLUDED.subject_kind, subject_id=EXCLUDED.subject_id,
           filename=EXCLUDED.filename, content_type=EXCLUDED.content_type,
           byte_size=EXCLUDED.byte_size, uploaded_at=EXCLUDED.uploaded_at,
           note=EXCLUDED.note, content=EXCLUDED.content`,
        [
          tenant, record.id, record.subjectKind, record.subjectId, record.filename,
          record.contentType, record.bytes, record.uploadedAt, record.note, content,
        ],
      ),
    );
  }

  async list(
    tenant: string, subject?: { kind: SubjectKind; id: string },
  ): Promise<AttachmentRecord[]> {
    return this.tx(tenant, async (db) => {
      // The content column is deliberately not selected: a list view that drags
      // every megabyte out of the database to render a filename is how a books
      // screen becomes slow for the clients with the best records.
      const res = subject
        ? await db.query(
            `SELECT id, subject_kind, subject_id, filename, content_type, byte_size,
                    uploaded_at, note
             FROM attachment WHERE tenant_id=$1 AND subject_kind=$2 AND subject_id=$3
             ORDER BY uploaded_at, id`,
            [tenant, subject.kind, subject.id],
          )
        : await db.query(
            `SELECT id, subject_kind, subject_id, filename, content_type, byte_size,
                    uploaded_at, note
             FROM attachment WHERE tenant_id=$1 ORDER BY uploaded_at, id`,
            [tenant],
          );
      return res.rows.map(recordFromRow);
    });
  }

  async get(
    tenant: string, id: string,
  ): Promise<{ record: AttachmentRecord; content: Buffer } | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM attachment WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      const raw = row["content"];
      const content = Buffer.isBuffer(raw) ? raw : Buffer.from(String(raw ?? ""), "binary");
      return { record: recordFromRow(row), content };
    });
  }

  async remove(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query("DELETE FROM attachment WHERE tenant_id=$1 AND id=$2", [tenant, id]),
    );
  }

  async counts(tenant: string, kind: SubjectKind): Promise<Map<string, number>> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT subject_id, COUNT(*) AS n FROM attachment
         WHERE tenant_id=$1 AND subject_kind=$2 GROUP BY subject_id`,
        [tenant, kind],
      );
      const out = new Map<string, number>();
      for (const r of res.rows) out.set(String(r["subject_id"]), Number(r["n"]));
      return out;
    });
  }
}

// --- the flow ----------------------------------------------------------------

/** The verdict from scanning an uploaded file. */
export interface ScanResult {
  readonly clean: boolean;
  readonly reason?: string;
}

/**
 * A content-safety / malware scanner seam. Production injects a real scanner (e.g.
 * a ClamAV adapter); the default `ALLOW_ALL_SCANNER` is for dev/tests. A receipt
 * that fails the scan is refused at upload time — quarantine-by-rejection — so an
 * infected file never lands in the store or reaches a later download.
 */
export interface ContentScanner {
  scan(content: Buffer, meta: { filename: string; contentType: string }): Promise<ScanResult>;
}

export const ALLOW_ALL_SCANNER: ContentScanner = {
  async scan(): Promise<ScanResult> {
    return { clean: true };
  },
};

export interface AttachmentContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly now: () => string;
  /** Optional content scanner; defaults to allow-all when omitted. */
  readonly scanner?: ContentScanner;
}

/**
 * The largest receipt worth keeping inline. A phone photo is 2–5 MB; a scanned
 * multi-page contract can be more, and belongs in a document system rather than
 * beside a journal entry.
 */
export const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;

export interface AttachmentInput {
  readonly subject_kind?: string;
  readonly subject_id?: string;
  readonly filename?: string;
  readonly content_type?: string;
  /** The file, base64-encoded. */
  readonly content_base64?: string;
  readonly note?: string;
  readonly id?: string;
}

export function attachmentJson(a: AttachmentRecord): Record<string, unknown> {
  return {
    id: a.id,
    subject_kind: a.subjectKind,
    subject_id: a.subjectId,
    filename: a.filename,
    content_type: a.contentType,
    bytes: a.bytes,
    uploaded_at: a.uploadedAt,
    note: a.note,
  };
}

function requireKind(raw: unknown): SubjectKind {
  const kind = String(raw ?? "").trim() as SubjectKind;
  if (!SUBJECT_KINDS.includes(kind)) {
    throw new AttachmentError(
      `subject_kind must be one of ${SUBJECT_KINDS.join(", ")}`,
    );
  }
  return kind;
}

/**
 * Does the thing this claims to evidence actually exist?
 *
 * An attachment pointing at nothing is worse than no attachment: it looks like
 * evidence in a list and produces a dead end when someone follows it.
 */
async function requireSubject(
  ctx: AttachmentContext, kind: SubjectKind, id: string,
): Promise<void> {
  const tenant = String(ctx.tenant);
  switch (kind) {
    case "entry": {
      const entry = await ctx.backend.store(ctx.tenant).getById(ctx.tenant, id as never);
      if (!entry) throw new AttachmentError(`unknown journal entry ${id}`);
      return;
    }
    case "feed": {
      if (!(await ctx.backend.feed().getTxn(tenant, id))) {
        throw new AttachmentError(`unknown bank transaction ${id}`);
      }
      return;
    }
    case "invoice":
    case "bill": {
      if (!(await ctx.backend.documents().getDoc(tenant, kind, id))) {
        throw new AttachmentError(`unknown ${kind} ${id}`);
      }
      return;
    }
    case "payroll": {
      if (!(await ctx.backend.payroll().getRun(tenant, id))) {
        throw new AttachmentError(`unknown payroll run ${id}`);
      }
      return;
    }
  }
}

export async function saveAttachment(
  ctx: AttachmentContext, input: AttachmentInput,
): Promise<AttachmentRecord> {
  const kind = requireKind(input.subject_kind);
  const subjectId = String(input.subject_id ?? "").trim();
  if (!subjectId) throw new AttachmentError("subject_id is required");
  await requireSubject(ctx, kind, subjectId);

  const filename = String(input.filename ?? "").trim() || "attachment";
  const encoded = String(input.content_base64 ?? "");
  if (!encoded) throw new AttachmentError("content_base64 is required");

  let content: Buffer;
  try {
    content = Buffer.from(encoded, "base64");
  } catch {
    throw new AttachmentError("content_base64 is not valid base64");
  }
  if (content.length === 0) throw new AttachmentError("the file is empty");
  if (content.length > MAX_ATTACHMENT_BYTES) {
    throw new AttachmentError(
      `the file is ${(content.length / 1_048_576).toFixed(1)} MB — the limit is `
      + `${MAX_ATTACHMENT_BYTES / 1_048_576} MB`,
    );
  }
  // Round-trip check: base64 that decodes to something which does not re-encode
  // to itself was silently truncated, and a truncated receipt is not a receipt.
  if (content.toString("base64").replace(/=+$/, "") !== encoded.replace(/[\s=]+/g, "")) {
    throw new AttachmentError("content_base64 is not valid base64");
  }

  const contentType = String(input.content_type ?? "").trim() || "application/octet-stream";

  // Content-safety gate: an infected/flagged file is refused before it is stored,
  // so it can never be served on a later download (quarantine-by-rejection).
  const scanner = ctx.scanner ?? ALLOW_ALL_SCANNER;
  const verdict = await scanner.scan(content, { filename, contentType });
  if (!verdict.clean) {
    throw new AttachmentError(`upload rejected by content scan: ${verdict.reason ?? "flagged as unsafe"}`);
  }

  const uploadedAt = ctx.now();
  // URL-safe by construction: an id that has to be escaped to appear in a path
  // is an id that will eventually be compared in its escaped form and not found.
  const id = String(input.id ?? "").trim()
    || `att-${kind}-${subjectId}-${uploadedAt}`.replace(/[^A-Za-z0-9_.-]/g, "-");

  const record: AttachmentRecord = {
    id,
    subjectKind: kind,
    subjectId,
    filename,
    contentType,
    bytes: content.length,
    uploadedAt,
    note: String(input.note ?? "").trim(),
  };
  await ctx.backend.attachments().save(String(ctx.tenant), record, content);
  return record;
}

export async function listAttachments(
  ctx: AttachmentContext, query: Readonly<Record<string, string>>,
): Promise<AttachmentRecord[]> {
  const kindRaw = String(query["subject_kind"] ?? "").trim();
  const subjectId = String(query["subject_id"] ?? "").trim();
  if (!kindRaw && !subjectId) {
    return ctx.backend.attachments().list(String(ctx.tenant));
  }
  const kind = requireKind(kindRaw);
  if (!subjectId) throw new AttachmentError("subject_id is required with subject_kind");
  return ctx.backend.attachments().list(String(ctx.tenant), { kind, id: subjectId });
}
