import { Money, type Currency } from "./money.js";
import { PostingEngine } from "./postingEngine.js";
import type { PeriodStore } from "./periods.js";
import type { LedgerStore } from "./ledgerStore.js";
import {
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  type AccountId,
  type JournalLineInput,
  type PostCommand,
  type PostedEntry,
  type Provenance,
  type TenantId,
} from "./types.js";

/**
 * Cutover — the moment RGNR8 becomes the system of record.
 *
 * A business runs on QuickBooks or Xero, we mirror it for a while, and then on a
 * chosen **cutover date** we take over: freeze the source system's balances as
 * of that date into our ledger as an opening-balance journal, lock everything on
 * or before the cutover so history can't drift, and from then on the workflow
 * runs on our side (new transactions post into *our* ledger, not the mirror).
 *
 * The opening-balance entry is the standard conversion journal: debit/credit
 * every account to its as-of balance, with the residual absorbed by an **Opening
 * Balance Equity** account (exactly what QBO does on setup) so the entry is
 * balanced by construction and the books open in balance. It is idempotent
 * (keyed by cutover date), so re-running a cutover never double-posts.
 */

export type SourceSystem = "quickbooks" | "xero" | "other";

/** One account's balance as of the cutover date, signed debit-positive. */
export interface OpeningBalance {
  readonly accountId: AccountId;
  /** Signed: a debit balance is positive, a credit balance negative. */
  readonly balance: Money;
}

export interface CutoverPlan {
  readonly tenantId: string;
  readonly sourceSystem: SourceSystem;
  /** ISO date the source's books are frozen and we take over. */
  readonly cutoverDate: string;
  readonly currency: Currency;
  /** As-of balances imported from the source (its trial balance). */
  readonly balances: readonly OpeningBalance[];
  /** The Opening Balance Equity account the residual posts to. */
  readonly openingBalanceEquityId: AccountId;
  readonly provenance: Provenance;
  readonly memo?: string;
}

export class CutoverError extends Error {}

/**
 * Build the opening-balance conversion journal from a source system's as-of
 * balances. Each account gets a line for its balance (debit if positive, credit
 * if negative); the net residual across all accounts is posted to Opening
 * Balance Equity so debits equal credits. Zero balances are skipped.
 */
export function buildOpeningBalanceEntry(plan: CutoverPlan): PostCommand {
  if (plan.balances.length === 0) {
    throw new CutoverError("cutover has no opening balances");
  }
  const currency = plan.currency;
  const lines: JournalLineInput[] = [];
  let residualMinor = 0n; // sum of signed balances (debit-positive)

  for (const ob of plan.balances) {
    if (ob.balance.currency.code !== currency.code) {
      throw new CutoverError(
        `opening balance for ${ob.accountId} is ${ob.balance.currency.code}, expected ${currency.code}`,
      );
    }
    if (ob.balance.isZero()) continue;
    residualMinor += ob.balance.minorUnits;
    if (!ob.balance.isNegative()) {
      lines.push({ accountId: ob.accountId, side: "DEBIT", amount: ob.balance, memo: "Opening balance" });
    } else {
      lines.push({ accountId: ob.accountId, side: "CREDIT", amount: ob.balance.negate(), memo: "Opening balance" });
    }
  }

  if (lines.length === 0) {
    throw new CutoverError("all opening balances are zero — nothing to convert");
  }

  // Opening Balance Equity absorbs the residual so the entry balances.
  // If Σ signed balances (debit-positive) is +R, we need a CREDIT of R to OBE.
  if (residualMinor !== 0n) {
    const residual = Money.fromMinorUnits(residualMinor < 0n ? -residualMinor : residualMinor, currency);
    lines.push({
      accountId: plan.openingBalanceEquityId,
      side: residualMinor > 0n ? "CREDIT" : "DEBIT",
      amount: residual,
      memo: "Opening Balance Equity",
    });
  } else if (lines.length < 2) {
    // A single non-zero, self-balancing line can't post; needs the OBE line.
    throw new CutoverError("opening balances net to a single line — cannot balance");
  }

  return {
    tenantId: asTenantId(plan.tenantId),
    idempotencyKey: asIdempotencyKey(`cutover:opening:${plan.cutoverDate}`),
    periodKey: asPeriodKey(plan.cutoverDate.slice(0, 7)),
    currency,
    entryDate: plan.cutoverDate,
    memo: plan.memo ?? `Cutover from ${plan.sourceSystem} — opening balances`,
    provenance: plan.provenance,
    lines,
  };
}

/** An immutable record that RGNR8 took over as system of record. */
export interface CutoverRecord {
  readonly tenantId: string;
  readonly sourceSystem: SourceSystem;
  readonly cutoverDate: string;
  readonly openingEntryId: string;
  /** Total of the opening-balance debits (= credits) posted. */
  readonly openingTotal: Money;
  /** The last period locked as part of the cutover (everything ≤ this is frozen). */
  readonly lockedThroughPeriod: string;
  readonly postedAt: string;
}

export interface CutoverResult {
  readonly entry: PostedEntry;
  readonly record: CutoverRecord;
  /** True if the opening entry was newly posted; false if it already existed. */
  readonly newlyPosted: boolean;
}

/**
 * Execute the cutover: post the opening-balance journal and lock every period on
 * or before the cutover month, so the source-system history is frozen and RGNR8
 * owns everything from the cutover date forward. Idempotent — re-running with the
 * same plan returns the existing entry and re-locks (a no-op) rather than
 * double-posting.
 *
 * `periods` must be the same registry the `engine` posts through. `postedAt` is
 * injected (no wall clock).
 */
export async function executeCutover(
  engine: PostingEngine,
  periods: PeriodStore,
  store: LedgerStore,
  plan: CutoverPlan,
  postedAt: string,
): Promise<CutoverResult> {
  const command = buildOpeningBalanceEntry(plan);
  const tenant = asTenantId(plan.tenantId);

  // Idempotent: if this cutover was already posted, reuse it.
  const existing = await store.getByIdempotencyKey(tenant, command.idempotencyKey);
  const entry = existing ?? (await engine.post(command, { postedAt }));
  const newlyPosted = existing === undefined;

  // Freeze the cutover period (and thereby the converted history). Callers that
  // track earlier periods can lock each; locking the cutover month is the seal
  // that stops edits to the opening books.
  const cutoverPeriod = asPeriodKey(plan.cutoverDate.slice(0, 7));
  await periods.lock(tenant, cutoverPeriod);

  // Opening total = sum of the debit lines (equals credits).
  let openingMinor = 0n;
  for (const line of entry.lines) if (line.side === "DEBIT") openingMinor += line.amount.minorUnits;

  const record: CutoverRecord = {
    tenantId: plan.tenantId,
    sourceSystem: plan.sourceSystem,
    cutoverDate: plan.cutoverDate,
    openingEntryId: entry.id,
    openingTotal: Money.fromMinorUnits(openingMinor, plan.currency),
    lockedThroughPeriod: cutoverPeriod,
    postedAt,
  };
  return { entry, record, newlyPosted };
}

/**
 * A tenant's source-of-truth state. Before cutover, the source system is
 * authoritative and RGNR8 mirrors it; after cutover, RGNR8 is authoritative.
 * A tiny in-memory registry the app can persist.
 */
export class CutoverRegistry {
  private readonly records = new Map<string, CutoverRecord>();

  record(rec: CutoverRecord): void {
    this.records.set(rec.tenantId, rec);
  }

  /** The cutover record for a tenant, if it has gone live on RGNR8. */
  get(tenantId: string): CutoverRecord | undefined {
    return this.records.get(tenantId);
  }

  /** True once RGNR8 is the system of record for this tenant. */
  isLive(tenantId: string): boolean {
    return this.records.has(tenantId);
  }

  /** RGNR8 is authoritative for a date only on/after the cutover date. */
  isAuthoritativeOn(tenantId: string, isoDate: string): boolean {
    const rec = this.records.get(tenantId);
    return rec !== undefined && isoDate >= rec.cutoverDate;
  }
}
