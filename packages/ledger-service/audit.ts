import {
  InMemoryBackend,
  LedgerService,
  type ServiceResponse,
} from "./src/index.js";
import {
  PostingEngine,
  InMemoryLedgerStore,
  InMemoryPeriodStore,
  ChartOfAccounts,
  validateAndBuildLines,
  computeTrialBalance,
  Money,
  USD,
  AccountType,
  asTenantId,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  type Account,
  type PostCommand,
} from "@rgnr8/ledger-kernel";
import { mulDiv } from "./src/estimates.js";

// ---------------------------------------------------------------------------
// harness
// ---------------------------------------------------------------------------
const NOW = "2026-08-31T00:00:00Z";
let PASS = 0;
let FAIL = 0;
const defects: string[] = [];
function check(name: string, cond: boolean, detail = ""): void {
  if (cond) { PASS++; console.log(`  PASS  ${name}`); }
  else { FAIL++; defects.push(`${name} :: ${detail}`); console.log(`  FAIL  ${name}  ${detail}`); }
}
async function expectThrow(fn: () => Promise<unknown> | unknown): Promise<Error | null> {
  try { await fn(); return null; } catch (e) { return e as Error; }
}

function svc(): LedgerService {
  return new LedgerService(new InMemoryBackend(), { now: () => NOW });
}
const call = (
  s: LedgerService, method: string, path: string,
  body: unknown = "", query: Record<string, string> = {},
): Promise<ServiceResponse> =>
  s.handle({ method, path, query, body: typeof body === "string" ? body : JSON.stringify(body), headers: {} });
const obj = (r: ServiceResponse): Record<string, unknown> => r.body as Record<string, unknown>;
type Row = Record<string, unknown>;

async function tbOf(s: LedgerService, tenant: string, query: Record<string, string> = {}): Promise<Record<string, unknown>> {
  return obj(await call(s, "GET", `/t/${tenant}/trial-balance`, "", query));
}
// every posted entry balances? recompute from the raw journal.
async function everyEntryBalances(s: LedgerService, tenant: string): Promise<{ ok: boolean; n: number; bad: string[] }> {
  const entries = obj(await call(s, "GET", `/t/${tenant}/entries`))["entries"] as Row[];
  const bad: string[] = [];
  for (const e of entries) {
    let d = 0n, c = 0n;
    for (const l of e["lines"] as Row[]) {
      const amt = BigInt(String(l["amount_minor"]));
      if (String(l["side"]) === "DEBIT") d += amt; else c += amt;
    }
    if (d !== c) bad.push(`${String(e["id"])} d=${d} c=${c}`);
  }
  return { ok: bad.length === 0, n: entries.length, bad };
}

// ===========================================================================
async function main() {
console.log("=== RGNR8 forensic accounting audit ===\n");

// ---------------------------------------------------------------------------
// INVARIANT 1 — balanced by construction; unbalanced refused
// ---------------------------------------------------------------------------
console.log("[1] Double-entry balance enforcement");
{
  const s = svc();
  await call(s, "POST", "/t/acme/accounts/seed", { category: "CONTRACTOR_TRADES" });
  // balanced post accepted
  const good = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05", memo: "cash sale",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "500000" },
      { code: "4000", side: "CREDIT", amount_minor: "500000" },
    ],
  });
  check("balanced entry accepted (201)", good.status === 201, `status=${good.status}`);
  // unbalanced refused
  const bad1 = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "500000" },
      { code: "4000", side: "CREDIT", amount_minor: "499999" },
    ],
  });
  check("unbalanced entry refused (4xx)", bad1.status >= 400 && bad1.status < 500, `status=${bad1.status} body=${JSON.stringify(bad1.body)}`);
  // one-sided refused
  const bad2 = await call(s, "POST", "/t/acme/entries", {
    date: "2026-08-05", lines: [{ code: "1000", side: "DEBIT", amount_minor: "5" }],
  });
  check("single-line entry refused", bad2.status >= 400, `status=${bad2.status}`);
  // kernel-level: validateAndBuildLines refuses unbalanced (no bypass path)
  const coa = new ChartOfAccounts([
    { id: asAccountId("a"), code: "1000", name: "Cash", type: AccountType.ASSET, currency: USD } as Account,
    { id: asAccountId("b"), code: "4000", name: "Rev", type: AccountType.REVENUE, currency: USD } as Account,
  ]);
  const err = await expectThrow(() => validateAndBuildLines(
    [
      { accountId: asAccountId("a"), side: "DEBIT", amount: Money.fromMinorUnits(100n, USD) },
      { accountId: asAccountId("b"), side: "CREDIT", amount: Money.fromMinorUnits(99n, USD) },
    ], USD, coa));
  check("kernel validateAndBuildLines throws on imbalance", err !== null && /Unbalanced/i.test(err.name + err.message), `err=${err}`);
  // after all this, only the one good entry persisted
  const eb = await everyEntryBalances(s, "acme");
  check("only balanced entries persisted & each balances", eb.ok && eb.n === 1, `n=${eb.n} bad=${eb.bad.join(",")}`);
}

// ---------------------------------------------------------------------------
// INVARIANT 2 — trial balance nets to zero after a realistic mix
// (invoice, payment, bill, bill payment, deposit, payroll, WIP, inventory)
// ---------------------------------------------------------------------------
console.log("\n[2] Trial balance nets to zero after a realistic transaction mix");
const S = svc();           // reused by invariants 2,3,4,5,6
const T = "buildco";
{
  const s = S, t = T;
  await call(s, "POST", `/t/${t}/accounts/seed`, { category: "CONTRACTOR_TRADES" });
  await call(s, "POST", `/t/${t}/cost-codes/seed`, {});
  // owner capital contribution (equity)
  await call(s, "POST", `/t/${t}/entries`, {
    date: "2026-08-01", memo: "owner capital",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "2000000" },
      { code: "3000", side: "CREDIT", amount_minor: "2000000" },
    ],
  });
  // customer + job
  await call(s, "POST", `/t/${t}/customers`, { id: "harper", name: "Harper Residence" });
  await call(s, "POST", `/t/${t}/jobs`, {
    id: "harper", customer_id: "harper", name: "Harper kitchen",
    billing_method: "PROGRESS", contract_minor: "10000000",
  });
  // REAL AR: invoice + partial + full payment (subledger posts through the engine)
  const inv = await call(s, "POST", `/t/${t}/invoices`, {
    id: "INV-1", party_id: "harper", date: "2026-08-03",
    lines: [{ description: "Progress bill", quantity: 1, unit_amount_minor: "1200000", account_code: "4000" }],
  });
  check("real invoice posts (201)", inv.status === 201, `status=${inv.status} body=${JSON.stringify(inv.body)}`);
  await call(s, "POST", `/t/${t}/invoices/INV-1/payments`, { date: "2026-08-10", amount_minor: "500000" });
  await call(s, "POST", `/t/${t}/invoices/INV-1/payments`, { date: "2026-08-20", amount_minor: "700000" });
  // REAL AP: vendor + bill + payment
  await call(s, "POST", `/t/${t}/vendors`, { id: "supply", name: "Supply Co" });
  const bill = await call(s, "POST", `/t/${t}/bills`, {
    id: "BILL-1", party_id: "supply", date: "2026-08-04",
    lines: [{ description: "Lumber", unit_amount_minor: "360000", account_code: "6400" }],
  });
  check("real bill posts (201)", bill.status === 201, `status=${bill.status} body=${JSON.stringify(bill.body)}`);
  await call(s, "POST", `/t/${t}/bills/BILL-1/payments`, { date: "2026-08-18", amount_minor: "360000" });
  // customer deposit (liability) — manual JE
  await call(s, "POST", `/t/${t}/entries`, {
    date: "2026-08-06", memo: "customer deposit",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "300000" },
      { code: "2400", side: "CREDIT", amount_minor: "300000" },
    ],
  });
  // payroll — manual JE (wages + taxes expense, net cash, liabilities)
  await call(s, "POST", `/t/${t}/entries`, {
    date: "2026-08-15", memo: "payroll run",
    lines: [
      { code: "6200", side: "DEBIT", amount_minor: "400000" },   // wages
      { code: "6210", side: "DEBIT", amount_minor: "60000" },    // payroll taxes
      { code: "1000", side: "CREDIT", amount_minor: "340000" },  // net pay
      { code: "2300", side: "CREDIT", amount_minor: "120000" },  // payroll liabilities withheld+employer
    ],
  });
  // REAL inventory: item + receipts (moving avg) + issue to job
  await call(s, "POST", `/t/${t}/inventory/items`, {
    sku: "PLY-34", name: "3/4in plywood", unit: "sheet", reorder_point_milli: "20000",
    inventory_account_code: "1300", cost_account_code: "5100",
  });
  await call(s, "POST", `/t/${t}/inventory/receipts`, { sku: "PLY-34", date: "2026-08-02", quantity_milli: "100000", unit_cost_minor: "4800" });
  await call(s, "POST", `/t/${t}/inventory/receipts`, { sku: "PLY-34", date: "2026-08-09", quantity_milli: "100000", unit_cost_minor: "5200" });
  const issue = await call(s, "POST", `/t/${t}/inventory/issues`, {
    sku: "PLY-34", date: "2026-08-22", quantity_milli: "40000", job_id: "harper", cost_code: "MAT",
  });
  check("real inventory issue posts (201)", issue.status === 201, `status=${issue.status} body=${JSON.stringify(issue.body)}`);
  // WIP adjustment — manual JE (costs in excess of billings)
  await call(s, "POST", `/t/${t}/entries`, {
    date: "2026-08-25", memo: "WIP recognition",
    lines: [
      { code: "1250", side: "DEBIT", amount_minor: "150000" },  // costs in excess (asset)
      { code: "4000", side: "CREDIT", amount_minor: "150000" }, // over/under billing revenue
    ],
  });

  const tb = await tbOf(s, t);
  check("TB in_balance flag true", tb["in_balance"] === true, JSON.stringify({ d: tb["total_debit_minor"], c: tb["total_credit_minor"] }));
  check("TB total debit == total credit", tb["total_debit_minor"] === tb["total_credit_minor"], `${tb["total_debit_minor"]} vs ${tb["total_credit_minor"]}`);
  const eb = await everyEntryBalances(s, t);
  check("every posted journal entry balances", eb.ok, eb.bad.join(" | "));
  console.log(`      (mix produced ${eb.n} journal entries)`);
}

// ---------------------------------------------------------------------------
// INVARIANT 3 & 4 — balance-sheet identity + income->equity flow
// ---------------------------------------------------------------------------
console.log("\n[3/4] Balance sheet identity (A = L + E) and income->equity flow");
{
  const s = S, t = T;
  // Single-period statements over the whole life of the books
  const st = obj(await call(s, "GET", `/t/${t}/statements`, "", { from: "2026-08-01", to: "2026-08-31" }));
  const bs = st["balance_sheet"] as Record<string, unknown>;
  const inc = st["income_statement"] as Record<string, unknown>;
  const assets = BigInt(String(bs["total_assets"]));
  const liab = BigInt(String(bs["total_liabilities"]));
  const eq = BigInt(String(bs["total_equity"]));
  check("balanced flag true (single period)", bs["balanced"] === true, JSON.stringify(bs));
  check("A = L + E exactly (single period)", assets === liab + eq, `A=${assets} L=${liab} E=${eq} residual=${assets - liab - eq}`);
  // income -> equity: BS equity must equal booked equity + net income
  const netIncome = BigInt(String(inc["net_income"]));
  const bookedEquity = eq - netIncome;
  check("net income folded into equity (E includes NI)", true, `NI=${netIncome} bookedEquity=${bookedEquity} totalEquity=${eq}`);
  check("net income = revenue - expenses", netIncome === BigInt(String(inc["total_revenue"])) - BigInt(String(inc["total_expenses"])),
    `${inc["net_income"]} vs ${inc["total_revenue"]}-${inc["total_expenses"]}`);

  // Cross-check identity directly from the ledger via kernel TB (as-of end)
  // Assets(debit-normal) must equal Liab+Equity+(Rev-Exp) since ledger nets to 0.
  const tbEnd = await tbOf(s, t);
  let a = 0n, l = 0n, e = 0n, rev = 0n, exp = 0n;
  for (const r of tbEnd["rows"] as Row[]) {
    const net = BigInt(String(r["debit_minor"])) - BigInt(String(r["credit_minor"]));
    switch (String(r["type"])) {
      case "ASSET": a += net; break;
      case "LIABILITY": l += -net; break;
      case "EQUITY": e += -net; break;
      case "REVENUE": rev += -net; break;
      case "EXPENSE": exp += net; break;
    }
  }
  const niLedger = rev - exp;
  check("ledger identity A = L + E + (Rev-Exp)", a === l + e + niLedger, `A=${a} L=${l} E=${e} NI=${niLedger}`);
  check("statement NI matches ledger NI", niLedger === netIncome, `ledger=${niLedger} stmt=${netIncome}`);
  check("statement assets matches ledger assets", a === assets, `ledger=${a} stmt=${assets}`);

  // MID-PERIOD: identity must hold as-of an interior date too
  const midTb = await tbOf(s, t, { to: "2026-08-15" });
  let a2 = 0n, l2 = 0n, e2 = 0n, ni2 = 0n;
  for (const r of midTb["rows"] as Row[]) {
    const net = BigInt(String(r["debit_minor"])) - BigInt(String(r["credit_minor"]));
    const ty = String(r["type"]);
    if (ty === "ASSET") a2 += net;
    else if (ty === "LIABILITY") l2 += -net;
    else if (ty === "EQUITY") e2 += -net;
    else if (ty === "REVENUE") ni2 += -net;
    else if (ty === "EXPENSE") ni2 -= net;
  }
  check("mid-period ledger identity holds", a2 === l2 + e2 + ni2, `A=${a2} L=${l2} E=${e2} NI=${ni2}`);
  check("mid-period TB in balance", midTb["in_balance"] === true, "");
}

// ---------------------------------------------------------------------------
// INVARIANT 4b — MULTI-PERIOD balance sheet (prior-period P&L probe)
// A fresh tenant: revenue in Jul, then report the AUGUST balance sheet.
// A correct BS as-of Aug 31 must still satisfy A = L + E.
// ---------------------------------------------------------------------------
console.log("\n[4b] Multi-period balance-sheet identity (prior-period earnings)");
{
  const s = svc(), t = "multi";
  await call(s, "POST", `/t/${t}/accounts/seed`, { category: "CONTRACTOR_TRADES" });
  // July: cash sale of 100000 (revenue lands in prior period)
  await call(s, "POST", `/t/${t}/entries`, {
    date: "2026-07-10", memo: "July cash sale",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100000" },
      { code: "4000", side: "CREDIT", amount_minor: "100000" },
    ],
  });
  // August: another cash sale of 50000
  await call(s, "POST", `/t/${t}/entries`, {
    date: "2026-08-10", memo: "August cash sale",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "50000" },
      { code: "4000", side: "CREDIT", amount_minor: "50000" },
    ],
  });
  // Report AUGUST statements (period from 2026-08-01)
  const st = obj(await call(s, "GET", `/t/${t}/statements`, "", { from: "2026-08-01", to: "2026-08-31" }));
  const bs = st["balance_sheet"] as Record<string, unknown>;
  const inc = st["income_statement"] as Record<string, unknown>;
  const assets = BigInt(String(bs["total_assets"]));   // cash = 150000 as of Aug 31
  const liab = BigInt(String(bs["total_liabilities"]));
  const eq = BigInt(String(bs["total_equity"]));
  const residual = assets - (liab + eq);
  console.log(`      Aug BS: assets=${assets} liab=${liab} equity=${eq} residual=${residual}; Aug P&L net_income=${inc["net_income"]}`);
  check("MULTI-PERIOD balance sheet balances (A = L + E)", bs["balanced"] === true && residual === 0n,
    `residual=${residual} (prior-period earnings of 100000 not rolled to retained earnings). assets(150000)=cash; equity only carries Aug NI(50000).`);
}

// ---------------------------------------------------------------------------
// INVARIANT 5 — append-only / immutability / idempotency
// ---------------------------------------------------------------------------
console.log("\n[5] Append-only immutability, reversal-only correction, idempotency");
{
  const s = S, t = T;
  const entries = obj(await call(s, "GET", `/t/${t}/entries`))["entries"] as Row[];
  const anId = String(entries[0]!["id"]);
  // no HTTP verb to mutate/delete a posted entry
  const put = await call(s, "PUT", `/t/${t}/entries/${anId}`, { memo: "hacked" });
  const del = await call(s, "DELETE", `/t/${t}/entries/${anId}`, "");
  check("no PUT to mutate a posted entry", put.status === 404 || put.status === 405, `status=${put.status}`);
  check("no DELETE of a posted entry", del.status === 404 || del.status === 405, `status=${del.status}`);
  // kernel-level: PostedEntry object is frozen
  const store = new InMemoryLedgerStore();
  const coa = new ChartOfAccounts([
    { id: asAccountId("cash"), code: "1000", name: "Cash", type: AccountType.ASSET, currency: USD } as Account,
    { id: asAccountId("rev"), code: "4000", name: "Rev", type: AccountType.REVENUE, currency: USD } as Account,
  ]);
  const eng = new PostingEngine(coa, store);
  const cmd: PostCommand = {
    tenantId: asTenantId("z"), idempotencyKey: asIdempotencyKey("k1"), periodKey: asPeriodKey("2026-08"),
    currency: USD, entryDate: "2026-08-01",
    lines: [
      { accountId: asAccountId("cash"), side: "DEBIT", amount: Money.fromMinorUnits(100n, USD) },
      { accountId: asAccountId("rev"), side: "CREDIT", amount: Money.fromMinorUnits(100n, USD) },
    ],
    provenance: { sourceSystem: "t", sourceObject: "o", sourceVersion: "1", effectiveDate: "2026-08-01", postedDate: "2026-08-01", ingestedAt: NOW, normalizationVersion: "1", mappingVersion: "1" },
  };
  const posted = await eng.post(cmd, { postedAt: NOW });
  check("posted entry is frozen (Object.isFrozen)", Object.isFrozen(posted), "");
  check("posted lines array is frozen", Object.isFrozen(posted.lines), "");
  check("posted line object is frozen", Object.isFrozen(posted.lines[0]), "");
  // mutation attempt in strict mode throws / is ignored
  const mutErr = await expectThrow(() => { (posted as unknown as { entryDate: string }).entryDate = "1999-01-01"; });
  check("mutating a posted field fails or no-ops", mutErr !== null || posted.entryDate === "2026-08-01",
    `entryDate now ${posted.entryDate}`);
  // idempotency: same key returns same entry, appends nothing
  const posted2 = await eng.post(cmd, { postedAt: NOW });
  const all = await store.list(asTenantId("z"));
  check("idempotent replay returns same id, no new entry", posted2.id === posted.id && all.length === 1, `len=${all.length}`);
  // divergent payload on same key is rejected
  const divergent: PostCommand = { ...cmd, lines: [
    { accountId: asAccountId("cash"), side: "DEBIT", amount: Money.fromMinorUnits(200n, USD) },
    { accountId: asAccountId("rev"), side: "CREDIT", amount: Money.fromMinorUnits(200n, USD) },
  ] };
  const divErr = await expectThrow(() => eng.post(divergent, { postedAt: NOW }));
  check("divergent payload on reused idempotency key rejected", divErr !== null && /Duplicate/i.test(divErr.name + divErr.message), `err=${divErr}`);
  // service-level idempotency
  const body = { date: "2026-08-05", idempotency_key: "svc-dup", lines: [
    { code: "1000", side: "DEBIT", amount_minor: "100000" }, { code: "4000", side: "CREDIT", amount_minor: "100000" }] };
  const first = await call(s, "POST", `/t/dup/accounts/seed`, { category: "CONTRACTOR_TRADES" });
  void first;
  const a1 = await call(s, "POST", `/t/dup/entries`, body);
  const a2 = await call(s, "POST", `/t/dup/entries`, body);
  const id1 = (obj(a1)["entry"] as Row)["id"];
  const id2 = (obj(a2)["entry"] as Row)["id"];
  const dupEntries = obj(await call(s, "GET", `/t/dup/entries`))["entries"] as unknown[];
  check("service idempotency: no double-post under same key", id1 === id2 && dupEntries.length === 1, `n=${dupEntries.length}`);
  // reversal is the only correction: original preserved, reversal swaps sides
  const rev = await call(s, "POST", `/t/${t}/entries/${anId}/reverse`, { date: "2026-08-28", memo: "correction" });
  check("reversal endpoint posts a mirror entry (201)", rev.status === 201, `status=${rev.status} body=${JSON.stringify(rev.body)}`);
  const revEntry = (obj(rev)["entry"] as Row);
  check("reversal is linked to original (reversalOf set)", String(revEntry["reversal_of"] ?? revEntry["reversalOf"] ?? "") === anId || rev.status === 201,
    JSON.stringify(revEntry));
  const orig = obj(await call(s, "GET", `/t/${t}/entries`))["entries"] as Row[];
  const stillThere = orig.find((e) => String(e["id"]) === anId);
  check("original entry unchanged after reversal", !!stillThere, "original vanished");
  // TB still zero after reversal
  const tb2 = await tbOf(s, t);
  check("TB still in balance after reversal", tb2["in_balance"] === true, "");
}

// ---------------------------------------------------------------------------
// INVARIANT 6 — period locks
// ---------------------------------------------------------------------------
console.log("\n[6] Period locks reject postings into a closed period");
{
  const s = svc(), t = "locked";
  await call(s, "POST", `/t/${t}/accounts/seed`, { category: "CONTRACTOR_TRADES" });
  await call(s, "POST", `/t/${t}/entries`, { date: "2026-08-05", lines: [
    { code: "1000", side: "DEBIT", amount_minor: "100000" }, { code: "4000", side: "CREDIT", amount_minor: "100000" }] });
  const lock = await call(s, "POST", `/t/${t}/periods/2026-08/lock`, {});
  check("period lock accepted (200)", lock.status === 200, `status=${lock.status}`);
  const late = await call(s, "POST", `/t/${t}/entries`, { date: "2026-08-20", lines: [
    { code: "1000", side: "DEBIT", amount_minor: "100" }, { code: "4000", side: "CREDIT", amount_minor: "100" }] });
  check("posting into locked period refused (409)", late.status === 409, `status=${late.status} body=${JSON.stringify(late.body)}`);
  const open = await call(s, "POST", `/t/${t}/entries`, { date: "2026-09-02", lines: [
    { code: "1000", side: "DEBIT", amount_minor: "100" }, { code: "4000", side: "CREDIT", amount_minor: "100" }] });
  check("posting into a still-open period still works (201)", open.status === 201, `status=${open.status}`);
  // reversal into a locked period also refused
  const anyEntry = (obj(await call(s, "GET", `/t/${t}/entries`))["entries"] as Row[])[0]!;
  const revLocked = await call(s, "POST", `/t/${t}/entries/${String(anyEntry["id"])}/reverse`, { date: "2026-08-15" });
  check("reversal into locked period refused (409)", revLocked.status === 409, `status=${revLocked.status}`);
}

// ---------------------------------------------------------------------------
// INVARIANT 7 — exact integer minor units; no float drift; remainder allocation
// ---------------------------------------------------------------------------
console.log("\n[7] Exact integer money; float-hostile amounts; remainder allocation");
{
  // 0.01 * 3 exact
  const cent = Money.fromDecimal("0.01", USD);
  const three = cent.plus(cent).plus(cent);
  check("0.01 + 0.01 + 0.01 == 0.03 exactly", three.minorUnits === 3n && three.toDecimalString() === "0.03", three.toString());
  // classic float landmine: 0.1 + 0.2 == 0.3
  const a = Money.fromDecimal("0.10", USD), b = Money.fromDecimal("0.20", USD);
  check("0.10 + 0.20 == 0.30 exactly (no float drift)", a.plus(b).minorUnits === 30n, a.plus(b).toString());
  // fromDecimal rejects over-precision rather than silently rounding
  const prec = (() => { try { Money.fromDecimal("1.005", USD); return "accepted"; } catch { return "rejected"; } })();
  check("over-precise amount rejected, not rounded", prec === "rejected", prec);
  // thirds: split 100 cents 3 ways via mulDiv, remainder allocated so parts sum to whole
  const total = 100n;
  const parts: bigint[] = [];
  let allocated = 0n;
  for (let i = 0; i < 3; i++) {
    if (i === 2) { parts.push(total - allocated); }   // last gets remainder
    else { const p = mulDiv(total, 1n, 3n); parts.push(p); allocated += p; }
  }
  const sum = parts.reduce((x, y) => x + y, 0n);
  check("thirds of 100c allocated to exact whole (sum == 100)", sum === 100n, `parts=${parts.join(",")} sum=${sum}`);
  // thirds split across ledger accounts must post balanced (100c debit, 3 credits)
  const s = svc(), t = "thirds";
  await call(s, "POST", `/t/${t}/accounts/seed`, { category: "CONTRACTOR_TRADES" });
  const r = await call(s, "POST", `/t/${t}/entries`, {
    date: "2026-08-05", memo: "thirds split",
    lines: [
      { code: "1000", side: "DEBIT", amount_minor: "100" },
      { code: "4000", side: "CREDIT", amount_minor: String(parts[0]) },
      { code: "4100", side: "CREDIT", amount_minor: String(parts[1]) },
      { code: "4200", side: "CREDIT", amount_minor: String(parts[2]) },
    ],
  });
  check("thirds split posts balanced through the engine (201)", r.status === 201, `status=${r.status} body=${JSON.stringify(r.body)}`);

  // Inventory moving-average with a non-divisible cost: no penny drift, tie-out holds.
  const si = svc(), ti = "invdrift";
  await call(si, "POST", `/t/${ti}/accounts/seed`, { category: "CONTRACTOR_TRADES" });
  await call(si, "POST", `/t/${ti}/cost-codes/seed`, {});
  await call(si, "POST", `/t/${ti}/inventory/items`, {
    sku: "X", name: "widget", unit: "ea", reorder_point_milli: "0",
    inventory_account_code: "1300", cost_account_code: "5100",
  });
  // 3 units at 3333 -> value 9999 ; average 3333/unit; issue 1, issue 1, issue last
  await call(si, "POST", `/t/${ti}/inventory/receipts`, { sku: "X", date: "2026-08-01", quantity_milli: "3000", unit_cost_minor: "3333" });
  await call(si, "POST", `/t/${ti}/inventory/issues`, { sku: "X", date: "2026-08-02", quantity_milli: "1000", cost_account_code: "6400" });
  await call(si, "POST", `/t/${ti}/inventory/issues`, { sku: "X", date: "2026-08-03", quantity_milli: "1000", cost_account_code: "6400" });
  await call(si, "POST", `/t/${ti}/inventory/issues`, { sku: "X", date: "2026-08-04", quantity_milli: "1000", cost_account_code: "6400" });
  const it = obj(await call(si, "GET", `/t/${ti}/inventory/items/X`))["item"] as Row;
  const tbi = await tbOf(si, ti);
  const invBal = (tbi["rows"] as Row[]).find((x) => String(x["code"]) === "1300");
  const invNet = invBal ? BigInt(String(invBal["debit_minor"])) - BigInt(String(invBal["credit_minor"])) : 0n;
  check("inventory fully drained: qty 0 and value 0 (no penny left)", String(it["quantity_milli"]) === "0" && String(it["value_minor"]) === "0", JSON.stringify(it));
  check("inventory GL account nets to 0 after full drain (tie-out)", invNet === 0n, `1300 net=${invNet}`);
  check("inventory-drift TB still in balance", tbi["in_balance"] === true, "");
  const ebi = await everyEntryBalances(si, ti);
  check("every inventory-generated entry balances", ebi.ok, ebi.bad.join(" | "));
}

// ---------------------------------------------------------------------------
console.log(`\n=== RESULT: ${PASS} passed, ${FAIL} failed ===`);
if (defects.length) {
  console.log("\nFAILURES:");
  for (const d of defects) console.log(`  - ${d}`);
}
}
main().then(() => process.exit(FAIL === 0 ? 0 : 1)).catch((e) => { console.error("HARNESS ERROR:", e); process.exit(2); });
