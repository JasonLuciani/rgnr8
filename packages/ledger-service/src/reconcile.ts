import { Money, type Currency, type TenantId } from "@rgnr8/ledger-kernel";
import {
  ClearedRegister,
  bankRegister,
  finishManualReconciliation,
  manualReconciliation,
  reconcileBankAccount,
  type ClearStatus,
  type ManualReconciliation,
  type Statement,
  type StatementLine,
} from "@rgnr8/reconciliation";
import { parseStatement, type StatementTxn } from "@rgnr8/ingestion";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";

/**
 * Bank reconciliation — proving the books against the bank.
 *
 * The journal is append-only, so "this entry cleared the bank" can't live on the
 * entry itself; it lives here, beside the ledger, keyed by (account, entry). An
 * owner works the way they do with a paper statement: tick the lines that appear
 * on it, watch the difference fall to zero, then lock the month in. Locked lines
 * become RECONCILED and are never re-ticked.
 *
 * This is what turns "the books are complete" into "the books are *trustworthy*":
 * an unreconciled ledger can be internally consistent and still wrong.
 */

export class ReconcileError extends Error {}

/** Durable cleared/reconciled status. Absent = UNCLEARED. */
export interface ReconStore {
  migrate(): Promise<void>;
  statuses(tenant: string, accountCode: string): Promise<Map<string, ClearStatus>>;
  setStatus(
    tenant: string,
    accountCode: string,
    entryId: string,
    status: ClearStatus,
  ): Promise<void>;
  setMany(
    tenant: string,
    accountCode: string,
    entryIds: readonly string[],
    status: ClearStatus,
  ): Promise<void>;
  reconciledThrough(tenant: string, accountCode: string): Promise<string | undefined>;
  setReconciledThrough(tenant: string, accountCode: string, date: string): Promise<void>;
}

export class InMemoryReconStore implements ReconStore {
  private readonly byAccount = new Map<string, Map<string, ClearStatus>>();
  private readonly through = new Map<string, string>();

  private key(tenant: string, code: string): string {
    return `${tenant} ${code}`;
  }

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  statuses(tenant: string, accountCode: string): Promise<Map<string, ClearStatus>> {
    return Promise.resolve(new Map(this.byAccount.get(this.key(tenant, accountCode)) ?? []));
  }

  setStatus(
    tenant: string,
    accountCode: string,
    entryId: string,
    status: ClearStatus,
  ): Promise<void> {
    const k = this.key(tenant, accountCode);
    const m = this.byAccount.get(k) ?? new Map<string, ClearStatus>();
    m.set(entryId, status);
    this.byAccount.set(k, m);
    return Promise.resolve();
  }

  async setMany(
    tenant: string,
    accountCode: string,
    entryIds: readonly string[],
    status: ClearStatus,
  ): Promise<void> {
    for (const id of entryIds) await this.setStatus(tenant, accountCode, id, status);
  }

  reconciledThrough(tenant: string, accountCode: string): Promise<string | undefined> {
    return Promise.resolve(this.through.get(this.key(tenant, accountCode)));
  }

  setReconciledThrough(tenant: string, accountCode: string, date: string): Promise<void> {
    const k = this.key(tenant, accountCode);
    const prev = this.through.get(k);
    if (!prev || date > prev) this.through.set(k, date);
    return Promise.resolve();
  }
}

export const RECON_DDL = `
CREATE TABLE IF NOT EXISTS cleared_status (
  tenant_id    text NOT NULL,
  account_code text NOT NULL,
  entry_id     text NOT NULL,
  status       text NOT NULL,
  CONSTRAINT cleared_status_pk PRIMARY KEY (tenant_id, account_code, entry_id)
);

CREATE TABLE IF NOT EXISTS reconciled_through (
  tenant_id    text NOT NULL,
  account_code text NOT NULL,
  through_date text NOT NULL,
  CONSTRAINT reconciled_through_pk PRIMARY KEY (tenant_id, account_code)
);
`;

export class PgReconStore implements ReconStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(RECON_DDL);
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

  async statuses(tenant: string, accountCode: string): Promise<Map<string, ClearStatus>> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT entry_id, status FROM cleared_status WHERE tenant_id=$1 AND account_code=$2",
        [tenant, accountCode],
      );
      const m = new Map<string, ClearStatus>();
      for (const r of res.rows) m.set(String(r["entry_id"]), String(r["status"]) as ClearStatus);
      return m;
    });
  }

  async setStatus(
    tenant: string,
    accountCode: string,
    entryId: string,
    status: ClearStatus,
  ): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO cleared_status (tenant_id, account_code, entry_id, status)
         VALUES ($1,$2,$3,$4)
         ON CONFLICT (tenant_id, account_code, entry_id) DO UPDATE SET status = EXCLUDED.status`,
        [tenant, accountCode, entryId, status],
      ),
    );
  }

  async setMany(
    tenant: string,
    accountCode: string,
    entryIds: readonly string[],
    status: ClearStatus,
  ): Promise<void> {
    await this.tx(tenant, async (db) => {
      for (const id of entryIds) {
        await db.query(
          `INSERT INTO cleared_status (tenant_id, account_code, entry_id, status)
           VALUES ($1,$2,$3,$4)
           ON CONFLICT (tenant_id, account_code, entry_id) DO UPDATE SET status = EXCLUDED.status`,
          [tenant, accountCode, id, status],
        );
      }
    });
  }

  async reconciledThrough(tenant: string, accountCode: string): Promise<string | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT through_date FROM reconciled_through WHERE tenant_id=$1 AND account_code=$2",
        [tenant, accountCode],
      );
      const row = res.rows[0];
      return row ? String(row["through_date"]) : undefined;
    });
  }

  async setReconciledThrough(tenant: string, accountCode: string, date: string): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO reconciled_through (tenant_id, account_code, through_date)
         VALUES ($1,$2,$3)
         ON CONFLICT (tenant_id, account_code) DO UPDATE SET
           through_date = GREATEST(reconciled_through.through_date, EXCLUDED.through_date)`,
        [tenant, accountCode, date],
      ),
    );
  }
}

// --- the flow ----------------------------------------------------------------

export interface ReconcileContext {
  readonly backend: LedgerBackend;
  readonly recon: ReconStore;
  readonly tenant: TenantId;
  readonly currency: Currency;
}

/** Hydrate an in-memory register from durable status, for the engine to read. */
async function loadRegister(
  ctx: ReconcileContext,
  accountId: string,
  accountCode: string,
): Promise<ClearedRegister> {
  const register = new ClearedRegister();
  const statuses = await ctx.recon.statuses(String(ctx.tenant), accountCode);
  for (const [entryId, status] of statuses) register.setStatus(accountId, entryId, status);
  return register;
}

async function resolveAccount(
  ctx: ReconcileContext,
  code: string,
): Promise<{ id: string; name: string }> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const account = chart.getByCode(code);
  if (!account) throw new ReconcileError(`unknown account code ${code}`);
  return { id: String(account.id), name: account.name };
}

export interface ReconcileView {
  readonly account_code: string;
  readonly account_name: string;
  readonly statement_date: string;
  readonly statement_balance_minor: string;
  readonly reconciled_through: string | null;
  readonly reconciled_balance_minor: string;
  readonly cleared_this_session_minor: string;
  readonly cleared_balance_minor: string;
  readonly difference_minor: string;
  readonly can_finish: boolean;
  readonly lines: ReadonlyArray<{
    readonly entry_id: string;
    readonly date: string;
    readonly memo: string;
    readonly amount_minor: string;
    readonly status: ClearStatus;
  }>;
}

function toView(
  code: string,
  name: string,
  recon: ManualReconciliation,
  reconciledThrough: string | undefined,
  lines: ReadonlyArray<{
    entryId: string;
    date: string;
    memo: string;
    amount: Money;
    status: ClearStatus;
  }>,
): ReconcileView {
  return {
    account_code: code,
    account_name: name,
    statement_date: recon.statementDate,
    statement_balance_minor: recon.statementBalance.minorUnits.toString(),
    reconciled_through: reconciledThrough ?? null,
    reconciled_balance_minor: recon.reconciledBalance.minorUnits.toString(),
    cleared_this_session_minor: recon.clearedThisSession.minorUnits.toString(),
    cleared_balance_minor: recon.clearedBalance.minorUnits.toString(),
    difference_minor: recon.difference.minorUnits.toString(),
    can_finish: recon.canFinish,
    lines: lines.map((l) => ({
      entry_id: l.entryId,
      date: l.date,
      memo: l.memo,
      amount_minor: l.amount.minorUnits.toString(),
      status: l.status,
    })),
  };
}

function parseBalance(raw: unknown, currency: Currency): Money {
  const text = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!/^-?\d+$/.test(text)) {
    throw new ReconcileError(
      "statement_balance_minor is required, as integer minor units (cents)",
    );
  }
  return Money.fromMinorUnits(BigInt(text), currency);
}

function requireDate(raw: unknown): string {
  const date = String(raw ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new ReconcileError("statement_date must be YYYY-MM-DD");
  }
  return date;
}

/** The current state of a reconciliation session for one bank account. */
export async function reconcileView(
  ctx: ReconcileContext,
  code: string,
  statementDateRaw: unknown,
  statementBalanceRaw: unknown,
): Promise<ReconcileView> {
  const { id, name } = await resolveAccount(ctx, code);
  const statementDate = requireDate(statementDateRaw);
  const statementBalance = parseBalance(statementBalanceRaw, ctx.currency);

  const register = await loadRegister(ctx, id, code);
  const entries = await ctx.backend.store(ctx.tenant).list(ctx.tenant);
  const lines = bankRegister(entries, id as never, ctx.currency, register);
  const recon = manualReconciliation(lines, statementDate, statementBalance);
  const through = await ctx.recon.reconciledThrough(String(ctx.tenant), code);
  return toView(code, name, recon, through, lines);
}

/** Tick (or untick) one register line. A RECONCILED line is immutable. */
export async function toggleCleared(
  ctx: ReconcileContext,
  code: string,
  entryId: string,
  cleared: boolean,
): Promise<void> {
  const { id } = await resolveAccount(ctx, code);
  if (!entryId.trim()) throw new ReconcileError("entry_id is required");
  const statuses = await ctx.recon.statuses(String(ctx.tenant), code);
  if (statuses.get(entryId) === "RECONCILED") {
    throw new ReconcileError(
      `${entryId} was locked in by a finished reconciliation and can't be changed`,
    );
  }
  // The entry must actually touch this account, or the tick is meaningless.
  const entries = await ctx.backend.store(ctx.tenant).list(ctx.tenant);
  const register = await loadRegister(ctx, id, code);
  const known = bankRegister(entries, id as never, ctx.currency, register)
    .some((l) => l.entryId === entryId);
  if (!known) throw new ReconcileError(`entry ${entryId} does not affect account ${code}`);

  await ctx.recon.setStatus(
    String(ctx.tenant), code, entryId, cleared ? "CLEARED" : "UNCLEARED",
  );
}

export interface FinishResult {
  readonly account_code: string;
  readonly statement_date: string;
  readonly reconciled_entries: number;
  readonly reconciled_through: string;
}

/** Lock in a balanced reconciliation. Refuses unless the difference is zero. */
export async function finishReconciliation(
  ctx: ReconcileContext,
  code: string,
  statementDateRaw: unknown,
  statementBalanceRaw: unknown,
): Promise<FinishResult> {
  const { id } = await resolveAccount(ctx, code);
  const statementDate = requireDate(statementDateRaw);
  const statementBalance = parseBalance(statementBalanceRaw, ctx.currency);

  const register = await loadRegister(ctx, id, code);
  const entries = await ctx.backend.store(ctx.tenant).list(ctx.tenant);
  const lines = bankRegister(entries, id as never, ctx.currency, register);
  const recon = manualReconciliation(lines, statementDate, statementBalance);

  // The engine owns the rule; it throws when the reconciliation doesn't tie out.
  try {
    finishManualReconciliation(recon, register);
  } catch (err) {
    // The engine speaks in internal account ids; an owner reads account codes.
    const message = err instanceof Error ? err.message : String(err);
    throw new ReconcileError(message.replace(String(id), code));
  }

  await ctx.recon.setMany(String(ctx.tenant), code, recon.clearedEntryIds, "RECONCILED");
  await ctx.recon.setReconciledThrough(String(ctx.tenant), code, statementDate);
  return {
    account_code: code,
    statement_date: statementDate,
    reconciled_entries: recon.clearedEntryIds.length,
    reconciled_through: statementDate,
  };
}


// --- importing a statement ---------------------------------------------------

export interface ImportResult {
  readonly account_code: string;
  readonly statement_date: string;
  readonly parsed: number;
  readonly matched: number;
  readonly newly_cleared: number;
  readonly difference_minor: string;
  readonly can_finish: boolean;
  /** Statement lines with no entry in the books — money the books don't know about. */
  readonly missing_from_books: ReadonlyArray<{
    readonly date: string;
    readonly amount_minor: string;
    readonly description: string;
  }>;
  /** Book lines the statement doesn't show — outstanding cheques, deposits in transit. */
  readonly not_on_statement: ReadonlyArray<{
    readonly entry_id: string;
    readonly date: string;
    readonly amount_minor: string;
    readonly memo: string;
  }>;
  readonly view: ReconcileView;
}

function toStatementLine(txn: StatementTxn, currency: Currency): StatementLine {
  // Statement amounts arrive as signed decimal strings from the bank; they
  // become exact minor units here and never pass through a float.
  return {
    id: txn.fitid,
    date: txn.date,
    amount: Money.fromDecimal(txn.amount, currency),
    description: txn.description,
  };
}

/**
 * Import an OFX or CSV statement and tick off everything it matches.
 *
 * This is the same reconciliation, done by machine for the obvious part. What
 * it deliberately does NOT do is finish: matching within a few days on an equal
 * amount is a good heuristic and a bad authority. The owner still sees the
 * difference and presses the button, because the value of a reconciliation is
 * that a person looked.
 *
 * Two lists come back and both matter. Statement lines with no book entry are
 * transactions the business doesn't know happened — the fraud case, and the
 * forgotten-subscription case. Book lines the statement doesn't show are
 * outstanding items, which are normal, or duplicates, which are not.
 */
export async function importStatement(
  ctx: ReconcileContext,
  code: string,
  text: string,
  statementDateRaw: unknown,
  statementBalanceRaw: unknown,
): Promise<ImportResult> {
  const { id } = await resolveAccount(ctx, code);
  const statementDate = requireDate(statementDateRaw);
  const closing = parseBalance(statementBalanceRaw, ctx.currency);

  if (!text.trim()) throw new ReconcileError("the statement file is empty");
  let parsed: StatementTxn[];
  try {
    parsed = parseStatement(text);
  } catch (err) {
    throw new ReconcileError(
      `this file could not be read as OFX or CSV: ${err instanceof Error ? err.message : String(err)}`,
    );
  }
  if (parsed.length === 0) {
    throw new ReconcileError(
      "no transactions were found in this file — check it is the transaction export "
      + "rather than a summary, and that it covers the right dates",
    );
  }

  const lines = parsed.map((t) => toStatementLine(t, ctx.currency));
  const register = await loadRegister(ctx, id, code);
  const entries = await ctx.backend.store(ctx.tenant).list(ctx.tenant);

  // The opening balance the statement implies: closing less everything on it.
  const movement = lines.reduce((acc, l) => acc + l.amount.minorUnits, 0n);
  const dates = lines.map((l) => l.date).sort();
  const statement: Statement = {
    accountId: id,
    periodStart: dates[0] ?? statementDate,
    periodEnd: statementDate,
    openingBalance: Money.fromMinorUnits(closing.minorUnits - movement, ctx.currency),
    closingBalance: closing,
    lines,
  };

  const before = await ctx.recon.statuses(String(ctx.tenant), code);
  const recon = reconcileBankAccount(entries, id as never, statement, register);

  // Persist the ticks. They are CLEARED, not RECONCILED: a machine match is a
  // suggestion the owner confirms by finishing, not a decision it makes alone.
  await ctx.recon.setMany(String(ctx.tenant), code, recon.newlyClearedEntryIds, "CLEARED");

  // Re-importing the same statement is normal — banks re-issue them, and people
  // click twice. Matching the same lines again is harmless, but reporting them
  // as newly cleared would overstate what this import actually did.
  const newlyCleared = recon.newlyClearedEntryIds.filter(
    (entryId) => before.get(entryId) !== "CLEARED",
  );

  const view = await reconcileView(ctx, code, statementDate, closing.minorUnits.toString());
  return {
    account_code: code,
    statement_date: statementDate,
    parsed: parsed.length,
    matched: recon.matched.length,
    newly_cleared: newlyCleared.length,
    difference_minor: view.difference_minor,
    can_finish: view.can_finish,
    missing_from_books: recon.unmatchedStatement.map((l) => ({
      date: l.date,
      amount_minor: l.amount.minorUnits.toString(),
      description: l.description,
    })),
    not_on_statement: recon.unclearedLines.map((l) => ({
      entry_id: l.entryId,
      date: l.date,
      amount_minor: l.amount.minorUnits.toString(),
      memo: l.memo,
    })),
    view,
  };
}
