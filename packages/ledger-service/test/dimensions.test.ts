import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * Dimensions — classes and locations.
 *
 * The whole value depends on one thing: that a value is *defined*, not typed.
 * Free text gives you "Denver", "denver", "Denver " and "Dnever" — four lines of
 * business where there is one, and a P&L by class that is quietly wrong rather
 * than loudly broken. So most of these tests are about what gets refused.
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

async function ready(): Promise<LedgerService> {
  const s = new LedgerService(new InMemoryBackend(), { now: () => NOW });
  await call(s, "POST", "/t/acme/accounts/seed", { category: "RESTAURANT" });
  return s;
}

const defineClass = (s: LedgerService, extra: Record<string, unknown> = {}) =>
  call(s, "POST", "/t/acme/dimensions", {
    key: "class", label: "Line of business",
    values: ["Dine-in", "Catering"], ...extra,
  });

async function postWithClass(
  s: LedgerService, memo: string, minor: string, klass?: string, date = "2026-08-05",
): Promise<ServiceResponse> {
  return call(s, "POST", "/t/acme/entries", {
    date, memo,
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: minor },
      {
        code: "4100", side: "CREDIT", amount_minor: minor,
        ...(klass ? { dimensions: { class: klass } } : {}),
      },
    ],
  });
}

// --- defining ----------------------------------------------------------------

test("a dimension is defined with its allowed values", async () => {
  const s = await ready();
  const r = await defineClass(s);
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const d = obj(r)["dimension"] as Row;
  assert.equal(d["key"], "class");
  assert.equal(d["label"], "Line of business");
  assert.deepEqual(d["values"], ["Dine-in", "Catering"]);
  assert.equal(d["required"], false);

  const listed = obj(await call(s, "GET", "/t/acme/dimensions"))["dimensions"] as Row[];
  assert.equal(listed.length, 1);
});

test("values that differ only in case or whitespace are one value", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/dimensions", {
    key: "class", values: ["Denver", " denver ", "DENVER", "Boulder"],
  });
  assert.deepEqual((obj(r)["dimension"] as Row)["values"], ["Denver", "Boulder"]);
});

test("a value containing a comma survives — it is not split into two", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/dimensions", {
    key: "location", values: ["Denver, CO", "Boulder, CO"],
  });
  assert.deepEqual((obj(r)["dimension"] as Row)["values"], ["Denver, CO", "Boulder, CO"]);
});

test("a required dimension with no allowed values is refused", async () => {
  const s = await ready();
  const r = await call(s, "POST", "/t/acme/dimensions", {
    key: "class", values: [], required: true,
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /needs its allowed values listed/);
});

test("a nonsense key is refused", async () => {
  const s = await ready();
  for (const key of ["", "Line of business", "1class", "class-name"]) {
    assert.equal(
      (await call(s, "POST", "/t/acme/dimensions", { key, values: ["A"] })).status, 400, key,
    );
  }
});

test("a dimension can be removed", async () => {
  const s = await ready();
  await defineClass(s);
  assert.equal((await call(s, "DELETE", "/t/acme/dimensions/class")).status, 200);
  assert.equal((obj(await call(s, "GET", "/t/acme/dimensions"))["dimensions"] as Row[]).length, 0);
});

// --- posting -----------------------------------------------------------------

test("a defined value posts and is kept on the line", async () => {
  const s = await ready();
  await defineClass(s);
  const r = await postWithClass(s, "Dinner service", "120000", "Dine-in");
  assert.equal(r.status, 201, JSON.stringify(r.body));
  const lines = ((obj(r)["entry"]) as Row)["lines"] as Row[];
  assert.deepEqual(lines[1]!["dimensions"], { class: "Dine-in" });
  assert.equal(lines[0]!["dimensions"], undefined, "the bank side carries no class");
});

test("a typo'd value is refused at the door, not silently accepted", async () => {
  const s = await ready();
  await defineClass(s);
  const r = await postWithClass(s, "Dinner service", "120000", "Dinein");
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /not allowed for dimension "class"/);
  // and nothing was posted
  assert.equal((obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Row[]).length, 0);
});

test("an unknown dimension key is refused", async () => {
  const s = await ready();
  await defineClass(s);
  const r = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05", memo: "x",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "4100", side: "CREDIT", amount_minor: "100", dimensions: { department: "Kitchen" } },
    ],
  });
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /unknown dimension/);
});

test("with nothing defined, tagging a line is a mistake rather than free text", async () => {
  const s = await ready();
  const r = await postWithClass(s, "Dinner service", "120000", "Dine-in");
  assert.equal(r.status, 400);
  assert.match(String(obj(r)["error"]), /no dimensions are defined/);
});

test("a required dimension is demanded on income and cost lines, not on cash", async () => {
  const s = await ready();
  await defineClass(s, { required: true });

  // untagged revenue is refused
  const untagged = await postWithClass(s, "Dinner service", "120000");
  assert.equal(untagged.status, 400);
  assert.match(String(obj(untagged)["error"]), /Line of business is required/);

  // tagging the revenue line is enough — the bank side needs no class, and
  // demanding one would make it impossible to post the other half of any entry
  assert.equal((await postWithClass(s, "Dinner service", "120000", "Dine-in")).status, 201);

  // the same rule applies to an expense
  const expense = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-06", memo: "Produce",
    lines: [
      { code: "5000", side: "DEBIT", amount_minor: "40000" },
      { code: "1000", side: "CREDIT", amount_minor: "40000" },
    ],
  });
  assert.equal(expense.status, 400);
  assert.match(String(obj(expense)["error"]), /5000: Line of business is required/);
});

test("dimensions must be an object of strings", async () => {
  const s = await ready();
  await defineClass(s);
  for (const dims of [["Dine-in"], "Dine-in", { class: 3 }]) {
    const r = await call(s, "POST", "/t/acme/entries", {
      date: "2026-08-05",
      lines: [
        { code: "1000", side: "DEBIT", amount_minor: "100" },
        { code: "4100", side: "CREDIT", amount_minor: "100", dimensions: dims },
      ],
    });
    assert.equal(r.status, 400, JSON.stringify(dims));
  }
});

test("a bank feed line can be classified as it is accepted", async () => {
  const s = await ready();
  await defineClass(s);
  await call(s, "POST", "/t/acme/feed/1000", {
    transactions: [
      { id: "bk-1", date: "2026-08-06", amount_minor: "-40000", description: "RESTAURANT DEPOT" },
    ],
  });
  const r = await call(s, "POST", "/t/acme/feed/txn/bk-1/accept", {
    category_code: "5000", dimensions: { class: "Catering" },
  });
  assert.equal(r.status, 200, JSON.stringify(r.body));
  const entries = obj(await call(s, "GET", "/t/acme/entries"))["entries"] as Row[];
  const lines = entries[0]!["lines"] as Row[];
  assert.deepEqual(lines[0]!["dimensions"], { class: "Catering" });

  // and a typo there is refused too, leaving the line in the queue
  await call(s, "POST", "/t/acme/feed/1000", {
    transactions: [{ id: "bk-2", date: "2026-08-07", amount_minor: "-1000", description: "X" }],
  });
  const bad = await call(s, "POST", "/t/acme/feed/txn/bk-2/accept", {
    category_code: "5000", dimensions: { class: "Ctering" },
  });
  assert.equal(bad.status, 400);
  assert.equal(obj(await call(s, "GET", "/t/acme/feed"))["pending"], 1);
});

// --- reporting ---------------------------------------------------------------

test("a trial balance per class is what makes P&L by line of business possible", async () => {
  const s = await ready();
  await defineClass(s);
  await postWithClass(s, "Dinner service", "120000", "Dine-in", "2026-08-05");
  await postWithClass(s, "Wedding", "800000", "Catering", "2026-08-12");
  await postWithClass(s, "Uncategorized takings", "5000", undefined, "2026-08-14");

  const r = await call(s, "GET", "/t/acme/dimensions/class/report");
  assert.equal(r.status, 200);
  assert.equal(obj(r)["contract"], "trial-balance-by-dimension/1");
  assert.equal(obj(r)["label"], "Line of business");

  const buckets = obj(r)["buckets"] as Row[];
  const by = (v: string): Row => buckets.find((b) => b["value"] === v)!;
  assert.equal(by("Dine-in")["total_credit_minor"], "120000");
  assert.equal(by("Catering")["total_credit_minor"], "800000");

  // the unattributed activity is shown, not dropped — and shown last
  const last = buckets[buckets.length - 1]!;
  assert.equal(last["unassigned"], true);
  assert.equal(String(last["value"]), "(unassigned)");
});

test("the report can be narrowed to a date window", async () => {
  const s = await ready();
  await defineClass(s);
  await postWithClass(s, "July wedding", "500000", "Catering", "2026-07-10");
  await postWithClass(s, "August wedding", "800000", "Catering", "2026-08-12");

  const r = await call(s, "GET", "/t/acme/dimensions/class/report", "", {
    from: "2026-08-01", to: "2026-08-31",
  });
  const catering = (obj(r)["buckets"] as Row[]).find((b) => b["value"] === "Catering")!;
  assert.equal(catering["total_credit_minor"], "800000");
  assert.equal((await call(s, "GET", "/t/acme/dimensions/class/report", "", {
    from: "August",
  })).status, 400);
  assert.equal((await call(s, "GET", "/t/acme/dimensions/nope/report")).status, 400);
});

test("one tenant's dimensions are invisible to another", async () => {
  const s = await ready();
  await call(s, "POST", "/t/beta/accounts/seed", { category: "RESTAURANT" });
  await defineClass(s);
  assert.equal(
    (obj(await call(s, "GET", "/t/beta/dimensions"))["dimensions"] as Row[]).length, 0,
  );
  // beta cannot use acme's class
  const r = await call(s, "POST", "/t/beta/entries", {
    date: "2026-08-05",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "4100", side: "CREDIT", amount_minor: "100", dimensions: { class: "Dine-in" } },
    ],
  });
  assert.equal(r.status, 400);
});
