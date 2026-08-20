/**
 * Field-level encryption at rest for sensitive columns (e.g. a vendor's TIN/EIN).
 *
 * AES-256-GCM with a per-value random IV and an authentication tag, keyed from
 * `RGNR8_SECRET_KEY` (the same secret the Python side uses for QBO tokens). The
 * stored form is `v1:<base64(iv|tag|ciphertext)>`; the `v1:` prefix lets a value
 * written before encryption existed (legacy plaintext) still read back — it is
 * re-encrypted on its next write. With no key set, the cipher is a passthrough
 * (dev/tests), so behaviour is unchanged until a key is provided.
 */

import { createCipheriv, createDecipheriv, randomBytes, scryptSync } from "node:crypto";

export interface FieldCipher {
  encrypt(plaintext: string): string;
  decrypt(stored: string): string;
}

const PREFIX = "v1:";

class NullFieldCipher implements FieldCipher {
  encrypt(plaintext: string): string {
    return plaintext;
  }
  // Still decrypts real ciphertext if a key later appears? No — without a key we
  // can't. A value written while a key existed can only be read with that key.
  decrypt(stored: string): string {
    return stored;
  }
}

class AesGcmFieldCipher implements FieldCipher {
  private readonly key: Buffer;
  constructor(secret: string) {
    // Derive a 32-byte key from the configured secret (a fixed salt is fine — the
    // secret is the entropy; per-value IVs provide semantic security).
    this.key = scryptSync(secret, "rgnr8-field-cipher", 32);
  }
  encrypt(plaintext: string): string {
    if (!plaintext) return plaintext; // don't expand empty values
    const iv = randomBytes(12);
    const c = createCipheriv("aes-256-gcm", this.key, iv);
    const ct = Buffer.concat([c.update(plaintext, "utf8"), c.final()]);
    const tag = c.getAuthTag();
    return PREFIX + Buffer.concat([iv, tag, ct]).toString("base64");
  }
  decrypt(stored: string): string {
    if (!stored.startsWith(PREFIX)) return stored; // legacy plaintext, read as-is
    const raw = Buffer.from(stored.slice(PREFIX.length), "base64");
    const iv = raw.subarray(0, 12);
    const tag = raw.subarray(12, 28);
    const ct = raw.subarray(28);
    const d = createDecipheriv("aes-256-gcm", this.key, iv);
    d.setAuthTag(tag);
    return Buffer.concat([d.update(ct), d.final()]).toString("utf8");
  }
}

export function fieldCipherFromEnv(env: NodeJS.ProcessEnv = process.env): FieldCipher {
  const secret = (env["RGNR8_SECRET_KEY"] ?? "").trim();
  return secret ? new AesGcmFieldCipher(secret) : new NullFieldCipher();
}

/** Mask a TIN/EIN for display: keep the last 4, hide the rest (e.g. `***-**-6789`). */
export function maskTaxId(taxId: string): string {
  const digits = taxId.replace(/\D/g, "");
  if (digits.length < 4) return taxId ? "***" : "";
  const last4 = digits.slice(-4);
  return `***-**-${last4}`;
}
