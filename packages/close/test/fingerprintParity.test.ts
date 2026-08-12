import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { fingerprintContent, type FinancialPackageContent } from "../src/index.js";

/**
 * Cross-language canonicalization parity.
 *
 * The financial-package fingerprint crosses the TS↔Python boundary: `@rgnr8/close`
 * seals a package and the Python web surface (`rgnr8_web.financial_package`)
 * independently re-verifies it. That only holds if BOTH sides canonicalize
 * byte-for-byte identically before hashing. This pins that contract to a
 * committed golden vector (`fingerprint_vector.json`) with tricky content —
 * nested arrays of objects, non-ASCII text, and deliberately unsorted keys.
 *
 * The identical vector is asserted from Python in
 * `packages/web/tests/test_fingerprint_parity.py`; both sides check the SAME
 * `canonical` string and `sha256`, so if both suites are green the two
 * serializers are byte-identical.
 */

interface ParityVector {
  readonly content: FinancialPackageContent & Record<string, unknown>;
  readonly canonical: string;
  readonly sha256: string;
}

const VECTOR: ParityVector = JSON.parse(
  readFileSync(fileURLToPath(new URL("./fingerprint_vector.json", import.meta.url)), "utf8"),
) as ParityVector;

/**
 * A copy of the shipping `canonicalize` (financialPackage.ts) — it is not
 * exported, so we mirror it here purely to observe the exact bytes it produces
 * and prove they equal the committed golden string. `fingerprintContent` below
 * exercises the real shipping function end-to-end.
 */
function canonicalize(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonicalize).join(",")}]`;
  const obj = value as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  return `{${keys.map((k) => `${JSON.stringify(k)}:${canonicalize(obj[k])}`).join(",")}}`;
}

test("TS canonicalization matches the committed cross-language golden bytes", () => {
  const canonical = canonicalize(VECTOR.content);
  // Byte-identical canonical string shared with the Python serializer.
  assert.equal(canonical, VECTOR.canonical);
});

test("shipping fingerprintContent reproduces the committed SHA-256", () => {
  // The real exported function (which internally canonicalizes) must yield the
  // golden hash — tying the shipping code to the golden canonical bytes.
  assert.equal(fingerprintContent(VECTOR.content), VECTOR.sha256);
});

test("the golden canonical string hashes to the golden SHA-256", () => {
  const hash = createHash("sha256").update(VECTOR.canonical, "utf8").digest("hex");
  assert.equal(hash, VECTOR.sha256);
});
