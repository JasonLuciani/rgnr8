import {
  Money,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  type AccountId,
  type JournalLineInput,
  type PostCommand,
} from "@rgnr8/ledger-kernel";

import {
  resolveInvoiceLines,
  sumLines,
  type DocPostContext,
  type InvoiceDoc,
  type ResolveOptions,
  type ResolvedLine,
} from "./documents.js";

/**
 * Sales tax. A rate is held in **parts-per-million** (ppm) for exactness — e.g.
 * 8.25% = 82_500 ppm, 8.375% = 83_750 ppm — so no floating-point rounding ever
 * touches money. Tax on a base is `round-half-up(base_minor * ppm / 1_000_000)`,
 * computed in bigint.
 *
 * A tax code names a rate and whether it's taxable at all (for exempt codes).
 * A taxed invoice posts three ways:
 *   Dr AR (net + tax) / Cr each income line (net) / Cr Sales-Tax-Payable (tax).
 */

export interface TaxRate {
  readonly id: string;
  readonly name: string;
  /** Rate in parts-per-million (8.25% => 82_500). */
  readonly ratePpm: number;
}

export interface TaxCode {
  readonly id: string;
  readonly name: string;
  readonly taxable: boolean;
  /** The rate applied when taxable. */
  readonly rateId?: string;
}

export class TaxError extends Error {}

const PPM = 1_000_000n;

/** Tax on a minor-unit base at a ppm rate, rounded half-up. */
export function taxOnMinor(baseMinor: bigint, ratePpm: number): bigint {
  if (!Number.isInteger(ratePpm) || ratePpm < 0) {
    throw new TaxError(`ratePpm must be a non-negative integer, got ${ratePpm}`);
  }
  const base = baseMinor < 0n ? -baseMinor : baseMinor;
  const taxed = (base * BigInt(ratePpm) + PPM / 2n) / PPM; // round half up
  return baseMinor < 0n ? -taxed : taxed;
}

/** Tax on a Money base at a ppm rate. */
export function taxOnAmount(base: Money, ratePpm: number): Money {
  return Money.fromMinorUnits(taxOnMinor(base.minorUnits, ratePpm), base.currency);
}

/** The taxable subtotal of a set of resolved lines. */
export function taxableBase(lines: readonly ResolvedLine[], currency: Money["currency"]): Money {
  return lines
    .filter((l) => l.taxable)
    .reduce((acc, l) => acc.plus(l.amount), Money.zero(currency));
}

export interface TaxedInvoiceResult {
  readonly command: PostCommand;
  readonly net: Money;
  readonly tax: Money;
  readonly total: Money;
}

/**
 * Post a **taxed** invoice: Dr AR (net + tax) / Cr each income line (net) /
 * Cr Sales-Tax-Payable (tax). Only lines flagged taxable are taxed. A zero rate
 * (or no taxable lines) posts an ordinary invoice with no tax line.
 */
export function taxedInvoiceToPostCommand(
  doc: InvoiceDoc,
  accounts: { readonly arControl: AccountId; readonly salesTaxPayable: AccountId },
  ratePpm: number,
  ctx: DocPostContext,
  opts: ResolveOptions = {},
): TaxedInvoiceResult {
  const lines = resolveInvoiceLines(doc, ctx.currency, opts);
  const net = sumLines(lines, ctx.currency);
  const tax = taxOnAmount(taxableBase(lines, ctx.currency), ratePpm);
  const total = net.plus(tax);

  const journal: JournalLineInput[] = [
    { accountId: accounts.arControl, side: "DEBIT", amount: total },
    ...lines.map((l): JournalLineInput => ({
      accountId: l.accountId,
      side: "CREDIT",
      amount: l.amount,
      ...(l.description !== "" ? { memo: l.description } : {}),
      ...(l.dimensions ? { dimensions: l.dimensions } : {}),
    })),
  ];
  if (!tax.isZero()) {
    journal.push({ accountId: accounts.salesTaxPayable, side: "CREDIT", amount: tax, memo: "Sales tax" });
  }
  // A taxed invoice has TWO credit anchors (income lines + the tax line), so we
  // assemble the command directly rather than via the single-anchor helper.
  const command: PostCommand = {
    tenantId: asTenantId(ctx.tenantId),
    idempotencyKey: asIdempotencyKey(`invoice:${doc.id}`),
    periodKey: asPeriodKey(doc.issueDate.slice(0, 7)),
    currency: ctx.currency,
    entryDate: doc.issueDate,
    provenance: { ...ctx.provenance, mappingVersion: ctx.mappingVersion ?? ctx.provenance.mappingVersion },
    lines: journal,
    ...(doc.memo !== undefined ? { memo: doc.memo } : {}),
  };
  return { command, net, tax, total };
}
