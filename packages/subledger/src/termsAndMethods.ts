/**
 * Payment methods and payment terms as first-class master data.
 *
 * QBO tracks how money moves (Check, ACH, Credit Card, Cash) and the terms a
 * customer/vendor is on (Net 30, 2/10 Net 30). Terms drive an invoice's due
 * date and any early-payment discount deadline. Discount rates are parts-per-
 * million so they stay exact, consistent with the tax engine.
 */

export interface PaymentMethod {
  readonly id: string;
  readonly name: string;
  /** Whether this method deposits to undeposited funds first (like QBO). */
  readonly groupWithUndeposited?: boolean;
  readonly active?: boolean;
}

export interface PaymentTerms {
  readonly id: string;
  readonly name: string;
  /** Net days until the balance is due. */
  readonly netDays: number;
  /** Early-payment discount rate in ppm (e.g. 2% = 20_000), optional. */
  readonly discountPpm?: number;
  /** Days within which the discount applies (e.g. 10 for "2/10 Net 30"). */
  readonly discountDays?: number;
}

function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}
function isLeap(y: number): boolean {
  return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
}
function lastDay(y: number, m: number): number {
  return [31, isLeap(y) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]!;
}
/** Add days to an ISO date (pure, no Date). */
export function addDaysIso(isoDate: string, n: number): string {
  let y = Number(isoDate.slice(0, 4));
  let m = Number(isoDate.slice(5, 7));
  let d = Number(isoDate.slice(8, 10)) + n;
  while (d > lastDay(y, m)) {
    d -= lastDay(y, m);
    m++;
    if (m > 12) { m = 1; y++; }
  }
  while (d < 1) {
    m--;
    if (m < 1) { m = 12; y--; }
    d += lastDay(y, m);
  }
  return `${y}-${pad2(m)}-${pad2(d)}`;
}

/** Due date implied by terms from a document date. */
export function dueDateFor(terms: PaymentTerms, documentDate: string): string {
  return addDaysIso(documentDate, terms.netDays);
}

/** Early-payment discount deadline, or undefined when the terms carry no discount. */
export function discountDeadlineFor(terms: PaymentTerms, documentDate: string): string | undefined {
  if (terms.discountDays === undefined || terms.discountPpm === undefined) return undefined;
  return addDaysIso(documentDate, terms.discountDays);
}

/** Exact early-payment discount on a base amount, in minor units (round half-up). */
export function discountMinor(terms: PaymentTerms, baseMinor: bigint): bigint {
  if (!terms.discountPpm) return 0n;
  const ppm = BigInt(terms.discountPpm);
  const PPM = 1_000_000n;
  const neg = baseMinor < 0n;
  const b = neg ? -baseMinor : baseMinor;
  const d = (b * ppm + PPM / 2n) / PPM;
  return neg ? -d : d;
}
