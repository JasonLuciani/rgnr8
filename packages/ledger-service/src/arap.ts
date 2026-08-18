import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  type AccountId,
  type ChartOfAccounts,
  type Currency,
  type JournalLineInput,
  type LedgerStore,
  type PeriodStore,
  type PostCommand,
  type PostedEntry,
  type Provenance,
} from "@rgnr8/ledger-kernel";
import {
  agingFromOpenItems,
  agingReportJson,
  dueDateFromTerms,
  invoiceToPostCommand,
  billToPostCommand,
  type AgingReportJson,
  type DocPostContext,
  type DocumentLine,
  type PartyOpenItem,
} from "@rgnr8/subledger";
import type {
  DocKind,
  DocLineRecord,
  DocRecord,
  DocStatus,
  DocumentStore,
  PartyKind,
  PartyRecord,
} from "./documents.js";

/**
 * Accounts receivable and payable — the day-to-day accounting an owner actually
 * lives in: invoice a customer, watch what's owed, collect it; enter a bill,
 * see what's due, pay it.
 *
 * Each document does two things that must not drift apart: it **posts a balanced
 * journal entry** (an invoice debits AR and credits income; collecting it debits
 * the bank and credits AR) and it **records the open item** so "who owes me, and
 * how late" is answerable. The posting goes through the same kernel as every
 * other entry, so AR/AP can never quietly unbalance the books, and the control
 * account always ties back to the sum of open documents.
 */

export class ArApError extends Error {}

/** Control accounts the AR/AP flows post against, overridable per request. */
export interface ControlAccounts {
  readonly arCode: string;
  readonly apCode: string;
  readonly bankCode: string;
}

export const DEFAULT_CONTROLS: ControlAccounts = {
  arCode: "1200",
  apCode: "2000",
  bankCode: "1000",
};

export interface DocumentLineInput {
  readonly description?: string;
  readonly quantity?: number;
  readonly unit_amount_minor: string | number;
  /** Income account for an invoice line; expense account for a bill line. */
  readonly account_code: string;
}

export interface CreateDocumentRequest {
  readonly id: string;
  readonly party_id: string;
  readonly date: string;
  readonly due_date?: string;
  readonly terms_days?: number;
  readonly memo?: string;
  readonly lines: readonly DocumentLineInput[];
  readonly accounts?: Partial<ControlAccounts>;
}

export interface RecordPaymentRequest {
  readonly id?: string;
  readonly date: string;
  readonly amount_minor: string | number;
  readonly bank_code?: string;
  readonly memo?: string;
}

interface Ctx {
  readonly tenant: string;
  readonly chart: ChartOfAccounts;
  readonly store: LedgerStore;
  readonly periods: PeriodStore;
  readonly docs: DocumentStore;
  readonly currency: Currency;
  readonly postedAt: string;
}

function idFor(chart: ChartOfAccounts, code: string, role: string): AccountId {
  const account = chart.getByCode(code);
  if (!account) throw new ArApError(`${role}: unknown account code ${code}`);
  return account.id;
}

function controls(overrides: Partial<ControlAccounts> | undefined): ControlAccounts {
  return {
    arCode: overrides?.arCode ?? DEFAULT_CONTROLS.arCode,
    apCode: overrides?.apCode ?? DEFAULT_CONTROLS.apCode,
    bankCode: overrides?.bankCode ?? DEFAULT_CONTROLS.bankCode,
  };
}

function minorOf(v: string | number, what: string): bigint {
  try {
    return BigInt(v);
  } catch {
    throw new ArApError(`${what} must be an integer minor-unit value`);
  }
}

function provenanceFor(kind: string, date: string, at: string): Provenance {
  return {
    sourceSystem: "rgnr8",
    sourceObject: kind,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "arap/1",
    mappingVersion: "arap/1",
  };
}

function validateDoc(req: CreateDocumentRequest): void {
  if (!req.id?.trim()) throw new ArApError("a document needs an id");
  if (!req.party_id?.trim()) throw new ArApError("a document needs a party");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(req.date))) {
    throw new ArApError("date must be YYYY-MM-DD");
  }
  if (!Array.isArray(req.lines) || req.lines.length === 0) {
    throw new ArApError("a document needs at least one line");
  }
}

/** Turn the wire line shape into the subledger's DocumentLine + a stored record. */
function buildLines(
  req: CreateDocumentRequest,
  chart: ChartOfAccounts,
  currency: Currency,
): { docLines: DocumentLine[]; records: DocLineRecord[]; totalMinor: bigint } {
  const docLines: DocumentLine[] = [];
  const records: DocLineRecord[] = [];
  let totalMinor = 0n;

  for (const l of req.lines) {
    const code = String(l.account_code ?? "").trim();
    const accountId = idFor(chart, code, `line ${code || "(blank)"}`);
    const unit = minorOf(l.unit_amount_minor, `line ${code} unit_amount_minor`);
    if (unit <= 0n) throw new ArApError(`line ${code}: unit amount must be positive`);
    const qty = l.quantity ?? 1;
    if (!Number.isInteger(qty) || qty < 1) {
      throw new ArApError(`line ${code}: quantity must be a positive whole number`);
    }
    const amount = unit * BigInt(qty);
    totalMinor += amount;
    docLines.push({
      accountId,
      quantity: qty,
      unitAmount: Money.fromMinorUnits(unit, currency),
      ...(l.description ? { description: l.description } : {}),
    });
    records.push({
      description: l.description ?? "",
      quantity: qty,
      unitAmountMinor: unit.toString(),
      accountCode: code,
      amountMinor: amount.toString(),
    });
  }
  return { docLines, records, totalMinor };
}

function statusFor(openMinor: bigint, totalMinor: bigint): DocStatus {
  if (openMinor <= 0n) return "PAID";
  return openMinor === totalMinor ? "OPEN" : "PARTIAL";
}

async function post(ctx: Ctx, command: PostCommand): Promise<PostedEntry> {
  const engine = new PostingEngine(ctx.chart, ctx.store, ctx.periods);
  return engine.post(command, { postedAt: ctx.postedAt });
}

// --- invoices + bills --------------------------------------------------------

export interface CreateDocumentResult {
  readonly doc: DocRecord;
  readonly entryId: string;
}

/**
 * Raise an invoice (AR) or enter a bill (AP): post the balanced journal entry
 * and store the open item. Idempotent through the kernel's posting key, so a
 * retried request never books the same document twice.
 */
export async function createDocument(
  kind: DocKind,
  req: CreateDocumentRequest,
  ctx: Ctx,
): Promise<CreateDocumentResult> {
  validateDoc(req);
  const existing = await ctx.docs.getDoc(ctx.tenant, kind, req.id);
  if (existing) throw new ArApError(`${kind} ${req.id} already exists`);

  const partyKind: PartyKind = kind === "invoice" ? "customer" : "vendor";
  const party = await ctx.docs.getParty(ctx.tenant, partyKind, req.party_id);
  if (!party) throw new ArApError(`unknown ${partyKind} ${req.party_id}`);

  const ctrl = controls(req.accounts);
  const { docLines, records, totalMinor } = buildLines(req, ctx.chart, ctx.currency);
  if (totalMinor <= 0n) throw new ArApError("a document must total more than zero");

  const termsDays = req.terms_days ?? party.termsDays ?? 30;
  const dueDate = req.due_date ?? dueDateFromTerms(req.date, termsDays);

  const docCtx: DocPostContext = {
    tenantId: ctx.tenant,
    currency: ctx.currency,
    provenance: provenanceFor(kind, req.date, ctx.postedAt),
  };

  const command =
    kind === "invoice"
      ? invoiceToPostCommand(
          {
            id: req.id,
            customerId: req.party_id,
            issueDate: req.date,
            dueDate,
            lines: docLines,
            ...(req.memo ? { memo: req.memo } : {}),
          },
          idFor(ctx.chart, ctrl.arCode, "AR control"),
          docCtx,
        )
      : billToPostCommand(
          {
            id: req.id,
            vendorId: req.party_id,
            billDate: req.date,
            dueDate,
            lines: docLines,
            ...(req.memo ? { memo: req.memo } : {}),
          },
          idFor(ctx.chart, ctrl.apCode, "AP control"),
          docCtx,
        );

  const entry = await post(ctx, command);

  const doc: DocRecord = {
    id: req.id,
    kind,
    partyId: req.party_id,
    date: req.date,
    dueDate,
    totalMinor: totalMinor.toString(),
    openMinor: totalMinor.toString(),
    status: "OPEN",
    memo: req.memo ?? "",
    lines: records,
  };
  await ctx.docs.saveDoc(ctx.tenant, doc);
  return { doc, entryId: entry.id };
}

// --- payments ----------------------------------------------------------------

export interface RecordPaymentResult {
  readonly doc: DocRecord;
  readonly entryId: string;
  readonly appliedMinor: string;
}

/**
 * Collect against an invoice, or pay a bill. Posts the cash movement against the
 * control account and reduces the open item. Overpayment is refused rather than
 * silently absorbed — a payment bigger than the balance is nearly always a typo,
 * and the ledger should say so.
 */
export async function recordPayment(
  kind: DocKind,
  docId: string,
  req: RecordPaymentRequest,
  ctx: Ctx,
  accounts?: Partial<ControlAccounts>,
): Promise<RecordPaymentResult> {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(req.date))) {
    throw new ArApError("date must be YYYY-MM-DD");
  }
  const doc = await ctx.docs.getDoc(ctx.tenant, kind, docId);
  if (!doc) throw new ArApError(`unknown ${kind} ${docId}`);

  const amount = minorOf(req.amount_minor, "amount_minor");
  if (amount <= 0n) throw new ArApError("a payment must be positive");
  const open = BigInt(doc.openMinor);
  if (open <= 0n) throw new ArApError(`${kind} ${docId} is already settled`);
  if (amount > open) {
    throw new ArApError(
      `payment ${amount} exceeds the ${open} still open on ${kind} ${docId}`,
    );
  }

  const ctrl = controls({ ...accounts, ...(req.bank_code ? { bankCode: req.bank_code } : {}) });
  const bank = idFor(ctx.chart, ctrl.bankCode, "bank");
  const control = idFor(
    ctx.chart,
    kind === "invoice" ? ctrl.arCode : ctrl.apCode,
    kind === "invoice" ? "AR control" : "AP control",
  );
  const money = Money.fromMinorUnits(amount, ctx.currency);

  // Collecting an invoice: Dr bank / Cr AR. Paying a bill: Dr AP / Cr bank.
  const lines: JournalLineInput[] =
    kind === "invoice"
      ? [
          { accountId: bank, side: "DEBIT", amount: money, memo: `Payment on ${docId}` },
          { accountId: control, side: "CREDIT", amount: money, memo: `Payment on ${docId}` },
        ]
      : [
          { accountId: control, side: "DEBIT", amount: money, memo: `Payment of ${docId}` },
          { accountId: bank, side: "CREDIT", amount: money, memo: `Payment of ${docId}` },
        ];

  const paymentId = req.id?.trim() || `${docId}-p${(await ctx.docs.listPayments(ctx.tenant, kind, docId)).length + 1}`;
  const command: PostCommand = {
    tenantId: asTenantId(ctx.tenant),
    idempotencyKey: asIdempotencyKey(`${kind}pay:${paymentId}`),
    periodKey: asPeriodKey(req.date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: req.date,
    memo: req.memo ?? (kind === "invoice" ? `Payment received on ${docId}` : `Payment of ${docId}`),
    provenance: provenanceFor(`${kind}.payment`, req.date, ctx.postedAt),
    lines,
  };
  const entry = await post(ctx, command);

  const remaining = open - amount;
  const status = statusFor(remaining, BigInt(doc.totalMinor));
  await ctx.docs.setOpen(ctx.tenant, kind, docId, remaining.toString(), status);
  await ctx.docs.addPayment(ctx.tenant, {
    id: paymentId,
    docKind: kind,
    docId,
    date: req.date,
    amountMinor: amount.toString(),
    bankCode: ctrl.bankCode,
  });

  return {
    doc: { ...doc, openMinor: remaining.toString(), status },
    entryId: entry.id,
    appliedMinor: amount.toString(),
  };
}

// --- aging -------------------------------------------------------------------

/** Age the open documents of one kind, by party, as of a date. */
export async function aging(
  kind: DocKind,
  asOf: string,
  ctx: Ctx,
): Promise<AgingReportJson> {
  const docs = await ctx.docs.listDocs(ctx.tenant, kind);
  const items: PartyOpenItem[] = docs
    .filter((d) => BigInt(d.openMinor) > 0n)
    .map((d) => ({
      partyId: d.partyId,
      dueDate: d.dueDate,
      openAmount: Money.fromMinorUnits(BigInt(d.openMinor), ctx.currency),
    }));
  return agingReportJson(
    agingFromOpenItems(items, asOf, ctx.currency),
    kind === "invoice" ? "AR" : "AP",
  );
}

// --- parties -----------------------------------------------------------------

export function validateParty(body: Record<string, unknown>): PartyRecord {
  const id = String(body["id"] ?? "").trim();
  const name = String(body["name"] ?? "").trim();
  if (!id || !name) throw new ArApError("a customer/vendor needs an id and a name");
  const email = String(body["email"] ?? "").trim();
  const termsRaw = body["terms_days"];
  const termsDays =
    termsRaw === undefined || termsRaw === null ? undefined : Number(termsRaw);
  if (termsDays !== undefined && (!Number.isInteger(termsDays) || termsDays < 0)) {
    throw new ArApError("terms_days must be a whole number of days");
  }
  return {
    id,
    name,
    ...(email ? { email } : {}),
    ...(termsDays !== undefined ? { termsDays } : {}),
  };
}

export type { Ctx as ArApContext };
