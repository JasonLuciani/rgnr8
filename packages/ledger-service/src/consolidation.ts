import {
  Money,
  computeTrialBalance,
  asAccountId,
  type Currency,
  type TenantId,
  asTenantId,
} from "@rgnr8/ledger-kernel";
import {
  balanceSheet,
  cashFlow,
  consolidateTrialBalances,
  financialStatementsJson,
  fromKernelTrialBalance,
  incomeStatement,
  makeTrialBalance,
  subtypeCashFlowClassifier,
  type EliminationLine,
  type TrialBalance,
  type TrialBalanceEntry,
} from "@rgnr8/financial-statements";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";

/**
 * Multi-entity consolidation.
 *
 * An owner with three LLCs has three sets of books — because they are three
 * legal entities, and pretending otherwise is how a business ends up filing one
 * return for two companies. But they have one bank manager, and that person
 * wants a single set of statements.
 *
 * Consolidation is therefore a *report*, never a fourth set of books: combine
 * the entities account by account, eliminate what they owe each other, and hand
 * the result to the same statement builders every single-entity report uses.
 * Nothing is posted anywhere.
 *
 * ## Crossing the tenant boundary, deliberately
 *
 * Every other read in this service is scoped to one tenant, and the database
 * enforces it. This one reads several, which is exactly the thing the isolation
 * model exists to prevent — so it is allowed only through a **group the parent
 * tenant explicitly declares**, stored in the parent's own rows. A tenant can
 * only ever consolidate entities it has named. There is no route that
 * consolidates by search, by prefix, or by anything a caller can guess.
 *
 * ## Intercompany that does not agree
 *
 * The interesting failure is not the arithmetic; it is that entity A says it is
 * owed $412 more than entity B says it owes. Every real group has this, usually
 * because of a payment in transit at the period end.
 *
 * A consolidation that silently plugs the difference is a lie with a total at
 * the bottom. So the default is to refuse: the report comes back with the
 * mismatch, per account, and no consolidated column. Asked explicitly, it will
 * consolidate anyway and put the residual on a line called what it is — a
 * difference nobody has explained yet.
 */

export class ConsolidationServiceError extends Error {}

export interface GroupMember {
  readonly tenantId: string;
  readonly label: string;
  /** Share of the entity the group owns, in parts per million. Reported only. */
  readonly ownershipPpm: number;
  /** The currency this entity keeps its books in. Empty = the group base. */
  readonly currency?: string;
  /** Current (closing) FX rate to the group base, ×1e6 (e.g. EUR→USD 1.08 =
   *  "1080000"). Applies to monetary balance-sheet items (assets, liabilities).
   *  Empty when the entity already reports in the base currency. */
  readonly rateMicro?: string;
  /** Historical rate for equity accounts, ×1e6. Defaults to the current rate;
   *  when it differs, the gap is the cumulative translation adjustment. */
  readonly equityRateMicro?: string;
  /** Period-AVERAGE rate for the income statement (revenue, expense), ×1e6.
   *  ASC 830 / IAS 21 translate P&L at the average rate, not the closing rate.
   *  Defaults to the current rate (a one-rate group is unchanged). */
  readonly averageRateMicro?: string;
  /** Cumulative translation adjustment carried in from prior periods, in base
   *  minor units. The reported CTA rolls this forward: cumulative = prior +
   *  the current period's translation gap. Defaults to 0. */
  readonly priorCtaMinor?: string;
}

/** The FX rate scale: rates are stored as integers × 1e6. */
const RATE_MICRO = 1_000_000n;
/** Where the cumulative translation adjustment lands on the consolidated sheet. */
const CTA_CODE = "3990";
const CTA_NAME = "Cumulative translation adjustment";

export interface GroupRecord {
  readonly id: string;
  readonly name: string;
  readonly members: readonly GroupMember[];
  /** Accounts that hold balances between group entities. */
  readonly intercompanyCodes: readonly string[];
}

export interface EliminationEntryLine {
  readonly accountCode: string;
  readonly side: "DEBIT" | "CREDIT";
  readonly amountMinor: string;
}

export interface EliminationEntry {
  readonly id: string;
  readonly groupId: string;
  readonly description: string;
  readonly lines: readonly EliminationEntryLine[];
}

export interface ConsolidationStore {
  migrate(): Promise<void>;
  listGroups(tenant: string): Promise<GroupRecord[]>;
  getGroup(tenant: string, id: string): Promise<GroupRecord | undefined>;
  saveGroup(tenant: string, group: GroupRecord): Promise<void>;
  removeGroup(tenant: string, id: string): Promise<void>;
  listEliminations(tenant: string, groupId: string): Promise<EliminationEntry[]>;
  saveElimination(tenant: string, entry: EliminationEntry): Promise<void>;
  removeElimination(tenant: string, id: string): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryConsolidationStore implements ConsolidationStore {
  private readonly groups = new Map<string, GroupRecord>();
  private readonly eliminations = new Map<string, EliminationEntry>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listGroups(tenant: string): Promise<GroupRecord[]> {
    const out: GroupRecord[] = [];
    for (const [k, v] of this.groups) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.id.localeCompare(b.id)));
  }

  getGroup(tenant: string, id: string): Promise<GroupRecord | undefined> {
    return Promise.resolve(this.groups.get(`${tenant}::${id}`));
  }

  saveGroup(tenant: string, group: GroupRecord): Promise<void> {
    this.groups.set(`${tenant}::${group.id}`, group);
    return Promise.resolve();
  }

  removeGroup(tenant: string, id: string): Promise<void> {
    this.groups.delete(`${tenant}::${id}`);
    return Promise.resolve();
  }

  listEliminations(tenant: string, groupId: string): Promise<EliminationEntry[]> {
    const out: EliminationEntry[] = [];
    for (const [k, v] of this.eliminations) {
      if (k.startsWith(`${tenant}::`) && v.groupId === groupId) out.push(v);
    }
    return Promise.resolve(out.sort((a, b) => a.id.localeCompare(b.id)));
  }

  saveElimination(tenant: string, entry: EliminationEntry): Promise<void> {
    this.eliminations.set(`${tenant}::${entry.id}`, entry);
    return Promise.resolve();
  }

  removeElimination(tenant: string, id: string): Promise<void> {
    this.eliminations.delete(`${tenant}::${id}`);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const CONSOLIDATION_DDL = `
CREATE TABLE IF NOT EXISTS entity_group (
  tenant_id          text NOT NULL,
  id                 text NOT NULL,
  name               text NOT NULL,
  intercompany_codes text NOT NULL DEFAULT '',
  CONSTRAINT entity_group_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS entity_group_member (
  tenant_id        text NOT NULL,
  group_id         text NOT NULL,
  member_tenant_id text NOT NULL,
  label            text NOT NULL DEFAULT '',
  ownership_ppm    integer NOT NULL DEFAULT 1000000,
  currency         text NOT NULL DEFAULT '',
  rate_micro       text NOT NULL DEFAULT '',
  equity_rate_micro text NOT NULL DEFAULT '',
  average_rate_micro text NOT NULL DEFAULT '',
  prior_cta_minor  text NOT NULL DEFAULT '',
  CONSTRAINT entity_group_member_pk PRIMARY KEY (tenant_id, group_id, member_tenant_id)
);
-- Members written before multi-currency had no currency/rate columns.
ALTER TABLE entity_group_member ADD COLUMN IF NOT EXISTS currency          text NOT NULL DEFAULT '';
ALTER TABLE entity_group_member ADD COLUMN IF NOT EXISTS rate_micro        text NOT NULL DEFAULT '';
ALTER TABLE entity_group_member ADD COLUMN IF NOT EXISTS equity_rate_micro text NOT NULL DEFAULT '';
-- Members written before average-rate P&L / CTA roll-forward had neither.
ALTER TABLE entity_group_member ADD COLUMN IF NOT EXISTS average_rate_micro text NOT NULL DEFAULT '';
ALTER TABLE entity_group_member ADD COLUMN IF NOT EXISTS prior_cta_minor   text NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS elimination_entry (
  tenant_id   text NOT NULL,
  id          text NOT NULL,
  group_id    text NOT NULL,
  description text NOT NULL DEFAULT '',
  CONSTRAINT elimination_entry_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS elimination_line (
  tenant_id      text NOT NULL,
  elimination_id text NOT NULL,
  line_no        integer NOT NULL,
  account_code   text NOT NULL,
  side           text NOT NULL,
  amount_minor   text NOT NULL DEFAULT '0',
  CONSTRAINT elimination_line_pk PRIMARY KEY (tenant_id, elimination_id, line_no)
);
`;

export class PgConsolidationStore implements ConsolidationStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(CONSOLIDATION_DDL);
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

  private async membersOf(
    db: Queryable, tenant: string, groupId: string,
  ): Promise<GroupMember[]> {
    const res = await db.query(
      `SELECT member_tenant_id, label, ownership_ppm, currency, rate_micro, equity_rate_micro,
              average_rate_micro, prior_cta_minor
       FROM entity_group_member
       WHERE tenant_id=$1 AND group_id=$2 ORDER BY member_tenant_id`,
      [tenant, groupId],
    );
    return res.rows.map((r) => ({
      tenantId: String(r["member_tenant_id"]),
      label: String(r["label"] ?? ""),
      ownershipPpm: Number(r["ownership_ppm"] ?? 1_000_000),
      currency: String(r["currency"] ?? ""),
      rateMicro: String(r["rate_micro"] ?? ""),
      equityRateMicro: String(r["equity_rate_micro"] ?? ""),
      averageRateMicro: String(r["average_rate_micro"] ?? ""),
      priorCtaMinor: String(r["prior_cta_minor"] ?? ""),
    }));
  }

  async listGroups(tenant: string): Promise<GroupRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM entity_group WHERE tenant_id=$1 ORDER BY id", [tenant],
      );
      const out: GroupRecord[] = [];
      for (const row of res.rows) {
        out.push({
          id: String(row["id"]),
          name: String(row["name"]),
          intercompanyCodes: String(row["intercompany_codes"] ?? "")
            .split(",").map((c) => c.trim()).filter(Boolean),
          members: await this.membersOf(db, tenant, String(row["id"])),
        });
      }
      return out;
    });
  }

  async getGroup(tenant: string, id: string): Promise<GroupRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM entity_group WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      if (!row) return undefined;
      return {
        id: String(row["id"]),
        name: String(row["name"]),
        intercompanyCodes: String(row["intercompany_codes"] ?? "")
          .split(",").map((c) => c.trim()).filter(Boolean),
        members: await this.membersOf(db, tenant, id),
      };
    });
  }

  async saveGroup(tenant: string, group: GroupRecord): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO entity_group (tenant_id, id, name, intercompany_codes)
         VALUES ($1,$2,$3,$4)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           name=EXCLUDED.name, intercompany_codes=EXCLUDED.intercompany_codes`,
        [tenant, group.id, group.name, group.intercompanyCodes.join(",")],
      );
      await db.query(
        "DELETE FROM entity_group_member WHERE tenant_id=$1 AND group_id=$2",
        [tenant, group.id],
      );
      for (const m of group.members) {
        await db.query(
          `INSERT INTO entity_group_member (tenant_id, group_id, member_tenant_id,
             label, ownership_ppm, currency, rate_micro, equity_rate_micro,
             average_rate_micro, prior_cta_minor)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)`,
          [
            tenant, group.id, m.tenantId, m.label, m.ownershipPpm,
            m.currency ?? "", m.rateMicro ?? "", m.equityRateMicro ?? "",
            m.averageRateMicro ?? "", m.priorCtaMinor ?? "",
          ],
        );
      }
    });
  }

  async removeGroup(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM entity_group_member WHERE tenant_id=$1 AND group_id=$2", [tenant, id],
      );
      await db.query("DELETE FROM entity_group WHERE tenant_id=$1 AND id=$2", [tenant, id]);
    });
  }

  async listEliminations(tenant: string, groupId: string): Promise<EliminationEntry[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM elimination_entry WHERE tenant_id=$1 AND group_id=$2 ORDER BY id",
        [tenant, groupId],
      );
      const out: EliminationEntry[] = [];
      for (const row of res.rows) {
        const lines = await db.query(
          `SELECT account_code, side, amount_minor FROM elimination_line
           WHERE tenant_id=$1 AND elimination_id=$2 ORDER BY line_no`,
          [tenant, String(row["id"])],
        );
        out.push({
          id: String(row["id"]),
          groupId: String(row["group_id"]),
          description: String(row["description"] ?? ""),
          lines: lines.rows.map((l) => ({
            accountCode: String(l["account_code"]),
            side: String(l["side"]) as "DEBIT" | "CREDIT",
            amountMinor: String(l["amount_minor"] ?? "0"),
          })),
        });
      }
      return out;
    });
  }

  async saveElimination(tenant: string, entry: EliminationEntry): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        `INSERT INTO elimination_entry (tenant_id, id, group_id, description)
         VALUES ($1,$2,$3,$4)
         ON CONFLICT (tenant_id, id) DO UPDATE SET
           group_id=EXCLUDED.group_id, description=EXCLUDED.description`,
        [tenant, entry.id, entry.groupId, entry.description],
      );
      await db.query(
        "DELETE FROM elimination_line WHERE tenant_id=$1 AND elimination_id=$2",
        [tenant, entry.id],
      );
      let n = 0;
      for (const l of entry.lines) {
        await db.query(
          `INSERT INTO elimination_line (tenant_id, elimination_id, line_no,
             account_code, side, amount_minor) VALUES ($1,$2,$3,$4,$5,$6)`,
          [tenant, entry.id, n, l.accountCode, l.side, l.amountMinor],
        );
        n += 1;
      }
    });
  }

  async removeElimination(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM elimination_line WHERE tenant_id=$1 AND elimination_id=$2", [tenant, id],
      );
      await db.query("DELETE FROM elimination_entry WHERE tenant_id=$1 AND id=$2", [tenant, id]);
    });
  }
}

// --- the flow ----------------------------------------------------------------

export interface ConsolidationContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
}

export interface GroupInput {
  readonly id?: string;
  readonly name?: string;
  readonly intercompany_codes?: readonly string[] | string;
  readonly members?: ReadonlyArray<{
    readonly tenant_id?: string;
    readonly label?: string;
    readonly ownership_ppm?: number;
    /** The currency this entity keeps its books in (empty = the group base). */
    readonly currency?: string;
    /** Current (closing) FX rate to the group base as a decimal (e.g. "1.08"). */
    readonly rate?: string | number;
    /** Historical rate for equity (defaults to the current rate). */
    readonly equity_rate?: string | number;
    /** Period-average rate for the P&L (defaults to the current rate). */
    readonly average_rate?: string | number;
    /** Cumulative translation adjustment carried in from prior periods, in base
     *  minor units (e.g. "-1234" = a $12.34 debit CTA). Defaults to 0. */
    readonly prior_cta_minor?: string | number;
  }>;
}

/** Parse an FX rate ("1.08") into integer micro-units (1_080_000). Empty → "". */
function rateToMicro(raw: string | number | undefined, label: string): string {
  const text = String(raw ?? "").trim();
  if (!text) return "";
  if (!/^\d+(\.\d{1,6})?$/.test(text)) {
    throw new ConsolidationServiceError(`${label} must be a positive rate with up to 6 decimals`);
  }
  const [whole = "0", frac = ""] = text.split(".");
  const micro = BigInt(whole) * RATE_MICRO + BigInt(frac.padEnd(6, "0"));
  if (micro <= 0n) throw new ConsolidationServiceError(`${label} must be greater than zero`);
  return micro.toString();
}

/** Parse a signed integer minor amount ("-1234"). Empty → "". */
function ctaMinor(raw: string | number | undefined, label: string): string {
  const text = String(raw ?? "").trim();
  if (!text) return "";
  if (!/^-?\d+$/.test(text)) {
    throw new ConsolidationServiceError(`${label} must be an integer amount in minor units`);
  }
  return BigInt(text).toString();
}

export async function saveGroup(
  ctx: ConsolidationContext, input: GroupInput,
): Promise<GroupRecord> {
  const name = String(input.name ?? "").trim();
  if (!name) throw new ConsolidationServiceError("a group needs a name");
  const id = String(input.id ?? "").trim()
    || name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
  if (!id) throw new ConsolidationServiceError("a group needs an id");

  const members: GroupMember[] = [];
  const seen = new Set<string>();
  for (const m of input.members ?? []) {
    const tenantId = String(m.tenant_id ?? "").trim();
    if (!tenantId) throw new ConsolidationServiceError("every member needs a tenant");
    if (seen.has(tenantId)) {
      throw new ConsolidationServiceError(`${tenantId} is listed twice`);
    }
    seen.add(tenantId);
    const ownershipPpm = m.ownership_ppm === undefined ? 1_000_000 : Number(m.ownership_ppm);
    if (!Number.isInteger(ownershipPpm) || ownershipPpm < 0 || ownershipPpm > 1_000_000) {
      throw new ConsolidationServiceError("ownership must be between 0 and 100%");
    }
    const currency = String(m.currency ?? "").trim().toUpperCase();
    const rateMicro = rateToMicro(m.rate, `translation rate for ${tenantId}`);
    const equityRateMicro = rateToMicro(m.equity_rate, `equity rate for ${tenantId}`);
    const averageRateMicro = rateToMicro(m.average_rate, `average rate for ${tenantId}`);
    const priorCtaMinor = ctaMinor(m.prior_cta_minor, `prior CTA for ${tenantId}`);
    members.push({
      tenantId, label: String(m.label ?? "").trim() || tenantId, ownershipPpm,
      currency, rateMicro, equityRateMicro, averageRateMicro, priorCtaMinor,
    });
  }
  if (members.length < 2) {
    throw new ConsolidationServiceError(
      "a group needs at least two entities — one company consolidated with itself is just its books",
    );
  }

  const codes = typeof input.intercompany_codes === "string"
    ? input.intercompany_codes.split(",")
    : (input.intercompany_codes ?? []);

  const record: GroupRecord = {
    id,
    name,
    members,
    intercompanyCodes: codes.map((c) => String(c).trim()).filter(Boolean),
  };
  await ctx.backend.consolidation().saveGroup(String(ctx.tenant), record);
  return record;
}

export interface EliminationInput {
  readonly id?: string;
  readonly description?: string;
  readonly lines?: ReadonlyArray<{
    readonly account_code?: string;
    readonly side?: string;
    readonly amount_minor?: string | number;
  }>;
}

export async function saveElimination(
  ctx: ConsolidationContext, groupId: string, input: EliminationInput,
): Promise<EliminationEntry> {
  const store = ctx.backend.consolidation();
  const group = await store.getGroup(String(ctx.tenant), groupId);
  if (!group) throw new ConsolidationServiceError(`unknown group ${groupId}`);

  const lines: EliminationEntryLine[] = [];
  let debit = 0n;
  let credit = 0n;
  for (const l of input.lines ?? []) {
    const accountCode = String(l.account_code ?? "").trim();
    if (!accountCode) throw new ConsolidationServiceError("every line needs an account code");
    const side = String(l.side ?? "").toUpperCase();
    if (side !== "DEBIT" && side !== "CREDIT") {
      throw new ConsolidationServiceError(`line ${accountCode}: side must be DEBIT or CREDIT`);
    }
    const raw = typeof l.amount_minor === "number"
      ? String(l.amount_minor)
      : String(l.amount_minor ?? "").trim();
    if (!/^\d+$/.test(raw) || BigInt(raw) <= 0n) {
      throw new ConsolidationServiceError(`line ${accountCode}: amount must be positive`);
    }
    if (side === "DEBIT") debit += BigInt(raw);
    else credit += BigInt(raw);
    lines.push({ accountCode, side, amountMinor: raw });
  }
  if (lines.length === 0) {
    throw new ConsolidationServiceError("an elimination needs at least one line");
  }
  if (debit !== credit) {
    throw new ConsolidationServiceError(
      `this elimination doesn't balance — debits ${debit} vs credits ${credit}. An elimination that doesn't balance moves value into or out of the group, which is not what eliminating means`,
    );
  }

  const id = String(input.id ?? "").trim()
    || `${groupId}-E${(await store.listEliminations(String(ctx.tenant), groupId)).length + 1}`;
  const entry: EliminationEntry = {
    id, groupId, description: String(input.description ?? "").trim(), lines,
  };
  await store.saveElimination(String(ctx.tenant), entry);
  return entry;
}

interface EntityBalances {
  readonly member: GroupMember;
  readonly tb: TrialBalance;
  /** Present only when the entity was translated from another currency. */
  readonly translation?: EntityTranslation;
}

/** Round a signed value by an FX rate (×1e6), half-away-from-zero. */
function translateAmount(signedMinor: bigint, rateMicro: bigint): bigint {
  const neg = signedMinor < 0n;
  const abs = neg ? -signedMinor : signedMinor;
  const scaled = abs * rateMicro + RATE_MICRO / 2n;
  const rounded = scaled / RATE_MICRO;
  return neg ? -rounded : rounded;
}

/** Which rate class translated a line — the per-line audit trail. */
export type RateClass = "current" | "historical" | "average";

export interface LineRateAudit {
  readonly accountId: string;
  readonly code: string;
  readonly rateClass: RateClass;
  /** The rate applied, ×1e6. */
  readonly rateMicro: string;
}

/** The cumulative translation adjustment, decomposed for roll-forward. */
export interface CtaBreakdown {
  /** Total CTA carried on the translated sheet (base minor units). */
  readonly cumulativeMinor: string;
  /** CTA carried in from prior periods. */
  readonly priorMinor: string;
  /** This period's movement = cumulative − prior. */
  readonly currentPeriodMinor: string;
}

export interface EntityTranslation {
  readonly tb: TrialBalance;
  /** Per-line record of which rate class/value translated it. */
  readonly lines: readonly LineRateAudit[];
  readonly cta: CtaBreakdown;
}

export interface TranslationRatesMicro {
  /** Closing rate for monetary/BS items (assets, liabilities), ×1e6. */
  readonly currentMicro: bigint;
  /** Historical rate for equity, ×1e6. */
  readonly equityMicro: bigint;
  /** Period-average rate for revenue/expense, ×1e6. */
  readonly averageMicro: bigint;
  /** CTA carried in from prior periods (base minor units). */
  readonly priorCtaMinor: bigint;
}

/**
 * Translate a trial balance into a reporting currency by ASC 830 / IAS 21 rate
 * classes, with a per-line rate audit trail and a roll-forward CTA:
 *
 *  - assets & liabilities → CURRENT (closing) rate,
 *  - equity → HISTORICAL rate,
 *  - revenue & expense → period-AVERAGE rate (NOT the closing rate),
 *
 * The gap the differing rates open is the cumulative translation adjustment,
 * booked to a CTA equity line so the translated books foot exactly. That plug is
 * the *cumulative* CTA (the trial balance is an as-of cumulative one); the prior
 * period's CTA is carried in so the reported movement is `cumulative − prior`.
 * With a single rate (or none) every class collapses to one multiply, the CTA is
 * zero, and a same-currency entity is unchanged.
 */
export function translateTrialBalance(
  tb: TrialBalance,
  currency: Currency,
  entityId: string,
  rates: TranslationRatesMicro,
): EntityTranslation {
  const audit: LineRateAudit[] = [];
  const entries: TrialBalanceEntry[] = tb.entries.map((e) => {
    let rateMicro: bigint;
    let rateClass: RateClass;
    if (e.accountClass === "equity") {
      rateMicro = rates.equityMicro;
      rateClass = "historical";
    } else if (e.accountClass === "revenue" || e.accountClass === "expense") {
      rateMicro = rates.averageMicro;
      rateClass = "average";
    } else {
      rateMicro = rates.currentMicro;
      rateClass = "current";
    }
    audit.push({ accountId: String(e.accountId), code: e.code, rateClass, rateMicro: rateMicro.toString() });
    return { ...e, signed: Money.fromMinorUnits(translateAmount(e.signed.minorUnits, rateMicro), currency) };
  });

  const residual = entries.reduce((acc, e) => acc + e.signed.minorUnits, 0n);
  const cumulativeCta = -residual; // the plug that makes the translated books foot
  if (residual !== 0n) {
    entries.push({
      accountId: asAccountId(`__cta__:${entityId}`),
      code: CTA_CODE,
      name: CTA_NAME,
      accountClass: "equity",
      signed: Money.fromMinorUnits(cumulativeCta, currency),
    });
  }
  const prior = rates.priorCtaMinor;
  return {
    tb: makeTrialBalance(currency, entries),
    lines: audit,
    cta: {
      cumulativeMinor: cumulativeCta.toString(),
      priorMinor: prior.toString(),
      currentPeriodMinor: (cumulativeCta - prior).toString(),
    },
  };
}

/** Translate one group member's trial balance, resolving its configured rates. */
function translateEntity(tb: TrialBalance, member: GroupMember, currency: Currency): EntityTranslation {
  const currentMicro = BigInt(member.rateMicro || RATE_MICRO.toString());
  const equityMicro = BigInt(member.equityRateMicro || member.rateMicro || RATE_MICRO.toString());
  const averageMicro = BigInt(member.averageRateMicro || member.rateMicro || RATE_MICRO.toString());
  const priorCtaMinor = BigInt(member.priorCtaMinor || "0");
  return translateTrialBalance(tb, currency, member.tenantId, {
    currentMicro,
    equityMicro,
    averageMicro,
    priorCtaMinor,
  });
}

async function entityBalances(
  ctx: ConsolidationContext, group: GroupRecord, window: { from?: string; to?: string },
): Promise<EntityBalances[]> {
  const settings = await ctx.backend.settings().get(String(ctx.tenant));
  const base = settings.baseCurrency || ctx.currency.code;
  const out: EntityBalances[] = [];
  for (const member of group.members) {
    const tenant = asTenantId(member.tenantId);
    const chart = await ctx.backend.chart(tenant);
    if (chart.list().length === 0) {
      throw new ConsolidationServiceError(
        `${member.tenantId} has no chart of accounts — it is not a set of books yet`,
      );
    }
    const kernel = await computeTrialBalance(
      ctx.backend.store(tenant), tenant, chart, ctx.currency, window,
    );
    const tb = fromKernelTrialBalance(kernel);
    const memberCurrency = (member.currency ?? "").trim().toUpperCase();
    if (memberCurrency && memberCurrency !== base) {
      // A member keeps its books in another currency: it must be translated, and
      // that requires the option to be on and a rate to be configured — never a
      // silent combination of mismatched currencies.
      if (!settings.multiCurrencyEnabled) {
        throw new ConsolidationServiceError(
          `${member.tenantId} reports in ${memberCurrency} but multi-currency consolidation `
          + "is off — enable it in settings to translate to the group base",
        );
      }
      if (!member.rateMicro) {
        throw new ConsolidationServiceError(
          `${member.tenantId} reports in ${memberCurrency} but no translation rate to ${base} `
          + "is configured for it",
        );
      }
      const translation = translateEntity(tb, member, ctx.currency);
      out.push({ member, tb: translation.tb, translation });
    } else {
      out.push({ member, tb });
    }
  }
  return out;
}

export interface ConsolidationInput {
  readonly through?: string;
  readonly from?: string;
  /** Consolidate even though intercompany balances don't agree. */
  readonly allow_mismatch?: boolean;
}

/**
 * The consolidation worksheet: every entity in a column, the combined total,
 * the eliminations, and the consolidated result — which is the form an
 * accountant will ask to see, because it is the only one where the eliminations
 * are visible rather than assumed.
 */
export async function consolidate(
  ctx: ConsolidationContext, groupId: string, input: ConsolidationInput = {},
): Promise<Record<string, unknown>> {
  const store = ctx.backend.consolidation();
  const group = await store.getGroup(String(ctx.tenant), groupId);
  if (!group) throw new ConsolidationServiceError(`unknown group ${groupId}`);

  const through = String(input.through ?? "").trim();
  const from = String(input.from ?? "").trim();
  for (const [value, label] of [[through, "through"], [from, "from"]] as const) {
    if (value && !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      throw new ConsolidationServiceError(`${label} must be YYYY-MM-DD`);
    }
  }
  const window = {
    ...(from ? { from } : {}),
    ...(through ? { to: through } : {}),
  };

  const entities = await entityBalances(ctx, group, window);

  // Intercompany: what each entity says, and whether they agree.
  const intercompany = new Set(group.intercompanyCodes);
  const intercompanyRows: Record<string, unknown>[] = [];
  let intercompanyNet = 0n;
  if (intercompany.size > 0) {
    for (const code of group.intercompanyCodes) {
      const byEntity: Record<string, string> = {};
      let net = 0n;
      for (const e of entities) {
        const entry = e.tb.entries.find((x) => x.code === code);
        const signed = entry?.signed.minorUnits ?? 0n;
        byEntity[e.member.tenantId] = signed.toString();
        net += signed;
      }
      intercompanyNet += net;
      intercompanyRows.push({ account_code: code, by_entity: byEntity, net_minor: net.toString() });
    }
  }

  const manual = await store.listEliminations(String(ctx.tenant), groupId);
  const eliminations: EliminationLine[] = [];
  const eliminationRows: Record<string, unknown>[] = [];

  // Look every account up once, from whichever entity has it.
  const accountOf = new Map<string, { accountId: string; name: string; accountClass: string }>();
  for (const e of entities) {
    for (const entry of e.tb.entries) {
      if (!accountOf.has(entry.code)) {
        accountOf.set(entry.code, {
          accountId: String(entry.accountId),
          name: entry.name,
          accountClass: entry.accountClass,
        });
      }
    }
  }

  const push = (code: string, signedMinor: bigint, why: string): void => {
    if (signedMinor === 0n) return;
    const account = accountOf.get(code);
    if (!account) {
      throw new ConsolidationServiceError(`no entity in this group has account ${code}`);
    }
    eliminations.push({
      accountId: account.accountId as EliminationLine["accountId"],
      code,
      name: account.name,
      accountClass: account.accountClass as EliminationLine["accountClass"],
      signed: Money.fromMinorUnits(signedMinor, ctx.currency),
    });
    eliminationRows.push({
      account_code: code, signed_minor: signedMinor.toString(), reason: why,
    });
  };

  // Automatic: take every intercompany account to zero.
  for (const row of intercompanyRows) {
    const net = BigInt(String(row["net_minor"]));
    for (const e of entities) {
      const entry = e.tb.entries.find((x) => x.code === String(row["account_code"]));
      if (!entry || entry.signed.isZero()) continue;
      push(entry.code, -entry.signed.minorUnits, "intercompany");
    }
    void net;
  }
  // Manual eliminations on top.
  for (const entry of manual) {
    for (const line of entry.lines) {
      const signed = line.side === "DEBIT" ? BigInt(line.amountMinor) : -BigInt(line.amountMinor);
      push(line.accountCode, signed, entry.description || entry.id);
    }
  }

  // The residual: what one entity claims and the other does not agree with.
  const mismatch = -intercompanyNet;
  const balancedEliminations = eliminations.reduce(
    (acc, e) => acc + e.signed.minorUnits, 0n,
  );
  if (balancedEliminations !== 0n && input.allow_mismatch !== true) {
    return {
      contract: "consolidation/1",
      currency: ctx.currency.code,
      group: groupJson(group),
      through,
      consolidated: null,
      intercompany: intercompanyRows,
      mismatch_minor: (-balancedEliminations).toString(),
      refused:
        "intercompany balances don't agree, so a consolidated total would be a plug. "
        + "Find the difference — it is usually a payment in transit at the period end — "
        + "or ask for it anyway and it will be shown on its own line.",
      entities: entities.map((e) => ({
        tenant_id: e.member.tenantId, label: e.member.label,
      })),
    };
  }

  // Asked for anyway: name the residual rather than hiding it in an account.
  let residualRow: Record<string, unknown> | null = null;
  if (balancedEliminations !== 0n) {
    const code = group.intercompanyCodes[0] ?? "";
    const account = accountOf.get(code);
    if (!account) {
      throw new ConsolidationServiceError(
        "there is a difference to show but no intercompany account to show it against",
      );
    }
    eliminations.push({
      accountId: account.accountId as EliminationLine["accountId"],
      code,
      name: "Unexplained intercompany difference",
      accountClass: account.accountClass as EliminationLine["accountClass"],
      signed: Money.fromMinorUnits(-balancedEliminations, ctx.currency),
    });
    residualRow = {
      account_code: code,
      signed_minor: (-balancedEliminations).toString(),
      reason: "unexplained intercompany difference",
    };
    eliminationRows.push(residualRow);
  }

  const result = consolidateTrialBalances(
    entities.map((e) => ({ entityId: e.member.tenantId, tb: e.tb })),
    eliminations,
  );

  // The worksheet: one row per account, one column per entity.
  const codes = new Set<string>();
  for (const e of entities) for (const entry of e.tb.entries) codes.add(entry.code);
  for (const entry of result.consolidated.entries) codes.add(entry.code);

  const rows = [...codes].sort().map((code) => {
    const byEntity: Record<string, string> = {};
    let combined = 0n;
    for (const e of entities) {
      const entry = e.tb.entries.find((x) => x.code === code);
      const signed = entry?.signed.minorUnits ?? 0n;
      byEntity[e.member.tenantId] = signed.toString();
      combined += signed;
    }
    const elimination = eliminations
      .filter((x) => x.code === code)
      .reduce((acc, x) => acc + x.signed.minorUnits, 0n);
    const consolidated = result.consolidated.entries.find((x) => x.code === code);
    return {
      account_code: code,
      name: accountOf.get(code)?.name ?? code,
      by_entity: byEntity,
      combined_minor: combined.toString(),
      elimination_minor: elimination.toString(),
      consolidated_minor: (consolidated?.signed.minorUnits ?? 0n).toString(),
    };
  });

  return {
    contract: "consolidation/1",
    currency: ctx.currency.code,
    group: groupJson(group),
    through,
    entities: entities.map((e) => ({
      tenant_id: e.member.tenantId,
      label: e.member.label,
      ownership_ppm: e.member.ownershipPpm,
    })),
    rows,
    intercompany: intercompanyRows,
    eliminations: eliminationRows,
    // FX translation audit: for every entity translated from another currency,
    // the CTA roll-forward and the per-line rate used (current/historical/average).
    translation: entities
      .filter((e) => e.translation !== undefined)
      .map((e) => ({
        tenant_id: e.member.tenantId,
        currency: (e.member.currency ?? "").toUpperCase(),
        cta: {
          cumulative_minor: e.translation!.cta.cumulativeMinor,
          prior_minor: e.translation!.cta.priorMinor,
          current_period_minor: e.translation!.cta.currentPeriodMinor,
        },
        rates: e.translation!.lines.map((l) => ({
          account_code: l.code,
          rate_class: l.rateClass,
          rate_micro: l.rateMicro,
        })),
      })),
    mismatch_minor: residualRow ? String(residualRow["signed_minor"]) : "0",
    balanced: result.balanced,
    consolidated: {
      rows: result.consolidated.entries.map((e) => ({
        account_code: e.code,
        name: e.name,
        signed_minor: e.signed.minorUnits.toString(),
      })),
    },
    ...(mismatch === 0n ? {} : {}),
  };
}

/** Consolidated financial statements, built from the consolidated balances. */
export async function consolidatedStatements(
  ctx: ConsolidationContext, groupId: string, from: string, to: string,
): Promise<Record<string, unknown>> {
  const store = ctx.backend.consolidation();
  const group = await store.getGroup(String(ctx.tenant), groupId);
  if (!group) throw new ConsolidationServiceError(`unknown group ${groupId}`);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(from) || !/^\d{4}-\d{2}-\d{2}$/.test(to)) {
    throw new ConsolidationServiceError("statements need from and to as YYYY-MM-DD");
  }

  const manual = await store.listEliminations(String(ctx.tenant), groupId);
  const build = async (window: { from?: string; to?: string }): Promise<TrialBalance> => {
    const entities = await entityBalances(ctx, group, window);
    const accountOf = new Map<string, EliminationLine>();
    for (const e of entities) {
      for (const entry of e.tb.entries) if (!accountOf.has(entry.code)) accountOf.set(entry.code, entry);
    }
    const eliminations: EliminationLine[] = [];
    for (const code of group.intercompanyCodes) {
      for (const e of entities) {
        const entry = e.tb.entries.find((x) => x.code === code);
        if (!entry || entry.signed.isZero()) continue;
        eliminations.push({ ...entry, signed: entry.signed.negate() });
      }
    }
    for (const entry of manual) {
      for (const line of entry.lines) {
        const template = accountOf.get(line.accountCode);
        if (!template) continue;
        const signed = line.side === "DEBIT"
          ? BigInt(line.amountMinor)
          : -BigInt(line.amountMinor);
        eliminations.push({ ...template, signed: Money.fromMinorUnits(signed, ctx.currency) });
      }
    }
    const net = eliminations.reduce((acc, e) => acc + e.signed.minorUnits, 0n);
    if (net !== 0n) {
      throw new ConsolidationServiceError(
        `intercompany balances don't agree by ${-net} — the group's statements would be a plug. Reconcile the entities first`,
      );
    }
    return consolidateTrialBalances(
      entities.map((e) => ({ entityId: e.member.tenantId, tb: e.tb })),
      eliminations,
    ).consolidated;
  };

  const period = await build({ from, to });
  const end = await build({ to });
  const start = await build({ to: previousDay(from) });

  const chart = await ctx.backend.chart(asTenantId(group.members[0]!.tenantId));
  const income = incomeStatement(period);
  // Consolidated equity as-of `to` carries the group's prior retained earnings,
  // or a mid-year consolidated balance sheet is out of balance by them.
  const beginningRetained = incomeStatement(start).netIncome;
  const bs = balanceSheet(end, income.netIncome, beginningRetained);
  const cf = cashFlow(start, end, income.netIncome, subtypeCashFlowClassifier(chart));

  return {
    group: groupJson(group),
    ...financialStatementsJson({
      period: `${from}..${to}`,
      currency: ctx.currency.code,
      income,
      balanceSheet: bs,
      cashFlow: cf,
    }),
  };
}

/** Day before an ISO date, computed purely. */
function previousDay(iso: string): string {
  const y = Number(iso.slice(0, 4));
  const m = Number(iso.slice(5, 7));
  const d = Number(iso.slice(8, 10));
  const leap = (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  let yy = y;
  let mm = m;
  let dd = d - 1;
  if (dd < 1) {
    mm -= 1;
    if (mm < 1) {
      mm = 12;
      yy -= 1;
    }
    dd = days[mm - 1]!;
  }
  const p2 = (n: number): string => (n < 10 ? `0${n}` : String(n));
  return `${yy}-${p2(mm)}-${p2(dd)}`;
}

export function groupJson(g: GroupRecord): Record<string, unknown> {
  return {
    id: g.id,
    name: g.name,
    intercompany_codes: g.intercompanyCodes,
    members: g.members.map((m) => ({
      tenant_id: m.tenantId, label: m.label, ownership_ppm: m.ownershipPpm,
      ...(m.currency ? { currency: m.currency } : {}),
      ...(m.rateMicro ? { rate_micro: m.rateMicro } : {}),
      ...(m.equityRateMicro ? { equity_rate_micro: m.equityRateMicro } : {}),
      ...(m.averageRateMicro ? { average_rate_micro: m.averageRateMicro } : {}),
      ...(m.priorCtaMinor ? { prior_cta_minor: m.priorCtaMinor } : {}),
    })),
  };
}

export function eliminationJson(e: EliminationEntry): Record<string, unknown> {
  return {
    id: e.id,
    group_id: e.groupId,
    description: e.description,
    lines: e.lines.map((l) => ({
      account_code: l.accountCode, side: l.side, amount_minor: l.amountMinor,
    })),
  };
}
