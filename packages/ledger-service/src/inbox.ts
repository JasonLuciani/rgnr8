import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  type Currency,
  type EntryId,
  type PostCommand,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { LedgerBackend } from "./backend.js";
import type { DocKind, DocRecord } from "./documents.js";
import type { FeedRuleRecord, FeedStatus, FeedTxnRecord } from "./feed.js";
import { LearnedModel, suggestFor, type Suggestion } from "./suggest.js";
import { recordPayment, type ArApContext } from "./arap.js";
import { validateDimensions } from "./dimensions.js";

/**
 * The review inbox — where a bank line becomes an accounting fact.
 *
 * A feed line arrives claiming money moved. Somebody has to say what the other
 * side of it was, and there are only four honest answers:
 *
 * - **Accept** — it's income or a cost: post a balanced journal entry against
 *   the chosen account.
 * - **Match** — it's the payment of an invoice you raised or a bill you entered:
 *   apply it to that document, so the open item clears instead of a duplicate
 *   revenue or expense appearing.
 * - **Exclude** — it isn't a business event (a personal card swipe, a duplicate
 *   the bank sent twice): record the decision, post nothing.
 * - **Leave it** — you don't know yet. It stays in the queue.
 *
 * Undo is a **reversal**, never a delete: the original entry stays in the
 * journal with a linked reversing entry beside it, and the feed line returns to
 * the queue. That's the difference between books you can defend and books you
 * can edit.
 */

export class InboxError extends Error {}

export interface InboxContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

// --- matching ---------------------------------------------------------------

export interface MatchCandidate {
  readonly kind: "invoice" | "bill" | "transfer";
  readonly id: string;
  readonly label: string;
  readonly date: string;
  readonly openMinor: string;
  /** How good the fit is: "exact" amount, or "partial" (it would part-pay). */
  readonly fit: "exact" | "partial";
}

/** Days either side of the document date we'll still call a plausible match. */
const MATCH_WINDOW_DAYS = 120;

function daysBetween(a: string, b: string): number {
  // Both are YYYY-MM-DD; compare as UTC days without touching a wall clock.
  const toDays = (iso: string): number =>
    Math.floor(Date.parse(`${iso}T00:00:00Z`) / 86_400_000);
  return Math.abs(toDays(a) - toDays(b));
}

/**
 * What open documents could this line be paying?
 *
 * Money in can only settle an invoice; money out can only settle a bill — the
 * sign of the transaction decides which side we even look at. Exact-amount fits
 * come first, then partials, then by proximity in time, so the obvious match is
 * the first thing the owner sees.
 */
export async function matchCandidates(
  ctx: InboxContext,
  txn: FeedTxnRecord,
  docs: readonly DocRecord[],
  feed: readonly FeedTxnRecord[],
): Promise<MatchCandidate[]> {
  const amount = BigInt(txn.amountMinor);
  const inflow = amount >= 0n;
  const magnitude = inflow ? amount : -amount;
  const wanted: DocKind = inflow ? "invoice" : "bill";

  const out: MatchCandidate[] = [];
  for (const doc of docs) {
    if (doc.kind !== wanted) continue;
    const open = BigInt(doc.openMinor);
    if (open <= 0n) continue;
    if (magnitude > open) continue; // would overpay — not a match, a mistake
    if (daysBetween(doc.date, txn.date) > MATCH_WINDOW_DAYS) continue;
    out.push({
      kind: wanted,
      id: doc.id,
      label: doc.memo || doc.id,
      date: doc.date,
      openMinor: doc.openMinor,
      fit: magnitude === open ? "exact" : "partial",
    });
  }
  out.sort((a, b) => {
    if (a.fit !== b.fit) return a.fit === "exact" ? -1 : 1;
    return daysBetween(a.date, txn.date) - daysBetween(b.date, txn.date);
  });

  // The other leg of an internal transfer: same magnitude, opposite sign, on a
  // different bank account, within a few days. Neither leg is income or a cost.
  for (const other of feed) {
    if (other.id === txn.id) continue;
    if (other.accountCode === txn.accountCode) continue;
    if (other.status !== "PENDING") continue;
    if (BigInt(other.amountMinor) !== -amount) continue;
    if (daysBetween(other.date, txn.date) > 5) continue;
    out.push({
      kind: "transfer",
      id: other.id,
      label: `Transfer ${inflow ? "from" : "to"} ${other.accountCode} — ${other.description}`,
      date: other.date,
      openMinor: other.amountMinor,
      fit: "exact",
    });
  }
  return out;
}

// --- the queue --------------------------------------------------------------

export interface InboxItem {
  readonly id: string;
  readonly account_code: string;
  readonly date: string;
  readonly amount_minor: string;
  readonly description: string;
  readonly counterparty: string;
  readonly status: FeedStatus;
  readonly category_code: string;
  readonly entry_id: string;
  readonly doc_kind: string;
  readonly doc_id: string;
  readonly note: string;
  readonly suggestion: {
    readonly account_code: string;
    readonly account_name: string;
    readonly confidence: number;
    readonly reason: string;
    readonly source: string;
    readonly rule_id: string;
  };
  readonly matches: readonly MatchCandidate[];
}

export interface InboxView {
  readonly account_code: string;
  readonly pending: number;
  readonly posted: number;
  readonly matched: number;
  readonly excluded: number;
  readonly pending_in_minor: string;
  readonly pending_out_minor: string;
  readonly items: readonly InboxItem[];
}

/** The review queue for one account (or all of them), with suggestions attached. */
export async function inboxView(
  ctx: InboxContext,
  opts: { accountCode?: string; status?: FeedStatus } = {},
): Promise<InboxView> {
  const store = ctx.backend.feed();
  const tenant = String(ctx.tenant);
  const all = await store.listTxns(tenant);
  const rules = await store.listRules(tenant);
  const learned = new LearnedModel(all);
  const chart = await ctx.backend.chart(ctx.tenant);

  const scoped = opts.accountCode
    ? all.filter((t) => t.accountCode === opts.accountCode)
    : all;
  const wanted = opts.status ?? "PENDING";
  const shown = scoped.filter((t) => t.status === wanted);

  const openDocs = await allDocuments(ctx);
  const items: InboxItem[] = [];
  for (const txn of shown) {
    const suggestion = suggestFor(txn, rules, learned);
    const matches = wanted === "PENDING" ? await matchCandidates(ctx, txn, openDocs, all) : [];
    items.push({
      id: txn.id,
      account_code: txn.accountCode,
      date: txn.date,
      amount_minor: txn.amountMinor,
      description: txn.description,
      counterparty: txn.counterparty,
      status: txn.status,
      category_code: txn.categoryCode,
      entry_id: txn.entryId,
      doc_kind: txn.docKind,
      doc_id: txn.docId,
      note: txn.note,
      suggestion: {
        account_code: suggestion.accountCode,
        account_name: suggestion.accountCode
          ? (chart.getByCode(suggestion.accountCode)?.name ?? suggestion.accountCode)
          : "",
        confidence: suggestion.confidence,
        reason: suggestion.reason,
        source: suggestion.source,
        rule_id: suggestion.ruleId,
      },
      matches,
    });
  }

  let pendingIn = 0n;
  let pendingOut = 0n;
  for (const t of scoped) {
    if (t.status !== "PENDING") continue;
    const v = BigInt(t.amountMinor);
    if (v >= 0n) pendingIn += v;
    else pendingOut += v;
  }

  return {
    account_code: opts.accountCode ?? "",
    pending: scoped.filter((t) => t.status === "PENDING").length,
    posted: scoped.filter((t) => t.status === "POSTED").length,
    matched: scoped.filter((t) => t.status === "MATCHED").length,
    excluded: scoped.filter((t) => t.status === "EXCLUDED").length,
    pending_in_minor: pendingIn.toString(),
    pending_out_minor: pendingOut.toString(),
    items,
  };
}

/** Every invoice and bill the tenant has, for the matcher to sift. */
async function allDocuments(ctx: InboxContext): Promise<DocRecord[]> {
  const docs = ctx.backend.documents();
  const tenant = String(ctx.tenant);
  const [invoices, bills] = await Promise.all([
    docs.listDocs(tenant, "invoice"),
    docs.listDocs(tenant, "bill"),
  ]);
  return [...invoices, ...bills];
}

// --- landing new lines ------------------------------------------------------

export interface FeedTxnInput {
  readonly id: string;
  readonly date: string;
  readonly amount_minor: string | number;
  readonly description?: string;
  readonly counterparty?: string;
}

export interface DeliverResult {
  readonly received: number;
  readonly added: number;
  readonly duplicates: number;
  readonly auto_posted: number;
  readonly pending: number;
}

/**
 * Land a batch of feed lines in the inbox. Idempotent by transaction id, so a
 * re-synced overlapping window adds nothing. Lines matching an **auto-post**
 * rule skip the queue and post immediately — which is only ever true because
 * the owner explicitly wrote a rule saying so.
 */
export async function deliverFeed(
  ctx: InboxContext,
  accountCode: string,
  txns: readonly FeedTxnInput[],
  source: string,
): Promise<DeliverResult> {
  const chart = await ctx.backend.chart(ctx.tenant);
  if (!chart.getByCode(accountCode)) {
    throw new InboxError(`unknown account code ${accountCode}`);
  }
  if (!Array.isArray(txns) || txns.length === 0) {
    throw new InboxError("no transactions delivered");
  }

  const records: FeedTxnRecord[] = txns.map((t) => {
    if (!t || typeof t.id !== "string" || !t.id.trim()) {
      throw new InboxError("each transaction needs an id");
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(t.date))) {
      throw new InboxError(`transaction ${t.id}: date must be YYYY-MM-DD`);
    }
    let minor: bigint;
    try {
      minor = BigInt(t.amount_minor);
    } catch {
      throw new InboxError(`transaction ${t.id}: amount_minor must be an integer`);
    }
    if (minor === 0n) throw new InboxError(`transaction ${t.id}: amount cannot be zero`);
    return {
      id: t.id,
      accountCode,
      date: String(t.date),
      amountMinor: minor.toString(),
      description: String(t.description ?? ""),
      counterparty: String(t.counterparty ?? ""),
      status: "PENDING",
      categoryCode: "",
      entryId: "",
      docKind: "",
      docId: "",
      note: "",
      source,
      revision: 0,
    };
  });

  const store = ctx.backend.feed();
  const tenant = String(ctx.tenant);
  const added = await store.addTxns(tenant, records);

  // Auto-post anything an explicit rule says to post without review.
  const rules = await store.listRules(tenant);
  const autoRules = rules.filter((r) => r.autoPost);
  let autoPosted = 0;
  if (autoRules.length > 0) {
    const learned = new LearnedModel([]);
    for (const record of records) {
      const fresh = await store.getTxn(tenant, record.id);
      if (!fresh || fresh.status !== "PENDING") continue;
      const suggestion = suggestFor(fresh, autoRules, learned);
      if (suggestion.source !== "rule" || !suggestion.autoPost) continue;
      try {
        await acceptTxn(ctx, fresh.id, suggestion.accountCode);
        autoPosted += 1;
      } catch {
        // A rule that can't post (closed period, deleted account) must not sink
        // the batch — the line simply stays in the queue for a human.
      }
    }
  }

  const pending = (await store.listTxns(tenant, { status: "PENDING" })).length;
  return {
    received: records.length,
    added,
    duplicates: records.length - added,
    auto_posted: autoPosted,
    pending,
  };
}

// --- actions ----------------------------------------------------------------

function provenanceFor(source: string, date: string, at: string): Provenance {
  return {
    sourceSystem: source,
    sourceObject: "bank.feed",
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/inbox-1",
    mappingVersion: "ledger-service/inbox-1",
  };
}

async function requirePending(ctx: InboxContext, id: string): Promise<FeedTxnRecord> {
  const txn = await ctx.backend.feed().getTxn(String(ctx.tenant), id);
  if (!txn) throw new InboxError(`unknown transaction ${id}`);
  if (txn.status !== "PENDING") {
    throw new InboxError(
      `${id} was already actioned (${txn.status.toLowerCase()}) — undo it first`,
    );
  }
  return txn;
}

export interface ActionResult {
  readonly id: string;
  readonly status: FeedStatus;
  readonly entry_id: string;
  readonly doc_kind: string;
  readonly doc_id: string;
  readonly category_code: string;
}

const result = (txn: FeedTxnRecord): ActionResult => ({
  id: txn.id,
  status: txn.status,
  entry_id: txn.entryId,
  doc_kind: txn.docKind,
  doc_id: txn.docId,
  category_code: txn.categoryCode,
});

/**
 * Categorize a line and post it. Money in credits the chosen account and debits
 * the bank; money out does the reverse. The idempotency key is derived from the
 * feed id, so the same line can never post twice even under a retry.
 */
export async function acceptTxn(
  ctx: InboxContext,
  id: string,
  categoryCode: string,
  dimensions?: Record<string, string>,
): Promise<ActionResult> {
  const txn = await requirePending(ctx, id);
  const code = categoryCode.trim();
  if (!code) throw new InboxError("a category account code is required");

  const chart = await ctx.backend.chart(ctx.tenant);
  const bank = chart.getByCode(txn.accountCode);
  if (!bank) throw new InboxError(`unknown account code ${txn.accountCode}`);
  const category = chart.getByCode(code);
  if (!category) throw new InboxError(`unknown account code ${code}`);
  if (category.id === bank.id) {
    throw new InboxError("a transaction cannot be categorized to its own bank account");
  }

  const amount = BigInt(txn.amountMinor);
  const inflow = amount >= 0n;
  const magnitude = Money.fromMinorUnits(inflow ? amount : -amount, ctx.currency);
  const memo = txn.description || txn.counterparty || `Bank ${inflow ? "deposit" : "payment"}`;

  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  const command: PostCommand = {
      tenantId: ctx.tenant,
      // The revision is what lets a line be re-categorized after an undo: the
      // reversed entry keeps the old key, the new one gets its own.
      idempotencyKey: asIdempotencyKey(`feed:${txn.id}#${txn.revision}`),
      periodKey: asPeriodKey(txn.date.slice(0, 7)),
      currency: ctx.currency,
      entryDate: txn.date,
      memo,
      provenance: provenanceFor(txn.source || "feed", txn.date, ctx.now()),
      // The bank side of a feed line carries no class: money in the account is
      // not attributable to a line of business, only what it was for is.
      lines: inflow
        ? [
            { accountId: bank.id, side: "DEBIT", amount: magnitude },
            {
              accountId: category.id, side: "CREDIT", amount: magnitude,
              ...(dimensions ? { dimensions } : {}),
            },
          ]
        : [
            {
              accountId: category.id, side: "DEBIT", amount: magnitude,
              ...(dimensions ? { dimensions } : {}),
            },
            { accountId: bank.id, side: "CREDIT", amount: magnitude },
          ],
  };
  await validateDimensions(
    { backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency }, command,
  );
  const entry = await engine.post(command, { postedAt: ctx.now() });

  const updated: FeedTxnRecord = {
    ...txn, status: "POSTED", categoryCode: code, entryId: String(entry.id),
  };
  await ctx.backend.feed().updateTxn(String(ctx.tenant), updated);
  return result(updated);
}

/**
 * Match a line to the invoice or bill it settles. This posts through the AR/AP
 * path, so the open item clears — rather than a second revenue or expense line
 * appearing next to the one the document already booked.
 */
export async function matchTxn(
  ctx: InboxContext,
  id: string,
  docKind: string,
  docId: string,
): Promise<ActionResult> {
  const txn = await requirePending(ctx, id);
  if (docKind !== "invoice" && docKind !== "bill") {
    throw new InboxError("doc_kind must be invoice or bill");
  }
  const amount = BigInt(txn.amountMinor);
  const inflow = amount >= 0n;
  if (inflow && docKind !== "invoice") {
    throw new InboxError("money in can only settle an invoice");
  }
  if (!inflow && docKind !== "bill") {
    throw new InboxError("money out can only settle a bill");
  }

  const arap: ArApContext = {
    tenant: String(ctx.tenant),
    chart: await ctx.backend.chart(ctx.tenant),
    store: ctx.backend.store(ctx.tenant),
    periods: ctx.backend.periods(ctx.tenant),
    docs: ctx.backend.documents(),
    currency: ctx.currency,
    postedAt: ctx.now(),
  };
  const paid = await recordPayment(
    docKind,
    docId,
    {
      id: `feed:${txn.id}#${txn.revision}`,
      date: txn.date,
      amount_minor: (inflow ? amount : -amount).toString(),
      bank_code: txn.accountCode,
      memo: txn.description,
    },
    arap,
  );

  const updated: FeedTxnRecord = {
    ...txn,
    status: "MATCHED",
    entryId: paid.entryId,
    docKind,
    docId,
  };
  await ctx.backend.feed().updateTxn(String(ctx.tenant), updated);
  return result(updated);
}

/** Mark a line as not a business event. Nothing is posted; the reason is kept. */
export async function excludeTxn(
  ctx: InboxContext,
  id: string,
  reason: string,
): Promise<ActionResult> {
  const txn = await requirePending(ctx, id);
  const updated: FeedTxnRecord = { ...txn, status: "EXCLUDED", note: reason.trim() };
  await ctx.backend.feed().updateTxn(String(ctx.tenant), updated);
  return result(updated);
}

/**
 * Undo an actioned line. Any entry it posted is **reversed**, not deleted — the
 * original stays in the journal with a linked reversing entry beside it — and
 * the line returns to the queue.
 */
export async function undoTxn(ctx: InboxContext, id: string): Promise<ActionResult> {
  const store = ctx.backend.feed();
  const txn = await store.getTxn(String(ctx.tenant), id);
  if (!txn) throw new InboxError(`unknown transaction ${id}`);
  if (txn.status === "PENDING") throw new InboxError(`${id} is already in the queue`);

  if (txn.entryId) {
    const chart = await ctx.backend.chart(ctx.tenant);
    const engine = new PostingEngine(
      chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
    );
    await engine.reverse(ctx.tenant, txn.entryId as EntryId, {
      idempotencyKey: asIdempotencyKey(`feed-undo:${txn.id}#${txn.revision}`),
      periodKey: asPeriodKey(txn.date.slice(0, 7)),
      entryDate: txn.date,
      postedAt: ctx.now(),
      provenance: provenanceFor(txn.source || "feed", txn.date, ctx.now()),
      memo: `Undo of bank line ${txn.id}`,
    });
  }

  // A matched line also has to give the open item back, or the invoice stays
  // settled while the money that settled it has been reversed out.
  if (txn.status === "MATCHED" && txn.docKind && txn.docId) {
    const docs = ctx.backend.documents();
    const kind = txn.docKind as DocKind;
    const doc = await docs.getDoc(String(ctx.tenant), kind, txn.docId);
    if (doc) {
      const amount = BigInt(txn.amountMinor);
      const restored = BigInt(doc.openMinor) + (amount >= 0n ? amount : -amount);
      const total = BigInt(doc.totalMinor);
      await docs.setOpen(
        String(ctx.tenant), kind, doc.id, restored.toString(),
        restored >= total ? "OPEN" : "PARTIAL",
      );
    }
  }

  const updated: FeedTxnRecord = {
    ...txn, status: "PENDING", entryId: "", docKind: "", docId: "", note: "",
    revision: txn.revision + 1,
  };
  await store.updateTxn(String(ctx.tenant), updated);
  return result(updated);
}

export interface BulkResult {
  readonly accepted: number;
  readonly skipped: number;
  readonly failures: readonly { readonly id: string; readonly error: string }[];
}

/**
 * Accept every pending line whose suggestion is at least `minConfidence`.
 *
 * The floor is deliberate and has no default of zero: bulk-accepting a guess is
 * how a queue gets cleared and the books get wrong. Lines below the bar, and
 * lines with no suggestion at all, are left exactly where they are.
 */
export async function bulkAccept(
  ctx: InboxContext,
  minConfidence: number,
  accountCode?: string,
): Promise<BulkResult> {
  if (!(minConfidence > 0) || minConfidence > 1) {
    throw new InboxError("min_confidence must be between 0 (exclusive) and 1");
  }
  const store = ctx.backend.feed();
  const tenant = String(ctx.tenant);
  const all = await store.listTxns(tenant);
  const rules = await store.listRules(tenant);
  const learned = new LearnedModel(all);

  let accepted = 0;
  let skipped = 0;
  const failures: { id: string; error: string }[] = [];
  for (const txn of all) {
    if (txn.status !== "PENDING") continue;
    if (accountCode && txn.accountCode !== accountCode) continue;
    const suggestion: Suggestion = suggestFor(txn, rules, learned);
    if (!suggestion.accountCode || suggestion.confidence < minConfidence) {
      skipped += 1;
      continue;
    }
    try {
      await acceptTxn(ctx, txn.id, suggestion.accountCode);
      accepted += 1;
    } catch (err) {
      failures.push({ id: txn.id, error: err instanceof Error ? err.message : String(err) });
    }
  }
  return { accepted, skipped, failures };
}

// --- rules ------------------------------------------------------------------

export interface RuleInput {
  readonly id?: string;
  readonly priority?: number;
  readonly account_code?: string;
  readonly description_contains?: string;
  readonly counterparty_equals?: string;
  readonly sign?: string;
  readonly auto_post?: boolean;
}

export function ruleJson(rule: FeedRuleRecord): Record<string, unknown> {
  return {
    id: rule.id,
    priority: rule.priority,
    account_code: rule.accountCode,
    description_contains: rule.descriptionContains,
    counterparty_equals: rule.counterpartyEquals,
    sign: rule.sign,
    auto_post: rule.autoPost,
  };
}

export async function saveRule(ctx: InboxContext, input: RuleInput): Promise<FeedRuleRecord> {
  const code = String(input.account_code ?? "").trim();
  if (!code) throw new InboxError("account_code is required");
  const chart = await ctx.backend.chart(ctx.tenant);
  if (!chart.getByCode(code)) throw new InboxError(`unknown account code ${code}`);

  const description = String(input.description_contains ?? "").trim();
  const counterparty = String(input.counterparty_equals ?? "").trim();
  const sign = String(input.sign ?? "").trim().toLowerCase();
  if (sign && sign !== "in" && sign !== "out") {
    throw new InboxError('sign must be "in", "out", or empty');
  }
  if (!description && !counterparty && !sign) {
    // A rule with no predicates would categorize the entire feed to one account.
    throw new InboxError("a rule needs at least one condition to match on");
  }

  const id = String(input.id ?? "").trim() || `rule-${description || counterparty || sign}`;
  const rule: FeedRuleRecord = {
    id,
    priority: Number.isFinite(Number(input.priority)) ? Number(input.priority) : 100,
    accountCode: code,
    descriptionContains: description,
    counterpartyEquals: counterparty,
    sign,
    autoPost: input.auto_post === true,
  };
  await ctx.backend.feed().saveRule(String(ctx.tenant), rule);
  return rule;
}

/** How many pending lines this rule would categorize, if applied right now. */
export async function ruleImpact(ctx: InboxContext, rule: FeedRuleRecord): Promise<number> {
  const pending = await ctx.backend.feed().listTxns(String(ctx.tenant), { status: "PENDING" });
  const learned = new LearnedModel([]);
  return pending.filter((t) => suggestFor(t, [rule], learned).source === "rule").length;
}
