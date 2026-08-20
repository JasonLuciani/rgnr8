import { test } from "node:test";
import assert from "node:assert/strict";
import { Money, USD, asAccountId } from "@rgnr8/ledger-kernel";
import { makeTrialBalance, type TrialBalanceEntry } from "@rgnr8/financial-statements";
import { translateTrialBalance, type TranslationRatesMicro } from "../src/index.js";

/**
 * Unit tests for the ASC 830 / IAS 21 translation primitive: P&L at the average
 * rate, monetary items at the current rate, equity at the historical rate, with a
 * per-line rate audit and a roll-forward CTA. Rates are ×1e6 integers.
 */

const M = 1_000_000n;
const usd = (minor: bigint) => Money.fromMinorUnits(minor, USD);

function entry(code: string, cls: TrialBalanceEntry["accountClass"], signedMinor: bigint): TrialBalanceEntry {
  return { accountId: asAccountId(`acct:${code}`), code, name: code, accountClass: cls, signed: usd(signedMinor) };
}

// A tiny foreign trial balance (debit-positive signed): cash 1000 (asset),
// equity -600, revenue -500, expense +100. Sums to zero (a sound ledger).
function foreignTb() {
  return makeTrialBalance(USD, [
    entry("1000", "asset", 1000_00n),
    entry("3000", "equity", -600_00n),
    entry("4000", "revenue", -500_00n),
    entry("5000", "expense", 100_00n),
  ]);
}

test("P&L translates at the AVERAGE rate; monetary at current; equity at historical", () => {
  const rates: TranslationRatesMicro = {
    currentMicro: 2n * M, // ×2 for assets/liabilities
    equityMicro: 3n * M, // ×3 for equity
    averageMicro: 4n * M, // ×4 for revenue/expense
    priorCtaMinor: 0n,
  };
  const t = translateTrialBalance(foreignTb(), USD, "sub", rates);

  const by = new Map(t.lines.map((l) => [l.code, l]));
  assert.equal(by.get("1000")!.rateClass, "current");
  assert.equal(by.get("3000")!.rateClass, "historical");
  assert.equal(by.get("4000")!.rateClass, "average");
  assert.equal(by.get("5000")!.rateClass, "average");

  const signedOf = (code: string) => t.tb.entries.find((e) => e.code === code)!.signed.minorUnits;
  assert.equal(signedOf("1000"), 2000_00n); // 1000 × 2 (current)
  assert.equal(signedOf("3000"), -1800_00n); // -600 × 3 (historical)
  assert.equal(signedOf("4000"), -2000_00n); // -500 × 4 (average)
  assert.equal(signedOf("5000"), 400_00n); // 100 × 4 (average)

  // The translated books still foot: the CTA plug absorbs the rate gaps.
  const total = t.tb.entries.reduce((a, e) => a + e.signed.minorUnits, 0n);
  assert.equal(total, 0n);
  // cumulative CTA = -(sum before plug) = -(2000-1800-2000+400) = 1400 (credit)
  assert.equal(t.cta.cumulativeMinor, "140000");
});

test("a single rate across all classes yields a zero CTA (same-currency behavior)", () => {
  const rates: TranslationRatesMicro = { currentMicro: M, equityMicro: M, averageMicro: M, priorCtaMinor: 0n };
  const t = translateTrialBalance(foreignTb(), USD, "sub", rates);
  assert.equal(t.cta.cumulativeMinor, "0");
  // No CTA line is appended when the books already foot.
  assert.equal(t.tb.entries.find((e) => e.code === "3990"), undefined);
});

test("CTA rolls forward: current-period movement = cumulative − prior", () => {
  const rates: TranslationRatesMicro = {
    currentMicro: 2n * M, equityMicro: 3n * M, averageMicro: 4n * M,
    priorCtaMinor: 100_00n, // $100 credit CTA carried in from prior periods
  };
  const t = translateTrialBalance(foreignTb(), USD, "sub", rates);
  assert.equal(t.cta.cumulativeMinor, "140000"); // total on the sheet, unchanged
  assert.equal(t.cta.priorMinor, "10000");
  assert.equal(t.cta.currentPeriodMinor, "130000"); // 1400 − 100
  // The CTA equity line still carries the full cumulative amount (books foot).
  const cta = t.tb.entries.find((e) => e.code === "3990")!;
  assert.equal(cta.signed.minorUnits, 140000n);
});
