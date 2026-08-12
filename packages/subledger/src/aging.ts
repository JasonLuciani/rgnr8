import { Money, sumMoney, type Currency } from "@rgnr8/ledger-kernel";
import type { Aging, AgingBucket } from "./types.js";

function dayNumber(iso: string): number {
  return Math.floor(Date.parse(iso.slice(0, 10) + "T00:00:00Z") / 86_400_000);
}

interface OpenItem {
  readonly dueDate: string;
  readonly openAmount: Money;
}

const BUCKETS: ReadonlyArray<{ label: string; min: number; max: number | null }> = [
  { label: "Current", min: -Infinity, max: 1 }, // not yet due (days past due <= 0)
  { label: "1-30", min: 1, max: 31 },
  { label: "31-60", min: 31, max: 61 },
  { label: "61-90", min: 61, max: 91 },
  { label: "90+", min: 91, max: null },
];

/** Age open items by days-past-due as of a date. */
export function ageItems(items: readonly OpenItem[], asOf: string, currency: Currency): Aging {
  const asOfDay = dayNumber(asOf);
  const sums = BUCKETS.map(() => Money.zero(currency));

  for (const it of items) {
    if (it.openAmount.minorUnits === 0n) continue;
    const daysPastDue = asOfDay - dayNumber(it.dueDate);
    for (let i = 0; i < BUCKETS.length; i++) {
      const b = BUCKETS[i]!;
      const inLower = daysPastDue >= b.min;
      const inUpper = b.max === null || daysPastDue < b.max;
      if (inLower && inUpper) {
        sums[i] = sums[i]!.plus(it.openAmount);
        break;
      }
    }
  }

  const buckets: AgingBucket[] = BUCKETS.map((b, i) => ({
    label: b.label,
    minDays: b.min === -Infinity ? -999999 : b.min,
    maxDays: b.max === null ? null : b.max - 1,
    amount: sums[i]!,
  }));

  return {
    asOf,
    buckets,
    total: sumMoney(
      items.map((i) => i.openAmount),
      currency,
    ),
  };
}
