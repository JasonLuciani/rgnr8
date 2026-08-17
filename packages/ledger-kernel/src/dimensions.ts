import { LedgerError } from "./errors.js";
import { Money, type Currency } from "./money.js";
import type { ChartOfAccounts } from "./chartOfAccounts.js";
import type { LedgerStore } from "./ledgerStore.js";
import type { DateWindow, TrialBalance, TrialBalanceRow } from "./trialBalance.js";
import type { AccountId, PostCommand, TenantId } from "./types.js";

/**
 * Reporting dimensions — QBO's "classes" and "locations" done as validated,
 * first-class tags on journal lines rather than free-form strings.
 *
 * A {@link PostedLine} already carries an optional `dimensions` map; this adds
 * the missing half: a registry that defines which dimension keys exist, what
 * values each allows, and whether it is required — plus a validator the caller
 * runs before posting so a typo'd class ("Markting") is rejected at the door
 * instead of silently fragmenting a report. And it adds dimension-scoped
 * reporting: a trial balance split by class/location, which is what makes P&L
 * by class possible.
 */

export class DimensionError extends LedgerError {}

export interface DimensionDef {
  readonly key: string;
  readonly label: string;
  /** Allowed values. Empty = any non-empty value is accepted (open dimension). */
  readonly values: readonly string[];
  /** When true, every posted line must carry this dimension. */
  readonly required?: boolean;
}

/** The bucket key for lines that don't carry a given dimension. */
export const UNASSIGNED = "(unassigned)";

export class DimensionRegistry {
  private readonly defs = new Map<string, DimensionDef>();

  constructor(defs: readonly DimensionDef[] = []) {
    for (const d of defs) this.defs.set(d.key, d);
  }

  define(def: DimensionDef): this {
    this.defs.set(def.key, def);
    return this;
  }

  list(): readonly DimensionDef[] {
    return [...this.defs.values()];
  }

  /** Validate one line's dimensions: known keys, allowed values, required present. */
  validateLineDimensions(dimensions?: Readonly<Record<string, string>>): void {
    const dims = dimensions ?? {};
    for (const [key, value] of Object.entries(dims)) {
      const def = this.defs.get(key);
      if (!def) throw new DimensionError(`unknown dimension "${key}"`);
      if (value.trim() === "") throw new DimensionError(`empty value for dimension "${key}"`);
      if (def.values.length > 0 && !def.values.includes(value)) {
        throw new DimensionError(`value "${value}" is not allowed for dimension "${key}"`);
      }
    }
    for (const def of this.defs.values()) {
      if (def.required && !(def.key in dims)) {
        throw new DimensionError(`dimension "${def.key}" is required`);
      }
    }
  }

  /** Validate every line of a post command. Call before posting. */
  validateCommand(cmd: PostCommand): void {
    for (const line of cmd.lines) this.validateLineDimensions(line.dimensions);
  }
}

function inWindow(entryDate: string, window?: DateWindow): boolean {
  if (window === undefined) return true;
  if (window.from !== undefined && entryDate < window.from) return false;
  if (window.to !== undefined && entryDate > window.to) return false;
  return true;
}

/**
 * Trial balances split by the value of one dimension (e.g. one TB per class).
 * A line with no value for the dimension lands in the {@link UNASSIGNED} bucket.
 * Each bucket balances on its own only if the books are dimensionally balanced;
 * the per-bucket `inBalance` flag reports it honestly (revenue/expense by class
 * is the common, useful case even when the balance sheet isn't class-tagged).
 */
export async function trialBalanceByDimension(
  store: LedgerStore,
  tenant: TenantId,
  coa: ChartOfAccounts,
  currency: Currency,
  dimensionKey: string,
  window?: DateWindow,
): Promise<Map<string, TrialBalance>> {
  // bucket -> account -> net minor (debit positive)
  const buckets = new Map<string, Map<AccountId, bigint>>();
  for (const entry of await store.list(tenant)) {
    if (!inWindow(entry.entryDate, window)) continue;
    for (const line of entry.lines) {
      const bucket = line.dimensions?.[dimensionKey] ?? UNASSIGNED;
      let net = buckets.get(bucket);
      if (!net) {
        net = new Map();
        buckets.set(bucket, net);
      }
      const delta = line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
      net.set(line.accountId, (net.get(line.accountId) ?? 0n) + delta);
    }
  }

  const out = new Map<string, TrialBalance>();
  for (const [bucket, net] of buckets) {
    const rows: TrialBalanceRow[] = [];
    let totalDebit = 0n;
    let totalCredit = 0n;
    const accounts = [...net.keys()]
      .map((id) => coa.get(id))
      .filter((a): a is NonNullable<typeof a> => a !== undefined)
      .sort((a, b) => a.code.localeCompare(b.code));
    for (const account of accounts) {
      const n = net.get(account.id) ?? 0n;
      const debit = n > 0n ? n : 0n;
      const credit = n < 0n ? -n : 0n;
      totalDebit += debit;
      totalCredit += credit;
      rows.push({
        accountId: account.id,
        code: account.code,
        name: account.name,
        type: account.type,
        debit: Money.fromMinorUnits(debit, currency),
        credit: Money.fromMinorUnits(credit, currency),
      });
    }
    out.set(bucket, {
      currency,
      rows,
      totalDebit: Money.fromMinorUnits(totalDebit, currency),
      totalCredit: Money.fromMinorUnits(totalCredit, currency),
      inBalance: totalDebit === totalCredit,
    });
  }
  return out;
}
