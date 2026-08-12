import { Money } from "@rgnr8/ledger-kernel";
import { TransactionKind, type CanonicalTransaction } from "@rgnr8/ingestion";
import type { FrequencyDTO, RecurringItemDTO } from "./dto.js";
import { moneyToDto } from "./dto.js";

export interface DetectOptions {
  readonly minOccurrences?: number;
  readonly amountToleranceBps?: number;
  readonly gapToleranceDays?: number;
}

function dayNumber(iso: string): number {
  return Math.floor(Date.parse(iso.slice(0, 10) + "T00:00:00Z") / 86_400_000);
}

function median(nums: number[]): number {
  const s = [...nums].sort((a, b) => a - b);
  const mid = Math.floor(s.length / 2);
  if (s.length % 2 === 1) return s[mid]!;
  return Math.round((s[mid - 1]! + s[mid]!) / 2);
}

function frequencyForGap(gap: number): { freq: FrequencyDTO } | null {
  if (gap >= 6 && gap <= 8) return { freq: "WEEKLY" };
  if (gap >= 12 && gap <= 16) return { freq: "BIWEEKLY" };
  if (gap >= 26 && gap <= 32) return { freq: "MONTHLY" };
  if (gap >= 85 && gap <= 95) return { freq: "QUARTERLY" };
  if (gap >= 358 && gap <= 372) return { freq: "ANNUAL" };
  return null;
}

function kindToCategory(kind: TransactionKind, inflow: boolean): string {
  switch (kind) {
    case TransactionKind.PAYROLL_NET:
      return "PAYROLL_NET";
    case TransactionKind.PAYROLL_TAX:
      return "PAYROLL_TAX";
    case TransactionKind.DEPOSIT:
      return "CUSTOMER_RECEIPT";
    case TransactionKind.INTEREST:
    case TransactionKind.REFUND:
      return "OTHER_INFLOW";
    case TransactionKind.FEE:
    case TransactionKind.PURCHASE:
      return "OTHER_OUTFLOW";
    default:
      return inflow ? "OTHER_INFLOW" : "OTHER_OUTFLOW";
  }
}

function normalize(s: string): string {
  return s.toLowerCase().replace(/\s+/g, " ").trim();
}

/**
 * Detect recurring flows from canonical transactions.
 *
 * Groups by counterparty/description and sign, then keeps a group only when it
 * has enough occurrences, the gaps between them are consistent and match a known
 * cadence, and the amounts are stable. Anchored at the most recent occurrence so
 * the forecast projects it forward. Internal transfers are ignored.
 */
export function detectRecurring(
  txns: readonly CanonicalTransaction[],
  opts: DetectOptions = {},
): RecurringItemDTO[] {
  const minOcc = opts.minOccurrences ?? 3;
  const amtTol = opts.amountToleranceBps ?? 500;
  const gapTol = opts.gapToleranceDays ?? 4;

  const groups = new Map<string, CanonicalTransaction[]>();
  for (const t of txns) {
    if (t.isInternalTransfer || t.amount.minorUnits === 0n) continue;
    const inflow = t.direction === "INFLOW";
    const key = `${inflow ? "IN" : "OUT"}|${normalize(t.counterparty ?? t.description)}`;
    (groups.get(key) ?? groups.set(key, []).get(key)!).push(t);
  }

  const out: RecurringItemDTO[] = [];
  for (const items of groups.values()) {
    if (items.length < minOcc) continue;
    const sorted = [...items].sort((a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : 0));
    const days = sorted.map((t) => dayNumber(t.date));
    const gaps: number[] = [];
    for (let i = 1; i < days.length; i++) gaps.push(days[i]! - days[i - 1]!);
    const medGap = median(gaps);
    if (!gaps.every((g) => Math.abs(g - medGap) <= gapTol)) continue;
    const freq = frequencyForGap(medGap);
    if (!freq) continue;

    const magnitudes = sorted.map((t) => (t.amount.minorUnits < 0n ? -t.amount.minorUnits : t.amount.minorUnits));
    const medAmt = BigInt(median(magnitudes.map((m) => Number(m))));
    if (medAmt === 0n) continue;
    const tol = (medAmt * BigInt(amtTol)) / 10000n;
    const stable = magnitudes.every((m) => (m > medAmt ? m - medAmt : medAmt - m) <= tol);
    if (!stable) continue;

    const first = sorted[0]!;
    const latest = sorted[sorted.length - 1]!;
    const inflow = first.direction === "INFLOW";
    const amount = Money.fromMinorUnits(medAmt, first.amount.currency);

    out.push({
      label: first.counterparty ?? first.description,
      category: kindToCategory(first.kind, inflow),
      direction: inflow ? "INFLOW" : "OUTFLOW",
      amount: moneyToDto(amount),
      recurrence: {
        frequency: freq.freq,
        anchor: latest.date.slice(0, 10),
        interval: 1,
        second_day: null,
        end: null,
        count: null,
      },
    });
  }

  out.sort((a, b) => Number(BigInt(b.amount.minor) - BigInt(a.amount.minor)));
  return out;
}
