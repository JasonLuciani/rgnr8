/**
 * Money — exact monetary values in integer minor units.
 *
 * INVARIANT: money is never represented as a binary floating-point number.
 * All values are stored as a `bigint` count of the currency's minor unit
 * (e.g. cents for USD). Arithmetic is therefore exact and reproducible.
 */

export interface Currency {
  /** ISO 4217-style code, e.g. "USD". */
  readonly code: string;
  /** Number of minor-unit digits, e.g. 2 for USD (cents). */
  readonly scale: number;
}

/** Registry of known currencies, so a stored currency code can be rehydrated. */
const CURRENCY_REGISTRY = new Map<string, Currency>();

export function defineCurrency(code: string, scale: number): Currency {
  if (!/^[A-Z]{3}$/.test(code)) {
    throw new Error(`Invalid currency code: ${code}`);
  }
  if (!Number.isInteger(scale) || scale < 0 || scale > 8) {
    throw new Error(`Invalid currency scale for ${code}: ${scale}`);
  }
  const existing = CURRENCY_REGISTRY.get(code);
  if (existing) {
    if (existing.scale !== scale) {
      throw new Error(`Currency ${code} already defined with scale ${existing.scale}`);
    }
    return existing;
  }
  const currency = Object.freeze({ code, scale });
  CURRENCY_REGISTRY.set(code, currency);
  return currency;
}

/** Resolve a currency by code. Throws if it has not been defined. */
export function getCurrency(code: string): Currency {
  const c = CURRENCY_REGISTRY.get(code);
  if (!c) throw new Error(`Unknown currency code: ${code}. Define it with defineCurrency() first.`);
  return c;
}

export const USD: Currency = defineCurrency("USD", 2);

export class CurrencyMismatchError extends Error {
  override readonly name = "CurrencyMismatchError";
  constructor(a: Currency, b: Currency) {
    super(`Currency mismatch: ${a.code} vs ${b.code}`);
  }
}

const DECIMAL_RE = /^(-)?(\d+)(?:\.(\d+))?$/;

export class Money {
  private constructor(
    readonly minorUnits: bigint,
    readonly currency: Currency,
  ) {
    Object.freeze(this);
  }

  static fromMinorUnits(amount: bigint | number, currency: Currency): Money {
    const v = typeof amount === "number" ? BigInt(assertSafeInteger(amount)) : amount;
    return new Money(v, currency);
  }

  static zero(currency: Currency): Money {
    return new Money(0n, currency);
  }

  /**
   * Parse an exact decimal string (e.g. "1234.56") into minor units.
   * Rejects more fractional digits than the currency scale rather than
   * silently rounding — precision loss must be an explicit caller decision.
   */
  static fromDecimal(value: string, currency: Currency): Money {
    const m = DECIMAL_RE.exec(value.trim());
    if (!m) throw new Error(`Not a valid decimal amount: "${value}"`);
    const sign = m[1] === "-" ? -1n : 1n;
    const whole = m[2] ?? "0";
    const frac = m[3] ?? "";
    if (frac.length > currency.scale) {
      throw new Error(
        `Amount "${value}" has more precision than ${currency.code} allows (scale ${currency.scale})`,
      );
    }
    const paddedFrac = frac.padEnd(currency.scale, "0");
    const combined = BigInt(whole) * 10n ** BigInt(currency.scale) + BigInt(paddedFrac || "0");
    return new Money(sign * combined, currency);
  }

  plus(other: Money): Money {
    this.assertSameCurrency(other);
    return new Money(this.minorUnits + other.minorUnits, this.currency);
  }

  minus(other: Money): Money {
    this.assertSameCurrency(other);
    return new Money(this.minorUnits - other.minorUnits, this.currency);
  }

  negate(): Money {
    return new Money(-this.minorUnits, this.currency);
  }

  abs(): Money {
    return this.minorUnits < 0n ? this.negate() : this;
  }

  /** Multiply by an integer factor (e.g. quantity). Kept integer-only on purpose. */
  timesInteger(factor: bigint | number): Money {
    const f = typeof factor === "number" ? BigInt(assertSafeInteger(factor)) : factor;
    return new Money(this.minorUnits * f, this.currency);
  }

  isZero(): boolean {
    return this.minorUnits === 0n;
  }

  isPositive(): boolean {
    return this.minorUnits > 0n;
  }

  isNegative(): boolean {
    return this.minorUnits < 0n;
  }

  equals(other: Money): boolean {
    return this.currency.code === other.currency.code && this.minorUnits === other.minorUnits;
  }

  /** -1, 0, or 1. Throws on currency mismatch. */
  compare(other: Money): -1 | 0 | 1 {
    this.assertSameCurrency(other);
    if (this.minorUnits < other.minorUnits) return -1;
    if (this.minorUnits > other.minorUnits) return 1;
    return 0;
  }

  lessThan(other: Money): boolean {
    return this.compare(other) < 0;
  }

  greaterThan(other: Money): boolean {
    return this.compare(other) > 0;
  }

  toDecimalString(): string {
    const neg = this.minorUnits < 0n;
    const abs = neg ? -this.minorUnits : this.minorUnits;
    const s = abs.toString().padStart(this.currency.scale + 1, "0");
    const cut = s.length - this.currency.scale;
    const whole = s.slice(0, cut);
    const frac = this.currency.scale > 0 ? "." + s.slice(cut) : "";
    return `${neg ? "-" : ""}${whole}${frac}`;
  }

  toString(): string {
    return `${this.currency.code} ${this.toDecimalString()}`;
  }

  private assertSameCurrency(other: Money): void {
    if (this.currency.code !== other.currency.code) {
      throw new CurrencyMismatchError(this.currency, other.currency);
    }
  }
}

/** Sum a list of Money, all of the same currency. Empty list requires a currency. */
export function sumMoney(items: readonly Money[], currency?: Currency): Money {
  if (items.length === 0) {
    if (!currency) throw new Error("sumMoney: empty list requires an explicit currency");
    return Money.zero(currency);
  }
  return items.reduce((acc, m) => acc.plus(m));
}

function assertSafeInteger(n: number): number {
  if (!Number.isInteger(n)) throw new Error(`Expected an integer, got ${n}`);
  if (!Number.isSafeInteger(n)) throw new Error(`Integer ${n} exceeds safe range; pass a bigint`);
  return n;
}
