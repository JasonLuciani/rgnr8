import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD, defineCurrency, sumMoney, CurrencyMismatchError } from "../src/index.js";

test("money adds exactly with no floating-point error (0.1 + 0.2 = 0.3)", () => {
  const total = Money.fromDecimal("0.10", USD).plus(Money.fromDecimal("0.20", USD));
  assert.equal(total.minorUnits, 30n);
  assert.equal(total.toDecimalString(), "0.30");
});

test("fromDecimal parses whole, fractional, and negative values", () => {
  assert.equal(Money.fromDecimal("1234.56", USD).minorUnits, 123456n);
  assert.equal(Money.fromDecimal("1000", USD).minorUnits, 100000n);
  assert.equal(Money.fromDecimal("-42.00", USD).minorUnits, -4200n);
  assert.equal(Money.fromDecimal("0.05", USD).minorUnits, 5n);
});

test("fromDecimal rejects more precision than the currency allows", () => {
  assert.throws(() => Money.fromDecimal("1.005", USD), /more precision/);
});

test("fromDecimal rejects non-numeric input", () => {
  assert.throws(() => Money.fromDecimal("abc", USD));
  assert.throws(() => Money.fromDecimal("1.2.3", USD));
});

test("toDecimalString round-trips and formats padding correctly", () => {
  assert.equal(Money.fromMinorUnits(5n, USD).toDecimalString(), "0.05");
  assert.equal(Money.fromMinorUnits(-5n, USD).toDecimalString(), "-0.05");
  assert.equal(Money.fromMinorUnits(100000n, USD).toDecimalString(), "1000.00");
  assert.equal(Money.fromMinorUnits(0n, USD).toDecimalString(), "0.00");
});

test("arithmetic across currencies throws", () => {
  const eur = defineCurrency("EUR", 2);
  assert.throws(() => Money.fromDecimal("1.00", USD).plus(Money.fromDecimal("1.00", eur)), CurrencyMismatchError);
});

test("compare / lessThan / greaterThan", () => {
  const a = Money.fromDecimal("10.00", USD);
  const b = Money.fromDecimal("20.00", USD);
  assert.equal(a.compare(b), -1);
  assert.equal(b.compare(a), 1);
  assert.equal(a.compare(a), 0);
  assert.ok(a.lessThan(b));
  assert.ok(b.greaterThan(a));
});

test("negate, abs, sign predicates", () => {
  const m = Money.fromDecimal("-7.50", USD);
  assert.ok(m.isNegative());
  assert.equal(m.negate().toDecimalString(), "7.50");
  assert.equal(m.abs().toDecimalString(), "7.50");
  assert.ok(Money.zero(USD).isZero());
});

test("timesInteger scales exactly", () => {
  assert.equal(Money.fromDecimal("1.11", USD).timesInteger(3).toDecimalString(), "3.33");
});

test("sumMoney totals a list and requires currency when empty", () => {
  const list = [Money.fromDecimal("1.00", USD), Money.fromDecimal("2.50", USD), Money.fromDecimal("0.01", USD)];
  assert.equal(sumMoney(list).toDecimalString(), "3.51");
  assert.equal(sumMoney([], USD).toDecimalString(), "0.00");
  assert.throws(() => sumMoney([]));
});

test("Money instances are frozen (immutable)", () => {
  const m = Money.fromDecimal("1.00", USD) as unknown as { minorUnits: bigint };
  assert.ok(Object.isFrozen(m));
});
