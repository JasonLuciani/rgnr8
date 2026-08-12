import type { Direction, NormalizedInput } from "./types.js";

/** Stable djb2 hash (hex) — deterministic, no crypto dependency needed here. */
function djb2(input: string): string {
  let h = 5381n;
  for (let i = 0; i < input.length; i++) {
    h = ((h * 33n) ^ BigInt(input.charCodeAt(i))) & 0xffffffffffffffffn;
  }
  return h.toString(16).padStart(16, "0");
}

/**
 * A stable dedupe key for a normalized record.
 *
 * When the provider supplies a stable external id we key on it (provider +
 * account + externalId). Otherwise we fall back to a content hash of the
 * fields that identify a transaction, so re-fetching the same feed is
 * idempotent either way.
 */
export function dedupeKey(n: NormalizedInput): string {
  if (n.externalId && n.externalId.length > 0) {
    return `${n.source.provider}:${n.accountId}:${n.externalId}`;
  }
  const basis = [
    n.source.provider,
    n.accountId,
    n.date,
    n.amount.currency.code,
    n.amount.minorUnits.toString(),
    n.description,
  ].join("|");
  return `${n.source.provider}:${n.accountId}:h${djb2(basis)}`;
}

export function directionOf(n: NormalizedInput): Direction {
  return n.amount.minorUnits >= 0n ? "INFLOW" : "OUTFLOW";
}
