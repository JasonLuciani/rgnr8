import { Money, sumMoney, type Currency } from "@rgnr8/ledger-kernel";
import { ageItems } from "./aging.js";
import type { ARSubledger } from "./ar.js";
import type { APSubledger } from "./ap.js";

/**
 * Per-party AR/AP aging reports — the detail an owner reads at month-end ("who
 * owes me, and how late"). The subledgers already expose a single rolled-up
 * aging; this breaks it out by customer/vendor, one row each with the same
 * bucket columns, plus a totals row that ties back to the control total.
 */

export interface AgingReportRow {
  readonly partyId: string;
  /** Bucket amounts aligned to {@link AgingReport.bucketLabels}. */
  readonly buckets: readonly Money[];
  readonly total: Money;
}

export interface AgingReport {
  readonly asOf: string;
  readonly bucketLabels: readonly string[];
  readonly rows: readonly AgingReportRow[];
  /** Column totals across all parties, aligned to `bucketLabels`. */
  readonly columnTotals: readonly Money[];
  readonly grandTotal: Money;
}

interface PartyItem {
  readonly partyId: string;
  readonly dueDate: string;
  readonly openAmount: Money;
}

function buildAgingReport(items: readonly PartyItem[], asOf: string, currency: Currency): AgingReport {
  const byParty = new Map<string, PartyItem[]>();
  for (const it of items) {
    if (it.openAmount.isZero()) continue;
    const list = byParty.get(it.partyId) ?? [];
    list.push(it);
    byParty.set(it.partyId, list);
  }

  // Derive bucket labels from an empty aging (deterministic label set/order).
  const labels = ageItems([], asOf, currency).buckets.map((b) => b.label);
  const zero = Money.zero(currency);
  const columnTotals = labels.map(() => zero);
  const totalsMutable = [...columnTotals];

  const rows: AgingReportRow[] = [];
  const partyIds = [...byParty.keys()].sort();
  for (const partyId of partyIds) {
    const aging = ageItems(byParty.get(partyId)!, asOf, currency);
    const buckets = aging.buckets.map((b) => b.amount);
    for (let i = 0; i < buckets.length; i++) totalsMutable[i] = totalsMutable[i]!.plus(buckets[i]!);
    rows.push({ partyId, buckets, total: aging.total });
  }

  return {
    asOf,
    bucketLabels: labels,
    rows,
    columnTotals: totalsMutable,
    grandTotal: sumMoney(rows.map((r) => r.total), currency),
  };
}

/** The serializable `aging/1` contract — what an owner-facing surface renders. */
export interface AgingReportJson {
  readonly contract: "aging/1";
  readonly kind: "AR" | "AP";
  readonly as_of: string;
  readonly bucket_labels: readonly string[];
  readonly rows: ReadonlyArray<{
    readonly party_id: string;
    readonly buckets_minor: readonly string[];
    readonly total_minor: string;
  }>;
  readonly column_totals_minor: readonly string[];
  readonly grand_total_minor: string;
}

/** Serialize an aging report to the `aging/1` contract (minor-unit strings). */
export function agingReportJson(report: AgingReport, kind: "AR" | "AP"): AgingReportJson {
  return {
    contract: "aging/1",
    kind,
    as_of: report.asOf,
    bucket_labels: report.bucketLabels,
    rows: report.rows.map((r) => ({
      party_id: r.partyId,
      buckets_minor: r.buckets.map((b) => b.minorUnits.toString()),
      total_minor: r.total.minorUnits.toString(),
    })),
    column_totals_minor: report.columnTotals.map((c) => c.minorUnits.toString()),
    grand_total_minor: report.grandTotal.minorUnits.toString(),
  };
}

/** AR aging by customer, as of a date. */
export function arAgingReport(ar: ARSubledger, asOf: string, currency: Currency): AgingReport {
  const items = ar.openInvoices().map((i) => ({ partyId: i.customerId, dueDate: i.dueDate, openAmount: i.openAmount }));
  return buildAgingReport(items, asOf, currency);
}

/** AP aging by vendor, as of a date. */
export function apAgingReport(ap: APSubledger, asOf: string, currency: Currency): AgingReport {
  const items = ap.openBills().map((b) => ({ partyId: b.vendorId, dueDate: b.dueDate, openAmount: b.openAmount }));
  return buildAgingReport(items, asOf, currency);
}
