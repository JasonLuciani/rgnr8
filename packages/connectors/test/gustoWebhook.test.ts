import { test } from "node:test";
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import {
  InMemoryConnectionStore,
  handleGustoWebhook,
  verifyHmacSignature,
  type Connection,
} from "../src/index.js";

const SECRET = "whsec_gusto_test_secret";

function conn(over: Partial<Connection> = {}): Connection {
  return {
    id: "g1",
    provider: "gusto",
    tenantId: "acme",
    accessToken: "gusto-tok",
    baseUrl: "https://api.gusto.com",
    secrets: { company_id: "company-abc", bank_account_id: "payroll" },
    health: "ACTIVE",
    ...over,
  };
}

function sign(body: string, secret = SECRET): string {
  return createHmac("sha256", secret).update(body, "utf8").digest("hex");
}

// --- verifyHmacSignature -----------------------------------------------------

test("verifyHmacSignature accepts a correct signature and rejects a tampered one", () => {
  const payload = JSON.stringify({ event_type: "payroll.paid", company_uuid: "company-abc" });
  const good = sign(payload);
  assert.equal(verifyHmacSignature(payload, good, SECRET), true);

  // Tampered payload -> different digest -> rejected.
  assert.equal(verifyHmacSignature(payload + " ", good, SECRET), false);
  // Wrong secret -> rejected.
  assert.equal(verifyHmacSignature(payload, good, "wrong-secret"), false);
  // Garbage signature of the wrong length -> rejected, no throw.
  assert.equal(verifyHmacSignature(payload, "deadbeef", SECRET), false);
  assert.equal(verifyHmacSignature(payload, "", SECRET), false);
});

test("verifyHmacSignature supports a configurable header prefix and base64 encoding", () => {
  const payload = "hello world";
  const hexSig = "sha256=" + sign(payload);
  assert.equal(verifyHmacSignature(payload, hexSig, SECRET, { prefix: "sha256=" }), true);

  const b64 = createHmac("sha256", SECRET).update(payload, "utf8").digest("base64");
  assert.equal(verifyHmacSignature(payload, b64, SECRET, { encoding: "base64" }), true);
  assert.equal(verifyHmacSignature(payload, b64, SECRET, { encoding: "hex" }), false);
});

test("verifyHmacSignature is constant-time: never throws on mismatched-length input", () => {
  const payload = "payload";
  // A range of wrong-length signatures must all safely return false.
  for (const bogus of ["", "a", "ab", "abc", "z".repeat(63), "f".repeat(200)]) {
    assert.doesNotThrow(() => verifyHmacSignature(payload, bogus, SECRET));
    assert.equal(verifyHmacSignature(payload, bogus, SECRET), false);
  }
});

// --- handleGustoWebhook ------------------------------------------------------

test("valid signature + payroll event resolves the connection and asks to sync", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const body = JSON.stringify({
    event_type: "payroll.paid",
    entity_type: "Payroll",
    entity_uuid: "pr-1",
    company_uuid: "company-abc",
  });
  const out = handleGustoWebhook(store, { rawBody: body, signature: sign(body), secret: SECRET });
  assert.equal(out.action, "sync");
  assert.equal(out.connectionId, "g1");
});

test("a tampered body is rejected as invalid_signature before anything else", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const body = JSON.stringify({ event_type: "payroll.paid", company_uuid: "company-abc" });
  const sig = sign(body);
  const tampered = body.replace("payroll.paid", "payroll.deleted");
  const out = handleGustoWebhook(store, { rawBody: tampered, signature: sig, secret: SECRET });
  assert.equal(out.action, "invalid_signature");
  assert.equal(out.connectionId, undefined);
});

test("a valid signature for an unknown company yields unknown_item", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const body = JSON.stringify({ event_type: "payroll.paid", company_uuid: "company-zzz" });
  const out = handleGustoWebhook(store, { rawBody: body, signature: sign(body), secret: SECRET });
  assert.equal(out.action, "unknown_item");
});

test("a signed but non-actionable event is ignored (connection still resolved)", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn());
  const body = JSON.stringify({ event_type: "company.updated", company_uuid: "company-abc" });
  const out = handleGustoWebhook(store, { rawBody: body, signature: sign(body), secret: SECRET });
  assert.equal(out.action, "ignore");
  assert.equal(out.connectionId, "g1");
});

test("a custom resolver and hmac prefix option are honored", () => {
  const store = new InMemoryConnectionStore();
  store.put(conn({ id: "g2" }));
  const body = JSON.stringify({ event_type: "payroll.processed", resource_uuid: "company-abc" });
  const out = handleGustoWebhook(
    store,
    { rawBody: body, signature: "sha256=" + sign(body), secret: SECRET, hmac: { prefix: "sha256=" } },
    () => "g2",
  );
  assert.equal(out.action, "sync");
  assert.equal(out.connectionId, "g2");
});
