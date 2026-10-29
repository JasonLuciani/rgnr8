/**
 * Cutover with a way back: moving an account's book of record from QuickBooks to
 * RGNR8 at the end of a month, checked first, signed off, and undoable until
 * RGNR8 has recorded anything of its own.
 *
 * During the parallel run the ledger is a mirror: the A/R and A/P balances are
 * right but no invoice or bill sits behind them, and QuickBooks' open items are
 * kept beside the books as a non-posting copy (openItems.ts). Cutover:
 *
 *  1. plans, writing nothing: the cutover date must be a month-end that has
 *     passed (a mid-month cutover would lock the rest of that month), the books
 *     must be a pure QuickBooks copy brought level through that date, and the
 *     open items must tie to A/R and A/P. The plan carries a fingerprint;
 *  2. executes only with the name of the person who signed that plan and its
 *     fingerprint (so nobody signs one report and runs another). The open
 *     items become real open invoices and bills WITHOUT posting: their amounts
 *     are already in A/R and A/P, and posting them again would double them.
 *     Then the months through the cutover are locked and RGNR8 becomes the book
 *     of record;
 *  3. can be backed out while RGNR8 has recorded nothing of its own (no entry
 *     that isn't a QuickBooks copy, no payment against the new documents).
 */

import { createHash } from "node:crypto";
import {
  AccountSubtype,
  asPeriodKey,
  computeTrialBalance,
  type Currency,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import { isQuickBooksCopy } from "./bookOfRecord.js";
import type { DocKind, DocRecord, PartyKind } from "./documents.js";
import type { OpenItem } from "./openItems.js";

export class CutoverError extends Error {}

export interface CutoverRecord {
  readonly cutoverDate: string;
  readonly signedBy: string;
  readonly reportHash: string;
  readonly executedAt: string;
  /** The documents cutover created, so a backout removes exactly those. */
  readonly docs: readonly { readonly kind: DocKind; readonly id: string }[];
  readonly state: "live" | "backed_out";
  readonly backedOutAt: string;
}

export interface CutoverStore {
  migrate(): Promise<void>;
  get(tenant: string): Promise<CutoverRecord | undefined>;
  save(tenant: string, record: CutoverRecord): Promise<void>;
}

export class InMemoryCutoverStore implements CutoverStore {
  private readonly rows = new Map<string, CutoverRecord>();
  migrate(): Promise<void> {
    return Promise.resolve();
  }
  get(tenant: string): Promise<CutoverRecord | undefined> {
    return Promise.resolve(this.rows.get(tenant));
  }
  save(tenant: string, record: CutoverRecord): Promise<void> {
    this.rows.set(tenant, record);
    return Promise.resolve();
  }
}

export const CUTOVER_DDL = `
CREATE TABLE IF NOT EXISTS rgnr8_cutover (
  tenant_id     text PRIMARY KEY,
  cutover_date  text NOT NULL,
  signed_by     text NOT NULL,
  report_hash   text NOT NULL,
  executed_at   text NOT NULL,
  docs          text NOT NULL,
  state         text NOT NULL CHECK (state IN ('live', 'backed_out')),
  backed_out_at text NOT NULL DEFAULT ''
);
`;

export class PgCutoverStore implements CutoverStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(CUTOVER_DDL);
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

  async get(tenant: string): Promise<CutoverRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM rgnr8_cutover WHERE tenant_id=$1", [tenant]);
      const r = res.rows[0];
      if (!r) return undefined;
      return {
        cutoverDate: String(r["cutover_date"]),
        signedBy: String(r["signed_by"]),
        reportHash: String(r["report_hash"]),
        executedAt: String(r["executed_at"]),
        docs: JSON.parse(String(r["docs"])) as CutoverRecord["docs"],
        state: String(r["state"]) === "backed_out" ? "backed_out" : "live",
        backedOutAt: String(r["backed_out_at"] ?? ""),
      };
    });
  }

  async save(tenant: string, c: CutoverRecord): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO rgnr8_cutover
           (tenant_id, cutover_date, signed_by, report_hash, executed_at, docs, state, backed_out_at)
         VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
         ON CONFLICT (tenant_id) DO UPDATE SET
           cutover_date=EXCLUDED.cutover_date, signed_by=EXCLUDED.signed_by,
           report_hash=EXCLUDED.report_hash, executed_at=EXCLUDED.executed_at,
           docs=EXCLUDED.docs, state=EXCLUDED.state, backed_out_at=EXCLUDED.backed_out_at`,
        [tenant, c.cutoverDate, c.signedBy, c.reportHash, c.executedAt,
          JSON.stringify(c.docs), c.state, c.backedOutAt],
      ),
    );
  }
}

// --- the plan -------------------------------------------------------------------

export interface PlannedDoc {
  readonly kind: DocKind;
  readonly id: string;
  readonly partyId: string;
  readonly partyName: string;
  readonly date: string;
  readonly dueDate: string;
  readonly openMinor: bigint;
  readonly qboId: string;
}

export interface TieOut {
  readonly control: "Accounts Receivable" | "Accounts Payable";
  readonly booksMinor: bigint;
  readonly openItemsMinor: bigint;
  readonly differenceMinor: bigint;
}

export interface CutoverPlan {
  readonly cutoverDate: string;
  readonly openItemsAsOf: string;
  readonly docs: readonly PlannedDoc[];
  readonly tieOut: readonly TieOut[];
  /** Why it can't go ahead; empty when it can. */
  readonly blockers: readonly string[];
  readonly hash: string;
}

export interface CutoverContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

const DATE = /^\d{4}-\d{2}-\d{2}$/;

/** Whether `date` (YYYY-MM-DD) is the last day of its month. */
export function isMonthEnd(date: string): boolean {
  if (!DATE.test(date)) return false;
  const d = new Date(`${date}T00:00:00Z`);
  const next = new Date(d.getTime() + 86_400_000);
  return next.getUTCDate() === 1;
}

const slug = (s: string): string =>
  s.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60) || "unnamed";

function plannedDoc(i: OpenItem): PlannedDoc {
  const kind: DocKind = i.kind;
  const prefix = kind === "invoice" ? "c" : "v";
  return {
    kind,
    id: kind === "invoice" && i.number ? i.number : `QBO-${kind === "invoice" ? "INV" : "BILL"}-${i.qboId}`,
    partyId: `qbo-${prefix}-${slug(i.party)}`,
    partyName: i.party || (kind === "invoice" ? "Customer" : "Vendor"),
    date: i.date,
    dueDate: i.dueDate || i.date,
    openMinor: i.openMinor,
    qboId: i.qboId,
  };
}

/** What a cutover at `cutoverDate` would do, and whether it may. Writes nothing. */
export async function planCutover(ctx: CutoverContext, cutoverDate: string): Promise<CutoverPlan> {
  const tenant = String(ctx.tenant);
  const blockers: string[] = [];
  const today = ctx.now().slice(0, 10);
  const settings = await ctx.backend.settings().get(tenant);
  if (settings.bookOfRecord !== "quickbooks") {
    blockers.push("RGNR8 is already the book of record for this account");
  }
  if (!isMonthEnd(cutoverDate)) {
    blockers.push(`${cutoverDate || "the cutover date"} is not the last day of a month: `
      + "a cutover mid-month would lock the rest of that month");
  } else if (cutoverDate >= today) {
    blockers.push(`${cutoverDate} hasn't ended yet: cut over once the month is closed`);
  }

  const entries = await ctx.backend.store(ctx.tenant).list(ctx.tenant);
  const own = entries.filter((e) => !isQuickBooksCopy(e));
  if (own.length) {
    blockers.push(`the books hold ${own.length} entr${own.length === 1 ? "y" : "ies"} that didn't `
      + "come from QuickBooks, so they are no longer an exact copy to cut over from");
  }
  const lastCopy = entries.map((e) => e.entryDate).sort().at(-1) ?? "";
  if (cutoverDate && lastCopy < cutoverDate) {
    blockers.push(`the books were last brought level with QuickBooks on ${lastCopy || "no date"}; `
      + `Refresh after ${cutoverDate} first`);
  }

  const snapshot = await ctx.backend.openItems().get(tenant);
  if (!snapshot.asOf) {
    blockers.push("QuickBooks' open invoices and bills haven't been copied yet: Refresh first");
  } else if (cutoverDate && snapshot.asOf < cutoverDate) {
    blockers.push(`the open invoices and bills were copied on ${snapshot.asOf}, before the `
      + `cutover date; Refresh after ${cutoverDate} first`);
  }
  const docs = snapshot.items.map(plannedDoc);
  const seen = new Set<string>();
  for (const d of docs) {
    const key = `${d.kind}:${d.id}`;
    if (seen.has(key)) blockers.push(`two open ${d.kind}s would both be numbered ${d.id}`);
    seen.add(key);
    if (await ctx.backend.documents().getDoc(tenant, d.kind, d.id)) {
      blockers.push(`an ${d.kind} numbered ${d.id} already exists in RGNR8`);
    }
  }

  // Tie the open items to the control accounts, found by what they are.
  const chart = await ctx.backend.chart(ctx.tenant);
  const tb = await computeTrialBalance(ctx.backend.store(ctx.tenant), ctx.tenant, chart, ctx.currency);
  const control = (subtype: AccountSubtype): bigint =>
    tb.rows.filter((r) => r.subtype === subtype)
      .reduce((sum, r) => sum + r.debit.minorUnits - r.credit.minorUnits, 0n);
  const sum = (kind: DocKind): bigint =>
    docs.filter((d) => d.kind === kind).reduce((s, d) => s + d.openMinor, 0n);
  const ar = control(AccountSubtype.ACCOUNTS_RECEIVABLE);
  const ap = -control(AccountSubtype.ACCOUNTS_PAYABLE); // a credit balance, as an amount owed
  const tieOut: TieOut[] = [
    { control: "Accounts Receivable", booksMinor: ar, openItemsMinor: sum("invoice"),
      differenceMinor: sum("invoice") - ar },
    { control: "Accounts Payable", booksMinor: ap, openItemsMinor: sum("bill"),
      differenceMinor: sum("bill") - ap },
  ];

  const hash = createHash("sha256").update(JSON.stringify({
    cutoverDate,
    openItemsAsOf: snapshot.asOf,
    docs: docs.map((d) => [d.kind, d.id, d.partyId, d.date, d.dueDate, d.openMinor.toString()]),
    tieOut: tieOut.map((t) => [t.control, t.booksMinor.toString(), t.openItemsMinor.toString()]),
    entries: entries.length,
  })).digest("hex");

  return { cutoverDate, openItemsAsOf: snapshot.asOf, docs, tieOut, blockers, hash };
}

export interface ExecuteRequest {
  readonly cutoverDate: string;
  readonly signedBy: string;
  readonly reportHash: string;
  /** The signer saw the tie-out differences in the report and accepts them. */
  readonly acceptDifferences: boolean;
}

/** Cut over. Refuses unless the plan is clean and is the one that was signed. */
export async function executeCutover(
  ctx: CutoverContext, req: ExecuteRequest,
): Promise<{ record: CutoverRecord; plan: CutoverPlan }> {
  const tenant = String(ctx.tenant);
  if (!req.signedBy.trim()) {
    throw new CutoverError("cutover needs the name of the person who signed the report");
  }
  const plan = await planCutover(ctx, req.cutoverDate);
  if (plan.blockers.length) throw new CutoverError(plan.blockers.join("; "));
  if (plan.hash !== req.reportHash) {
    throw new CutoverError("the books changed after the report was signed: review the new report");
  }
  const differs = plan.tieOut.filter((t) => t.differenceMinor !== 0n);
  if (differs.length && !req.acceptDifferences) {
    throw new CutoverError(`${differs.map((t) => t.control).join(" and ")} don't tie to the open `
      + "items; the signer has to accept the difference shown in the report");
  }

  const docs = ctx.backend.documents();
  const settings = await ctx.backend.settings().get(tenant);
  const controls = {
    invoice: await controlCode(ctx, AccountSubtype.ACCOUNTS_RECEIVABLE),
    bill: await controlCode(ctx, AccountSubtype.ACCOUNTS_PAYABLE),
  };
  for (const d of plan.docs) {
    const partyKind: PartyKind = d.kind === "invoice" ? "customer" : "vendor";
    if (!(await docs.getParty(tenant, partyKind, d.partyId))) {
      await docs.upsertParty(tenant, partyKind, { id: d.partyId, name: d.partyName });
    }
    const amount = d.openMinor.toString();
    const record: DocRecord = {
      id: d.id, kind: d.kind, partyId: d.partyId, date: d.date, dueDate: d.dueDate,
      netMinor: amount, taxMinor: "0", taxRatePpm: 0, totalMinor: amount, openMinor: amount,
      status: "OPEN",
      memo: `Open in QuickBooks at cutover on ${plan.cutoverDate} (QuickBooks id ${d.qboId})`,
      lines: [{
        description: "Balance open in QuickBooks at cutover", quantity: 1,
        unitAmountMinor: amount, accountCode: controls[d.kind], amountMinor: amount,
        taxable: false,
      }],
    };
    // Saved, never posted: this money is already in A/R or A/P.
    await docs.saveDoc(tenant, record);
  }
  await ctx.backend.periods(ctx.tenant).lockThrough(ctx.tenant, asPeriodKey(plan.cutoverDate.slice(0, 7)));
  await ctx.backend.settings().save(tenant, { ...settings, bookOfRecord: "rgnr8" });
  await ctx.backend.openItems().replace(tenant, { asOf: "", items: [] });
  const record: CutoverRecord = {
    cutoverDate: plan.cutoverDate, signedBy: req.signedBy.trim(), reportHash: plan.hash,
    executedAt: ctx.now(), docs: plan.docs.map((d) => ({ kind: d.kind, id: d.id })),
    state: "live", backedOutAt: "",
  };
  await ctx.backend.cutovers().save(tenant, record);
  return { record, plan };
}

async function controlCode(ctx: CutoverContext, subtype: AccountSubtype): Promise<string> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const match = chart.listActive().filter((a) => a.subtype === subtype)
    .sort((a, b) => a.code.localeCompare(b.code))[0];
  return match?.code ?? "";
}

/** Why a backout can't happen now, or "" when it can. */
export async function backoutBlocker(ctx: CutoverContext): Promise<string> {
  const tenant = String(ctx.tenant);
  const record = await ctx.backend.cutovers().get(tenant);
  if (!record || record.state !== "live") return "there is no cutover to back out";
  const own = (await ctx.backend.store(ctx.tenant).list(ctx.tenant)).filter((e) => !isQuickBooksCopy(e));
  if (own.length) {
    return "RGNR8 has recorded its first entries since the cutover; backing out would lose them";
  }
  for (const d of record.docs) {
    const doc = await ctx.backend.documents().getDoc(tenant, d.kind, d.id);
    if (doc && doc.openMinor !== doc.totalMinor) {
      return `${d.kind} ${d.id} has had a payment recorded in RGNR8 since the cutover`;
    }
  }
  return "";
}

/** Undo a cutover that RGNR8 hasn't built on yet: QuickBooks is the book again. */
export async function backOutCutover(ctx: CutoverContext): Promise<CutoverRecord> {
  const tenant = String(ctx.tenant);
  const blocker = await backoutBlocker(ctx);
  if (blocker) throw new CutoverError(blocker);
  const record = (await ctx.backend.cutovers().get(tenant))!;
  for (const d of record.docs) await ctx.backend.documents().deleteDoc(tenant, d.kind, d.id);
  const settings = await ctx.backend.settings().get(tenant);
  await ctx.backend.settings().save(tenant, { ...settings, bookOfRecord: "quickbooks" });
  const backedOut: CutoverRecord = { ...record, state: "backed_out", backedOutAt: ctx.now() };
  await ctx.backend.cutovers().save(tenant, backedOut);
  return backedOut;
}

export function planJson(p: CutoverPlan): Record<string, unknown> {
  return {
    cutover_date: p.cutoverDate,
    open_items_as_of: p.openItemsAsOf,
    blockers: p.blockers,
    ready: p.blockers.length === 0,
    report_hash: p.hash,
    tie_out: p.tieOut.map((t) => ({
      control: t.control, books_minor: t.booksMinor.toString(),
      open_items_minor: t.openItemsMinor.toString(), difference_minor: t.differenceMinor.toString(),
    })),
    documents: p.docs.map((d) => ({
      kind: d.kind, id: d.id, party_id: d.partyId, party: d.partyName, date: d.date,
      due_date: d.dueDate, open_minor: d.openMinor.toString(), qbo_id: d.qboId,
    })),
  };
}

export function recordJson(r: CutoverRecord | undefined): Record<string, unknown> | null {
  if (!r) return null;
  return {
    cutover_date: r.cutoverDate, signed_by: r.signedBy, report_hash: r.reportHash,
    executed_at: r.executedAt, documents: r.docs, state: r.state, backed_out_at: r.backedOutAt,
  };
}
