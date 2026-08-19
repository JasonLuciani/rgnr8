import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  type Currency,
  type Provenance,
  type PostCommand,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";

/**
 * Fixed assets and depreciation — the register QuickBooks reserves for its top
 * tier and Xero bolts on, done here as part of the ledger.
 *
 * An asset is bought once and expensed over its life. The register holds what it
 * cost, how it is being written down, and how much has been taken so far; a
 * depreciation run posts the *delta* between what should have accumulated by a
 * date and what already has — so re-running a month, or catching up several,
 * never double-counts (the same delta-posting discipline the WIP schedule uses).
 *
 * Three methods, all integer-exact:
 *  - STRAIGHT_LINE — equal amounts over the life; the depreciable base
 *    (cost − salvage) spread across the months, with the final month truing up
 *    so accumulated lands exactly on the base.
 *  - DECLINING_BALANCE — a fixed rate on the *reducing* book value (double-
 *    declining when the factor is 2×), never taking book value below salvage.
 *  - UNITS_OF_PRODUCTION — a rate per unit of output; depreciation follows use,
 *    posted as usage is recorded rather than by the calendar.
 */

export class FixedAssetError extends Error {}

export type DepreciationMethod =
  | "STRAIGHT_LINE"
  | "DECLINING_BALANCE"
  | "UNITS_OF_PRODUCTION";

const METHODS: readonly DepreciationMethod[] = [
  "STRAIGHT_LINE", "DECLINING_BALANCE", "UNITS_OF_PRODUCTION",
];

export interface AssetRecord {
  readonly id: string;
  readonly name: string;
  readonly category: string;
  readonly inServiceDate: string;
  readonly costMinor: string;
  readonly salvageMinor: string;
  readonly method: DepreciationMethod;
  /** Life in months (straight-line, declining-balance). */
  readonly usefulLifeMonths: number;
  /** Declining-balance factor × 1e6 (2× = 2000000). */
  readonly decliningFactorMicro: string;
  /** Total expected output units, for units-of-production. */
  readonly totalUnitsMilli: string;
  /** Output consumed so far, for units-of-production. */
  readonly unitsUsedMilli: string;
  /** Depreciation taken to date, minor units. */
  readonly accumulatedMinor: string;
  readonly assetAccountCode: string;
  readonly accumAccountCode: string;
  readonly expenseAccountCode: string;
  readonly status: "ACTIVE" | "DISPOSED";
  readonly disposedDate: string;
  readonly memo: string;
}

export interface DepreciationRow {
  readonly period: number;
  readonly throughDate: string;
  readonly expenseMinor: string;
  readonly accumulatedMinor: string;
  readonly bookValueMinor: string;
}

// --- the arithmetic (pure) ---------------------------------------------------

const MICRO = 1_000_000n;

function roundDiv(num: bigint, den: bigint): bigint {
  if (den === 0n) return 0n;
  const neg = (num < 0n) !== (den < 0n);
  const a = num < 0n ? -num : num;
  const b = den < 0n ? -den : den;
  const q = (a + b / 2n) / b;
  return neg ? -q : q;
}

/** Depreciable base — what will be written off over the asset's life. */
export function depreciableBase(asset: AssetRecord): bigint {
  const base = BigInt(asset.costMinor) - BigInt(asset.salvageMinor);
  return base > 0n ? base : 0n;
}

/**
 * The straight-line / declining-balance schedule, month by month. Each row's
 * `accumulatedMinor` is the total that should have been taken by that month, so
 * a depreciation run can post the difference from what it has already booked.
 */
export function depreciationSchedule(asset: AssetRecord): DepreciationRow[] {
  if (asset.method === "UNITS_OF_PRODUCTION") return []; // driven by usage, not months
  const life = asset.usefulLifeMonths;
  if (life <= 0) return [];
  const base = depreciableBase(asset);
  const cost = BigInt(asset.costMinor);
  const salvage = BigInt(asset.salvageMinor);
  const rows: DepreciationRow[] = [];
  let accumulated = 0n;

  if (asset.method === "STRAIGHT_LINE") {
    const per = base / BigInt(life); // even portion; remainder trued up at the end
    for (let m = 1; m <= life; m++) {
      let expense = per;
      if (m === life) expense = base - accumulated; // final month lands on the base exactly
      accumulated += expense;
      rows.push(row(asset, m, expense, accumulated, cost));
    }
    return rows;
  }

  // DECLINING_BALANCE: rate on reducing book value, floored at salvage. The rate
  // is factor/life; the expense is computed in a single division (book × factor
  // ÷ (life)) so the rate isn't truncated before it's applied.
  const factor = BigInt(asset.decliningFactorMicro) > 0n ? BigInt(asset.decliningFactorMicro) : 2n * MICRO;
  const lifeN = BigInt(life);
  let book = cost;
  for (let m = 1; m <= life; m++) {
    let expense = roundDiv(book * factor, MICRO * lifeN);
    // never depreciate below salvage
    if (book - expense < salvage) expense = book - salvage;
    if (expense < 0n) expense = 0n;
    // last month: sweep any remaining depreciable value so we land on salvage
    if (m === life) expense = book - salvage > 0n ? book - salvage : 0n;
    accumulated += expense;
    book -= expense;
    rows.push(row(asset, m, expense, accumulated, cost));
    if (book <= salvage) break;
  }
  return rows;
}

function row(asset: AssetRecord, m: number, expense: bigint, accumulated: bigint, cost: bigint): DepreciationRow {
  return {
    period: m,
    throughDate: addMonths(asset.inServiceDate, m),
    expenseMinor: expense.toString(),
    accumulatedMinor: accumulated.toString(),
    bookValueMinor: (cost - accumulated).toString(),
  };
}

/** Accumulated depreciation that should stand by `throughDate` (calendar methods). */
export function targetAccumulated(asset: AssetRecord, throughDate: string): bigint {
  const schedule = depreciationSchedule(asset);
  if (schedule.length === 0) return BigInt(asset.accumulatedMinor);
  let target = 0n;
  for (const r of schedule) {
    if (r.throughDate <= throughDate) target = BigInt(r.accumulatedMinor);
    else break;
  }
  return target;
}

// --- date math (UTC, pure) ---------------------------------------------------

function addMonths(startDate: string, months: number): string {
  const [y, m, d] = startDate.split("-").map((p) => Number.parseInt(p, 10));
  if (!y || !m || !d) throw new FixedAssetError(`date must be YYYY-MM-DD, got ${startDate}`);
  const total = (y * 12 + (m - 1)) + months;
  const ny = Math.floor(total / 12);
  const nm = total % 12;
  const lastDay = new Date(Date.UTC(ny, nm + 1, 0)).getUTCDate();
  return new Date(Date.UTC(ny, nm, Math.min(d, lastDay))).toISOString().slice(0, 10);
}

// --- store seam --------------------------------------------------------------

export interface FixedAssetStore {
  migrate(): Promise<void>;
  listAssets(tenant: string): Promise<AssetRecord[]>;
  getAsset(tenant: string, id: string): Promise<AssetRecord | undefined>;
  saveAsset(tenant: string, asset: AssetRecord): Promise<void>;
  /** Has a depreciation entry for this asset+period already posted? */
  postedPeriod(tenant: string, assetId: string, period: string): Promise<boolean>;
  markPeriod(tenant: string, assetId: string, period: string, entryId: string): Promise<void>;
}

export class InMemoryFixedAssetStore implements FixedAssetStore {
  private readonly assets = new Map<string, AssetRecord>();
  private readonly periods = new Map<string, string>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listAssets(tenant: string): Promise<AssetRecord[]> {
    const out: AssetRecord[] = [];
    for (const [k, v] of this.assets) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.id.localeCompare(b.id)));
  }

  getAsset(tenant: string, id: string): Promise<AssetRecord | undefined> {
    return Promise.resolve(this.assets.get(`${tenant}::${id}`));
  }

  saveAsset(tenant: string, asset: AssetRecord): Promise<void> {
    this.assets.set(`${tenant}::${asset.id}`, asset);
    return Promise.resolve();
  }

  postedPeriod(tenant: string, assetId: string, period: string): Promise<boolean> {
    return Promise.resolve(this.periods.has(`${tenant}::${assetId}::${period}`));
  }

  markPeriod(tenant: string, assetId: string, period: string, entryId: string): Promise<void> {
    this.periods.set(`${tenant}::${assetId}::${period}`, entryId);
    return Promise.resolve();
  }
}

export const FIXED_ASSET_DDL = `
CREATE TABLE IF NOT EXISTS fixed_asset (
  tenant_id             text NOT NULL,
  id                    text NOT NULL,
  name                  text NOT NULL,
  category              text NOT NULL DEFAULT '',
  in_service_date       text NOT NULL,
  cost_minor            text NOT NULL DEFAULT '0',
  salvage_minor         text NOT NULL DEFAULT '0',
  method                text NOT NULL DEFAULT 'STRAIGHT_LINE',
  useful_life_months    integer NOT NULL DEFAULT 0,
  declining_factor_micro text NOT NULL DEFAULT '2000000',
  total_units_milli     text NOT NULL DEFAULT '0',
  units_used_milli      text NOT NULL DEFAULT '0',
  accumulated_minor     text NOT NULL DEFAULT '0',
  asset_account_code    text NOT NULL DEFAULT '1500',
  accum_account_code    text NOT NULL DEFAULT '1510',
  expense_account_code  text NOT NULL DEFAULT '6900',
  status                text NOT NULL DEFAULT 'ACTIVE',
  disposed_date         text NOT NULL DEFAULT '',
  memo                  text NOT NULL DEFAULT '',
  CONSTRAINT fixed_asset_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS fixed_asset_period (
  tenant_id text NOT NULL,
  asset_id  text NOT NULL,
  period    text NOT NULL,
  entry_id  text NOT NULL DEFAULT '',
  CONSTRAINT fixed_asset_period_pk PRIMARY KEY (tenant_id, asset_id, period)
);
`;

export class PgFixedAssetStore implements FixedAssetStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(FIXED_ASSET_DDL);
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

  private from(r: Record<string, unknown>): AssetRecord {
    return {
      id: String(r["id"]),
      name: String(r["name"] ?? ""),
      category: String(r["category"] ?? ""),
      inServiceDate: String(r["in_service_date"] ?? ""),
      costMinor: String(r["cost_minor"] ?? "0"),
      salvageMinor: String(r["salvage_minor"] ?? "0"),
      method: String(r["method"] ?? "STRAIGHT_LINE") as DepreciationMethod,
      usefulLifeMonths: Number(r["useful_life_months"] ?? 0),
      decliningFactorMicro: String(r["declining_factor_micro"] ?? "2000000"),
      totalUnitsMilli: String(r["total_units_milli"] ?? "0"),
      unitsUsedMilli: String(r["units_used_milli"] ?? "0"),
      accumulatedMinor: String(r["accumulated_minor"] ?? "0"),
      assetAccountCode: String(r["asset_account_code"] ?? "1500"),
      accumAccountCode: String(r["accum_account_code"] ?? "1510"),
      expenseAccountCode: String(r["expense_account_code"] ?? "6900"),
      status: String(r["status"] ?? "ACTIVE") as "ACTIVE" | "DISPOSED",
      disposedDate: String(r["disposed_date"] ?? ""),
      memo: String(r["memo"] ?? ""),
    };
  }

  async listAssets(tenant: string): Promise<AssetRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM fixed_asset WHERE tenant_id=$1 ORDER BY id", [tenant]);
      return res.rows.map((r) => this.from(r));
    });
  }

  async getAsset(tenant: string, id: string): Promise<AssetRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query("SELECT * FROM fixed_asset WHERE tenant_id=$1 AND id=$2", [tenant, id]);
      const r = res.rows[0];
      return r ? this.from(r) : undefined;
    });
  }

  async saveAsset(tenant: string, a: AssetRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO fixed_asset (tenant_id, id, name, category, in_service_date, cost_minor,
         salvage_minor, method, useful_life_months, declining_factor_micro, total_units_milli,
         units_used_milli, accumulated_minor, asset_account_code, accum_account_code,
         expense_account_code, status, disposed_date, memo)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         name=EXCLUDED.name, category=EXCLUDED.category, in_service_date=EXCLUDED.in_service_date,
         cost_minor=EXCLUDED.cost_minor, salvage_minor=EXCLUDED.salvage_minor, method=EXCLUDED.method,
         useful_life_months=EXCLUDED.useful_life_months,
         declining_factor_micro=EXCLUDED.declining_factor_micro,
         total_units_milli=EXCLUDED.total_units_milli, units_used_milli=EXCLUDED.units_used_milli,
         accumulated_minor=EXCLUDED.accumulated_minor, asset_account_code=EXCLUDED.asset_account_code,
         accum_account_code=EXCLUDED.accum_account_code, expense_account_code=EXCLUDED.expense_account_code,
         status=EXCLUDED.status, disposed_date=EXCLUDED.disposed_date, memo=EXCLUDED.memo`,
      [
        tenant, a.id, a.name, a.category, a.inServiceDate, a.costMinor, a.salvageMinor, a.method,
        a.usefulLifeMonths, a.decliningFactorMicro, a.totalUnitsMilli, a.unitsUsedMilli,
        a.accumulatedMinor, a.assetAccountCode, a.accumAccountCode, a.expenseAccountCode,
        a.status, a.disposedDate, a.memo,
      ],
    ));
  }

  async postedPeriod(tenant: string, assetId: string, period: string): Promise<boolean> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT 1 FROM fixed_asset_period WHERE tenant_id=$1 AND asset_id=$2 AND period=$3",
        [tenant, assetId, period]);
      return res.rows.length > 0;
    });
  }

  async markPeriod(tenant: string, assetId: string, period: string, entryId: string): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO fixed_asset_period (tenant_id, asset_id, period, entry_id)
       VALUES ($1,$2,$3,$4) ON CONFLICT (tenant_id, asset_id, period) DO NOTHING`,
      [tenant, assetId, period, entryId]));
  }
}

// --- operations --------------------------------------------------------------

export interface FixedAssetContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

function requireDate(raw: unknown, label: string): string {
  const date = String(raw ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new FixedAssetError(`${label} must be YYYY-MM-DD`);
  return date;
}

function minorOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^-?\d+$/.test(value)) throw new FixedAssetError(`${label} must be a whole number`);
  return BigInt(value);
}

function provenanceFor(id: string, date: string, at: string): Provenance {
  return {
    sourceSystem: "fixed-assets",
    sourceObject: id,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/fixed-assets-1",
    mappingVersion: "ledger-service/fixed-assets-1",
  };
}

export interface AssetInput {
  readonly id?: string;
  readonly name?: string;
  readonly category?: string;
  readonly in_service_date?: string;
  readonly cost_minor?: string | number;
  readonly salvage_minor?: string | number;
  readonly method?: string;
  readonly useful_life_months?: string | number;
  readonly declining_factor_micro?: string | number;
  readonly total_units_milli?: string | number;
  readonly asset_account_code?: string;
  readonly accum_account_code?: string;
  readonly expense_account_code?: string;
  readonly memo?: string;
  /** When set on creation, capitalise the asset: debit the asset account, credit this. */
  readonly paid_from_code?: string;
}

/** Add (or edit) an asset. On creation, optionally capitalises it onto the balance sheet. */
export async function saveAsset(
  ctx: FixedAssetContext, input: AssetInput,
): Promise<{ asset: AssetRecord; entryId: string }> {
  const id = String(input.id ?? "").trim();
  if (!id) throw new FixedAssetError("an asset needs an id");
  const name = String(input.name ?? "").trim();
  if (!name) throw new FixedAssetError("an asset needs a name");
  const method = String(input.method ?? "STRAIGHT_LINE").trim().toUpperCase() as DepreciationMethod;
  if (!METHODS.includes(method)) throw new FixedAssetError(`method must be one of ${METHODS.join(", ")}`);
  const cost = minorOf(input.cost_minor, "cost");
  if (cost <= 0n) throw new FixedAssetError("an asset needs a cost");
  const salvage = minorOf(input.salvage_minor, "salvage");
  if (salvage < 0n || salvage > cost) throw new FixedAssetError("salvage must be between 0 and cost");
  const life = Number(input.useful_life_months ?? 0);
  if (!Number.isInteger(life) || life < 0) throw new FixedAssetError("useful life must be whole months");
  if (method !== "UNITS_OF_PRODUCTION" && life <= 0) {
    throw new FixedAssetError("a time-based method needs a useful life in months");
  }
  const totalUnits = minorOf(input.total_units_milli, "total units");
  if (method === "UNITS_OF_PRODUCTION" && totalUnits <= 0n) {
    throw new FixedAssetError("units-of-production needs total expected units");
  }

  const chart = await ctx.backend.chart(ctx.tenant);
  const need = (code: string, role: string, type: string) => {
    const account = chart.getByCode(code);
    if (!account) throw new FixedAssetError(`unknown account code ${code}`);
    if (account.type !== type) throw new FixedAssetError(`${code} is not a ${role} account`);
    return code;
  };
  const assetAccountCode = need(String(input.asset_account_code ?? "").trim() || "1500", "asset", "ASSET");
  const accumAccountCode = need(String(input.accum_account_code ?? "").trim() || "1510", "asset", "ASSET");
  const expenseAccountCode = need(String(input.expense_account_code ?? "").trim() || "6900", "expense", "EXPENSE");

  const existing = await ctx.backend.fixedAssets().getAsset(String(ctx.tenant), id);
  const asset: AssetRecord = {
    id,
    name,
    category: String(input.category ?? "").trim(),
    inServiceDate: requireDate(input.in_service_date, "in-service date"),
    costMinor: cost.toString(),
    salvageMinor: salvage.toString(),
    method,
    usefulLifeMonths: life,
    decliningFactorMicro: (minorOf(input.declining_factor_micro, "declining factor") || 2_000_000n).toString(),
    totalUnitsMilli: totalUnits.toString(),
    unitsUsedMilli: existing?.unitsUsedMilli ?? "0",
    accumulatedMinor: existing?.accumulatedMinor ?? "0",
    assetAccountCode,
    accumAccountCode,
    expenseAccountCode,
    status: existing?.status ?? "ACTIVE",
    disposedDate: existing?.disposedDate ?? "",
    memo: String(input.memo ?? ""),
  };

  let entryId = "";
  const paidFrom = String(input.paid_from_code ?? "").trim();
  if (!existing && paidFrom) {
    const from = chart.getByCode(paidFrom);
    const assetAcct = chart.getByCode(assetAccountCode);
    if (!from) throw new FixedAssetError(`unknown account code ${paidFrom}`);
    if (!assetAcct) throw new FixedAssetError(`unknown account code ${assetAccountCode}`);
    const command: PostCommand = {
      tenantId: ctx.tenant,
      idempotencyKey: asIdempotencyKey(`asset-buy:${id}`),
      periodKey: asPeriodKey(asset.inServiceDate.slice(0, 7)),
      currency: ctx.currency,
      entryDate: asset.inServiceDate,
      memo: `Asset acquired — ${name}`,
      provenance: provenanceFor(id, asset.inServiceDate, ctx.now()),
      lines: [
        { accountId: assetAcct.id, side: "DEBIT", amount: Money.fromMinorUnits(cost, ctx.currency), memo: name },
        { accountId: from.id, side: "CREDIT", amount: Money.fromMinorUnits(cost, ctx.currency), memo: name },
      ],
    };
    const engine = new PostingEngine(chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant));
    entryId = String((await engine.post(command, { postedAt: ctx.now() })).id);
  }
  await ctx.backend.fixedAssets().saveAsset(String(ctx.tenant), asset);
  return { asset, entryId };
}

async function postDepreciation(
  ctx: FixedAssetContext, asset: AssetRecord, amount: bigint, date: string, period: string,
): Promise<string> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const expense = chart.getByCode(asset.expenseAccountCode);
  const accum = chart.getByCode(asset.accumAccountCode);
  if (!expense) throw new FixedAssetError(`unknown account code ${asset.expenseAccountCode}`);
  if (!accum) throw new FixedAssetError(`unknown account code ${asset.accumAccountCode}`);
  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`depr:${asset.id}:${period}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: `Depreciation — ${asset.name}`,
    provenance: provenanceFor(`${asset.id}:${period}`, date, ctx.now()),
    lines: [
      { accountId: expense.id, side: "DEBIT", amount: Money.fromMinorUnits(amount, ctx.currency), memo: asset.name },
      // Accumulated depreciation is a contra-asset: a credit reduces net book value.
      { accountId: accum.id, side: "CREDIT", amount: Money.fromMinorUnits(amount, ctx.currency), memo: asset.name },
    ],
  };
  const engine = new PostingEngine(chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant));
  const entry = await engine.post(command, { postedAt: ctx.now() });
  return String(entry.id);
}

export interface RunDepreciationInput {
  readonly asset_id?: string;
  readonly through_date?: string;
}

/**
 * Depreciate a calendar-method asset up to a date. Posts the difference between
 * what should have accumulated by then and what already has — so running the
 * same month twice, or catching several up at once, books each month once.
 */
export async function runDepreciation(
  ctx: FixedAssetContext, input: RunDepreciationInput,
): Promise<{ asset: AssetRecord; postedMinor: string; entryId: string }> {
  const assetId = String(input.asset_id ?? "").trim();
  const asset = await ctx.backend.fixedAssets().getAsset(String(ctx.tenant), assetId);
  if (!asset) throw new FixedAssetError(`unknown asset ${assetId}`);
  if (asset.status === "DISPOSED") throw new FixedAssetError(`${assetId} is disposed`);
  if (asset.method === "UNITS_OF_PRODUCTION") {
    throw new FixedAssetError(`${assetId} depreciates by usage — record production instead`);
  }
  const throughDate = requireDate(input.through_date, "through date");
  const target = targetAccumulated(asset, throughDate);
  const already = BigInt(asset.accumulatedMinor);
  const delta = target - already;
  if (delta <= 0n) return { asset, postedMinor: "0", entryId: "" };

  const period = throughDate.slice(0, 7);
  if (await ctx.backend.fixedAssets().postedPeriod(String(ctx.tenant), assetId, period)) {
    return { asset, postedMinor: "0", entryId: "" };
  }
  const entryId = await postDepreciation(ctx, asset, delta, throughDate, period);
  const updated: AssetRecord = { ...asset, accumulatedMinor: target.toString() };
  await ctx.backend.fixedAssets().saveAsset(String(ctx.tenant), updated);
  await ctx.backend.fixedAssets().markPeriod(String(ctx.tenant), assetId, period, entryId);
  return { asset: updated, postedMinor: delta.toString(), entryId };
}

export interface RecordUsageInput {
  readonly asset_id?: string;
  readonly date?: string;
  readonly units_milli?: string | number;
}

/** Depreciate a units-of-production asset by output consumed. */
export async function recordUsage(
  ctx: FixedAssetContext, input: RecordUsageInput,
): Promise<{ asset: AssetRecord; postedMinor: string; entryId: string }> {
  const assetId = String(input.asset_id ?? "").trim();
  const asset = await ctx.backend.fixedAssets().getAsset(String(ctx.tenant), assetId);
  if (!asset) throw new FixedAssetError(`unknown asset ${assetId}`);
  if (asset.status === "DISPOSED") throw new FixedAssetError(`${assetId} is disposed`);
  if (asset.method !== "UNITS_OF_PRODUCTION") {
    throw new FixedAssetError(`${assetId} is not a units-of-production asset`);
  }
  const date = requireDate(input.date, "date");
  const units = minorOf(input.units_milli, "units");
  if (units <= 0n) throw new FixedAssetError("usage needs a positive number of units");

  const base = depreciableBase(asset);
  const totalUnits = BigInt(asset.totalUnitsMilli);
  let amount = roundDiv(base * units, totalUnits);
  const remaining = base - BigInt(asset.accumulatedMinor);
  if (amount > remaining) amount = remaining; // never depreciate past the base
  if (amount <= 0n) return { asset, postedMinor: "0", entryId: "" };

  const period = `${date}#${asset.unitsUsedMilli}`; // each usage entry is distinct
  const entryId = await postDepreciation(ctx, asset, amount, date, period);
  const updated: AssetRecord = {
    ...asset,
    accumulatedMinor: (BigInt(asset.accumulatedMinor) + amount).toString(),
    unitsUsedMilli: (BigInt(asset.unitsUsedMilli) + units).toString(),
  };
  await ctx.backend.fixedAssets().saveAsset(String(ctx.tenant), updated);
  return { asset: updated, postedMinor: amount.toString(), entryId };
}

export interface DisposeInput {
  readonly asset_id?: string;
  readonly date?: string;
  readonly proceeds_minor?: string | number;
  readonly proceeds_to_code?: string;
  readonly gain_loss_code?: string;
}

/**
 * Dispose of an asset: remove its cost and accumulated depreciation, take in any
 * proceeds, and book the gain or loss (proceeds − net book value). The entry is
 * balanced by construction; the gain/loss line is whatever makes it foot.
 */
export async function disposeAsset(
  ctx: FixedAssetContext, input: DisposeInput,
): Promise<{ asset: AssetRecord; entryId: string; gainLossMinor: string }> {
  const assetId = String(input.asset_id ?? "").trim();
  const asset = await ctx.backend.fixedAssets().getAsset(String(ctx.tenant), assetId);
  if (!asset) throw new FixedAssetError(`unknown asset ${assetId}`);
  if (asset.status === "DISPOSED") throw new FixedAssetError(`${assetId} is already disposed`);
  const date = requireDate(input.date, "date");
  const proceeds = minorOf(input.proceeds_minor, "proceeds");
  if (proceeds < 0n) throw new FixedAssetError("proceeds cannot be negative");

  const chart = await ctx.backend.chart(ctx.tenant);
  const cost = BigInt(asset.costMinor);
  const accum = BigInt(asset.accumulatedMinor);
  const bookValue = cost - accum;
  const gainLoss = proceeds - bookValue; // positive = gain, negative = loss

  const assetAcct = chart.getByCode(asset.assetAccountCode);
  const accumAcct = chart.getByCode(asset.accumAccountCode);
  const proceedsCode = String(input.proceeds_to_code ?? "").trim() || "1000";
  const proceedsAcct = chart.getByCode(proceedsCode);
  const glCode = String(input.gain_loss_code ?? "").trim() || "4900"; // Other Income
  const glAcct = chart.getByCode(glCode);
  if (!assetAcct) throw new FixedAssetError(`unknown account code ${asset.assetAccountCode}`);
  if (!accumAcct) throw new FixedAssetError(`unknown account code ${asset.accumAccountCode}`);
  if (!proceedsAcct) throw new FixedAssetError(`unknown account code ${proceedsCode}`);
  if (!glAcct) throw new FixedAssetError(`unknown account code ${glCode}`);

  const lines: PostCommand["lines"][number][] = [
    // remove the asset at cost (credit) and clear its accumulated depreciation (debit)
    { accountId: assetAcct.id, side: "CREDIT", amount: Money.fromMinorUnits(cost, ctx.currency), memo: asset.name },
  ];
  if (accum > 0n) {
    lines.push({ accountId: accumAcct.id, side: "DEBIT", amount: Money.fromMinorUnits(accum, ctx.currency), memo: "Accumulated depreciation" });
  }
  if (proceeds > 0n) {
    lines.push({ accountId: proceedsAcct.id, side: "DEBIT", amount: Money.fromMinorUnits(proceeds, ctx.currency), memo: "Disposal proceeds" });
  }
  if (gainLoss > 0n) {
    lines.push({ accountId: glAcct.id, side: "CREDIT", amount: Money.fromMinorUnits(gainLoss, ctx.currency), memo: "Gain on disposal" });
  } else if (gainLoss < 0n) {
    lines.push({ accountId: glAcct.id, side: "DEBIT", amount: Money.fromMinorUnits(-gainLoss, ctx.currency), memo: "Loss on disposal" });
  }

  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(`asset-dispose:${assetId}`),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: `Asset disposed — ${asset.name}`,
    provenance: provenanceFor(`dispose:${assetId}`, date, ctx.now()),
    lines,
  };
  const engine = new PostingEngine(chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant));
  const entry = await engine.post(command, { postedAt: ctx.now() });
  const updated: AssetRecord = { ...asset, status: "DISPOSED", disposedDate: date };
  await ctx.backend.fixedAssets().saveAsset(String(ctx.tenant), updated);
  return { asset: updated, entryId: String(entry.id), gainLossMinor: gainLoss.toString() };
}

// --- serialization + register ------------------------------------------------

export function depreciationRowJson(r: DepreciationRow): Record<string, unknown> {
  return {
    period: r.period,
    through_date: r.throughDate,
    expense_minor: r.expenseMinor,
    accumulated_minor: r.accumulatedMinor,
    book_value_minor: r.bookValueMinor,
  };
}

export function assetJson(a: AssetRecord): Record<string, unknown> {
  const cost = BigInt(a.costMinor);
  const accum = BigInt(a.accumulatedMinor);
  return {
    id: a.id,
    name: a.name,
    category: a.category,
    in_service_date: a.inServiceDate,
    cost_minor: a.costMinor,
    salvage_minor: a.salvageMinor,
    method: a.method,
    useful_life_months: a.usefulLifeMonths,
    declining_factor_micro: a.decliningFactorMicro,
    total_units_milli: a.totalUnitsMilli,
    units_used_milli: a.unitsUsedMilli,
    accumulated_minor: a.accumulatedMinor,
    book_value_minor: (cost - accum).toString(),
    asset_account_code: a.assetAccountCode,
    accum_account_code: a.accumAccountCode,
    expense_account_code: a.expenseAccountCode,
    status: a.status,
    disposed_date: a.disposedDate,
    memo: a.memo,
  };
}

/** The fixed-asset register with a balance-sheet tie-out on net book value. */
export async function assetRegister(ctx: FixedAssetContext): Promise<Record<string, unknown>> {
  const assets = await ctx.backend.fixedAssets().listAssets(String(ctx.tenant));
  const active = assets.filter((a) => a.status === "ACTIVE");
  let totalCost = 0n;
  let totalAccum = 0n;
  for (const a of active) {
    totalCost += BigInt(a.costMinor);
    totalAccum += BigInt(a.accumulatedMinor);
  }
  return {
    contract: "fixed-asset-register/1",
    currency: ctx.currency.code,
    assets: assets.map(assetJson),
    totals: {
      cost_minor: totalCost.toString(),
      accumulated_minor: totalAccum.toString(),
      book_value_minor: (totalCost - totalAccum).toString(),
      active_count: active.length,
    },
  };
}
