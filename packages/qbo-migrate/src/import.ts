import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  type Currency,
  type JournalLineInput,
  type PostCommand,
  type PostedEntry,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import { QBO_MAPPING_VERSION, type QboChart } from "./accountMap.js";
import type { QboExport, QboJournalEntry } from "./types.js";

export interface ImportOptions {
  readonly tenantId: TenantId;
  readonly currency: Currency;
  readonly ingestedAt: string; // ISO; injected, not generated in-core
  readonly sourceVersion?: string;
}

export interface ImportIssue {
  readonly entryId: string;
  readonly kind: "unknown_account" | "unbalanced" | "empty" | "bad_amount";
  readonly detail: string;
}

export interface BuildResult {
  readonly commands: readonly PostCommand[];
  readonly issues: readonly ImportIssue[];
}

function periodKeyOf(isoDate: string): string {
  return isoDate.slice(0, 7); // YYYY-MM
}

/**
 * Convert a QBO export into balanced kernel PostCommands. Pure and total: an
 * entry that references an unknown account, is unbalanced, or carries a
 * malformed amount is *dropped to an issue* rather than throwing, so a whole
 * migration isn't lost to one bad row. The caller inspects `issues`.
 */
export function toPostCommands(exp: QboExport, chart: QboChart, opts: ImportOptions): BuildResult {
  const commands: PostCommand[] = [];
  const issues: ImportIssue[] = [];
  const sourceVersion = opts.sourceVersion ?? "1";

  for (const entry of exp.entries) {
    const built = buildOne(entry, chart, opts, sourceVersion);
    if ("issue" in built) {
      issues.push(built.issue);
    } else {
      commands.push(built.command);
    }
  }
  return { commands, issues };
}

function buildOne(
  entry: QboJournalEntry,
  chart: QboChart,
  opts: ImportOptions,
  sourceVersion: string,
): { command: PostCommand } | { issue: ImportIssue } {
  if (entry.lines.length === 0) {
    return { issue: { entryId: entry.id, kind: "empty", detail: "no lines" } };
  }

  const lines: JournalLineInput[] = [];
  let debitMinor = 0n;
  let creditMinor = 0n;

  for (const l of entry.lines) {
    const accountId = chart.byName.get(l.account);
    if (accountId === undefined) {
      return { issue: { entryId: entry.id, kind: "unknown_account", detail: l.account } };
    }
    const hasDebit = l.debit !== undefined && l.debit !== "";
    const hasCredit = l.credit !== undefined && l.credit !== "";
    if (hasDebit === hasCredit) {
      return {
        issue: { entryId: entry.id, kind: "bad_amount", detail: `line for ${l.account} needs exactly one of debit/credit` },
      };
    }
    let amount: Money;
    try {
      amount = Money.fromDecimal(hasDebit ? (l.debit as string) : (l.credit as string), opts.currency);
    } catch {
      return { issue: { entryId: entry.id, kind: "bad_amount", detail: `${l.account}: ${l.debit ?? l.credit}` } };
    }
    if (!amount.isPositive()) {
      return { issue: { entryId: entry.id, kind: "bad_amount", detail: `${l.account}: non-positive amount` } };
    }
    if (hasDebit) {
      debitMinor += amount.minorUnits;
      lines.push({ accountId, side: "DEBIT", amount, ...(l.memo !== undefined ? { memo: l.memo } : {}) });
    } else {
      creditMinor += amount.minorUnits;
      lines.push({ accountId, side: "CREDIT", amount, ...(l.memo !== undefined ? { memo: l.memo } : {}) });
    }
  }

  if (debitMinor !== creditMinor) {
    return {
      issue: {
        entryId: entry.id,
        kind: "unbalanced",
        detail: `debits ${debitMinor} != credits ${creditMinor}`,
      },
    };
  }

  const provenance: Provenance = {
    sourceSystem: "quickbooks",
    sourceObject: `journal:${entry.id}`,
    sourceVersion,
    effectiveDate: entry.date,
    postedDate: entry.date,
    ingestedAt: opts.ingestedAt,
    normalizationVersion: "qbo-norm/1",
    mappingVersion: QBO_MAPPING_VERSION,
  };

  const command: PostCommand = {
    tenantId: opts.tenantId,
    idempotencyKey: asIdempotencyKey(`qbo:${entry.id}`),
    periodKey: asPeriodKey(periodKeyOf(entry.date)),
    currency: opts.currency,
    entryDate: entry.date,
    lines,
    provenance,
    ...(entry.memo !== undefined ? { memo: entry.memo } : {}),
  };
  return { command };
}

export interface ImportReport {
  readonly posted: number;
  readonly skipped: number;
  readonly issues: readonly ImportIssue[];
  readonly periods: readonly string[];
}

/**
 * Post an export into the ledger via the kernel PostingEngine. Idempotent by
 * construction: re-importing the same export replays the same idempotency keys
 * and posts nothing new. `postedAt` is injected.
 */
export async function importQbo(
  engine: PostingEngine,
  exp: QboExport,
  chart: QboChart,
  opts: ImportOptions,
  postedAt: string,
): Promise<ImportReport> {
  const { commands, issues } = toPostCommands(exp, chart, opts);
  const periods = new Set<string>();
  let posted = 0;
  for (const command of commands) {
    await engine.post(command, { postedAt });
    periods.add(command.periodKey as unknown as string);
    posted += 1;
  }
  return { posted, skipped: issues.length, issues, periods: [...periods].sort() };
}
