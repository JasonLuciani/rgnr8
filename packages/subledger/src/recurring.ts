/**
 * Recurring / memorized transactions.
 *
 * A memorized transaction is a template plus a schedule: "the $2,400 rent bill,
 * the 1st of every month." This materializes the occurrence *dates* between two
 * bounds deterministically (no wall clock — the window is supplied), so a caller
 * can turn each date into a real posting via its own command factory. Keeping
 * date generation pure and separate from posting means the same schedule is
 * fully testable and never double-fires: the caller keys each generated posting
 * by `${templateId}:${date}` for idempotency.
 */

export type Frequency = "DAILY" | "WEEKLY" | "MONTHLY" | "YEARLY";

export interface RecurringSchedule {
  readonly frequency: Frequency;
  /** Every `interval` periods (e.g. interval 2 + WEEKLY = biweekly). */
  readonly interval: number;
  readonly startDate: string; // ISO
  /** Inclusive last date the schedule may fire (optional). */
  readonly endDate?: string;
  /** Maximum number of occurrences (optional cap). */
  readonly count?: number;
}

export interface RecurringTemplate<T = unknown> {
  readonly id: string;
  readonly schedule: RecurringSchedule;
  /** The memorized payload (e.g. the lines/among of the transaction). */
  readonly payload: T;
}

function parts(iso: string): { y: number; m: number; d: number } {
  return { y: Number(iso.slice(0, 4)), m: Number(iso.slice(5, 7)), d: Number(iso.slice(8, 10)) };
}
function pad2(n: number): string {
  return n < 10 ? `0${n}` : String(n);
}
function iso(y: number, m: number, d: number): string {
  return `${y}-${pad2(m)}-${pad2(d)}`;
}
function isLeap(y: number): boolean {
  return (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
}
function lastDay(y: number, m: number): number {
  return [31, isLeap(y) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]!;
}
/** Serial day number (proleptic) for ordering/stepping ISO dates without Date. */
function dayNumber(iso: string): number {
  const { y, m, d } = parts(iso);
  // days from year 0; good enough for ordering + day stepping across our ranges.
  let days = d;
  for (let mm = 1; mm < m; mm++) days += lastDay(y, mm);
  days += y * 365 + Math.floor(y / 4) - Math.floor(y / 100) + Math.floor(y / 400);
  return days;
}
function addDays(isoDate: string, n: number): string {
  const { y, m, d } = parts(isoDate);
  let year = y;
  let month = m;
  let day = d + n;
  while (day > lastDay(year, month)) {
    day -= lastDay(year, month);
    month++;
    if (month > 12) { month = 1; year++; }
  }
  while (day < 1) {
    month--;
    if (month < 1) { month = 12; year--; }
    day += lastDay(year, month);
  }
  return iso(year, month, day);
}
function addMonths(isoDate: string, n: number): string {
  const { y, m, d } = parts(isoDate);
  const total = (y * 12 + (m - 1)) + n;
  const year = Math.floor(total / 12);
  const month = (total % 12) + 1;
  const day = Math.min(d, lastDay(year, month)); // clamp (e.g. Jan 31 -> Feb 28)
  return iso(year, month, day);
}

function step(isoDate: string, schedule: RecurringSchedule, i: number): string {
  const n = schedule.interval * i;
  switch (schedule.frequency) {
    case "DAILY":
      return addDays(isoDate, n);
    case "WEEKLY":
      return addDays(isoDate, n * 7);
    case "MONTHLY":
      return addMonths(isoDate, n);
    case "YEARLY":
      return addMonths(isoDate, n * 12);
  }
}

/**
 * The occurrence dates of a schedule that fall within `[from, to]` (inclusive),
 * respecting the schedule's own endDate/count caps. Deterministic and pure.
 */
export function occurrencesBetween(
  schedule: RecurringSchedule,
  from: string,
  to: string,
): string[] {
  if (schedule.interval < 1) throw new Error("recurring interval must be >= 1");
  const out: string[] = [];
  const hardEnd = schedule.endDate;
  const toDay = dayNumber(to);
  const fromDay = dayNumber(from);
  const maxIterations = 100_000; // safety backstop
  for (let i = 0, fired = 0; i < maxIterations; i++) {
    if (schedule.count !== undefined && fired >= schedule.count) break;
    const date = step(schedule.startDate, schedule, i);
    if (hardEnd !== undefined && date > hardEnd) break;
    const dn = dayNumber(date);
    if (dn > toDay) break;
    fired++; // counts against the schedule's own `count` cap from its start
    if (dn >= fromDay) out.push(date);
  }
  return out;
}

/**
 * Materialize a template into `{ date, payload }` occurrences over a window.
 * The caller maps each into a posting keyed by `${template.id}:${date}`.
 */
export function materializeRecurring<T>(
  template: RecurringTemplate<T>,
  from: string,
  to: string,
): Array<{ date: string; payload: T; idempotencyKey: string }> {
  return occurrencesBetween(template.schedule, from, to).map((date) => ({
    date,
    payload: template.payload,
    idempotencyKey: `${template.id}:${date}`,
  }));
}
