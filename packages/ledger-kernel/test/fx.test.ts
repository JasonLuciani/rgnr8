import { test } from "node:test";
import assert from "node:assert/strict";
import {
  FxRateTable,
  Money,
  USD,
  asAccountId,
  convert,
  defineCurrency,
  fxRevaluationCommand,
  revalue,
  type Provenance,
} from "../src/index.js";

const EUR = defineCurrency("EUR", 2);
const prov: Provenance = {
  sourceSystem: "test", sourceObject: "fx", sourceVersion: "1",
  effectiveDate: "2026-08-31", postedDate: "2026-08-31", ingestedAt: "2026-08-31",
  normalizationVersion: "1", mappingVersion: "1",
};

test("convert multiplies by rate and rounds half-up to target scale", () => {
  // 100.00 EUR at 1.0875 USD/EUR = 108.75 USD
  const usd = convert(Money.fromDecimal("100.00", EUR), USD, "1.0875");
  assert.equal(usd.toDecimalString(), "108.75");
  assert.equal(usd.currency.code, "USD");
  // rounding: 33.33 EUR * 1.10005 = 36.6646665 -> 36.66
  const r = convert(Money.fromDecimal("33.33", EUR), USD, "1.10005");
  assert.equal(r.toDecimalString(), "36.66");
});

test("rate table finds the latest rate on/before a date, and inverts when needed", () => {
  const table = new FxRateTable()
    .add({ from: "EUR", to: "USD", asOf: "2026-08-01", rate: "1.08" })
    .add({ from: "EUR", to: "USD", asOf: "2026-08-20", rate: "1.09" });
  assert.equal(table.rateOn("EUR", "USD", "2026-08-15"), "1.08");
  assert.equal(table.rateOn("EUR", "USD", "2026-08-31"), "1.09");
  // inverse lookup USD->EUR from the EUR->USD rate (~0.9174...)
  const inv = table.rateOn("USD", "EUR", "2026-08-31");
  assert.ok(inv && inv.startsWith("0.91"));
  assert.equal(table.rateOn("USD", "USD", "2026-08-31"), "1");
});

test("revaluation computes unrealized gain and builds a balanced adjustment", () => {
  // A EUR bank account holding 1000.00 EUR, booked at 1080.00 USD.
  // At period end EUR strengthened to 1.09 -> current 1090.00 USD -> +10.00 gain.
  const reval = revalue({
    foreignBalance: Money.fromDecimal("1000.00", EUR),
    bookedFunctional: Money.fromDecimal("1080.00", USD),
    functional: USD,
    rate: "1.09",
  });
  assert.equal(reval.currentFunctional.toDecimalString(), "1090.00");
  assert.equal(reval.unrealizedGainLoss.toDecimalString(), "10.00");

  const cmd = fxRevaluationCommand(reval, {
    tenantId: "acme", entryDate: "2026-08-31",
    monetaryAccountId: asAccountId("gl.eur_bank"),
    fxGainLossAccountId: asAccountId("gl.fx_gain_loss"),
    provenance: prov,
  })!;
  assert.ok(cmd);
  // Dr bank 10 / Cr FX gain 10
  const dr = cmd.lines.find((l) => l.side === "DEBIT")!;
  const cr = cmd.lines.find((l) => l.side === "CREDIT")!;
  assert.equal(String(dr.accountId), "gl.eur_bank");
  assert.equal(dr.amount.toDecimalString(), "10.00");
  assert.equal(String(cr.accountId), "gl.fx_gain_loss");
});

test("no adjustment when the balance is unchanged in functional terms", () => {
  const reval = revalue({
    foreignBalance: Money.fromDecimal("1000.00", EUR),
    bookedFunctional: Money.fromDecimal("1090.00", USD),
    functional: USD,
    rate: "1.09",
  });
  assert.ok(reval.unrealizedGainLoss.isZero());
  assert.equal(
    fxRevaluationCommand(reval, {
      tenantId: "acme", entryDate: "2026-08-31",
      monetaryAccountId: asAccountId("gl.eur_bank"),
      fxGainLossAccountId: asAccountId("gl.fx_gain_loss"),
      provenance: prov,
    }),
    undefined,
  );
});
