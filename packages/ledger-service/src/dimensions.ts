import {
  DimensionRegistry,
  DimensionError,
  UNASSIGNED,
  trialBalanceByDimension,
  type Currency,
  type DimensionDef,
  type PostCommand,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";

/**
 * Dimensions — QuickBooks calls them classes and locations.
 *
 * The question they answer is the one a chart of accounts cannot: not "how much
 * did we spend on wages" but "how much did *the Denver shop* spend on wages".
 * Without them a growing business either lives with a P&L that averages its
 * lines of business into meaninglessness, or explodes the chart into
 * "Wages — Denver", "Wages — Boulder", "Wages — Catering" and loses the ability
 * to ask what wages cost in total.
 *
 * The important design decision is that dimension values are **defined, not
 * typed**. An open text field produces "Denver", "denver", "Denver ", and
 * "Dnever" — four lines of business where there is one, and a report that is
 * quietly wrong rather than loudly broken. So a dimension declares its allowed
 * values and a typo is refused at the door.
 *
 * A dimension can also be **required**, which is how a business that runs two
 * locations makes it impossible to post a cost to neither of them.
 */

export class DimensionServiceError extends Error {}

export interface DimensionRecord {
  readonly key: string;
  readonly label: string;
  /** Allowed values. Empty means any non-blank value is accepted. */
  readonly values: readonly string[];
  readonly required: boolean;
}

export interface DimensionStore {
  migrate(): Promise<void>;
  list(tenant: string): Promise<DimensionRecord[]>;
  save(tenant: string, dimension: DimensionRecord): Promise<void>;
  remove(tenant: string, key: string): Promise<void>;
}

export class InMemoryDimensionStore implements DimensionStore {
  private readonly defs = new Map<string, DimensionRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  list(tenant: string): Promise<DimensionRecord[]> {
    const prefix = `${tenant}::`;
    const out: DimensionRecord[] = [];
    for (const [k, v] of this.defs) if (k.startsWith(prefix)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.key.localeCompare(b.key)));
  }

  save(tenant: string, dimension: DimensionRecord): Promise<void> {
    this.defs.set(`${tenant}::${dimension.key}`, dimension);
    return Promise.resolve();
  }

  remove(tenant: string, key: string): Promise<void> {
    this.defs.delete(`${tenant}::${key}`);
    return Promise.resolve();
  }
}

/**
 * A unit separator, not a comma. A dimension value may legitimately contain a
 * comma ("Denver, CO"), and splitting on one would silently invent a value that
 * nobody defined.
 */
const VALUE_SEPARATOR = "\u001f";

export const DIMENSION_DDL = `
CREATE TABLE IF NOT EXISTS dimension_def (
  tenant_id  text NOT NULL,
  dim_key    text NOT NULL,
  label      text NOT NULL,
  values_csv text NOT NULL DEFAULT '',
  required   boolean NOT NULL DEFAULT false,
  CONSTRAINT dimension_def_pk PRIMARY KEY (tenant_id, dim_key)
);
`;

export class PgDimensionStore implements DimensionStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(DIMENSION_DDL);
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

  async list(tenant: string): Promise<DimensionRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT dim_key, label, values_csv, required FROM dimension_def
         WHERE tenant_id=$1 ORDER BY dim_key`,
        [tenant],
      );
      return res.rows.map((r) => ({
        key: String(r["dim_key"]),
        label: String(r["label"]),
        values: String(r["values_csv"] ?? "").split(VALUE_SEPARATOR).filter(Boolean),
        required: r["required"] === true || r["required"] === "t",
      }));
    });
  }

  async save(tenant: string, dimension: DimensionRecord): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query(
        `INSERT INTO dimension_def (tenant_id, dim_key, label, values_csv, required)
         VALUES ($1,$2,$3,$4,$5)
         ON CONFLICT (tenant_id, dim_key) DO UPDATE SET
           label=EXCLUDED.label, values_csv=EXCLUDED.values_csv,
           required=EXCLUDED.required`,
        [tenant, dimension.key, dimension.label, dimension.values.join(VALUE_SEPARATOR),
         dimension.required],
      ),
    );
  }

  async remove(tenant: string, key: string): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query("DELETE FROM dimension_def WHERE tenant_id=$1 AND dim_key=$2", [tenant, key]),
    );
  }
}

// --- the flow ----------------------------------------------------------------

export interface DimensionContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
}

export function dimensionJson(d: DimensionRecord): Record<string, unknown> {
  return { key: d.key, label: d.label, values: [...d.values], required: d.required };
}

/** The tenant's registry, built from what they defined. */
export async function registryFor(ctx: DimensionContext): Promise<DimensionRegistry> {
  const defs = await ctx.backend.dimensions().list(String(ctx.tenant));
  return new DimensionRegistry(
    defs.map((d): DimensionDef => ({
      key: d.key,
      label: d.label,
      values: [...d.values],
      required: d.required,
    })),
  );
}

export interface DimensionInput {
  readonly key?: string;
  readonly label?: string;
  readonly values?: readonly string[] | string;
  readonly required?: boolean;
}

function parseValues(raw: DimensionInput["values"]): string[] {
  const list = Array.isArray(raw)
    ? raw
    : String(raw ?? "").split("\n").flatMap((line) => line.split(","));
  const seen = new Set<string>();
  const out: string[] = [];
  for (const v of list) {
    const value = String(v).trim();
    if (!value) continue;
    // "Denver" and "denver" are one place; keeping both is how a report
    // fragments into two lines of business that don't exist.
    const fold = value.toLowerCase();
    if (seen.has(fold)) continue;
    seen.add(fold);
    out.push(value);
  }
  return out;
}

export async function saveDimension(
  ctx: DimensionContext, input: DimensionInput,
): Promise<DimensionRecord> {
  const key = String(input.key ?? "").trim().toLowerCase();
  if (!/^[a-z][a-z0-9_]*$/.test(key)) {
    throw new DimensionServiceError(
      "a dimension key must be a short lowercase name like \"class\" or \"location\"",
    );
  }
  const label = String(input.label ?? "").trim() || key;
  const values = parseValues(input.values);
  const required = input.required === true;
  if (required && values.length === 0) {
    throw new DimensionServiceError(
      "a required dimension needs its allowed values listed — otherwise every "
      + "posting must carry a value that nothing validates",
    );
  }
  const record: DimensionRecord = { key, label, values, required };
  await ctx.backend.dimensions().save(String(ctx.tenant), record);
  return record;
}

/**
 * `job` and `cost_code` are dimensions the business does not define — the jobs
 * and cost codes it has created define them.
 *
 * Costing a job is the same act as classifying a cost, so it travels the same
 * way: as a dimension on a journal line. That means every posting path already
 * built — a bill, a bank feed line, payroll, a recurring template — costs a job
 * without a single new code path, and the job report and the trial balance
 * cannot disagree, because they are reading the same rows. The values are
 * validated against the real jobs and cost codes, so a typo is refused exactly
 * like a mistyped class.
 */
async function reservedDimensions(ctx: DimensionContext): Promise<DimensionRecord[]> {
  const store = ctx.backend.jobs();
  const jobs = await store.listJobs(String(ctx.tenant));
  if (jobs.length === 0) return [];
  const codes = await store.listCostCodes(String(ctx.tenant));
  return [
    { key: "job", label: "Job", values: jobs.map((j) => j.id), required: false },
    {
      key: "cost_code",
      label: "Cost code",
      values: codes.map((c) => c.code),
      required: false,
    },
  ];
}

/**
 * Validate a command's line dimensions against the tenant's registry.
 *
 * Called before every post, so a typo is refused at the door rather than
 * silently fragmenting a report months later.
 *
 * Keys and values are checked on *every* line — a typo anywhere is a typo. But
 * **required** is enforced only on income and expense lines, which is the only
 * reading that survives contact with real bookkeeping: "which line of business
 * was this?" is a question about revenue and costs. The cash that moved has no
 * line of business, and demanding one would make it impossible to post the
 * other half of any entry.
 */
export async function validateDimensions(
  ctx: DimensionContext, command: PostCommand,
): Promise<void> {
  const defs = [
    ...await ctx.backend.dimensions().list(String(ctx.tenant)),
    ...await reservedDimensions(ctx),
  ];
  if (defs.length === 0) {
    // Nothing defined: a line carrying dimensions is a caller mistake, not a
    // silently-accepted free-text tag.
    for (const line of command.lines) {
      const keys = Object.keys(line.dimensions ?? {});
      if (keys.length > 0) {
        throw new DimensionServiceError(
          `no dimensions are defined for this business, so "${keys[0]}" cannot be used`,
        );
      }
    }
    return;
  }

  // A registry without `required`, because the kernel enforces it on every
  // line and we want it only where it means something. Keys and values are
  // still validated everywhere.
  const shapes = new DimensionRegistry(
    defs.map((d): DimensionDef => ({ key: d.key, label: d.label, values: [...d.values] })),
  );
  const chart = await ctx.backend.chart(ctx.tenant);
  const required = defs.filter((d) => d.required);

  for (const line of command.lines) {
    try {
      shapes.validateLineDimensions(line.dimensions);
    } catch (err) {
      if (err instanceof DimensionError) throw new DimensionServiceError(err.message);
      throw err;
    }
    if (required.length === 0) continue;
    const account = chart.get(line.accountId);
    const type = account?.type;
    if (type !== "REVENUE" && type !== "EXPENSE") continue;
    for (const def of required) {
      if (!line.dimensions?.[def.key]) {
        throw new DimensionServiceError(
          `${account?.code ?? "this line"}: ${def.label} is required on income and expense lines`,
        );
      }
    }
  }
}

/** Read a `dimensions` object off a request line, refusing anything odd. */
export function dimensionsOf(raw: unknown, label: string): Record<string, string> | undefined {
  if (raw === undefined || raw === null) return undefined;
  if (typeof raw !== "object" || Array.isArray(raw)) {
    throw new DimensionServiceError(`${label}: dimensions must be an object`);
  }
  const out: Record<string, string> = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value !== "string") {
      throw new DimensionServiceError(`${label}: dimension "${key}" must be a string`);
    }
    if (!value.trim()) continue;   // a blank selection means "not set"
    out[key] = value.trim();
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

export interface DimensionReport {
  readonly contract: "trial-balance-by-dimension/1";
  readonly dimension: string;
  readonly label: string;
  readonly currency: string;
  readonly buckets: ReadonlyArray<Record<string, unknown>>;
}

/**
 * A trial balance per dimension value — the thing that makes P&L by class
 * possible.
 *
 * Lines with no value land in an explicit "(unassigned)" bucket rather than
 * being dropped. That bucket is usually the most informative one on the report:
 * it is exactly the activity nobody has attributed yet.
 */
export async function reportByDimension(
  ctx: DimensionContext,
  key: string,
  query: Readonly<Record<string, string>>,
): Promise<DimensionReport> {
  const defs = await ctx.backend.dimensions().list(String(ctx.tenant));
  const def = defs.find((d) => d.key === key);
  if (!def) throw new DimensionServiceError(`unknown dimension "${key}"`);

  const from = String(query["from"] ?? "").trim();
  const to = String(query["to"] ?? "").trim();
  for (const [value, label] of [[from, "from"], [to, "to"]] as const) {
    if (value && !/^\d{4}-\d{2}-\d{2}$/.test(value)) {
      throw new DimensionServiceError(`${label} must be YYYY-MM-DD`);
    }
  }
  const window = from || to
    ? { ...(from ? { from } : {}), ...(to ? { to } : {}) }
    : undefined;

  const chart = await ctx.backend.chart(ctx.tenant);
  const byBucket = await trialBalanceByDimension(
    ctx.backend.store(ctx.tenant), ctx.tenant, chart, ctx.currency, key, window,
  );

  const buckets = [...byBucket.entries()]
    .sort((a, b) => {
      // "(unassigned)" last: it is a gap to close, not a line of business.
      if (a[0] === UNASSIGNED) return 1;
      if (b[0] === UNASSIGNED) return -1;
      return a[0].localeCompare(b[0]);
    })
    .map(([value, tb]) => ({
      value,
      unassigned: value === UNASSIGNED,
      in_balance: tb.inBalance,
      total_debit_minor: tb.totalDebit.minorUnits.toString(),
      total_credit_minor: tb.totalCredit.minorUnits.toString(),
      rows: tb.rows.map((r) => ({
        code: r.code,
        name: r.name,
        type: r.type,
        debit_minor: r.debit.minorUnits.toString(),
        credit_minor: r.credit.minorUnits.toString(),
      })),
    }));

  return {
    contract: "trial-balance-by-dimension/1",
    dimension: key,
    label: def.label,
    currency: ctx.currency.code,
    buckets,
  };
}
