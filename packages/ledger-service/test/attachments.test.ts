import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Attachments — the receipt behind the number.
 *
 * The property that matters is that an attachment is *evidence for something*.
 * One pointing at nothing looks like evidence in a list and dead-ends whoever
 * follows it, which is worse than having none. So most of what's tested here is
 * the link: what it can attach to, and what happens when that thing isn't real.
 */

const NOW = "2026-08-20T00:00:00Z";

const call = (
  s: LedgerService, method: string, path: string,
  body: unknown = "", query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({
    method, path, query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Row = Record<string, unknown>;

const RECEIPT = Buffer.from("%PDF-1.4 a scanned receipt for the software renewal");
const B64 = RECEIPT.toString("base64");

async function ready(): Promise<{ svc: LedgerService; entryId: string }> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  const posted = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-21", memo: "Software",
    lines: [
      { code: "6500", side: "DEBIT", amount_minor: "24900" },
      { code: "1000", side: "CREDIT", amount_minor: "24900" },
    ],
  });
  const entryId = String((obj(posted)["entry"] as Row)["id"]);
  return { svc: s, entryId };
}

const attach = (
  s: LedgerService, kind: string, id: string, extra: Record<string, unknown> = {},
): Promise<ServiceResponse> =>
  call(s, "POST", "/t/acme/attachments", {
    subject_kind: kind, subject_id: id,
    filename: "receipt.pdf", content_type: "application/pdf",
    content_base64: B64, ...extra,
  });

// --- attaching ---------------------------------------------------------------

test("a receipt attaches to the entry it evidences and comes back whole", async () => {
  const { svc, entryId } = await ready();
  const r = await attach(svc, "entry", entryId, { note: "Adobe annual renewal" });
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const a = obj(r)["attachment"] as Row;
  assert.equal(a["subject_kind"], "entry");
  assert.equal(a["subject_id"], entryId);
  assert.equal(a["filename"], "receipt.pdf");
  assert.equal(a["bytes"], RECEIPT.length);
  assert.equal(a["note"], "Adobe annual renewal");

  const back = await call(svc, "GET", `/t/acme/attachments/${String(a["id"])}/content`);
  assert.equal(back.status, 200);
  assert.equal(
    Buffer.from(String(obj(back)["content_base64"]), "base64").toString(),
    RECEIPT.toString(),
    "the bytes that come back are the bytes that went in",
  );
});

test("it can be found by what it is evidence for", async () => {
  const { svc, entryId } = await ready();
  await attach(svc, "entry", entryId);
  await attach(svc, "entry", entryId, { filename: "second.jpg", id: "att-2" });

  const found = obj(await call(svc, "GET", "/t/acme/attachments", "", {
    subject_kind: "entry", subject_id: entryId,
  }))["attachments"] as Row[];
  assert.equal(found.length, 2);

  const none = obj(await call(svc, "GET", "/t/acme/attachments", "", {
    subject_kind: "entry", subject_id: "acme:999",
  }))["attachments"] as Row[];
  assert.equal(none.length, 0);
});

test("a bank line, a bill, an invoice and a payroll run can all carry one", async () => {
  const { svc } = await ready();
  await call(svc, "POST", "/t/acme/feed/1000", {
    transactions: [{ id: "bk-1", date: "2026-08-21", amount_minor: "-24900", description: "ADOBE" }],
  });
  await call(svc, "POST", "/t/acme/customers", { id: "halcyon", name: "Halcyon" });
  await call(svc, "POST", "/t/acme/invoices", {
    id: "INV-1", party_id: "halcyon", date: "2026-08-01",
    lines: [{ unit_amount_minor: "100000", account_code: "4100" }],
  });
  await call(svc, "POST", "/t/acme/vendors", { id: "adobe", name: "Adobe" });
  await call(svc, "POST", "/t/acme/bills", {
    id: "BILL-1", party_id: "adobe", date: "2026-08-01",
    lines: [{ unit_amount_minor: "24900", account_code: "6500" }],
  });
  await call(svc, "POST", "/t/acme/payroll/employees", { id: "ada", name: "Ada" });
  await call(svc, "POST", "/t/acme/payroll/runs", {
    id: "PR-1", date: "2026-08-15",
    lines: [{ employee_id: "ada", gross_minor: "100000" }],
  });

  for (const [kind, id] of [
    ["feed", "bk-1"], ["invoice", "INV-1"], ["bill", "BILL-1"], ["payroll", "PR-1"],
  ] as const) {
    assert.equal((await attach(svc, kind, id, { id: `att-${kind}` })).status, 201, kind);
  }
});

// --- the refusals ------------------------------------------------------------

test("an attachment pointing at nothing is refused", async () => {
  const { svc } = await ready();
  for (const [kind, id] of [
    ["entry", "acme:999"], ["feed", "nope"], ["invoice", "INV-X"],
    ["bill", "BILL-X"], ["payroll", "PR-X"],
  ] as const) {
    const r = await attach(svc, kind, id);
    assert.equal(r.status, 400, kind);
    assert.match(String(obj(r)["error"]), /unknown/);
  }
});

test("an unknown subject kind is refused rather than stored loose", async () => {
  const { svc, entryId } = await ready();
  const r = await attach(svc, "vibes", entryId);
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /subject_kind must be one of/);
  assert.equal((await attach(svc, "entry", "")).status, 400);
});

test("an empty, missing or corrupt file is refused", async () => {
  const { svc, entryId } = await ready();
  assert.equal((await attach(svc, "entry", entryId, { content_base64: "" })).status, 400);
  assert.equal((await attach(svc, "entry", entryId, { content_base64: "!!!not base64!!!" })).status, 400);
  // base64 that decodes to nothing is an empty file, not a receipt
  assert.equal((await attach(svc, "entry", entryId, { content_base64: "====" })).status, 400);
});

test("a file past the size limit is refused with the size named", async () => {
  const { svc, entryId } = await ready();
  const huge = Buffer.alloc(9 * 1024 * 1024, 1).toString("base64");
  const r = await attach(svc, "entry", entryId, { content_base64: huge });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /9\.0 MB — the limit is 8 MB/);
});

// --- listing and removing ----------------------------------------------------

test("counts let a list view show which rows have evidence", async () => {
  const { svc } = await ready();
  await call(svc, "POST", "/t/acme/feed/1000", {
    transactions: [
      { id: "bk-1", date: "2026-08-21", amount_minor: "-24900", description: "ADOBE" },
      { id: "bk-2", date: "2026-08-22", amount_minor: "-1000", description: "COFFEE" },
    ],
  });
  await attach(svc, "feed", "bk-1", { id: "a1" });
  await attach(svc, "feed", "bk-1", { id: "a2" });

  const counts = obj(await call(svc, "GET", "/t/acme/attachments/counts", "", {
    subject_kind: "feed",
  }))["counts"] as Record<string, number>;
  assert.equal(counts["bk-1"], 2);
  assert.equal(counts["bk-2"], undefined, "a row with no receipt is simply absent");
});

test("an attachment can be removed", async () => {
  const { svc, entryId } = await ready();
  const r = await attach(svc, "entry", entryId, { id: "att-1" });
  assert.equal(r.status, 201);
  assert.equal((await call(svc, "DELETE", "/t/acme/attachments/att-1")).status, 200);
  assert.equal((await call(svc, "GET", "/t/acme/attachments/att-1/content")).status, 404);
});

test("one tenant's attachments are invisible to another", async () => {
  const { svc, entryId } = await ready();
  await call(svc, "POST", "/t/beta/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await attach(svc, "entry", entryId, { id: "att-1" });

  assert.equal(
    (obj(await call(svc, "GET", "/t/beta/attachments"))["attachments"] as Row[]).length, 0,
  );
  assert.equal((await call(svc, "GET", "/t/beta/attachments/att-1/content")).status, 404);
  // and beta cannot attach to acme's entry
  const r = await svc.handle({
    method: "POST", path: "/t/beta/attachments", query: {},
    body: JSON.stringify({
      subject_kind: "entry", subject_id: entryId, content_base64: B64,
    }),
    headers: {},
  });
  assert.equal(r.status, 400);
});

test("a flagged upload is refused (content scan), a clean one is accepted", async () => {
  // A scanner that flags anything containing the EICAR marker (a standard AV
  // test string). Production injects a real scanner; this proves the gate.
  const scanner = {
    async scan(content: Buffer) {
      return content.includes("EICAR")
        ? { clean: false, reason: "eicar test signature" }
        : { clean: true };
    },
  };
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW, scanner });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  const posted = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-21", memo: "Software",
    lines: [
      { code: "6500", side: "DEBIT", amount_minor: "24900" },
      { code: "1000", side: "CREDIT", amount_minor: "24900" },
    ],
  });
  const entryId = String((obj(posted)["entry"] as Row)["id"]);

  const bad = await call(s, "POST", "/t/acme/attachments", {
    subject_kind: "entry", subject_id: entryId, filename: "x.txt",
    content_base64: Buffer.from("EICAR-STANDARD-ANTIVIRUS-TEST-FILE").toString("base64"),
  });
  assert.equal(bad.status, 400);
  assert.match(String((obj(bad)["error"])), /content scan/);

  const good = await call(s, "POST", "/t/acme/attachments", {
    subject_kind: "entry", subject_id: entryId, filename: "r.pdf", content_base64: B64,
  });
  assert.equal(good.status, 201);
});
