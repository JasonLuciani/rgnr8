import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * The authoritative close/publish/reopen surface, end to end over the service.
 * Publishing seals the period durably (locks it + persists the immutable
 * package); reopening is a two-step, separation-of-duties-gated workflow.
 */

const NOW = "2026-09-01T17:00:00Z";

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { now: () => NOW });
}
const call = (
  s: LedgerService, method: string, path: string, body: unknown = "", query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({ method, path, query, body: typeof body === "string" ? body : JSON.stringify(body), headers: {} });
const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

/** Seed a tenant with a small set of balanced books for August. */
async function books(s: LedgerService): Promise<void> {
  await call(s, "POST", "/t/acme/accounts/seed", { category: "SERVICE_GENERAL" });
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05", memo: "sale",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "500000" },
      { code: "4000", side: "CREDIT", amount_minor: "500000" },
    ],
  });
}

/** Reconcile the Cash account through the period end so the close gate's bank
 * reconciliation control passes (the authoritative gate requires it). */
async function reconcileCash(s: LedgerService): Promise<void> {
  const q = { statement_date: "2026-08-31", statement_balance_minor: "500000" };
  const view = obj(await call(s, "GET", "/t/acme/accounts/1000/reconcile", "", q));
  for (const l of (view["lines"] as Array<{ entry_id: string }>)) {
    await call(s, "POST", "/t/acme/accounts/1000/reconcile/toggle",
      { entry_id: l.entry_id, cleared: true, ...q });
  }
  await call(s, "POST", "/t/acme/accounts/1000/reconcile/finish", q);
}

test("publish is blocked until the bank is reconciled (real gate, not board flags)", async () => {
  const s = svc();
  await books(s);
  // Cash is not reconciled through the period end → the authoritative gate refuses.
  const blocked = await call(s, "POST", "/t/acme/close/publish", {
    from: "2026-08-01", to: "2026-08-31", published_by: "sam",
  });
  assert.equal(blocked.status, 409, JSON.stringify(blocked.body));
  assert.match(String(obj(blocked)["error"]), /reconcil/i);
  // Reconcile, then the same publish succeeds.
  await reconcileCash(s);
  const ok = await call(s, "POST", "/t/acme/close/publish", {
    from: "2026-08-01", to: "2026-08-31", published_by: "sam",
  });
  assert.equal(ok.status, 200, JSON.stringify(ok.body));
});

test("publish seals the period: locks it durably and persists the package", async () => {
  const s = svc();
  await books(s);
  await reconcileCash(s);

  const res = await call(s, "POST", "/t/acme/close/publish", {
    from: "2026-08-01", to: "2026-08-31", period: "2026-08",
    prepared_by: "alex", published_by: "sam",
  });
  assert.equal(res.status, 200, JSON.stringify(res.body));
  const body = obj(res);
  assert.equal(body["status"], "PUBLISHED");
  assert.equal(body["published_by"], "sam");
  assert.ok(body["package_fingerprint"]);

  // The period is now locked — a back-dated posting into it is refused.
  const late = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-20", memo: "late",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "4000", side: "CREDIT", amount_minor: "100" },
    ],
  });
  assert.equal(late.status >= 400, true, "posting into a sealed period must be refused");

  // The sealed package is retrievable, and the close state is durable.
  const pkg = await call(s, "GET", "/t/acme/packages/2026-08");
  assert.equal(pkg.status, 200);
  const state = await call(s, "GET", "/t/acme/close/state", "", { period: "2026-08" });
  assert.equal(obj(state)["status"], "PUBLISHED");
});

test("separation of duties: the preparer cannot also publish", async () => {
  const s = svc();
  await books(s);
  await reconcileCash(s);
  const res = await call(s, "POST", "/t/acme/close/publish", {
    from: "2026-08-01", to: "2026-08-31", prepared_by: "sam", published_by: "sam",
  });
  assert.equal(res.status, 409, JSON.stringify(res.body));
  assert.match(String(obj(res)["error"]), /sign off|second person/i);
});

test("a real AR control mismatch blocks the publish — nothing is sealed", async () => {
  const s = svc();
  await books(s);
  await reconcileCash(s);
  // Post straight to the AR control account with no matching open invoice, so the
  // GL AR balance no longer ties to the (empty) subledger.
  await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-07", memo: "orphan AR",
    lines: [
      { code: "1200", side: "DEBIT", amount_minor: "9000" },
      { code: "4000", side: "CREDIT", amount_minor: "9000" },
    ],
  });
  const res = await call(s, "POST", "/t/acme/close/publish", {
    from: "2026-08-01", to: "2026-08-31", published_by: "sam",
  });
  assert.equal(res.status, 409, JSON.stringify(res.body));
  assert.match(String(obj(res)["error"]), /receivable|subledger|blocked/i);
  const state = await call(s, "GET", "/t/acme/close/state", "", { period: "2026-08" });
  assert.equal(state.status, 404, "no close state should exist after a blocked publish");
});

test("reopen is two-step and separation-of-duties gated; evidence is preserved", async () => {
  const s = svc();
  await books(s);
  await reconcileCash(s);
  await call(s, "POST", "/t/acme/close/publish", {
    from: "2026-08-01", to: "2026-08-31", published_by: "sam",
  });

  // Request records intent but does not unlock.
  const req = await call(s, "POST", "/t/acme/close/reopen/request", {
    period: "2026-08", requested_by: "alex", reason: "missing bill",
  });
  assert.equal(obj(req)["status"], "REOPEN_REQUESTED");

  // The requester cannot approve their own reopen.
  const selfApprove = await call(s, "POST", "/t/acme/close/reopen/approve", {
    period: "2026-08", approved_by: "alex",
  });
  assert.equal(selfApprove.status, 409);

  // A different person approves → period unlocks and posting works again.
  const approved = await call(s, "POST", "/t/acme/close/reopen/approve", {
    period: "2026-08", approved_by: "sam",
  });
  assert.equal(obj(approved)["status"], "REOPENED");
  const again = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-20", memo: "adjustment",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "4000", side: "CREDIT", amount_minor: "100" },
    ],
  });
  assert.equal(again.status < 300, true, "posting works after an approved reopen");

  // Evidence preserved: the sealed package still exists, full history recorded.
  const pkg = await call(s, "GET", "/t/acme/packages/2026-08");
  assert.equal(pkg.status, 200);
  const history = obj(approved)["history"] as Array<{ action: string }>;
  assert.deepEqual(history.map((h) => h.action), ["PUBLISH", "REQUEST_REOPEN", "APPROVE_REOPEN"]);
});
