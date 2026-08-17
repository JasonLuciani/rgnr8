import { Money, type Currency } from "./money.js";
import {
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  type AccountId,
  type JournalLineInput,
  type PostCommand,
  type Provenance,
} from "./types.js";

/**
 * Multi-currency support: an FX rate table, exact conversion, and period-end
 * revaluation of foreign-currency monetary balances with the resulting
 * unrealized gain/loss.
 *
 * Rates are decimal strings (functional units per one foreign unit) so they stay
 * exact — no binary floats. Conversion multiplies the foreign minor-unit amount
 * by the rate and rounds half-up to the functional currency's scale, using only
 * bigint arithmetic. Revaluation compares a monetary account's *current*
 * functional value (foreign balance × today's rate) to its *booked* functional
 * value (what it was recorded at) and books the difference to an FX gain/loss
 * account — the standard ASC 830 / IAS 21 remeasurement.
 */

const DECIMAL_RE = /^(-)?(\d+)(?:\.(\d+))?$/;

/** Parse a decimal string into (integer numerator, scale) — e.g. "1.2345" -> (12345n, 4). */
function parseDecimal(s: string): { num: bigint; scale: number } {
  const m = DECIMAL_RE.exec(s.trim());
  if (!m) throw new Error(`Not a valid rate: "${s}"`);
  const neg = m[1] === "-";
  const intPart = m[2] ?? "0";
  const frac = m[3] ?? "";
  const num = BigInt(intPart + frac) * (neg ? -1n : 1n);
  return { num, scale: frac.length };
}

function pow10(n: number): bigint {
  return 10n ** BigInt(n);
}

/** Divide two bigints, rounding half away from zero. */
function divRoundHalfUp(numerator: bigint, denominator: bigint): bigint {
  if (denominator < 0n) return divRoundHalfUp(-numerator, -denominator);
  const neg = numerator < 0n;
  const n = neg ? -numerator : numerator;
  const q = n / denominator;
  const r = n % denominator;
  const rounded = r * 2n >= denominator ? q + 1n : q;
  return neg ? -rounded : rounded;
}

/**
 * Convert a Money amount into `to` at `rateDecimal` (functional units of `to`
 * per one unit of the source currency). Exact, round-half-up to `to`'s scale.
 */
export function convert(amount: Money, to: Currency, rateDecimal: string): Money {
  const { num: rateNum, scale: rateScale } = parseDecimal(rateDecimal);
  // functionalMinor = foreignMinor * rate * 10^toScale / 10^fromScale
  // rate = rateNum / 10^rateScale
  const numerator = amount.minorUnits * rateNum * pow10(to.scale);
  const denominator = pow10(rateScale) * pow10(amount.currency.scale);
  const minor = divRoundHalfUp(numerator, denominator);
  return Money.fromMinorUnits(minor, to);
}

export interface FxRate {
  /** Source currency code (the foreign currency). */
  readonly from: string;
  /** Target currency code (the functional/reporting currency). */
  readonly to: string;
  readonly asOf: string; // ISO date the rate is effective on/from
  readonly rate: string; // decimal, `to` units per one `from` unit
}

/** Reciprocal of a decimal rate, to `digits` places (for inverse lookups). */
function reciprocal(rate: string, digits = 10): string {
  const { num, scale } = parseDecimal(rate);
  if (num === 0n) throw new Error("cannot invert a zero rate");
  // 1 / (num/10^scale) = 10^scale / num, expressed to `digits` decimals.
  const numerator = pow10(scale) * pow10(digits);
  const q = divRoundHalfUp(numerator, num);
  const s = q.toString().padStart(digits + 1, "0");
  const intPart = s.slice(0, s.length - digits) || "0";
  const frac = s.slice(s.length - digits);
  return `${intPart}.${frac}`;
}

export class FxRateTable {
  private readonly rates: FxRate[] = [];

  add(rate: FxRate): this {
    this.rates.push(rate);
    return this;
  }

  /** Most recent rate for from→to effective on/before `asOf`, direct or inverted. */
  rateOn(from: string, to: string, asOf: string): string | undefined {
    if (from === to) return "1";
    const direct = this.latest(from, to, asOf);
    if (direct) return direct.rate;
    const inverse = this.latest(to, from, asOf);
    if (inverse) return reciprocal(inverse.rate);
    return undefined;
  }

  private latest(from: string, to: string, asOf: string): FxRate | undefined {
    let best: FxRate | undefined;
    for (const r of this.rates) {
      if (r.from !== from || r.to !== to) continue;
      if (r.asOf > asOf) continue;
      if (!best || r.asOf > best.asOf) best = r;
    }
    return best;
  }
}

export interface RevaluationInput {
  /** The account's balance in its foreign currency. */
  readonly foreignBalance: Money;
  /** What that balance is currently carried at in the functional currency. */
  readonly bookedFunctional: Money;
  /** The functional/reporting currency. */
  readonly functional: Currency;
  /** Current period-end rate (functional per foreign unit). */
  readonly rate: string;
}

export interface Revaluation {
  readonly currentFunctional: Money;
  readonly bookedFunctional: Money;
  /** currentFunctional − bookedFunctional. Positive = gain (for an asset). */
  readonly unrealizedGainLoss: Money;
}

/** Compute the unrealized FX gain/loss on a monetary balance. */
export function revalue(input: RevaluationInput): Revaluation {
  const currentFunctional = convert(input.foreignBalance, input.functional, input.rate);
  const unrealizedGainLoss = currentFunctional.minus(input.bookedFunctional);
  return { currentFunctional, bookedFunctional: input.bookedFunctional, unrealizedGainLoss };
}

export interface FxRevaluationCommandOptions {
  readonly tenantId: string;
  readonly entryDate: string;
  /** The monetary account being remeasured (its functional carrying value). */
  readonly monetaryAccountId: AccountId;
  /** FX gain/loss P&L account. */
  readonly fxGainLossAccountId: AccountId;
  readonly provenance: Provenance;
  readonly idempotencyKey?: string;
  readonly memo?: string;
}

/**
 * Build the balanced journal that books an FX revaluation. For a gain on an
 * asset (current > booked): Dr asset / Cr FX gain. For a loss: Cr asset / Dr FX
 * loss. Returns undefined when there's no adjustment (gain/loss is zero).
 */
export function fxRevaluationCommand(
  reval: Revaluation,
  opts: FxRevaluationCommandOptions,
): PostCommand | undefined {
  const delta = reval.unrealizedGainLoss;
  if (delta.isZero()) return undefined;
  const functional = delta.currency;
  const gain = !delta.isNegative();
  const amount = gain ? delta : delta.negate();

  const lines: JournalLineInput[] = gain
    ? [
        { accountId: opts.monetaryAccountId, side: "DEBIT", amount, memo: "FX revaluation" },
        { accountId: opts.fxGainLossAccountId, side: "CREDIT", amount, memo: "Unrealized FX gain" },
      ]
    : [
        { accountId: opts.fxGainLossAccountId, side: "DEBIT", amount, memo: "Unrealized FX loss" },
        { accountId: opts.monetaryAccountId, side: "CREDIT", amount, memo: "FX revaluation" },
      ];

  return {
    tenantId: asTenantId(opts.tenantId),
    idempotencyKey: asIdempotencyKey(opts.idempotencyKey ?? `fxreval:${opts.monetaryAccountId}:${opts.entryDate}`),
    periodKey: asPeriodKey(opts.entryDate.slice(0, 7)),
    currency: functional,
    entryDate: opts.entryDate,
    memo: opts.memo ?? "FX revaluation",
    provenance: opts.provenance,
    lines,
  };
}
