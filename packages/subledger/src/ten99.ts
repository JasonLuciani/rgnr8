import { Money, sumMoney, type Currency } from "@rgnr8/ledger-kernel";
import type { Vendor } from "./masterdata.js";

/**
 * 1099-NEC tracking and generation.
 *
 * A service business must issue a 1099-NEC to each unincorporated contractor it
 * paid $600 or more in nonemployee compensation during the calendar year. This
 * aggregates the year's payments per vendor, keeps only the vendors flagged as
 * 1099 with a tax id, applies the $600 threshold, and emits the filing rows
 * (box 1). Amounts are exact `Money`; the threshold and totals never touch a
 * float.
 */

/** A cash payment to a vendor (already filtered to the reportable ones). */
export interface VendorPayment {
  readonly vendorId: string;
  readonly date: string; // ISO
  readonly amount: Money; // positive amount paid
}

/** The default IRS 1099-NEC reporting threshold. */
export const DEFAULT_1099_THRESHOLD_MINOR = 60000n; // $600.00

export interface Form1099Row {
  readonly vendorId: string;
  readonly vendorName: string;
  readonly taxId: string;
  /** Box 1 — nonemployee compensation for the year. */
  readonly nonemployeeCompensation: Money;
}

export interface Form1099Report {
  readonly year: number;
  readonly rows: readonly Form1099Row[];
  /** Vendors flagged 1099 but missing a tax id — must be collected (W-9). */
  readonly missingTaxId: readonly string[];
  /** 1099 vendors paid under the threshold (tracked, not filed). */
  readonly belowThreshold: readonly string[];
  readonly total: Money;
}

function yearOf(iso: string): number {
  return Number(iso.slice(0, 4));
}

/**
 * Build the 1099-NEC report for a calendar year from vendor master data and the
 * year's payments. Only vendors with `is1099` participate; a payment to a
 * non-1099 vendor is ignored. Vendors at/over the threshold with a tax id
 * produce a filing row; ones missing a tax id or under the threshold are
 * surfaced separately so nothing is silently dropped.
 */
export function build1099Report(
  vendors: readonly Vendor[],
  payments: readonly VendorPayment[],
  year: number,
  currency: Currency,
  thresholdMinor: bigint = DEFAULT_1099_THRESHOLD_MINOR,
): Form1099Report {
  const vendorById = new Map(vendors.map((v) => [v.id, v]));

  // Sum this year's payments per 1099 vendor.
  const totals = new Map<string, Money>();
  for (const p of payments) {
    if (yearOf(p.date) !== year) continue;
    const vendor = vendorById.get(p.vendorId);
    if (!vendor?.is1099) continue;
    totals.set(p.vendorId, (totals.get(p.vendorId) ?? Money.zero(currency)).plus(p.amount));
  }

  const rows: Form1099Row[] = [];
  const missingTaxId: string[] = [];
  const belowThreshold: string[] = [];

  for (const [vendorId, amount] of totals) {
    const vendor = vendorById.get(vendorId)!;
    if (amount.minorUnits < thresholdMinor) {
      belowThreshold.push(vendorId);
      continue;
    }
    if (!vendor.taxId || vendor.taxId.trim() === "") {
      missingTaxId.push(vendorId);
      continue;
    }
    rows.push({
      vendorId,
      vendorName: vendor.name,
      taxId: vendor.taxId,
      nonemployeeCompensation: amount,
    });
  }

  rows.sort((a, b) => a.vendorName.localeCompare(b.vendorName));
  missingTaxId.sort();
  belowThreshold.sort();

  return {
    year,
    rows,
    missingTaxId,
    belowThreshold,
    total: sumMoney(rows.map((r) => r.nonemployeeCompensation), currency),
  };
}
