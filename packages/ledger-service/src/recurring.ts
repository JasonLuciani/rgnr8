import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  type Currency,
  type JournalLineInput,
  type PostCommand,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import { occurrencesBetween, type Frequency, type RecurringSchedule } from "@rgnr8/subledger";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import { validateDimensions } from "./dimensions.js";

/**
 * Recurring transactions — rent, subscriptions, retainers.
 *
 * The obvious design is a scheduler that posts on its own. It is also the wrong
 * one for a small business's books. A rule that fires unattended keeps posting
 * a rent payment after the lease ends, keeps invoicing a client who cancelled,
 * and does it silently into months that may already be closed. By the time
 * anyone looks, the books contain a year of confident fiction.
 *
 * So a recurring transaction here is a **memorized template with a schedule**,
 * and running it is an action someone takes. The screen says "these three are
 * due" and the owner posts them — one click for all of them, but a click. That
 * keeps the convenience (nobody retypes rent twelve times) without the failure
 * mode (nobody notices the books have been wrong since March).
 *
 * Each occurrence posts idempotently under a key derived from the template and
 * the date, so running twice on the same day, or on two machines, cannot
 * duplicate rent.
 */

export class RecurringError extends Error {}

export interface RecurringLineRecord {
  readonly accountCode: string;
  readonly side: "DEBIT" | "CREDIT";
  readonly amountMinor: string;
  readonly memo: string;
  readonly dimensions: Readonly<Record<string, string>>;
}

export interface RecurringRecord {
  readonly id: string;
  readonly name: string;
  readonly frequency: Frequency;
  readonly interval: number;
  readonly startDate: string;
  readonly endDate: string;
  readonly memo: string;
  readonly active: boolean;
  /** The last date actually posted, so "what's due" is answerable. */
  readonly lastPosted: string;
  readonly lines: readonly RecurringLineRecord[];
}

export interface RecurringStore {
  migrate(): Promise<void>;
  list(tenant: string): Promise<RecurringRecord[]>;
  get(tenant: string, id: string): Promise<RecurringRecord | undefined>;
  save(tenant: string, record: RecurringRecord): Promise<void>;
  remove(tenant: string, id: string): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryRecurringStore implements RecurringStore {
  private readonly items = new Map<string, RecurringRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  list(tenant: string): Promise<RecurringRecord[]> {
    const prefix = `${tenant}::`;
    const out: RecurringRecord[] = [];
    for (const [k, v] of this.items) if (k.startsWith(prefix)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.name.localeCompare(b.name)));
  }

  get(tenant: string, id: string): Promise<RecurringRecord | undefined> {
    return Promise.resolve(this.items.get(`${tenant}::${id}`));
  }

  save(tenant: string, record: RecurringRecord): Promise<void> {
    this.items.set(`${tenant}::${record.id}`, record);
    return Promise.resolve();
  }

  remove(tenant: string, id: string): Promise<void> {
    this.items.delete(`${tenant}::${id}`);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const RECURRING_DDL = `
CREATE TABLE IF NOT EXISTS recurring_txn (
  tenant_id   text NOT NULL,
  id          text NOT NULL,
  name        text NOT NULL,
  frequency   text NOT NULL,
  interval_n  integer NOT NULL DEFAULT 1,
  start_date  text NOT NULL,
  end_date    text NOT NULL DEFAULT '',
  memo        text NOT NULL DEFAULT '',
  active      boolean NOT NULL DEFAULT true,
  last_posted text NOT NULL DEFAULT '',
  CONSTRAINT recurring_txn_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS recurring_txn_line (
  tenant_id    text NOT NULL,
  recurring_id text NOT NULL,
  line_no      integer NOT NULL,
  account_code text NOT NULL,
  side         text NOT NULL,
  amount_minor text NOT NULL,
  memo         text NOT NULL DEFAULT '',
  dimensions   text NOT NULL DEFAULT '{}',
  CONSTRAINT recurring_txn_line_pk PRIMARY KEY (tenant_id, recurring_id, line_no)
);
`;

export class PgRecurringStore implements RecurringStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(RECURRING_DDL);
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

  private async linesFor(
    db: Queryable, tenant: string, id: string,
  ): Promise<RecurringLineRecord[]> {
    const res = await db.query(
      `SELECT account_code, side, amount_minor, memo, dimensions
       FROM recurring_txn_line WHERE tenant_id=$1 AND recurring_id=$2 ORDER BY line_no`,
      [tenant, id],
    );
    return res.rows.map((r) => {
      let dimensions: Record<string, string> = {};
      try {
        const parsed: unknown = JSON.parse(String(r["dimensions"] ?? "{}"));
        if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
          dimensions = parsed as Record<string, string>;
        }
      } catch {
        /* a template with unreadable dimensions posts without them */
      }
      return {
        accountCode: String(r["account_code"]),
        side: String(r["side"]) as "DEBIT" | "CREDIT",
        amountMinor: String(r["amount_minor"]),
        memo: String(r["memo"] ?? ""),
        dimensions,
      };
    });
  }

  private fromRow(r: Record<string, unknown>, lines: RecurringLineRecord[]): RecurringRecord {
    return {
      id: String(r["id"]),
      name: String(r["name"]),
      frequency: String(r["frequency"]) as Frequency,
      interval: Number(r["interval_n"] ?? 1),
      startDate: String(r["start_date"]),
      endDate: String(r["end_date"] ?? ""),
      memo: String(r["memo"] ?? ""),
      active: r["active"] === true || r["active"] === "t",
      lastPosted: String(r["last_posted"] ?? ""),
      lines,
    };
  }

  async list(tenant: string): Promise<RecurringRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM recurring_txn WHERE tenant_id=$1 ORDER BY name", [tenant],
      );
      const out: RecurringRecord[] = [];
      for (const row of res.rows) {
        out.push(this.fromRow(row, await this.linesFor(db, tenant, String(row["id"]))));
      }
      return out;
    });
  }

  async get(tenant: string, id: string): Promise<RecurringRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM recurring_txn WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      return this.fromRow(row, await this.linesFor(db, tenant, id));
    });
  }

  async save(tenant: string, record: RecurringRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO recurring_txn (tenant_id, id, name, frequency, interval_n,
           start_date, end_date, memo, active, last_posted)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           name=EXCLUDED.name, frequency=EXCLUDED.frequency,
           interval_n=EXCLUDED.interval_n, start_date=EXCLUDED.start_date,
           end_date=EXCLUDED.end_date, memo=EXCLUDED.memo, active=EXCLUDED.active,
           last_posted=EXCLUDED.last_posted`,
        [
          tenant, record.id, record.name, record.frequency, record.interval,
          record.startDate, record.endDate, record.memo, record.active, record.lastPosted,
        ],
      );
      await db.query(
        "DELETE FROM recurring_txn_line WHERE tenant_id=$1 AND recurring_id=$2",
        [tenant, record.id],
      );
      let n = 0;
      for (const l of record.lines) {
        await db.query(
          `INSERT INTO recurring_txn_line (tenant_id, recurring_id, line_no,
             account_code, side, amount_minor, memo, dimensions)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)`,
          [
            tenant, record.id, n, l.accountCode, l.side, l.amountMinor, l.memo,
            JSON.stringify(l.dimensions ?? {}),
          ],
        );
        n += 1;
      }
    });
  }

  async remove(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM recurring_txn_line WHERE tenant_id=$1 AND recurring_id=$2", [tenant, id],
      );
      await db.query("DELETE FROM recurring_txn WHERE tenant_id=$1 AND id=$2", [tenant, id]);
    });
  }
}

// --- the flow ----------------------------------------------------------------

export interface RecurringContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

const FREQUENCIES: readonly Frequency[] = ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"];

function requireDate(raw: unknown, label: string): string {
  const date = String(raw ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new RecurringError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

export interface RecurringInput {
  readonly id?: string;
  readonly name?: string;
  readonly frequency?: string;
  readonly interval?: number | string;
  readonly start_date?: string;
  readonly end_date?: string;
  readonly memo?: string;
  readonly active?: boolean;
  readonly lines?: ReadonlyArray<{
    readonly account_code?: string;
    readonly side?: string;
    readonly amount_minor?: string | number;
    readonly memo?: string;
    readonly dimensions?: Record<string, string>;
  }>;
}

export function recurringJson(r: RecurringRecord): Record<string, unknown> {
  return {
    id: r.id,
    name: r.name,
    frequency: r.frequency,
    interval: r.interval,
    start_date: r.startDate,
    end_date: r.endDate,
    memo: r.memo,
    active: r.active,
    last_posted: r.lastPosted,
    lines: r.lines.map((l) => ({
      account_code: l.accountCode,
      side: l.side,
      amount_minor: l.amountMinor,
      memo: l.memo,
      dimensions: l.dimensions,
    })),
  };
}

export async function saveRecurring(
  ctx: RecurringContext, input: RecurringInput,
): Promise<RecurringRecord> {
  const name = String(input.name ?? "").trim();
  if (!name) throw new RecurringError("a recurring transaction needs a name");

  const frequency = String(input.frequency ?? "").trim().toUpperCase() as Frequency;
  if (!FREQUENCIES.includes(frequency)) {
    throw new RecurringError(`frequency must be one of ${FREQUENCIES.join(", ")}`);
  }
  const interval = Number(input.interval ?? 1);
  if (!Number.isInteger(interval) || interval < 1) {
    throw new RecurringError("interval must be a whole number of periods, at least 1");
  }
  const startDate = requireDate(input.start_date, "start date");
  const endDate = String(input.end_date ?? "").trim();
  if (endDate) {
    requireDate(endDate, "end date");
    if (endDate < startDate) throw new RecurringError("the end date is before the start date");
  }

  const chart = await ctx.backend.chart(ctx.tenant);
  const rawLines = Array.isArray(input.lines) ? input.lines : [];
  if (rawLines.length < 2) {
    throw new RecurringError("a recurring transaction needs at least two lines");
  }
  let debit = 0n;
  let credit = 0n;
  const lines: RecurringLineRecord[] = [];
  for (const l of rawLines) {
    const code = String(l.account_code ?? "").trim();
    if (!chart.getByCode(code)) throw new RecurringError(`unknown account code ${code}`);
    const side = String(l.side ?? "").toUpperCase();
    if (side !== "DEBIT" && side !== "CREDIT") {
      throw new RecurringError(`line ${code}: side must be DEBIT or CREDIT`);
    }
    const raw = typeof l.amount_minor === "number"
      ? String(l.amount_minor)
      : String(l.amount_minor ?? "").trim();
    if (!/^\d+$/.test(raw) || BigInt(raw) <= 0n) {
      throw new RecurringError(
        `line ${code}: amount must be a positive integer minor-unit value`,
      );
    }
    if (side === "DEBIT") debit += BigInt(raw);
    else credit += BigInt(raw);
    lines.push({
      accountCode: code,
      side,
      amountMinor: raw,
      memo: String(l.memo ?? ""),
      dimensions: l.dimensions ?? {},
    });
  }
  // Checked here rather than at post time: a template that can never balance
  // should fail when someone writes it, not silently on the first of the month.
  if (debit !== credit) {
    throw new RecurringError(
      `this template doesn't balance — debits ${debit} vs credits ${credit}`,
    );
  }

  const existing = input.id ? await ctx.backend.recurring().get(String(ctx.tenant), String(input.id)) : undefined;
  const record: RecurringRecord = {
    id: String(input.id ?? "").trim()
      || name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, ""),
    name,
    frequency,
    interval,
    startDate,
    endDate,
    memo: String(input.memo ?? "").trim(),
    active: input.active !== false,
    lastPosted: existing?.lastPosted ?? "",
    lines,
  };
  if (!record.id) throw new RecurringError("a recurring transaction needs a name");
  await ctx.backend.recurring().save(String(ctx.tenant), record);
  return record;
}

function scheduleOf(r: RecurringRecord): RecurringSchedule {
  return {
    frequency: r.frequency,
    interval: r.interval,
    startDate: r.startDate,
    ...(r.endDate ? { endDate: r.endDate } : {}),
  };
}

export interface DueOccurrence {
  readonly id: string;
  readonly name: string;
  readonly date: string;
  readonly memo: string;
  readonly amount_minor: string;
}

/**
 * What is due on or before `asOf` and hasn't been posted.
 *
 * "Hasn't been posted" is answered by asking the ledger, not by trusting a
 * `lastPosted` column: the column can drift if a post half-failed, and a
 * recurring transaction that quietly stops firing is a worse bug than one that
 * offers a duplicate for a person to decline.
 */
export async function dueOccurrences(
  ctx: RecurringContext, asOfRaw: unknown,
): Promise<DueOccurrence[]> {
  const asOf = requireDate(asOfRaw, "as-of date");
  const templates = await ctx.backend.recurring().list(String(ctx.tenant));
  const store = ctx.backend.store(ctx.tenant);

  const out: DueOccurrence[] = [];
  for (const t of templates) {
    if (!t.active) continue;
    for (const date of occurrencesBetween(scheduleOf(t), t.startDate, asOf)) {
      const key = asIdempotencyKey(idempotencyKeyFor(t.id, date));
      if (await store.getByIdempotencyKey(ctx.tenant, key)) continue;
      const total = t.lines
        .filter((l) => l.side === "DEBIT")
        .reduce((acc, l) => acc + BigInt(l.amountMinor), 0n);
      out.push({
        id: t.id,
        name: t.name,
        date,
        memo: t.memo || t.name,
        amount_minor: total.toString(),
      });
    }
  }
  return out.sort((a, b) => (a.date === b.date ? a.id.localeCompare(b.id) : a.date.localeCompare(b.date)));
}

function idempotencyKeyFor(templateId: string, date: string): string {
  return `recurring:${templateId}:${date}`;
}

function provenanceFor(templateId: string, date: string, at: string): Provenance {
  return {
    sourceSystem: "recurring",
    sourceObject: templateId,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/recurring-1",
    mappingVersion: "ledger-service/recurring-1",
  };
}

export interface RunResult {
  readonly posted: number;
  readonly skipped: number;
  readonly entries: ReadonlyArray<{
    readonly id: string;
    readonly date: string;
    readonly entry_id: string;
  }>;
  readonly failures: ReadonlyArray<{
    readonly id: string;
    readonly date: string;
    readonly error: string;
  }>;
}

/**
 * Post what's due. Nothing runs unattended — this is called because somebody
 * pressed a button, having seen the list.
 *
 * A failure on one occurrence never sinks the rest: a closed period or a
 * deleted account should stop that rent payment, not the other four.
 */
export async function runDue(
  ctx: RecurringContext, asOfRaw: unknown, onlyId?: string,
): Promise<RunResult> {
  const asOf = requireDate(asOfRaw, "as-of date");
  const due = (await dueOccurrences(ctx, asOf))
    .filter((d) => !onlyId || d.id === onlyId);

  const chart = await ctx.backend.chart(ctx.tenant);
  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  const store = ctx.backend.recurring();

  const entries: { id: string; date: string; entry_id: string }[] = [];
  const failures: { id: string; date: string; error: string }[] = [];

  for (const occurrence of due) {
    const template = await store.get(String(ctx.tenant), occurrence.id);
    if (!template) continue;
    const lines: JournalLineInput[] = template.lines.map((l) => {
      const account = chart.getByCode(l.accountCode);
      if (!account) throw new RecurringError(`unknown account code ${l.accountCode}`);
      return {
        accountId: account.id,
        side: l.side,
        amount: Money.fromMinorUnits(BigInt(l.amountMinor), ctx.currency),
        ...(l.memo ? { memo: l.memo } : {}),
        ...(Object.keys(l.dimensions ?? {}).length ? { dimensions: l.dimensions } : {}),
      };
    });
    const command: PostCommand = {
      tenantId: ctx.tenant,
      idempotencyKey: asIdempotencyKey(idempotencyKeyFor(template.id, occurrence.date)),
      periodKey: asPeriodKey(occurrence.date.slice(0, 7)),
      currency: ctx.currency,
      entryDate: occurrence.date,
      memo: template.memo || template.name,
      provenance: provenanceFor(template.id, occurrence.date, ctx.now()),
      lines,
    };
    try {
      await validateDimensions(
        { backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency }, command,
      );
      const entry = await engine.post(command, { postedAt: ctx.now() });
      entries.push({ id: template.id, date: occurrence.date, entry_id: String(entry.id) });
      if (occurrence.date > template.lastPosted) {
        await store.save(String(ctx.tenant), { ...template, lastPosted: occurrence.date });
      }
    } catch (err) {
      failures.push({
        id: template.id,
        date: occurrence.date,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  return {
    posted: entries.length,
    skipped: failures.length,
    entries,
    failures,
  };
}
