import { test } from "node:test";
import assert from "node:assert/strict";
import { fieldCipherFromEnv, maskTaxId } from "../src/fieldCipher.js";

test("encrypts at rest and decrypts back (with a key)", () => {
  const c = fieldCipherFromEnv({ RGNR8_SECRET_KEY: "a-secret-key" });
  const ct = c.encrypt("12-3456789");
  assert.ok(ct.startsWith("v1:"));           // stored form is versioned ciphertext
  assert.ok(!ct.includes("3456789"));        // the TIN is not in the stored value
  assert.equal(c.decrypt(ct), "12-3456789"); // round-trips
  // random IV → same plaintext encrypts to different ciphertext each time
  assert.notEqual(c.encrypt("12-3456789"), c.encrypt("12-3456789"));
});

test("passthrough with no key; legacy plaintext still reads", () => {
  const nul = fieldCipherFromEnv({});
  assert.equal(nul.encrypt("12-3456789"), "12-3456789");
  assert.equal(nul.decrypt("12-3456789"), "12-3456789");
  // a keyed cipher reading a legacy (unprefixed) plaintext value returns it as-is
  const c = fieldCipherFromEnv({ RGNR8_SECRET_KEY: "k" });
  assert.equal(c.decrypt("legacy-plaintext"), "legacy-plaintext");
});

test("a tampered ciphertext is rejected (GCM auth tag)", () => {
  const c = fieldCipherFromEnv({ RGNR8_SECRET_KEY: "k" });
  const ct = c.encrypt("999-99-9999");
  const bad = ct.slice(0, -2) + (ct.endsWith("AA") ? "BB" : "AA");
  assert.throws(() => c.decrypt(bad));
});

test("maskTaxId keeps only the last four", () => {
  assert.equal(maskTaxId("12-3456789"), "***-**-6789");
  assert.equal(maskTaxId("123456789"), "***-**-6789");
  assert.equal(maskTaxId(""), "");
});
