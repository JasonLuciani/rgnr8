/**
 * Every bank and card transaction QuickBooks holds, kept as a non-posting copy.
 *
 * During a parallel run the books are a QuickBooks mirror: an opening entry plus
 * catch-ups, one total per account. That keeps the balances exact, but it means
 * RGNR8 has no transactions behind them: no record of when cash came in or went
 * out, so a cash outlook can only guess, and the cutover would leave the history
 * behind in QuickBooks.
 *
 * This keeps the line-level history beside the books. It posts nothing (the
 * mirror's totals stay the books), so it is allowed while QuickBooks is the book
 * of record. Each import replaces one date window: re-reading an overlapping
 * window is normal, and the result is what QuickBooks says for those dates.
 *
 * Amounts are signed minor units from the account's own point of view: for a
 * bank account money in is positive and money out negative; for a credit card a
 * charge is negative (spending) and a payment to the card positive. That way
 * "out" means the same thing on every account.
 */

import type { Pool, Queryable } from "@rgnr8/ledger-postgres";

export type HistoryAccountKind = "bank" | "card";

export interface HistoryLine {
  /** Stable within an import: QuickBooks' transaction id, type and account. */
  readonly id: string;
  readonly accountCode: string;
  readonly accountName: string;
  readonly accountKind: HistoryAccountKind;
  readonly date: string;
  /** QuickBooks' transaction type as it shows it: "Expense", "Deposit", "Paycheck"... */
  readonly txnType: string;
  readonly number: string;
  /** The customer, vendor or employee. */
  readonly name: string;
  readonly memo: string;
  /** The other side of the transaction, as QuickBooks names it ("-Split-" for several). */
  readonly split: string;
  /** Signed: positive money in, negative money out (see the module note). */
  readonly amountMinor: bigint;
}

/** One account's check of an import against QuickBooks' own running balance. */
export interface HistoryTieOut {
  readonly accountCode: string;
  readonly accountName: string;
  /** QuickBooks' balance at the start of the window, as its report gives it. */
  readonly openingMinor: bigint;
  /** The sum of the lines in the window. */
  readonly movementMinor: bigint;
  /** QuickBooks' balance at the end of the window. */
  readonly closingMinor: bigint;
}

export interface HistoryCoverage {
  /** The first and last day any import has covered; "" before the first import. */
  readonly from: string;
  readonly to: string;
  /** When QuickBooks was last read for this history (ISO timestamp). */
  readonly importedAt: string;
  /** The last import's per-account tie-out. */
  readonly tieOut: readonly HistoryTieOut[];
}

export interface HistoryImport {
  readonly start: string;
  readonly end: string;
  readonly importedAt: string;
  readonly lines: readonly HistoryLine[];
  readonly tieOut: readonly HistoryTieOut[];
}

export interface HistoryQuery {
  readonly from?: string;
  readonly to?: string;
  readonly account?: string;
}

export class BankHistoryError extends Error {}

export interface BankHistoryStore {
  migrate(): Promise<void>;
  coverage(tenant: string): Promise<HistoryCoverage>;
  lines(tenant: string, q: HistoryQuery): Promise<HistoryLine[]>;
  /** Replace every line dated inside [start, end] with these. */
  replaceWindow(tenant: string, imp: HistoryImport): Promise<void>;
}

const NONE: HistoryCoverage = { from: "", to: "", importedAt: "", tieOut: [] };

function widen(c: HistoryCoverage, imp: HistoryImport): HistoryCoverage {
  return {
    from: c.from && c.from < imp.start ? c.from : imp.start,
    to: c.to && c.to > imp.end ? c.to : imp.end,
    importedAt: imp.importedAt,
    tieOut: imp.tieOut,
  };
}

function matches(l: HistoryLine, q: HistoryQuery): boolean {
  if (q.from && l.date < q.from) return false;
  if (q.to && l.date > q.to) return false;
  if (q.account && l.accountCode !== q.account) return false;
  return true;
}

const byDate = (a: HistoryLine, b: HistoryLine): number =>
  a.date === b.date ? (a.id < b.id ? -1 : a.id > b.id ? 1 : 0) : a.date < b.date ? -1 : 1;

export class InMemoryBankHistoryStore implements BankHistoryStore {
  private readonly rows = new Map<string, HistoryLine[]>();
  private readonly cover = new Map<string, HistoryCoverage>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  coverage(tenant: string): Promise<HistoryCoverage> {
    return Promise.resolve(this.cover.get(tenant) ?? NONE);
  }

  lines(tenant: string, q: HistoryQuery): Promise<HistoryLine[]> {
    return Promise.resolve((this.rows.get(tenant) ?? []).filter((l) => matches(l, q)).sort(byDate));
  }

  replaceWindow(tenant: string, imp: HistoryImport): Promise<void> {
    const kept = (this.rows.get(tenant) ?? []).filter((l) => l.date < imp.start || l.date > imp.end);
    this.rows.set(tenant, [...kept, ...imp.lines]);
    this.cover.set(tenant, widen(this.cover.get(tenant) ?? NONE, imp));
    return Promise.resolve();
  }
}

export const BANK_HISTORY_DDL = `
CREATE TABLE IF NOT EXISTS mirror_bank_line (
  tenant_id    text   NOT NULL,
  id           text   NOT NULL,
  account_code text   NOT NULL,
  account_name text   NOT NULL DEFAULT '',
  account_kind text   NOT NULL CHECK (account_kind IN ('bank', 'card')),
  txn_date     text   NOT NULL,
  txn_type     text   NOT NULL DEFAULT '',
  number       text   NOT NULL DEFAULT '',
  name         text   NOT NULL DEFAULT '',
  memo         text   NOT NULL DEFAULT '',
  split        text   NOT NULL DEFAULT '',
  amount_minor bigint NOT NULL,
  PRIMARY KEY (tenant_id, id)
);
CREATE INDEX IF NOT EXISTS mirror_bank_line_date ON mirror_bank_line (tenant_id, txn_date);
CREATE TABLE IF NOT EXISTS mirror_bank_history (
  tenant_id   text NOT NULL PRIMARY KEY,
  covered_from text NOT NULL,
  covered_to   text NOT NULL,
  imported_at  text NOT NULL,
  tie_out      text NOT NULL DEFAULT '[]'
);
`;

const INSERT_CHUNK = 200;

function tieOutToJson(t: readonly HistoryTieOut[]): string {
  return JSON.stringify(t.map((x) => ({
    account_code: x.accountCode, account_name: x.accountName,
    opening_minor: x.openingMinor.toString(), movement_minor: x.movementMinor.toString(),
    closing_minor: x.closingMinor.toString(),
  })));
}

function tieOutFromJson(raw: string): HistoryTieOut[] {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.flatMap((o) => {
      if (typeof o !== "object" || o === null) return [];
      const r = o as Record<string, unknown>;
      return [{
        accountCode: String(r["account_code"] ?? ""), accountName: String(r["account_name"] ?? ""),
        openingMinor: BigInt(String(r["opening_minor"] ?? "0")),
        movementMinor: BigInt(String(r["movement_minor"] ?? "0")),
        closingMinor: BigInt(String(r["closing_minor"] ?? "0")),
      }];
    });
  } catch {
    return [];
  }
}

export class PgBankHistoryStore implements BankHistoryStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(BANK_HISTORY_DDL);
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

  async coverage(tenant: string): Promise<HistoryCoverage> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT covered_from, covered_to, imported_at, tie_out FROM mirror_bank_history WHERE tenant_id=$1",
        [tenant],
      );
      const r = res.rows[0];
      if (!r) return NONE;
      return {
        from: String(r["covered_from"]), to: String(r["covered_to"]),
        importedAt: String(r["imported_at"]), tieOut: tieOutFromJson(String(r["tie_out"] ?? "[]")),
      };
    });
  }

  async lines(tenant: string, q: HistoryQuery): Promise<HistoryLine[]> {
    return this.tx(tenant, async (db) => {
      const where = ["tenant_id=$1"];
      const args: unknown[] = [tenant];
      if (q.from) { args.push(q.from); where.push(`txn_date >= $${args.length}`); }
      if (q.to) { args.push(q.to); where.push(`txn_date <= $${args.length}`); }
      if (q.account) { args.push(q.account); where.push(`account_code = $${args.length}`); }
      const res = await db.query(
        `SELECT id, account_code, account_name, account_kind, txn_date, txn_type, number, name,
                memo, split, amount_minor
         FROM mirror_bank_line WHERE ${where.join(" AND ")} ORDER BY txn_date, id`,
        args,
      );
      return res.rows.map((r) => ({
        id: String(r["id"]),
        accountCode: String(r["account_code"]),
        accountName: String(r["account_name"] ?? ""),
        accountKind: String(r["account_kind"]) === "card" ? "card" : "bank",
        date: String(r["txn_date"]),
        txnType: String(r["txn_type"] ?? ""),
        number: String(r["number"] ?? ""),
        name: String(r["name"] ?? ""),
        memo: String(r["memo"] ?? ""),
        split: String(r["split"] ?? ""),
        amountMinor: BigInt(String(r["amount_minor"])),
      }));
    });
  }

  async replaceWindow(tenant: string, imp: HistoryImport): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM mirror_bank_line WHERE tenant_id=$1 AND txn_date >= $2 AND txn_date <= $3",
        [tenant, imp.start, imp.end],
      );
      for (let i = 0; i < imp.lines.length; i += INSERT_CHUNK) {
        const chunk = imp.lines.slice(i, i + INSERT_CHUNK);
        const args: unknown[] = [];
        const values = chunk.map((l) => {
          const base = args.length;
          args.push(tenant, l.id, l.accountCode, l.accountName, l.accountKind, l.date, l.txnType,
            l.number, l.name, l.memo, l.split, l.amountMinor.toString());
          return `(${Array.from({ length: 12 }, (_, k) => `$${base + k + 1}`).join(", ")})`;
        });
        await db.query(
          `INSERT INTO mirror_bank_line
             (tenant_id, id, account_code, account_name, account_kind, txn_date, txn_type, number,
              name, memo, split, amount_minor)
           VALUES ${values.join(", ")}`,
          args,
        );
      }
      const prev = await db.query(
        "SELECT covered_from, covered_to FROM mirror_bank_history WHERE tenant_id=$1", [tenant]);
      const p = prev.rows[0];
      const next = widen(
        p ? { from: String(p["covered_from"]), to: String(p["covered_to"]), importedAt: "", tieOut: [] }
          : NONE,
        imp,
      );
      await db.query(
        `INSERT INTO mirror_bank_history (tenant_id, covered_from, covered_to, imported_at, tie_out)
         VALUES ($1, $2, $3, $4, $5)
         ON CONFLICT (tenant_id) DO UPDATE SET covered_from=EXCLUDED.covered_from,
           covered_to=EXCLUDED.covered_to, imported_at=EXCLUDED.imported_at, tie_out=EXCLUDED.tie_out`,
        [tenant, next.from, next.to, next.importedAt, tieOutToJson(imp.tieOut)],
      );
    });
  }
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;
/** An import is a window of QuickBooks history; a whole decade in one call is a mistake. */
const MAX_LINES = 50_000;

function minor(v: unknown, what: string): bigint {
  try {
    return BigInt(String(v ?? ""));
  } catch {
    throw new BankHistoryError(`${what} must be an integer number of cents`);
  }
}

/** Parse one import from the web app's request body, refusing anything malformed. */
export function importFromJson(data: Record<string, unknown>): HistoryImport {
  const start = String(data["start"] ?? "").trim();
  const end = String(data["end"] ?? "").trim();
  if (!DATE.test(start) || !DATE.test(end)) throw new BankHistoryError("start and end must be YYYY-MM-DD");
  if (start > end) throw new BankHistoryError("start must not be after end");
  const importedAt = String(data["imported_at"] ?? "").trim() || new Date().toISOString();
  const raw = data["lines"] ?? [];
  if (!Array.isArray(raw)) throw new BankHistoryError("lines must be an array");
  if (raw.length > MAX_LINES) throw new BankHistoryError(`at most ${MAX_LINES} lines per import`);
  const seen = new Set<string>();
  const lines: HistoryLine[] = raw.map((r: unknown) => {
    if (typeof r !== "object" || r === null) throw new BankHistoryError("each line must be an object");
    const o = r as Record<string, unknown>;
    const id = String(o["id"] ?? "").trim();
    if (!id) throw new BankHistoryError("a line has no id");
    if (seen.has(id)) throw new BankHistoryError(`line ${id} appears twice`);
    seen.add(id);
    const date = String(o["date"] ?? "");
    if (!DATE.test(date)) throw new BankHistoryError(`line ${id}: date must be YYYY-MM-DD`);
    if (date < start || date > end) throw new BankHistoryError(`line ${id} is dated outside ${start} to ${end}`);
    const code = String(o["account_code"] ?? "").trim();
    if (!code) throw new BankHistoryError(`line ${id} has no account_code`);
    const kind = String(o["account_kind"] ?? "");
    if (kind !== "bank" && kind !== "card") throw new BankHistoryError(`line ${id}: account_kind must be bank or card`);
    return {
      id, date, accountCode: code, accountKind: kind,
      accountName: String(o["account_name"] ?? ""),
      txnType: String(o["txn_type"] ?? ""),
      number: String(o["number"] ?? ""),
      name: String(o["name"] ?? ""),
      memo: String(o["memo"] ?? ""),
      split: String(o["split"] ?? ""),
      amountMinor: minor(o["amount_minor"], `line ${id}: amount_minor`),
    };
  });
  const tieRaw = data["tie_out"] ?? [];
  if (!Array.isArray(tieRaw)) throw new BankHistoryError("tie_out must be an array");
  const tieOut: HistoryTieOut[] = tieRaw.map((r: unknown) => {
    if (typeof r !== "object" || r === null) throw new BankHistoryError("each tie_out row must be an object");
    const o = r as Record<string, unknown>;
    return {
      accountCode: String(o["account_code"] ?? ""),
      accountName: String(o["account_name"] ?? ""),
      openingMinor: minor(o["opening_minor"], "tie_out opening_minor"),
      movementMinor: minor(o["movement_minor"], "tie_out movement_minor"),
      closingMinor: minor(o["closing_minor"], "tie_out closing_minor"),
    };
  });
  return { start, end, importedAt, lines, tieOut };
}

export type HistoryGroup = "week" | "month";

/** The Monday a date falls in, as YYYY-MM-DD (UTC calendar arithmetic). */
function weekOf(date: string): string {
  const d = new Date(`${date}T00:00:00Z`);
  const back = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - back);
  return d.toISOString().slice(0, 10);
}

/**
 * Money in and out per account per period: what a cash outlook learns timing
 * from. Credit-card lines are spending detail, not cash leaving the bank (that
 * happens when the card is paid), so the bank and card totals are kept apart.
 */
export function summarize(lines: readonly HistoryLine[], group: HistoryGroup): Record<string, unknown>[] {
  const buckets = new Map<string, { period: string; code: string; name: string; kind: string; inn: bigint; out: bigint; n: number }>();
  for (const l of lines) {
    const period = group === "week" ? weekOf(l.date) : l.date.slice(0, 7);
    const key = `${period}|${l.accountCode}`;
    const b = buckets.get(key) ?? { period, code: l.accountCode, name: l.accountName, kind: l.accountKind, inn: 0n, out: 0n, n: 0 };
    if (l.amountMinor >= 0n) b.inn += l.amountMinor;
    else b.out -= l.amountMinor;
    b.n += 1;
    buckets.set(key, b);
  }
  return [...buckets.values()]
    .sort((a, b) => (a.period === b.period ? (a.code < b.code ? -1 : 1) : a.period < b.period ? -1 : 1))
    .map((b) => ({
      period: b.period, account_code: b.code, account_name: b.name, account_kind: b.kind,
      in_minor: b.inn.toString(), out_minor: b.out.toString(), count: b.n,
    }));
}

export function lineJson(l: HistoryLine): Record<string, unknown> {
  return {
    id: l.id, account_code: l.accountCode, account_name: l.accountName, account_kind: l.accountKind,
    date: l.date, txn_type: l.txnType, number: l.number, name: l.name, memo: l.memo, split: l.split,
    amount_minor: l.amountMinor.toString(),
  };
}

export function coverageJson(c: HistoryCoverage): Record<string, unknown> {
  return {
    covered_from: c.from, covered_to: c.to, imported_at: c.importedAt,
    tie_out: c.tieOut.map((t) => ({
      account_code: t.accountCode, account_name: t.accountName,
      opening_minor: t.openingMinor.toString(), movement_minor: t.movementMinor.toString(),
      closing_minor: t.closingMinor.toString(),
      difference_minor: (t.openingMinor + t.movementMinor - t.closingMinor).toString(),
    })),
  };
}
