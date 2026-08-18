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
  taxedInvoiceToPostCommand,
  creditMemoToPostCommand,
  refundReceiptToPostCommand,
  vendorCreditToPostCommand,
  TaxError,
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

/** Where sales tax collected on behalf of the state is held until it's remitted. */
export const DEFAULT_SALES_TAX_CODE = "2200";

export interface DocumentLineInput {
  readonly description?: string;
  readonly quantity?: number;
  readonly unit_amount_minor: string | number;
  /** Income account for an invoice line; expense account for a bill line. */
  readonly account_code: string;
  /**
   * Whether sales tax applies to this line. Defaults to true on an invoice,
   * because most things a service business sells are taxable and forgetting to
   * charge tax is the expensive mistake; set it false for exempt lines
   * (professional services in some states, resale, shipping in others).
   */
  readonly taxable?: boolean;
  /**
   * Class, location, `job`, `cost_code` — carried onto the journal line this
   * document posts. A subcontractor's bill coded to a job IS the job cost.
   */
  readonly dimensions?: Readonly<Record<string, string>>;
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
  /**
   * Sales tax rate in parts per million — 8.25% is 82500. Parts per million,
   * not a float, because a tax rate multiplied by a float is how you end up a
   * cent out on every invoice and a lot out over a year.
   */
  readonly tax_rate_ppm?: number;
  /** Where the collected tax is held. Defaults to Sales Tax Payable (2200). */
  readonly sales_tax_code?: string;
}

export interface RecordPaymentRequest {
  readonly id?: string;
  readonly date: string;
  readonly amount_minor: string | number;
  readonly bank_code?: string;
  readonly memo?: string;
  /**
   * Dimensions for the *cash* side of the payment. Normally there are none —
   * money is money. They matter when the "bank account" is a job-scoped
   * liability: drawing a customer deposit down against an invoice has to
   * relieve that job's deposit, not the pooled balance, or the job goes on
   * claiming it holds money it has already used.
   */
  readonly dimensions?: Readonly<Record<string, string>>;
}

interface Ctx {
  readonly tenant: string;
  readonly chart: ChartOfAccounts;
  readonly store: LedgerStore;
  readonly periods: PeriodStore;
  readonly docs: DocumentStore;
  readonly currency: Currency;
  readonly postedAt: string;
  /**
   * Checked before anything posts. AR/AP doesn't own dimension validation —
   * it borrows it — so a job or class typo on an invoice line is refused by
   * exactly the same rule that refuses it on a manual entry.
   */
  readonly validate?: (command: PostCommand) => Promise<void>;
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
    const taxable = l.taxable !== false;
    docLines.push({
      accountId,
      quantity: qty,
      unitAmount: Money.fromMinorUnits(unit, currency),
      taxable,
      ...(l.description ? { description: l.description } : {}),
      ...(l.dimensions && Object.keys(l.dimensions).length > 0
        ? { dimensions: l.dimensions }
        : {}),
    });
    records.push({
      description: l.description ?? "",
      quantity: qty,
      unitAmountMinor: unit.toString(),
      accountCode: code,
      amountMinor: amount.toString(),
      taxable,
      ...(l.dimensions && Object.keys(l.dimensions).length > 0
        ? { dimensions: l.dimensions }
        : {}),
    });
  }
  return { docLines, records, totalMinor };
}

/** A tax rate, validated: parts per million, a whole number, never above 100%. */
function taxRateOf(req: CreateDocumentRequest): number {
  const raw = req.tax_rate_ppm;
  if (raw === undefined || raw === null || raw === 0) return 0;
  const rate = Number(raw);
  if (!Number.isInteger(rate) || rate < 0) {
    throw new ArApError("tax_rate_ppm must be a whole number of parts per million");
  }
  if (rate > 1_000_000) {
    throw new ArApError("a tax rate above 100% is a typo, not a rate");
  }
  return rate;
}

function statusFor(openMinor: bigint, totalMinor: bigint): DocStatus {
  if (openMinor <= 0n) return "PAID";
  return openMinor === totalMinor ? "OPEN" : "PARTIAL";
}

async function post(ctx: Ctx, command: PostCommand): Promise<PostedEntry> {
  if (ctx.validate) await ctx.validate(command);
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
  const { docLines, records, totalMinor: netMinor } = buildLines(req, ctx.chart, ctx.currency);
  if (netMinor <= 0n) throw new ArApError("a document must total more than zero");

  const termsDays = req.terms_days ?? party.termsDays ?? 30;
  const dueDate = req.due_date ?? dueDateFromTerms(req.date, termsDays);

  const docCtx: DocPostContext = {
    tenantId: ctx.tenant,
    currency: ctx.currency,
    provenance: provenanceFor(kind, req.date, ctx.postedAt),
  };

  // Sales tax applies to what you sell, not to what you buy: a bill's tax is
  // part of its cost and is already in the line amounts.
  const ratePpm = kind === "invoice" ? taxRateOf(req) : 0;

  let command;
  let taxMinor = 0n;
  if (kind === "invoice") {
    const taxCode = String(req.sales_tax_code ?? "").trim() || DEFAULT_SALES_TAX_CODE;
    let taxed;
    try {
      taxed = taxedInvoiceToPostCommand(
        {
          id: req.id,
          customerId: req.party_id,
          issueDate: req.date,
          dueDate,
          lines: docLines,
          ...(req.memo ? { memo: req.memo } : {}),
        },
        {
          arControl: idFor(ctx.chart, ctrl.arCode, "AR control"),
          salesTaxPayable: ratePpm > 0
            ? idFor(ctx.chart, taxCode, "sales tax payable")
            : idFor(ctx.chart, ctrl.arCode, "AR control"),
        },
        ratePpm,
        docCtx,
      );
    } catch (err) {
      if (err instanceof TaxError) throw new ArApError(err.message);
      throw err;
    }
    command = taxed.command;
    taxMinor = taxed.tax.minorUnits;
  } else {
    command = billToPostCommand(
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
  }

  const totalMinor = netMinor + taxMinor;
  const entry = await post(ctx, command);

  const doc: DocRecord = {
    id: req.id,
    kind,
    partyId: req.party_id,
    date: req.date,
    dueDate,
    netMinor: netMinor.toString(),
    taxMinor: taxMinor.toString(),
    taxRatePpm: ratePpm,
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
  const cashDimensions = req.dimensions && Object.keys(req.dimensions).length > 0
    ? { dimensions: req.dimensions }
    : {};
  const lines: JournalLineInput[] =
    kind === "invoice"
      ? [
          {
            accountId: bank, side: "DEBIT", amount: money,
            memo: `Payment on ${docId}`, ...cashDimensions,
          },
          { accountId: control, side: "CREDIT", amount: money, memo: `Payment on ${docId}` },
        ]
      : [
          { accountId: control, side: "DEBIT", amount: money, memo: `Payment of ${docId}` },
          {
            accountId: bank, side: "CREDIT", amount: money,
            memo: `Payment of ${docId}`, ...cashDimensions,
          },
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
  const taxId = String(body["tax_id"] ?? "").trim();
  return {
    id,
    name,
    ...(email ? { email } : {}),
    ...(termsDays !== undefined ? { termsDays } : {}),
    // A contractor can be flagged before their W-9 arrives — that is the whole
    // point of flagging early, so the missing one is visible in January rather
    // than discovered the week the forms are due.
    ...(body["is_1099"] === true || body["is_1099"] === "true" ? { is1099: true } : {}),
    ...(taxId ? { taxId } : {}),
  };
}

// --- credits and refunds -----------------------------------------------------

export interface CreditRequest {
  readonly id?: string;
  readonly date?: string;
  readonly memo?: string;
  readonly amount_minor?: string | number;
  /** Where the credit lands: the income account it reverses (or expense, for a
   *  vendor credit). Defaults to the document's own largest line. */
  readonly account_code?: string;
  /** For a refund: the bank account the money goes back out of. */
  readonly bank_code?: string;
  readonly accounts?: Partial<ControlAccounts>;
}

export interface CreditResult {
  readonly doc: DocRecord;
  readonly entryId: string;
  readonly appliedMinor: string;
  readonly kind: "credit" | "refund";
}

/** The account a credit should reverse: the document's biggest line, by default. */
function primaryAccountOf(doc: DocRecord, override: string | undefined): string {
  const explicit = String(override ?? "").trim();
  if (explicit) return explicit;
  let best = doc.lines[0];
  for (const l of doc.lines) {
    if (!best || BigInt(l.amountMinor) > BigInt(best.amountMinor)) best = l;
  }
  if (!best) throw new ArApError(`${doc.kind} ${doc.id} has no lines to credit against`);
  return best.accountCode;
}

function creditAmountOf(req: CreditRequest, doc: DocRecord): bigint {
  const open = BigInt(doc.openMinor);
  if (req.amount_minor === undefined || req.amount_minor === null || req.amount_minor === "") {
    return open;
  }
  const amount = minorOf(req.amount_minor, "amount_minor");
  if (amount <= 0n) throw new ArApError("a credit must be positive");
  return amount;
}

/**
 * Credit a customer against an open invoice (or a vendor against an open bill).
 *
 * This is the honest answer to "we billed them too much" or "they sent it back".
 * It reduces the open item and reverses the income (or expense) that the
 * document booked — the mirror image of the original entry, posted forward.
 * Nothing is edited: the invoice keeps saying what it said, and the credit says
 * what changed.
 *
 * Crediting more than is still open is refused. If the customer already paid,
 * the money has to physically go back, which is a refund — a different entry
 * against a different account, and it should not be reachable by accident.
 */
export async function issueCredit(
  kind: DocKind,
  docId: string,
  req: CreditRequest,
  ctx: Ctx,
): Promise<CreditResult> {
  const date = String(req.date ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new ArApError("date must be YYYY-MM-DD");

  const doc = await ctx.docs.getDoc(ctx.tenant, kind, docId);
  if (!doc) throw new ArApError(`unknown ${kind} ${docId}`);
  const open = BigInt(doc.openMinor);
  if (open <= 0n) {
    throw new ArApError(
      `${kind} ${docId} is already settled — money that has been paid comes back as a refund, not a credit`,
    );
  }
  const amount = creditAmountOf(req, doc);
  if (amount > open) {
    throw new ArApError(
      `${kind} ${docId} has ${fmt(open)} outstanding — a credit of ${fmt(amount)} is more than that`,
    );
  }

  const ctrl = controls(req.accounts);
  const accountCode = primaryAccountOf(doc, req.account_code);
  const accountId = idFor(ctx.chart, accountCode, `credit account ${accountCode}`);
  const creditId = String(req.id ?? "").trim() || `CM-${docId}-${date}`;
  const money = Money.fromMinorUnits(amount, ctx.currency);
  const memo = String(req.memo ?? "").trim() || `Credit against ${docId}`;

  const docCtx: DocPostContext = {
    tenantId: ctx.tenant,
    currency: ctx.currency,
    provenance: provenanceFor(kind === "invoice" ? "credit-memo" : "vendor-credit", date, ctx.postedAt),
  };
  const lines = [{ accountId, quantity: 1, unitAmount: money, description: memo }];

  const command =
    kind === "invoice"
      ? creditMemoToPostCommand(
          { id: creditId, customerId: doc.partyId, date, lines, memo },
          idFor(ctx.chart, ctrl.arCode, "AR control"),
          docCtx,
        )
      : vendorCreditToPostCommand(
          { id: creditId, vendorId: doc.partyId, date, lines, memo },
          idFor(ctx.chart, ctrl.apCode, "AP control"),
          docCtx,
        );

  const entry = await post(ctx, command);
  const remaining = open - amount;
  await ctx.docs.setOpen(
    ctx.tenant, kind, docId, remaining.toString(),
    statusFor(remaining, BigInt(doc.totalMinor)),
  );
  const updated = await ctx.docs.getDoc(ctx.tenant, kind, docId);
  return {
    doc: updated ?? doc,
    entryId: entry.id,
    appliedMinor: amount.toString(),
    kind: "credit",
  };
}

/**
 * Refund a customer who already paid: the money physically leaves the bank and
 * the income it was booked against is reversed.
 *
 * Deliberately separate from a credit. A credit adjusts what is owed; a refund
 * moves cash. Conflating them is how a business ends up with a balance sheet
 * that says it holds money it has already sent back.
 */
export async function issueRefund(
  docId: string,
  req: CreditRequest,
  ctx: Ctx,
): Promise<CreditResult> {
  const date = String(req.date ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new ArApError("date must be YYYY-MM-DD");

  const doc = await ctx.docs.getDoc(ctx.tenant, "invoice", docId);
  if (!doc) throw new ArApError(`unknown invoice ${docId}`);
  const paid = BigInt(doc.totalMinor) - BigInt(doc.openMinor);
  if (paid <= 0n) {
    throw new ArApError(
      `nothing has been collected against ${docId} — reduce what is owed with a credit instead`,
    );
  }
  const amount =
    req.amount_minor === undefined || req.amount_minor === null || req.amount_minor === ""
      ? paid
      : minorOf(req.amount_minor, "amount_minor");
  if (amount <= 0n) throw new ArApError("a refund must be positive");
  if (amount > paid) {
    throw new ArApError(
      `only ${fmt(paid)} has been collected against ${docId} — you cannot refund ${fmt(amount)}`,
    );
  }

  const ctrl = controls(req.accounts);
  const bankCode = String(req.bank_code ?? "").trim() || ctrl.bankCode;
  const accountCode = primaryAccountOf(doc, req.account_code);
  const money = Money.fromMinorUnits(amount, ctx.currency);
  const memo = String(req.memo ?? "").trim() || `Refund of ${docId}`;
  const refundId = String(req.id ?? "").trim() || `RF-${docId}-${date}`;

  const command = refundReceiptToPostCommand(
    {
      id: refundId,
      customerId: doc.partyId,
      date,
      lines: [{
        accountId: idFor(ctx.chart, accountCode, `refund account ${accountCode}`),
        quantity: 1,
        unitAmount: money,
        description: memo,
      }],
      memo,
    },
    idFor(ctx.chart, bankCode, "bank"),
    {
      tenantId: ctx.tenant,
      currency: ctx.currency,
      provenance: provenanceFor("refund", date, ctx.postedAt),
    },
  );

  const entry = await post(ctx, command);
  // The invoice's open balance is untouched: it was collected and stays
  // collected. What changed is that the cash went back out.
  return { doc, entryId: entry.id, appliedMinor: amount.toString(), kind: "refund" };
}

/** Minor units as a decimal string, for a message a person will read. */
function fmt(minor: bigint): string {
  const negative = minor < 0n;
  const abs = negative ? -minor : minor;
  return `${negative ? "-" : ""}${abs / 100n}.${String(abs % 100n).padStart(2, "0")}`;
}

export type { Ctx as ArApContext };
