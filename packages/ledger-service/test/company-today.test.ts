import { test } from "node:test";
import assert from "node:assert/strict";
import { InMemoryBackend, LedgerService, type ServiceResponse } from "../src/index.js";

/**
 * "Today" is the business's date, which the web app works out in the company's
 * time zone and passes in. At 2026-10-31 23:30 America/Denver the server's UTC
 * date is already 2026-11-01: an invoice due on the 31st is not yet overdue, and
 * the month-end cutover is not yet over, whatever the server's clock says.
 */

const NOW = "2026-11-01T05:30:00Z"; // 2026-10-31 23:30 in America/Denver

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { now: () => NOW });
}

const call = (
  s: LedgerService,
  method: string,
  path: string,
  body: unknown = "",
  query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({
    method,
    path,
    query,
    body: typeof body === "string" ? body : JSON.stringify(body),
    headers: {},
  });

const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;

async function withOneOverdueCandidate(): Promise<LedgerService> {
  const s = svc();
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  await call(s, "POST", "/t/acme/customers", {
    id: "cust-1", name: "Northwind Ltd", email: "ap@northwind.com", terms_days: 30,
  });
  await call(s, "POST", "/t/acme/invoices", {
    id: "INV-1", party_id: "cust-1", date: "2026-10-01", due_date: "2026-10-31",
    lines: [{ unit_amount_minor: "100000", account_code: "4100" }],
  });
  return s;
}

test("an invoice due today, by the business's calendar, is not overdue", async () => {
  const s = await withOneOverdueCandidate();
  const asOfCompany = obj(await call(s, "GET", "/t/acme/invoices", "", { today: "2026-10-31" }));
  assert.equal(asOfCompany["overdue_total_minor"], "0");
});

test("without the business's date the server's UTC date would call it overdue", async () => {
  const s = await withOneOverdueCandidate();
  const serverUtc = obj(await call(s, "GET", "/t/acme/invoices"));
  assert.equal(serverUtc["overdue_total_minor"], "100000");
});

test("a malformed today is ignored in favour of the server's date", async () => {
  const s = await withOneOverdueCandidate();
  const bad = obj(await call(s, "GET", "/t/acme/invoices", "", { today: "tomorrow" }));
  assert.equal(bad["overdue_total_minor"], "100000");
});

test("a cutover on a month-end the business has not finished is blocked", async () => {
  const s = svc();
  await call(s, "POST", "/t/acme/accounts/seed", { category: "PROFESSIONAL_SERVICES" });
  const plan = (body: unknown) => call(s, "POST", "/t/acme/cutover/plan", body);
  const blockers = (r: ServiceResponse): string[] =>
    ((obj(r)["plan"] as Record<string, unknown>)["blockers"] as string[]);

  const onCompanyToday = blockers(await plan({ cutover_date: "2026-10-31", today: "2026-10-31" }));
  assert.ok(onCompanyToday.some((b) => b.includes("hasn't ended yet")), onCompanyToday.join(" | "));

  const onServerUtc = blockers(await plan({ cutover_date: "2026-10-31" }));
  assert.ok(!onServerUtc.some((b) => b.includes("hasn't ended yet")), onServerUtc.join(" | "));
});
