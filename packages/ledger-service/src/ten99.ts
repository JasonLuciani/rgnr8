import { Money, type Currency, type TenantId } from "@rgnr8/ledger-kernel";
import {
  DEFAULT_1099_THRESHOLD_MINOR,
  build1099Report,
  type VendorPayment,
} from "@rgnr8/subledger";
import type { LedgerBackend } from "./backend.js";

/**
 * 1099-NEC — what you paid contractors, and what the IRS wants to know about it.
 *
 * The mechanics are simple: a vendor flagged as a contractor, everything paid to
 * them in a calendar year, and a $600 threshold above which a form must be
 * filed. What makes this worth building carefully is *when* it goes wrong.
 *
 * It goes wrong in January. The threshold is crossed in March, the W-9 is never
 * collected, and nobody discovers it until the forms are due — at which point
 * the contractor is unreachable and the business is filing without a TIN. So
 * this reports two things the engine already distinguishes and most software
 * buries: **who is over the threshold with no tax id on file**, and **who is
 * under it but still being tracked**, because the second group becomes the first
 * one with one more invoice.
 *
 * There is a third gap this reports honestly rather than papering over. Payments
 * are counted from bill payments, because that is where a payment is definitely
 * linked to a vendor. A contractor paid straight from the bank feed has no such
 * link — so any accepted bank line whose counterparty looks like a 1099 vendor,
 * but which was never matched to a bill, is surfaced as *possibly missing*. It
 * is a prompt, not a number: guessing would produce a total nobody can defend.
 */

export class Ten99Error extends Error {}

export interface Ten99Context {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
}

function requireYear(raw: unknown): number {
  const year = Number(String(raw ?? "").trim());
  if (!Number.isInteger(year) || year < 1900 || year > 3000) {
    throw new Ten99Error("year must be a four-digit calendar year");
  }
  return year;
}

/** Normalize a name for comparison: case and punctuation are not identity. */
function fold(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

export interface Ten99Json {
  readonly contract: "form-1099/1";
  readonly year: number;
  readonly currency: string;
  readonly threshold_minor: string;
  readonly rows: ReadonlyArray<{
    readonly vendor_id: string;
    readonly vendor_name: string;
    readonly tax_id: string;
    readonly amount_minor: string;
    readonly needs_w9: boolean;
  }>;
  readonly total_minor: string;
  /** Flagged, over the threshold, and no tax id — a form cannot be filed. */
  readonly missing_tax_id: readonly string[];
  /** Flagged and tracked, but under the threshold this year. */
  readonly below_threshold: ReadonlyArray<{
    readonly vendor_id: string;
    readonly vendor_name: string;
    readonly amount_minor: string;
    readonly short_by_minor: string;
  }>;
  /** Bank payments that look like they belong to a contractor but weren't billed. */
  readonly possibly_missing: ReadonlyArray<{
    readonly vendor_name: string;
    readonly date: string;
    readonly amount_minor: string;
    readonly description: string;
  }>;
}

/**
 * The year's 1099-NEC figures for every vendor flagged as a contractor.
 */
export async function ten99Report(
  ctx: Ten99Context, yearRaw: unknown,
): Promise<Ten99Json> {
  const year = requireYear(yearRaw);
  const tenant = String(ctx.tenant);
  const docs = ctx.backend.documents();

  const vendors = await docs.listParties(tenant, "vendor");
  const flagged = vendors.filter((v) => v.is1099 === true);

  // Payments, from where a payment is definitely tied to a vendor: a payment
  // recorded against that vendor's bill.
  const flaggedIds = new Set(flagged.map((v) => v.id));
  const bills = await docs.listDocs(tenant, "bill");
  const billVendor = new Map(bills.map((b) => [b.id, b.partyId]));
  const payments: VendorPayment[] = [];
  for (const bill of bills) {
    if (!flaggedIds.has(bill.partyId)) continue;   // the landlord is not a contractor
    for (const p of await docs.listPayments(tenant, "bill", bill.id)) {
      const vendorId = billVendor.get(p.docId);
      if (!vendorId) continue;
      payments.push({
        vendorId,
        date: p.date,
        amount: Money.fromMinorUnits(BigInt(p.amountMinor), ctx.currency),
      });
    }
  }

  const report = build1099Report(
    flagged.map((v) => ({
      id: v.id,
      name: v.name,
      ...(v.is1099 !== undefined ? { is1099: v.is1099 } : {}),
      ...(v.taxId ? { taxId: v.taxId } : {}),
    })),
    payments,
    year,
    ctx.currency,
  );

  const paidByVendor = new Map<string, bigint>();
  for (const p of payments) {
    if (Number(p.date.slice(0, 4)) !== year) continue;
    paidByVendor.set(
      p.vendorId, (paidByVendor.get(p.vendorId) ?? 0n) + p.amount.minorUnits,
    );
  }
  const byId = new Map(flagged.map((v) => [v.id, v]));

  // Every vendor over the threshold is listed, INCLUDING the ones with no tax
  // id. The engine excludes those from a filable report, correctly — but the
  // amount is exactly what makes a missing W-9 urgent, and hiding it means the
  // warning reads as bureaucracy instead of "you owe this person a form for
  // $4,000 and cannot file it".
  const rows = [...paidByVendor.entries()]
    .filter(([, paid]) => paid >= DEFAULT_1099_THRESHOLD_MINOR)
    .map(([vendorId, paid]) => {
      const v = byId.get(vendorId);
      return {
        vendor_id: vendorId,
        vendor_name: v?.name ?? vendorId,
        tax_id: v?.taxId ?? "",
        amount_minor: paid.toString(),
        needs_w9: !v?.taxId,
      };
    })
    .sort((a, b) => a.vendor_name.localeCompare(b.vendor_name));
  const total = rows.reduce((acc, r) => acc + BigInt(r.amount_minor), 0n);

  // What each below-threshold vendor was paid, and how far short they are —
  // "$540, $60 short" is actionable in a way that "below threshold" is not.
  const belowThreshold = report.belowThreshold.map((vendorId) => {
    const paid = paidByVendor.get(vendorId) ?? 0n;
    return {
      vendor_id: vendorId,
      vendor_name: byId.get(vendorId)?.name ?? vendorId,
      amount_minor: paid.toString(),
      short_by_minor: (DEFAULT_1099_THRESHOLD_MINOR - paid).toString(),
    };
  });

  // The honest gap: money that left the bank to a name that looks like a
  // contractor, never run through a bill, so never counted above.
  const contractorNames = new Map(flagged.map((v) => [fold(v.name), v.name]));
  const possiblyMissing: Ten99Json["possibly_missing"][number][] = [];
  if (contractorNames.size > 0) {
    for (const txn of await ctx.backend.feed().listTxns(tenant)) {
      if (txn.status !== "POSTED") continue;              // matched ones are counted
      if (txn.date.slice(0, 4) !== String(year)) continue;
      if (!txn.amountMinor.startsWith("-")) continue;     // money out only
      const name = contractorNames.get(fold(txn.counterparty || txn.description));
      if (!name) continue;
      possiblyMissing.push({
        vendor_name: name,
        date: txn.date,
        amount_minor: txn.amountMinor,
        description: txn.description,
      });
    }
  }

  return {
    contract: "form-1099/1",
    year,
    currency: ctx.currency.code,
    threshold_minor: DEFAULT_1099_THRESHOLD_MINOR.toString(),
    rows,
    total_minor: total.toString(),
    missing_tax_id: report.missingTaxId,
    below_threshold: belowThreshold,
    possibly_missing: possiblyMissing.sort((a, b) => a.date.localeCompare(b.date)),
  };
}
