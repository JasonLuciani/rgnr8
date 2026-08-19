import { createHash, timingSafeEqual } from "node:crypto";
import { SettingsError, mergeSettings, settingsJson } from "./settings.js";
import {
  AccountSubtype,
  AccountType,
  BusinessCategory,
  ChartOfAccounts,
  Money,
  PeriodClosedError,
  PostingEngine,
  USD,
  accountTypeOfSubtype,
  asAccountId,
  asIdempotencyKey,
  asPeriodKey,
  asTenantId,
  computeTrialBalance,
  getCurrency,
  goLiveFromDto,
  executeGoLive,
  type Account,
  type Currency,
  type GoLiveDto,
  type JournalLineInput,
  type PostCommand,
  type PostedEntry,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import {
  accountLedger,
  balanceSheet,
  cashFlow,
  financialStatementsJson,
  fromKernelTrialBalance,
  incomeStatement,
  subtypeCashFlowClassifier,
} from "@rgnr8/financial-statements";
import type { LedgerBackend } from "./backend.js";
import { ingestTransactions, type IngestRequest } from "./ingest.js";
import {
  ArApError,
  aging,
  createDocument,
  issueCredit,
  issueRefund,
  recordPayment,
  validateParty,
  type ArApContext,
  type CreateDocumentRequest,
  type RecordPaymentRequest,
} from "./arap.js";
import type { DocKind, DocRecord, PartyKind } from "./documents.js";
import {
  InboxError,
  acceptTxn,
  bulkAccept,
  deliverFeed,
  excludeTxn,
  inboxView,
  matchTxn,
  ruleImpact,
  ruleJson,
  saveRule,
  undoTxn,
  type InboxContext,
} from "./inbox.js";
import type { FeedStatus } from "./feed.js";
import { Ten99Error, ten99Report, } from "./ten99.js";
import {
  RecurringError,
  dueOccurrences,
  recurringJson,
  runDue,
  saveRecurring,
  type RecurringContext,
} from "./recurring.js";
import {
  AttachmentError,
  attachmentJson,
  listAttachments,
  saveAttachment,
  type AttachmentContext,
  type SubjectKind,
} from "./attachments.js";
import {
  DimensionServiceError,
  dimensionJson,
  dimensionsOf,
  reportByDimension,
  saveDimension,
  validateDimensions,
  type DimensionContext,
} from "./dimensions.js";
import {
  ReportingError,
  budgetReport,
  generalLedger,
  saveBudgetLines,
  type ReportingContext,
} from "./reporting.js";
import {
  PayrollServiceError,
  createRun,
  liabilityView,
  postRun,
  remit,
  runJson,
  saveEmployee,
  voidRun,
  type PayrollContext,
} from "./payroll.js";
import {
  JOB_DIMENSION,
  JobError,
  costCodeJson,
  jobCostReport,
  jobJson,
  jobList,
  saveCostCode,
  saveJob,
  saveJobBudget,
  seedCostCodes,
  type JobContext,
} from "./jobs.js";
import {
  EstimateError,
  acceptEstimate,
  estimateJson,
  estimateToInvoiceRequest,
  reviseEstimate,
  saveEstimate,
  setEstimateStatus,
  type EstimateContext,
  type EstimateStatus,
} from "./estimates.js";
import {
  SalesOrderError,
  backlog,
  invoiceFromOrder,
  salesOrderJson,
  saveSalesOrder,
  setOrderStatus,
  type SalesOrderContext,
  type SalesOrderStatus,
} from "./salesorders.js";
import {
  WorkOrderError,
  addEntry,
  completeWorkOrder,
  entryJson as workEntryJson,
  saveWorkOrder,
  workOrderJson,
  workOrdersForJob,
  type WorkOrderContext,
} from "./workorders.js";
import {
  PurchasingError,
  committedByCostCode,
  matchToBill,
  outstandingOrders,
  postGrniAdjustment,
  purchaseOrderJson,
  receive as receiveGoods,
  receiptJson,
  savePurchaseOrder,
  setPurchaseOrderStatus,
  type PurchaseOrderStatus,
  type PurchasingContext,
} from "./purchasing.js";
import {
  WipError,
  postWipAdjustment,
  wipSchedule,
  type WipContext,
} from "./wip.js";
import { CUSTOMER_DEPOSITS_CODE } from "./billing.js";
import {
  BillingError,
  applyDraft,
  billMilestone,
  billProgress,
  billTimeAndMaterials,
  billingView,
  depositApplication,
  postRetainage,
  releaseRetainageRequest,
  saveMilestones,
  saveSchedule,
  takeDeposit,
  type BillingContext,
  type BillingDraft,
} from "./billing.js";
import {
  InventoryError,
  issueStock,
  itemJson,
  lotJson,
  movementJson,
  receiveStock,
  recordCount,
  saveItem,
  valuation,
  type InventoryContext,
} from "./inventory.js";
import {
  DebtError,
  saveLoan,
  recordLoanPayment,
  recordDraw,
  amortizationSchedule,
  debtDashboard,
  payoffPlan,
  loanJson,
  scheduleRowJson,
  type DebtContext,
} from "./debt.js";
import {
  FixedAssetError,
  saveAsset,
  runDepreciation,
  recordUsage,
  disposeAsset,
  depreciationSchedule,
  assetJson,
  depreciationRowJson,
  assetRegister,
  type FixedAssetContext,
} from "./fixedassets.js";
import {
  CrmError,
  attachEstimate,
  convertLead,
  events as crmEvents,
  leadJson,
  loseOpportunity,
  reopenOpportunity,
  opportunityJson,
  pipeline,
  recordEvent,
  saveLead,
  saveOpportunity,
  winOpportunity,
  type CrmContext,
} from "./crm.js";
import {
  ConsolidationServiceError,
  consolidate,
  consolidatedStatements,
  eliminationJson,
  groupJson,
  saveElimination,
  saveGroup,
  type ConsolidationContext,
} from "./consolidation.js";
import {
  ReconcileError,
  finishReconciliation,
  importStatement,
  reconcileView,
  toggleCleared,
  type ReconcileContext,
} from "./reconcile.js";

/**
 * The service's request handlers — pure `(request) => response`, with no HTTP
 * in sight, so the whole API surface is testable directly. `server.ts` is a thin
 * node:http adapter over these.
 *
 * The API speaks **account codes**, not internal ids: a caller says "1000", not
 * "acct:1000". Codes are what an owner and an accountant actually use, and it
 * keeps the Python client free of the kernel's branded-id types.
 *
 * Money crosses the boundary as integer **minor-unit strings** — never a float.
 */

export interface ServiceRequest {
  readonly method: string;
  readonly path: string;
  readonly query: Readonly<Record<string, string>>;
  readonly body: string;
  readonly headers: Readonly<Record<string, string>>;
}

export interface ServiceResponse {
  readonly status: number;
  readonly body: unknown;
}

export interface ServiceOptions {
  /** Shared secret required as `Authorization: Bearer <token>`. */
  readonly authToken?: string;
  /** Injected clock (ISO instant) — the service never reads a wall clock itself. */
  readonly now: () => string;
  readonly defaultCurrency?: Currency;
}

const ok = (body: unknown): ServiceResponse => ({ status: 200, body });
const created = (body: unknown): ServiceResponse => ({ status: 201, body });
const bad = (error: string): ServiceResponse => ({ status: 400, body: { error } });
const notFound = (error: string): ServiceResponse => ({ status: 404, body: { error } });
const conflict = (error: string): ServiceResponse => ({ status: 409, body: { error } });

/**
 * Compare a presented bearer token to the configured one in constant time.
 *
 * A plain `===` short-circuits on the first differing byte, so response time
 * leaks how long a prefix an attacker has guessed — enough, over many requests,
 * to recover the token byte by byte. Hashing both sides to a fixed-width digest
 * first makes the comparison both length-independent and timing-safe.
 */
function timingSafeTokenEqual(presented: string, expected: string): boolean {
  if (!presented) return false;
  const a = createHash("sha256").update(presented).digest();
  const b = createHash("sha256").update(expected).digest();
  return timingSafeEqual(a, b);
}

function parseJson(body: string): Record<string, unknown> | null {
  if (!body.trim()) return {};
  try {
    const v: unknown = JSON.parse(body);
    return typeof v === "object" && v !== null && !Array.isArray(v)
      ? (v as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** Day before an ISO date, computed purely (no Date parsing / DST surprises). */
function previousDay(iso: string): string {
  const y = Number(iso.slice(0, 4));
  const m = Number(iso.slice(5, 7));
  const d = Number(iso.slice(8, 10));
  const leap = (y % 4 === 0 && y % 100 !== 0) || y % 400 === 0;
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  let yy = y;
  let mm = m;
  let dd = d - 1;
  if (dd < 1) {
    mm -= 1;
    if (mm < 1) {
      mm = 12;
      yy -= 1;
    }
    dd = days[mm - 1]!;
  }
  const p2 = (n: number): string => (n < 10 ? `0${n}` : String(n));
  return `${yy}-${p2(mm)}-${p2(dd)}`;
}

function provenanceFor(source: string, date: string, at: string): Provenance {
  return {
    sourceSystem: source,
    sourceObject: "journal",
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/1",
    mappingVersion: "ledger-service/1",
  };
}

function accountJson(a: Account): Record<string, unknown> {
  return {
    code: a.code,
    name: a.name,
    type: a.type,
    subtype: a.subtype ?? null,
    currency: a.currency.code,
    active: a.active !== false,
  };
}

function entryJson(e: PostedEntry): Record<string, unknown> {
  return {
    id: e.id,
    sequence: e.sequence,
    date: e.entryDate,
    period: e.periodKey,
    memo: e.memo ?? "",
    status: e.status,
    currency: e.currency.code,
    posted_at: e.postedAt,
    lines: e.lines.map((l) => ({
      account_id: l.accountId,
      side: l.side,
      amount_minor: l.amount.minorUnits.toString(),
      memo: l.memo ?? "",
      ...(l.dimensions ? { dimensions: l.dimensions } : {}),
    })),
  };
}

export class LedgerService {
  private readonly currency: Currency;

  constructor(
    private readonly backend: LedgerBackend,
    private readonly opts: ServiceOptions,
  ) {
    this.currency = opts.defaultCurrency ?? USD;
  }

  async handle(req: ServiceRequest): Promise<ServiceResponse> {
    const parts = req.path.split("/").filter(Boolean);

    if (req.path === "/health") return ok({ status: "ok" });
    if (req.path === "/ready") {
      const ready = await this.backend.ping();
      return ready
        ? ok({ status: "ready" })
        : { status: 503, body: { status: "unavailable", error: "backend not reachable" } };
    }

    if (this.opts.authToken) {
      const auth = req.headers["authorization"] ?? "";
      const presented = auth.toLowerCase().startsWith("bearer ") ? auth.slice(7).trim() : "";
      if (!timingSafeTokenEqual(presented, this.opts.authToken)) {
        return { status: 401, body: { error: "unauthorized" } };
      }
    }

    if (parts[0] !== "t" || parts.length < 2) return notFound("not found");
    const tenant = asTenantId(parts[1]!);
    const rest = parts.slice(2);

    try {
      // --- per-account settings -------------------------------------------
      if (rest[0] === "settings" && rest.length === 1) {
        const store = this.backend.settings();
        if (req.method === "GET") {
          return ok({ tenant, settings: settingsJson(await store.get(String(tenant)) ) });
        }
        if (req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const current = await store.get(String(tenant));
          const next = mergeSettings(current, data);
          // Switching the inventory costing method mid-stream would strand the
          // existing on-hand value (moving average keeps no lots; FIFO keeps no
          // blended figure), so it is only allowed when nothing is on the shelf.
          if (next.inventoryCostingMethod !== current.inventoryCostingMethod) {
            const onHand = (await this.backend.inventory().listItems(String(tenant)))
              .filter((i) => BigInt(i.quantityMilli) !== 0n);
            if (onHand.length > 0) {
              return bad(
                `cannot change the inventory costing method while ${onHand.length} item(s) `
                + "have stock on hand — issue or write off the remaining quantity first",
              );
            }
          }
          await store.save(String(tenant), next);
          return ok({ tenant, settings: settingsJson(next) });
        }
      }
      if (rest[0] === "accounts" && rest.length === 1) {
        if (req.method === "GET") return await this.listAccounts(tenant);
        if (req.method === "POST") return await this.createAccount(tenant, req.body);
      }
      if (rest[0] === "accounts" && rest[1] === "seed" && req.method === "POST") {
        return await this.seedAccounts(tenant, req.body);
      }
      if (rest[0] === "accounts" && rest.length === 3 && rest[2] === "register" && req.method === "GET") {
        return await this.register(tenant, rest[1]!, req.query);
      }
      // --- bank reconciliation -------------------------------------------
      if (rest[0] === "accounts" && rest[2] === "reconcile") {
        const code = rest[1]!;
        if (rest.length === 3 && req.method === "GET") {
          return ok(
            await reconcileView(
              this.reconCtx(tenant), code,
              req.query["statement_date"], req.query["statement_balance_minor"],
            ),
          );
        }
        if (rest.length === 4 && rest[3] === "toggle" && req.method === "POST") {
          return await this.reconToggle(tenant, code, req.body);
        }
        if (rest.length === 4 && rest[3] === "finish" && req.method === "POST") {
          return await this.reconFinish(tenant, code, req.body);
        }
        if (rest.length === 4 && rest[3] === "import" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("body must be a JSON object");
          return ok(await importStatement(
            this.reconCtx(tenant), code, str(data["statement_text"]),
            data["statement_date"], data["statement_balance_minor"],
          ));
        }
      }
      if (rest[0] === "entries" && rest.length === 1) {
        if (req.method === "POST") return await this.postEntry(tenant, req.body);
        if (req.method === "GET") return await this.listEntries(tenant, req.query);
      }
      if (rest[0] === "entries" && rest.length === 3 && rest[2] === "reverse"
          && req.method === "POST") {
        return await this.reverseEntry(tenant, rest[1]!, req.body);
      }
      if (rest[0] === "trial-balance" && req.method === "GET") {
        return await this.trialBalance(tenant, req.query);
      }
      if (rest[0] === "statements" && req.method === "GET") {
        return await this.statements(tenant, req.query);
      }
      if (rest[0] === "periods" && rest.length === 3 && rest[2] === "lock" && req.method === "POST") {
        await this.backend.periods(tenant).lock(tenant, asPeriodKey(rest[1]!));
        return ok({ tenant, period: rest[1], locked: true });
      }
      // --- AR / AP -------------------------------------------------------
      if (rest[0] === "customers" || rest[0] === "vendors") {
        const partyKind: PartyKind = rest[0] === "customers" ? "customer" : "vendor";
        if (rest.length === 1 && req.method === "GET") return await this.listParties(tenant, partyKind);
        if (rest.length === 1 && req.method === "POST") {
          return await this.createParty(tenant, partyKind, req.body);
        }
      }
      if (rest[0] === "invoices" || rest[0] === "bills") {
        const docKind: DocKind = rest[0] === "invoices" ? "invoice" : "bill";
        if (rest.length === 1 && req.method === "GET") return await this.listDocs(tenant, docKind);
        if (rest.length === 1 && req.method === "POST") {
          return await this.createDoc(tenant, docKind, req.body);
        }
        if (rest.length === 2 && req.method === "GET") {
          return await this.getDoc(tenant, docKind, rest[1]!);
        }
        if (rest.length === 3 && rest[2] === "payments" && req.method === "POST") {
          return await this.payDoc(tenant, docKind, rest[1]!, req.body);
        }
        // A credit reduces what is owed; a refund sends money back. Different
        // entries, deliberately different routes.
        if (rest.length === 3 && rest[2] === "credits" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return this.creditJson(tenant, await issueCredit(
            docKind, rest[1]!, data as Parameters<typeof issueCredit>[2],
            await this.arapCtx(tenant),
          ));
        }
        if (rest.length === 3 && rest[2] === "refunds" && req.method === "POST") {
          if (docKind !== "invoice") return bad("only an invoice can be refunded");
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return this.creditJson(tenant, await issueRefund(
            rest[1]!, data as Parameters<typeof issueRefund>[1],
            await this.arapCtx(tenant),
          ));
        }
      }
      if (rest[0] === "aging" && rest.length === 2 && req.method === "GET") {
        if (rest[1] !== "ar" && rest[1] !== "ap") return notFound("aging must be ar or ap");
        return await this.aging(tenant, rest[1] === "ar" ? "invoice" : "bill", req.query);
      }

      // --- 1099 contractors -----------------------------------------------
      if (rest[0] === "1099" && rest.length === 2 && req.method === "GET") {
        return ok(await ten99Report(
          { backend: this.backend, tenant, currency: this.currency }, rest[1],
        ));
      }

      // --- multi-entity consolidation ---------------------------------------
      if (rest[0] === "consolidation") {
        const ctx = this.consolidationCtx(tenant);
        const store = this.backend.consolidation();
        if (rest.length === 2 && rest[1] === "groups") {
          if (req.method === "GET") {
            const groups = await store.listGroups(String(tenant));
            return ok({ tenant, groups: groups.map(groupJson) });
          }
          if (req.method === "POST") {
            const data = parseJson(req.body);
            if (!data) return bad("invalid JSON body");
            return created({ tenant, group: groupJson(await saveGroup(ctx, data)) });
          }
        }
        if (rest.length === 3 && rest[1] === "groups") {
          if (req.method === "GET") {
            const group = await store.getGroup(String(tenant), rest[2]!);
            if (!group) return notFound(`unknown group ${rest[2]}`);
            return ok({
              tenant,
              group: groupJson(group),
              eliminations: (await store.listEliminations(String(tenant), rest[2]!))
                .map(eliminationJson),
            });
          }
          if (req.method === "DELETE") {
            await store.removeGroup(String(tenant), rest[2]!);
            return ok({ tenant, removed: rest[2] });
          }
        }
        if (rest.length === 4 && rest[1] === "groups" && rest[3] === "eliminations"
            && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant, elimination: eliminationJson(await saveElimination(ctx, rest[2]!, data)),
          });
        }
        if (rest.length === 4 && rest[1] === "groups" && rest[3] === "report"
            && req.method === "GET") {
          return ok(await consolidate(ctx, rest[2]!, {
            ...(req.query["through"] ? { through: req.query["through"] } : {}),
            ...(req.query["from"] ? { from: req.query["from"] } : {}),
            ...(req.query["allow_mismatch"] === "1" ? { allow_mismatch: true } : {}),
          }));
        }
        if (rest.length === 4 && rest[1] === "groups" && rest[3] === "statements"
            && req.method === "GET") {
          const from = req.query["from"];
          const to = req.query["to"];
          if (!from || !to) return bad("statements need ?from=YYYY-MM-DD&to=YYYY-MM-DD");
          return ok(await consolidatedStatements(ctx, rest[2]!, from, to));
        }
      }

      // --- the pipeline -----------------------------------------------------
      if (rest[0] === "leads") {
        const ctx = this.crmCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          const leads = await this.backend.crm().listLeads(String(tenant));
          return ok({ tenant, leads: leads.map(leadJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({ tenant, lead: leadJson(await saveLead(ctx, data)) });
        }
        if (rest.length === 2 && req.method === "GET") {
          const lead = await this.backend.crm().getLead(String(tenant), rest[1]!);
          if (!lead) return notFound(`unknown lead ${rest[1]}`);
          return ok({ tenant, lead: leadJson(lead) });
        }
        if (rest.length === 3 && rest[2] === "convert" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await convertLead(ctx, rest[1]!, data);
          return created({
            tenant,
            lead: leadJson(result.lead),
            customer_id: result.customerId,
            ...(result.opportunity
              ? { opportunity: opportunityJson(result.opportunity) }
              : {}),
          });
        }
      }

      if (rest[0] === "opportunities") {
        const ctx = this.crmCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          const list = await this.backend.crm().listOpportunities(String(tenant));
          return ok({ tenant, opportunities: list.map(opportunityJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant, opportunity: opportunityJson(await saveOpportunity(ctx, data)),
          });
        }
        if (rest.length === 2 && req.method === "GET") {
          const o = await this.backend.crm().getOpportunity(String(tenant), rest[1]!);
          if (!o) return notFound(`unknown opportunity ${rest[1]}`);
          return ok({ tenant, opportunity: opportunityJson(o) });
        }
        if (rest.length === 3 && rest[2] === "estimate" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const o = await this.backend.crm().getOpportunity(String(tenant), rest[1]!);
          if (!o) return notFound(`unknown opportunity ${rest[1]}`);
          // Raise the estimate for the opportunity's customer, then link it.
          const estimate = await saveEstimate(this.estimateCtx(tenant), {
            ...(data as Parameters<typeof saveEstimate>[1]),
            customer_id: str(data["customer_id"]).trim() || o.customerId,
            memo: str(data["memo"]).trim() || o.name,
          });
          const linked = await attachEstimate(ctx, o.id, estimate.id);
          return created({
            tenant,
            opportunity: opportunityJson(linked),
            estimate: estimateJson(estimate),
          });
        }
        if (rest.length === 3 && rest[2] === "win" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return ok({
            tenant, opportunity: opportunityJson(await winOpportunity(ctx, rest[1]!, data)),
          });
        }
        if (rest.length === 3 && rest[2] === "lose" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return ok({
            tenant, opportunity: opportunityJson(await loseOpportunity(ctx, rest[1]!, data)),
          });
        }
        if (rest.length === 3 && rest[2] === "reopen" && req.method === "POST") {
          const data = parseJson(req.body) ?? {};
          return ok({
            tenant,
            opportunity: opportunityJson(
              await reopenOpportunity(ctx, rest[1]!, str(data["stage"]).trim() || undefined),
            ),
          });
        }
      }

      if (rest[0] === "pipeline" && rest.length === 1 && req.method === "GET") {
        return ok(await pipeline(this.crmCtx(tenant), {
          ...(req.query["owner"] ? { owner: req.query["owner"] } : {}),
        }));
      }

      if (rest[0] === "events" && rest.length === 1 && req.method === "GET") {
        return ok(await crmEvents(
          this.crmCtx(tenant), req.query["since"], req.query["limit"],
        ));
      }

      // --- inventory --------------------------------------------------------
      if (rest[0] === "inventory") {
        const ctx = this.inventoryCtx(tenant);
        const store = this.backend.inventory();
        if (rest.length === 1 && req.method === "GET") return ok(await valuation(ctx));
        if (rest.length === 2 && rest[1] === "items") {
          if (req.method === "GET") {
            const items = await store.listItems(String(tenant));
            return ok({ tenant, items: items.map(itemJson) });
          }
          if (req.method === "POST") {
            const data = parseJson(req.body);
            if (!data) return bad("invalid JSON body");
            return created({ tenant, item: itemJson(await saveItem(ctx, data)) });
          }
        }
        if (rest.length === 3 && rest[1] === "items" && req.method === "GET") {
          const item = await store.getItem(String(tenant), rest[2]!.toUpperCase());
          if (!item) return notFound(`unknown item ${rest[2]}`);
          return ok({
            tenant,
            item: itemJson(item),
            movements: (await store.listMovements(String(tenant), item.sku)).map(movementJson),
          });
        }
        if (rest.length === 4 && rest[1] === "items" && rest[3] === "lots" && req.method === "GET") {
          // Open cost layers for a SKU — the source of truth for FIFO/LIFO issues
          // and the lot numbers a specific-identification issue names.
          const item = await store.getItem(String(tenant), rest[2]!.toUpperCase());
          if (!item) return notFound(`unknown item ${rest[2]}`);
          const open = (await store.lots(String(tenant), item.sku))
            .filter((l) => BigInt(l.remainingQtyMilli) > 0n);
          return ok({ tenant, sku: item.sku, lots: open.map(lotJson) });
        }
        if (rest.length === 2 && rest[1] === "movements" && req.method === "GET") {
          const sku = req.query["sku"];
          const movements = await store.listMovements(
            String(tenant), sku ? sku.toUpperCase() : undefined,
          );
          return ok({ tenant, movements: movements.map(movementJson) });
        }
        if (rest.length === 2 && rest[1] === "receipts" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await receiveStock(ctx, data);
          return created({
            tenant, item: itemJson(result.item), entry_id: result.entryId,
          });
        }
        if (rest.length === 2 && rest[1] === "issues" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await issueStock(ctx, data);
          return created({
            tenant, item: itemJson(result.item),
            entry_id: result.entryId, value_minor: result.valueMinor,
          });
        }
        if (rest.length === 2 && rest[1] === "counts" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await recordCount(ctx, data);
          return created({
            tenant, item: itemJson(result.item),
            entry_id: result.entryId, difference_milli: result.differenceMilli,
          });
        }
      }

      // --- debt: loans, lines of credit, and their payments -----------------
      if (rest[0] === "debt") {
        const ctx = this.debtCtx(tenant);
        const store = this.backend.debt();
        if (rest.length === 1 && req.method === "GET") {
          return ok(await debtDashboard(ctx, req.query["as_of"]));
        }
        if (rest.length === 2 && rest[1] === "loans") {
          if (req.method === "GET") {
            const loans = await store.listLoans(String(tenant));
            return ok({ tenant, loans: loans.map(loanJson) });
          }
          if (req.method === "POST") {
            const data = parseJson(req.body);
            if (!data) return bad("invalid JSON body");
            const result = await saveLoan(ctx, data);
            return created({ tenant, loan: loanJson(result.loan), entry_id: result.entryId });
          }
        }
        if (rest.length === 3 && rest[1] === "loans" && req.method === "GET") {
          const loan = await store.getLoan(String(tenant), rest[2]!);
          if (!loan) return notFound(`unknown loan ${rest[2]}`);
          return ok({
            tenant,
            loan: loanJson(loan),
            schedule: amortizationSchedule(loan).map(scheduleRowJson),
            payments: await store.listPayments(String(tenant), loan.id),
          });
        }
        if (rest.length === 4 && rest[1] === "loans" && rest[3] === "schedule" && req.method === "GET") {
          const loan = await store.getLoan(String(tenant), rest[2]!);
          if (!loan) return notFound(`unknown loan ${rest[2]}`);
          return ok({ tenant, loan_id: loan.id, schedule: amortizationSchedule(loan).map(scheduleRowJson) });
        }
        if (rest.length === 4 && rest[1] === "loans" && rest[3] === "payoff" && req.method === "GET") {
          const loan = await store.getLoan(String(tenant), rest[2]!);
          if (!loan) return notFound(`unknown loan ${rest[2]}`);
          const extra = BigInt(String(req.query["extra_per_period_minor"] ?? "0") || "0");
          return ok(payoffPlan(loan, extra));
        }
        if (rest.length === 2 && rest[1] === "payments" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await recordLoanPayment(ctx, data);
          return created({
            tenant, loan: loanJson(result.loan), entry_id: result.entryId,
            principal_minor: result.principalMinor, interest_minor: result.interestMinor,
          });
        }
        if (rest.length === 2 && rest[1] === "draws" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await recordDraw(ctx, data);
          return created({ tenant, loan: loanJson(result.loan), entry_id: result.entryId });
        }
      }

      // --- fixed assets and depreciation ------------------------------------
      if (rest[0] === "assets") {
        const ctx = this.fixedAssetCtx(tenant);
        const store = this.backend.fixedAssets();
        if (rest.length === 1 && req.method === "GET") {
          return ok(await assetRegister(ctx));
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await saveAsset(ctx, data);
          return created({ tenant, asset: assetJson(result.asset), entry_id: result.entryId });
        }
        if (rest.length === 2 && req.method === "GET") {
          const asset = await store.getAsset(String(tenant), rest[1]!);
          if (!asset) return notFound(`unknown asset ${rest[1]}`);
          return ok({ tenant, asset: assetJson(asset), schedule: depreciationSchedule(asset).map(depreciationRowJson) });
        }
        if (rest.length === 3 && rest[2] === "depreciate" && req.method === "POST") {
          const data = parseJson(req.body) ?? {};
          const result = await runDepreciation(ctx, { ...data, asset_id: rest[1]! });
          return created({
            tenant, asset: assetJson(result.asset),
            posted_minor: result.postedMinor, entry_id: result.entryId,
          });
        }
        if (rest.length === 3 && rest[2] === "usage" && req.method === "POST") {
          const data = parseJson(req.body) ?? {};
          const result = await recordUsage(ctx, { ...data, asset_id: rest[1]! });
          return created({
            tenant, asset: assetJson(result.asset),
            posted_minor: result.postedMinor, entry_id: result.entryId,
          });
        }
        if (rest.length === 3 && rest[2] === "dispose" && req.method === "POST") {
          const data = parseJson(req.body) ?? {};
          const result = await disposeAsset(ctx, { ...data, asset_id: rest[1]! });
          return created({
            tenant, asset: assetJson(result.asset),
            gain_loss_minor: result.gainLossMinor, entry_id: result.entryId,
          });
        }
      }

      // --- work in progress -------------------------------------------------
      if (rest[0] === "wip") {
        const ctx = this.wipCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          return ok(await wipSchedule(ctx, req.query["through"]));
        }
        if (rest.length === 2 && rest[1] === "post" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return ok(await postWipAdjustment(ctx, data));
        }
      }

      // --- purchase orders, receiving and the three-way match ---------------
      if (rest[0] === "purchase-orders") {
        const ctx = this.purchasingCtx(tenant);
        const store = this.backend.purchasing();
        if (rest.length === 1 && req.method === "GET") {
          const list = await store.list(String(tenant));
          return ok({ tenant, purchase_orders: list.map(purchaseOrderJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant, purchase_order: purchaseOrderJson(await savePurchaseOrder(ctx, data)),
          });
        }
        if (rest.length === 2 && rest[1] === "committed" && req.method === "GET") {
          return ok(await outstandingOrders(ctx, {
            ...(req.query["job_id"] ? { job_id: req.query["job_id"] } : {}),
            ...(req.query["vendor_id"] ? { vendor_id: req.query["vendor_id"] } : {}),
          }));
        }
        if (rest.length === 2 && req.method === "GET") {
          const order = await store.get(String(tenant), rest[1]!);
          if (!order) return notFound(`unknown purchase order ${rest[1]}`);
          return ok({
            tenant,
            purchase_order: purchaseOrderJson(order),
            receipts: (await store.listReceipts(String(tenant), rest[1]!)).map(receiptJson),
          });
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await store.remove(String(tenant), rest[1]!);
          return ok({ tenant, removed: rest[1] });
        }
        if (rest.length === 3 && rest[2] === "status" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const status = str(data["status"]).trim().toUpperCase();
          if (!["OPEN", "CLOSED", "CANCELLED"].includes(status)) {
            return bad("status must be OPEN, CLOSED or CANCELLED — receiving sets the rest");
          }
          return ok({
            tenant,
            purchase_order: purchaseOrderJson(
              await setPurchaseOrderStatus(ctx, rest[1]!, status as PurchaseOrderStatus),
            ),
          });
        }
        if (rest.length === 3 && rest[2] === "receipts" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await receiveGoods(ctx, rest[1]!, data);
          return created({
            tenant,
            receipt: receiptJson(result.receipt),
            purchase_order: purchaseOrderJson(result.order),
            accrued_minor: result.accruedMinor,
          });
        }
        if (rest.length === 3 && rest[2] === "bill" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const draft = await matchToBill(ctx, rest[1]!, data);
          const result = await createDocument(
            "bill", draft.request as unknown as CreateDocumentRequest,
            await this.arapCtx(tenant),
          );
          await this.backend.purchasing().save(String(tenant), draft.order);
          const varianceEntry = await postGrniAdjustment(
            ctx, draft.order, String(draft.request["id"]),
            String(draft.request["date"]), draft.grniAdjustment,
          );
          return created({
            tenant,
            purchase_order: purchaseOrderJson(draft.order),
            bill: this.docJson(result.doc),
            entry_id: result.entryId,
            variances: draft.variances,
            variance_entry_id: varianceEntry,
          });
        }
      }

      // --- work orders ------------------------------------------------------
      if (rest[0] === "work-orders") {
        const ctx = this.workOrderCtx(tenant);
        const store = this.backend.workOrders();
        if (rest.length === 1 && req.method === "GET") {
          const jobId = req.query["job_id"] ?? "";
          const all = await store.list(String(tenant));
          const entries = await store.listEntries(String(tenant));
          const rows = (jobId ? all.filter((o) => o.jobId === jobId) : all)
            .map((o) => workOrderJson(o, entries.filter((e) => e.workOrderId === o.id)));
          return ok({ tenant, work_orders: rows });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({ tenant, work_order: workOrderJson(await saveWorkOrder(ctx, data)) });
        }
        if (rest.length === 2 && req.method === "GET") {
          const order = await store.get(String(tenant), rest[1]!);
          if (!order) return notFound(`unknown work order ${rest[1]}`);
          return ok({
            tenant,
            work_order: workOrderJson(order, await store.listEntries(String(tenant), rest[1]!)),
          });
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await store.remove(String(tenant), rest[1]!);
          return ok({ tenant, removed: rest[1] });
        }
        if (rest.length === 3 && rest[2] === "complete" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return ok({
            tenant,
            work_order: workOrderJson(await completeWorkOrder(ctx, rest[1]!, data["date"])),
          });
        }
        if (rest.length === 3 && rest[2] === "entries" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await addEntry(ctx, rest[1]!, data);
          return created({
            tenant,
            entry: workEntryJson(result.entry),
            unposted_reason: result.unposted_reason,
          });
        }
        if (rest.length === 4 && rest[2] === "entries" && req.method === "DELETE") {
          const entry = await store.getEntry(String(tenant), rest[3]!);
          if (entry?.entryId) {
            return bad(
              `entry ${rest[3]} posted an allocation — reverse entry ${entry.entryId} instead`,
            );
          }
          await store.removeEntry(String(tenant), rest[3]!);
          return ok({ tenant, removed: rest[3] });
        }
      }

      // --- sales orders -----------------------------------------------------
      if (rest[0] === "sales-orders") {
        const ctx = this.salesOrderCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          const list = await this.backend.salesOrders().list(String(tenant));
          return ok({ tenant, orders: list.map(salesOrderJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({ tenant, order: salesOrderJson(await saveSalesOrder(ctx, data)) });
        }
        if (rest.length === 2 && rest[1] === "backlog" && req.method === "GET") {
          return ok(await backlog(ctx, {
            ...(req.query["job_id"] ? { job_id: req.query["job_id"] } : {}),
            ...(req.query["customer_id"] ? { customer_id: req.query["customer_id"] } : {}),
          }));
        }
        if (rest.length === 2 && req.method === "GET") {
          const order = await this.backend.salesOrders().get(String(tenant), rest[1]!);
          if (!order) return notFound(`unknown order ${rest[1]}`);
          return ok({ tenant, order: salesOrderJson(order) });
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await this.backend.salesOrders().remove(String(tenant), rest[1]!);
          return ok({ tenant, removed: rest[1] });
        }
        if (rest.length === 3 && rest[2] === "status" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const status = str(data["status"]).trim().toUpperCase();
          if (!["OPEN", "CLOSED", "CANCELLED"].includes(status)) {
            return bad("status must be OPEN, CLOSED or CANCELLED — invoicing sets the rest");
          }
          return ok({
            tenant,
            order: salesOrderJson(
              await setOrderStatus(ctx, rest[1]!, status as SalesOrderStatus),
            ),
          });
        }
        if (rest.length === 3 && rest[2] === "invoice" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const draft = await invoiceFromOrder(ctx, rest[1]!, data);
          const result = await createDocument(
            "invoice", draft.request as unknown as CreateDocumentRequest,
            await this.arapCtx(tenant),
          );
          // Only draw the order down once the invoice has actually posted.
          await this.backend.salesOrders().save(String(tenant), draft.order);
          return created({
            tenant,
            order: salesOrderJson(draft.order),
            invoice: this.docJson(result.doc),
            entry_id: result.entryId,
          });
        }
      }

      // --- estimates -------------------------------------------------------
      if (rest[0] === "estimates") {
        const ctx = this.estimateCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          const all = await this.backend.estimates().list(String(tenant));
          const latestOnly = req.query["all"] !== "1";
          const rows = latestOnly
            ? all.filter((e) => e.status !== "SUPERSEDED")
            : all;
          return ok({ tenant, estimates: rows.map(estimateJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({ tenant, estimate: estimateJson(await saveEstimate(ctx, data)) });
        }
        if (rest.length === 2 && req.method === "GET") {
          const estimate = await this.backend.estimates().get(String(tenant), rest[1]!);
          if (!estimate) return notFound(`unknown estimate ${rest[1]}`);
          return ok({ tenant, estimate: estimateJson(estimate) });
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await this.backend.estimates().remove(String(tenant), rest[1]!);
          return ok({ tenant, removed: rest[1] });
        }
        if (rest.length === 3 && rest[2] === "revise" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant, estimate: estimateJson(await reviseEstimate(ctx, rest[1]!, data)),
          });
        }
        if (rest.length === 3 && rest[2] === "status" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const status = str(data["status"]).trim().toUpperCase();
          if (!["DRAFT", "SENT", "DECLINED", "EXPIRED"].includes(status)) {
            return bad("status must be DRAFT, SENT, DECLINED or EXPIRED — accept has its own route");
          }
          return ok({
            tenant,
            estimate: estimateJson(
              await setEstimateStatus(ctx, rest[1]!, status as EstimateStatus),
            ),
          });
        }
        if (rest.length === 3 && rest[2] === "accept" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await acceptEstimate(ctx, rest[1]!, data);
          // The feed an external CRM reads should know the deal closed.
          await recordEvent(this.crmCtx(tenant), "estimate.accepted", result.estimate.id, {
            customer_id: result.estimate.customerId,
            job_id: result.jobId,
            job_created: result.created,
          });
          return ok({
            tenant,
            estimate: estimateJson(result.estimate),
            job_id: result.jobId,
            job_created: result.created,
            budget_lines_seeded: result.budgetSeeded,
          });
        }
        if (rest.length === 3 && rest[2] === "invoice" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const estimate = await this.backend.estimates().get(String(tenant), rest[1]!);
          if (!estimate) return notFound(`unknown estimate ${rest[1]}`);
          const invoiceId = str(data["id"]).trim();
          if (!invoiceId) return bad("the invoice needs an id");
          const request = estimateToInvoiceRequest(estimate, {
            id: invoiceId,
            date: str(data["date"]).trim() || estimate.date,
            ...(str(data["due_date"]).trim() ? { due_date: str(data["due_date"]).trim() } : {}),
          });
          const result = await createDocument(
            "invoice", request as unknown as CreateDocumentRequest,
            await this.arapCtx(tenant),
          );
          return created({
            tenant, estimate_id: estimate.id,
            invoice: this.docJson(result.doc), entry_id: result.entryId,
          });
        }
      }

      // --- jobs: cost codes, projects, budgets ----------------------------
      if (rest[0] === "cost-codes") {
        const ctx = this.jobCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          const list = await this.backend.jobs().listCostCodes(String(tenant));
          return ok({ tenant, cost_codes: list.map(costCodeJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant, cost_code: costCodeJson(await saveCostCode(ctx, data)),
          });
        }
        if (rest.length === 2 && rest[1] === "seed" && req.method === "POST") {
          return created({ tenant, cost_codes: (await seedCostCodes(ctx)).map(costCodeJson) });
        }
      }

      if (rest[0] === "jobs") {
        const ctx = this.jobCtx(tenant);
        if (rest.length === 1 && req.method === "GET") return ok(await jobList(ctx));
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({ tenant, job: jobJson(await saveJob(ctx, data)) });
        }
        if (rest.length === 2 && req.method === "GET") {
          const job = await this.backend.jobs().getJob(String(tenant), rest[1]!);
          if (!job) return notFound(`unknown job ${rest[1]}`);
          const budget = await this.backend.jobs().listBudget(String(tenant), rest[1]!);
          return ok({
            tenant,
            job: jobJson(job),
            budget: budget.map((b) => ({
              cost_code: b.costCode,
              budget_cost_minor: b.budgetCostMinor,
              revised_cost_minor: b.revisedCostMinor,
              budget_revenue_minor: b.budgetRevenueMinor,
            })),
          });
        }
        if (rest.length === 3 && rest[2] === "budget" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const lines = await saveJobBudget(ctx, rest[1]!, data);
          return ok({
            tenant,
            job_id: rest[1],
            budget: lines.map((b) => ({
              cost_code: b.costCode,
              budget_cost_minor: b.budgetCostMinor,
              revised_cost_minor: b.revisedCostMinor,
              budget_revenue_minor: b.budgetRevenueMinor,
            })),
          });
        }
        // --- billing a job ------------------------------------------------
        if (rest.length === 3 && rest[2] === "billing" && req.method === "GET") {
          return ok(await billingView(this.billingCtx(tenant), rest[1]!));
        }
        if (rest.length === 3 && rest[2] === "schedule" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const lines = await saveSchedule(this.billingCtx(tenant), rest[1]!, data);
          return ok({
            tenant,
            job_id: rest[1],
            schedule: lines.map((l) => ({
              line_no: l.lineNo,
              description: l.description,
              cost_code: l.costCode,
              scheduled_value_minor: l.scheduledValueMinor,
              billed_minor: l.billedMinor,
            })),
          });
        }
        if (rest.length === 3 && rest[2] === "milestones" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const saved = await saveMilestones(this.billingCtx(tenant), rest[1]!, data);
          return created({
            tenant,
            job_id: rest[1],
            milestones: saved.map((m) => ({
              id: m.id, name: m.name, amount_minor: m.amountMinor,
              due_date: m.dueDate, status: m.status,
            })),
          });
        }
        if (rest.length === 4 && rest[2] === "bill" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const ctx = this.billingCtx(tenant);
          let draft: BillingDraft;
          if (rest[3] === "progress") draft = await billProgress(ctx, rest[1]!, data);
          else if (rest[3] === "milestone") draft = await billMilestone(ctx, rest[1]!, data);
          else if (rest[3] === "time-and-materials") {
            draft = await billTimeAndMaterials(ctx, rest[1]!, data);
          } else return notFound(`unknown billing method ${rest[3]}`);
          return await this.postBillingDraft(tenant, ctx, draft);
        }
        if (rest.length === 4 && rest[2] === "retainage" && rest[3] === "release"
            && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const request = await releaseRetainageRequest(
            this.billingCtx(tenant), rest[1]!, data,
          );
          const result = await createDocument(
            "invoice", request as unknown as CreateDocumentRequest,
            await this.arapCtx(tenant),
          );
          return created({
            tenant, invoice: this.docJson(result.doc), entry_id: result.entryId,
          });
        }
        if (rest.length === 3 && rest[2] === "deposits" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const result = await takeDeposit(this.billingCtx(tenant), rest[1]!, data);
          return created({
            tenant, job_id: rest[1],
            entry_id: result.entryId, deposit_held_minor: result.heldMinor,
          });
        }
        if (rest.length === 4 && rest[2] === "deposits" && rest[3] === "apply"
            && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const ctx = this.billingCtx(tenant);
          const application = await depositApplication(ctx, rest[1]!, data);
          const result = await recordPayment(
            "invoice", application.docId,
            {
              date: application.date,
              amount_minor: application.amountMinor,
              bank_code: CUSTOMER_DEPOSITS_CODE,
              memo: "Deposit applied",
              // The deposit is held per job, so relieving it has to be too —
              // otherwise the job goes on claiming money it has already spent.
              dimensions: { [JOB_DIMENSION]: rest[1]! },
            },
            await this.arapCtx(tenant),
          );
          return created({
            tenant,
            job_id: rest[1],
            invoice: this.docJson(result.doc),
            entry_id: result.entryId,
          });
        }
        if (rest.length === 3 && rest[2] === "work-orders" && req.method === "GET") {
          return ok(await workOrdersForJob(this.workOrderCtx(tenant), rest[1]!));
        }
        if (rest.length === 3 && rest[2] === "cost" && req.method === "GET") {
          const through = req.query["through"];
          // Committed cost comes from purchasing, which the jobs module has no
          // opinion about — it is passed in so the report works either way.
          const committed = await committedByCostCode(this.purchasingCtx(tenant), rest[1]!);
          return ok(await jobCostReport(ctx, rest[1]!, {
            ...(through ? { through } : {}),
            committed,
          }));
        }
      }

      // --- recurring transactions -----------------------------------------
      if (rest[0] === "recurring") {
        const ctx = this.recurringCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          const list = await this.backend.recurring().list(String(tenant));
          return ok({ tenant, recurring: list.map(recurringJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant,
            recurring: recurringJson(
              await saveRecurring(ctx, data as Parameters<typeof saveRecurring>[1]),
            ),
          });
        }
        if (rest.length === 2 && rest[1] === "due" && req.method === "GET") {
          return ok({
            tenant,
            as_of: req.query["as_of"] ?? "",
            due: await dueOccurrences(ctx, req.query["as_of"]),
          });
        }
        if (rest.length === 2 && rest[1] === "run" && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          const only = str(data["id"]).trim();
          return ok(await runDue(ctx, data["as_of"], only || undefined));
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await this.backend.recurring().remove(String(tenant), rest[1]!);
          return ok({ tenant, removed: rest[1] });
        }
      }

      // --- attachments (the receipt behind the number) --------------------
      if (rest[0] === "attachments") {
        const ctx = this.attachmentCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          const list = await listAttachments(ctx, req.query);
          return ok({ tenant, attachments: list.map(attachmentJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant,
            attachment: attachmentJson(
              await saveAttachment(ctx, data as Parameters<typeof saveAttachment>[1]),
            ),
          });
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await this.backend.attachments().remove(String(tenant), rest[1]!);
          return ok({ tenant, removed: rest[1] });
        }
        if (rest.length === 3 && rest[2] === "content" && req.method === "GET") {
          const found = await this.backend.attachments().get(String(tenant), rest[1]!);
          if (!found) return notFound(`unknown attachment ${rest[1]}`);
          return ok({
            ...attachmentJson(found.record),
            content_base64: found.content.toString("base64"),
          });
        }
        if (rest.length === 2 && rest[1] === "counts" && req.method === "GET") {
          const kind = String(req.query["subject_kind"] ?? "") as SubjectKind;
          const counts = await this.backend.attachments().counts(String(tenant), kind);
          return ok({ tenant, counts: Object.fromEntries(counts) });
        }
      }

      // --- reporting dimensions (classes, locations) ----------------------
      if (rest[0] === "dimensions") {
        const ctx = this.dimensionCtx(tenant);
        if (rest.length === 1 && req.method === "GET") {
          const defs = await this.backend.dimensions().list(String(tenant));
          return ok({ tenant, dimensions: defs.map(dimensionJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant,
            dimension: dimensionJson(
              await saveDimension(ctx, data as Parameters<typeof saveDimension>[1]),
            ),
          });
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await this.backend.dimensions().remove(String(tenant), rest[1]!);
          return ok({ tenant, removed: rest[1] });
        }
        if (rest.length === 3 && rest[2] === "report" && req.method === "GET") {
          return ok(await reportByDimension(ctx, rest[1]!, req.query));
        }
      }

      // --- reporting ------------------------------------------------------
      if (rest[0] === "gl" && rest.length === 1 && req.method === "GET") {
        return ok(await generalLedger(this.reportingCtx(tenant), req.query));
      }
      if (rest[0] === "budget") {
        const ctx = this.reportingCtx(tenant);
        if (rest.length === 1 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return created({
            tenant, ...(await saveBudgetLines(ctx, data as Parameters<typeof saveBudgetLines>[1])),
          });
        }
        if (rest.length === 2 && req.method === "GET") {
          return ok(await budgetReport(ctx, rest[1]));
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await this.backend.budgets().deleteBudget(String(tenant), rest[1]!);
          return ok({ tenant, period: rest[1], cleared: true });
        }
        if (rest.length === 2 && rest[1] === "lines" && req.method === "GET") {
          return ok({
            tenant,
            lines: await this.backend.budgets().listBudget(String(tenant)),
          });
        }
      }

      // --- payroll --------------------------------------------------------
      if (rest[0] === "payroll") {
        const ctx = this.payrollCtx(tenant);
        if (rest[1] === "employees" && rest.length === 2) {
          if (req.method === "GET") {
            return ok({ tenant, employees: await this.backend.payroll().listEmployees(String(tenant)) });
          }
          if (req.method === "POST") {
            const data = parseJson(req.body);
            if (!data) return bad("invalid JSON body");
            return created({ tenant, employee: await saveEmployee(ctx, data) });
          }
        }
        if (rest[1] === "runs" && rest.length === 2) {
          if (req.method === "GET") {
            const runs = await this.backend.payroll().listRuns(String(tenant));
            return ok({ tenant, runs: runs.map(runJson) });
          }
          if (req.method === "POST") {
            const data = parseJson(req.body);
            if (!data) return bad("invalid JSON body");
            return created({
              tenant,
              run: runJson(await createRun(ctx, data as Parameters<typeof createRun>[1])),
            });
          }
        }
        if (rest[1] === "runs" && rest.length === 3 && req.method === "GET") {
          const run = await this.backend.payroll().getRun(String(tenant), rest[2]!);
          if (!run) return notFound(`unknown payroll run ${rest[2]}`);
          return ok({ tenant, run: runJson(run) });
        }
        if (rest[1] === "runs" && rest.length === 4 && req.method === "POST") {
          if (rest[3] === "post") return ok(await postRun(ctx, rest[2]!));
          if (rest[3] === "void") return ok({ tenant, run: await voidRun(ctx, rest[2]!) });
        }
        if (rest[1] === "liabilities" && rest.length === 2 && req.method === "GET") {
          return ok(await liabilityView(ctx, req.query["code"] || undefined));
        }
        if (rest[1] === "remit" && rest.length === 2 && req.method === "POST") {
          const data = parseJson(req.body);
          if (!data) return bad("invalid JSON body");
          return ok(await remit(ctx, data as Parameters<typeof remit>[1]));
        }
      }

      // --- the bank feed review inbox ------------------------------------
      if (rest[0] === "feed") {
        // POST /feed/bulk-accept        — checked first: it is not an account code
        if (rest.length === 2 && rest[1] === "bulk-accept" && req.method === "POST") {
          return await this.feedBulkAccept(tenant, req.body);
        }
        // POST /feed/:accountCode        — land a batch of bank lines
        if (rest.length === 2 && req.method === "POST") {
          return await this.feedDeliver(tenant, rest[1]!, req.body);
        }
        // GET  /feed                     — the review queue
        if (rest.length === 1 && req.method === "GET") {
          return ok(await inboxView(this.inboxCtx(tenant), {
            ...(req.query["account_code"] ? { accountCode: req.query["account_code"] } : {}),
            ...(req.query["status"] ? { status: req.query["status"] as FeedStatus } : {}),
          }));
        }
        // POST /feed/txn/:id/:action     — accept | match | exclude | undo
        if (rest.length === 4 && rest[1] === "txn" && req.method === "POST") {
          return await this.feedAction(tenant, rest[2]!, rest[3]!, req.body);
        }
      }
      if (rest[0] === "feed-rules") {
        if (rest.length === 1 && req.method === "GET") {
          const rules = await this.backend.feed().listRules(String(tenant));
          return ok({ tenant, rules: rules.map(ruleJson) });
        }
        if (rest.length === 1 && req.method === "POST") {
          return await this.feedSaveRule(tenant, req.body);
        }
        if (rest.length === 2 && req.method === "DELETE") {
          await this.backend.feed().deleteRule(String(tenant), rest[1]!);
          return ok({ tenant, deleted: rest[1] });
        }
      }

      if (rest[0] === "ingest" && req.method === "POST") {
        return await this.ingest(tenant, req.body);
      }
      if (rest[0] === "go-live" && req.method === "POST") {
        return await this.goLive(tenant, req.body);
      }
    } catch (err) {
      if (err instanceof PeriodClosedError) return conflict(err.message);
      if (err instanceof ArApError) return bad(err.message);
      if (err instanceof ReconcileError) return bad(err.message);
      if (err instanceof InboxError) return bad(err.message);
      if (err instanceof PayrollServiceError) return bad(err.message);
      if (err instanceof ReportingError) return bad(err.message);
      if (err instanceof DimensionServiceError) return bad(err.message);
      if (err instanceof AttachmentError) return bad(err.message);
      if (err instanceof RecurringError) return bad(err.message);
      if (err instanceof Ten99Error) return bad(err.message);
      if (err instanceof JobError) return bad(err.message);
      if (err instanceof EstimateError) return bad(err.message);
      if (err instanceof SalesOrderError) return bad(err.message);
      if (err instanceof WorkOrderError) return bad(err.message);
      if (err instanceof PurchasingError) return bad(err.message);
      if (err instanceof WipError) return bad(err.message);
      if (err instanceof BillingError) return bad(err.message);
      if (err instanceof InventoryError) return bad(err.message);
      if (err instanceof CrmError) return bad(err.message);
      if (err instanceof ConsolidationServiceError) return bad(err.message);
      if (err instanceof SettingsError) return bad(err.message);
      if (err instanceof DebtError) return bad(err.message);
      if (err instanceof FixedAssetError) return bad(err.message);
      return bad(err instanceof Error ? err.message : String(err));
    }
    return notFound("not found");
  }

  // --- accounts -------------------------------------------------------------

  private async listAccounts(tenant: TenantId): Promise<ServiceResponse> {
    const chart = await this.backend.chart(tenant);
    const accounts = [...chart.list()].sort((a, b) => a.code.localeCompare(b.code));
    return ok({ tenant, accounts: accounts.map(accountJson) });
  }

  private async createAccount(tenant: TenantId, body: string): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const code = str(data["code"]).trim();
    const name = str(data["name"]).trim();
    if (!code || !name) return bad("code and name are required");

    const subtypeRaw = str(data["subtype"]).trim();
    const typeRaw = str(data["type"]).trim();
    let type: AccountType;
    let subtype: AccountSubtype | undefined;
    if (subtypeRaw) {
      if (!(Object.values(AccountSubtype) as string[]).includes(subtypeRaw)) {
        return bad(`unknown subtype ${subtypeRaw}`);
      }
      subtype = subtypeRaw as AccountSubtype;
      type = accountTypeOfSubtype(subtype);
    } else if (typeRaw) {
      if (!(Object.values(AccountType) as string[]).includes(typeRaw)) {
        return bad(`unknown type ${typeRaw}`);
      }
      type = typeRaw as AccountType;
    } else {
      return bad("an account needs a subtype or a type");
    }

    const chart = await this.backend.chart(tenant);
    if (chart.getByCode(code)) return conflict(`account ${code} already exists`);

    const account: Account = {
      id: asAccountId(`acct:${code}`),
      code,
      name,
      type,
      currency: this.currency,
      ...(subtype ? { subtype } : {}),
      active: true,
    };
    new ChartOfAccounts([...chart.list(), account]); // kernel-validate before persisting
    await this.backend.saveAccount(tenant, account);
    return created({ tenant, account: accountJson(account) });
  }

  private async seedAccounts(tenant: TenantId, body: string): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const category = str(data["category"]).trim();
    if (!(Object.values(BusinessCategory) as string[]).includes(category)) {
      return bad(`unknown category ${category}`);
    }
    const currency = str(data["currency"]) ? getCurrency(str(data["currency"])) : this.currency;
    const seeded = await this.backend.seedChart(tenant, category as BusinessCategory, currency);
    return created({ tenant, category, seeded: seeded.length });
  }

  // --- journal --------------------------------------------------------------

  /**
   * Correct a posted entry the only way the ledger allows: by posting its
   * mirror image. The original stays exactly where it is, which is what makes
   * the journal something an auditor can read backwards.
   */
  private async reverseEntry(
    tenant: TenantId, entryId: string, body: string,
  ): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const original = await this.backend.store(tenant)
      .getById(tenant, entryId as unknown as PostedEntry["id"]);
    if (!original) return notFound(`unknown entry ${entryId}`);
    const date = str(data["date"]).trim() || original.entryDate;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return bad("date must be YYYY-MM-DD");
    const engine = new PostingEngine(
      await this.backend.chart(tenant), this.backend.store(tenant), this.backend.periods(tenant),
    );
    const memo = str(data["memo"]).trim();
    const reversal = await engine.reverse(tenant, original.id, {
      idempotencyKey: asIdempotencyKey(`reverse:${entryId}`),
      periodKey: asPeriodKey(date.slice(0, 7)),
      entryDate: date,
      postedAt: this.opts.now(),
      provenance: provenanceFor("manual-reversal", date, this.opts.now()),
      ...(memo ? { memo } : {}),
    });
    return created({ tenant, reversed: entryId, entry: entryJson(reversal) });
  }

  private async postEntry(tenant: TenantId, body: string): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const date = str(data["date"]).trim();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return bad("date must be YYYY-MM-DD");
    const rawLines = data["lines"];
    if (!Array.isArray(rawLines) || rawLines.length < 2) {
      return bad("an entry needs at least two lines");
    }

    const chart = await this.backend.chart(tenant);
    const lines: JournalLineInput[] = [];
    for (const raw of rawLines) {
      if (typeof raw !== "object" || raw === null) return bad("each line must be an object");
      const l = raw as Record<string, unknown>;
      const code = str(l["code"]).trim();
      const account = chart.getByCode(code);
      if (!account) return bad(`unknown account code ${code}`);
      const side = str(l["side"]).toUpperCase();
      if (side !== "DEBIT" && side !== "CREDIT") {
        return bad(`line ${code}: side must be DEBIT or CREDIT`);
      }
      const amountRaw = l["amount_minor"];
      if (typeof amountRaw !== "string" && typeof amountRaw !== "number") {
        return bad(`line ${code}: amount_minor must be an integer minor-unit value`);
      }
      let minor: bigint;
      try {
        minor = BigInt(amountRaw);
      } catch {
        return bad(`line ${code}: amount_minor is not an integer`);
      }
      if (minor <= 0n) {
        return bad(`line ${code}: amount must be positive (the side conveys direction)`);
      }
      const dimensions = dimensionsOf(l["dimensions"], `line ${code}`);
      lines.push({
        accountId: account.id,
        side,
        amount: Money.fromMinorUnits(minor, this.currency),
        ...(str(l["memo"]) ? { memo: str(l["memo"]) } : {}),
        ...(dimensions ? { dimensions } : {}),
      });
    }

    const memo = str(data["memo"]);
    const fingerprint = lines
      .map((l) => `${String(l.accountId)}:${l.side}:${l.amount.minorUnits}`)
      .join("|");
    const key = str(data["idempotency_key"]).trim() || `je:${date}:${fingerprint}`;
    const command: PostCommand = {
      tenantId: tenant,
      idempotencyKey: asIdempotencyKey(key),
      periodKey: asPeriodKey(date.slice(0, 7)),
      currency: this.currency,
      entryDate: date,
      ...(memo ? { memo } : {}),
      provenance: provenanceFor(str(data["source"]) || "manual", date, this.opts.now()),
      lines,
    };

    // A typo'd class is refused at the door rather than silently fragmenting a
    // report months later.
    await validateDimensions(this.dimensionCtx(tenant), command);

    const engine = new PostingEngine(chart, this.backend.store(tenant), this.backend.periods(tenant));
    const entry = await engine.post(command, { postedAt: this.opts.now() });
    return created({ tenant, entry: entryJson(entry) });
  }

  private async listEntries(
    tenant: TenantId,
    query: Readonly<Record<string, string>>,
  ): Promise<ServiceResponse> {
    const all = await this.backend.store(tenant).list(tenant);
    const from = query["from"];
    const to = query["to"];
    const rows = all.filter(
      (e) => (from === undefined || e.entryDate >= from) && (to === undefined || e.entryDate <= to),
    );
    return ok({ tenant, entries: rows.map(entryJson) });
  }

  // --- reporting ------------------------------------------------------------

  private windowFrom(
    query: Readonly<Record<string, string>>,
  ): { from?: string; to?: string } | undefined {
    const from = query["from"];
    const to = query["to"];
    if (!from && !to) return undefined;
    return { ...(from ? { from } : {}), ...(to ? { to } : {}) };
  }

  private async trialBalance(
    tenant: TenantId,
    query: Readonly<Record<string, string>>,
  ): Promise<ServiceResponse> {
    const chart = await this.backend.chart(tenant);
    const window = this.windowFrom(query);
    const tb = await computeTrialBalance(
      this.backend.store(tenant),
      tenant,
      chart,
      this.currency,
      window,
    );
    return ok({
      tenant,
      currency: tb.currency.code,
      in_balance: tb.inBalance,
      total_debit_minor: tb.totalDebit.minorUnits.toString(),
      total_credit_minor: tb.totalCredit.minorUnits.toString(),
      rows: tb.rows.map((r) => ({
        code: r.code,
        name: r.name,
        type: r.type,
        debit_minor: r.debit.minorUnits.toString(),
        credit_minor: r.credit.minorUnits.toString(),
      })),
    });
  }

  /**
   * The three statements for a period as `financial-statements/1` — computed
   * from the tenant's own posted books (period P&L, as-of balance sheet, and an
   * indirect cash flow between the opening and closing positions).
   */
  private async statements(
    tenant: TenantId,
    query: Readonly<Record<string, string>>,
  ): Promise<ServiceResponse> {
    const from = query["from"];
    const to = query["to"];
    if (!from || !to) return bad("statements need ?from=YYYY-MM-DD&to=YYYY-MM-DD");
    const chart = await this.backend.chart(tenant);
    const store = this.backend.store(tenant);

    const periodTb = fromKernelTrialBalance(
      await computeTrialBalance(store, tenant, chart, this.currency, { from, to }),
    );
    const endTb = fromKernelTrialBalance(
      await computeTrialBalance(store, tenant, chart, this.currency, { to }),
    );
    const startTb = fromKernelTrialBalance(
      await computeTrialBalance(store, tenant, chart, this.currency, { to: previousDay(from) }),
    );

    const income = incomeStatement(periodTb);
    // Equity as-of `to` must carry earnings retained before the window opened,
    // or a mid-year balance sheet is out of balance by prior net income.
    const beginningRetained = incomeStatement(startTb).netIncome;
    const bs = balanceSheet(endTb, income.netIncome, beginningRetained);
    const cf = cashFlow(startTb, endTb, income.netIncome, subtypeCashFlowClassifier(chart));

    return ok(
      financialStatementsJson({
        period: `${from}..${to}`,
        currency: this.currency.code,
        income,
        balanceSheet: bs,
        cashFlow: cf,
      }),
    );
  }

  /** One account's register (GL detail with a running balance). */
  private async register(
    tenant: TenantId,
    code: string,
    query: Readonly<Record<string, string>>,
  ): Promise<ServiceResponse> {
    const chart = await this.backend.chart(tenant);
    const account = chart.getByCode(code);
    if (!account) return notFound(`unknown account code ${code}`);
    const entries = await this.backend.store(tenant).list(tenant);
    const window = this.windowFrom(query);
    const detail = accountLedger(entries, account.id, chart, this.currency, window);
    if (!detail) return notFound(`unknown account code ${code}`);
    return ok({
      tenant,
      code: detail.code,
      name: detail.name,
      type: detail.type,
      opening_minor: detail.opening.minorUnits.toString(),
      closing_minor: detail.closing.minorUnits.toString(),
      total_debit_minor: detail.totalDebit.minorUnits.toString(),
      total_credit_minor: detail.totalCredit.minorUnits.toString(),
      rows: detail.rows.map((r) => ({
        entry_id: r.entryId,
        date: r.date,
        memo: r.memo,
        debit_minor: r.debit.minorUnits.toString(),
        credit_minor: r.credit.minorUnits.toString(),
        balance_minor: r.balance.minorUnits.toString(),
      })),
    });
  }

  // --- recurring transactions ------------------------------------------------

  private recurringCtx(tenant: TenantId): RecurringContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  // --- jobs ------------------------------------------------------------------

  private consolidationCtx(tenant: TenantId): ConsolidationContext {
    return { backend: this.backend, tenant, currency: this.currency };
  }

  private crmCtx(tenant: TenantId): CrmContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  private inventoryCtx(tenant: TenantId): InventoryContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  private debtCtx(tenant: TenantId): DebtContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  private fixedAssetCtx(tenant: TenantId): FixedAssetContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  private billingCtx(tenant: TenantId): BillingContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  /**
   * Post a billing draft: the invoice through the ordinary AR path, then the
   * retained portion, then whatever the draft consumed. In that order, so a
   * refused invoice leaves the schedule, the milestone and the timesheets
   * exactly as they were.
   */
  private async postBillingDraft(
    tenant: TenantId, ctx: BillingContext, draft: BillingDraft,
  ): Promise<ServiceResponse> {
    const result = await createDocument(
      "invoice", draft.request as unknown as CreateDocumentRequest,
      await this.arapCtx(tenant),
    );
    const invoiceId = String(draft.request["id"]);
    const date = String(draft.request["date"]);
    const retainageEntry = await postRetainage(ctx, draft, invoiceId, date);
    await applyDraft(ctx, draft, invoiceId);
    return created({
      tenant,
      job_id: draft.job.id,
      invoice: this.docJson(result.doc),
      entry_id: result.entryId,
      gross_minor: draft.grossMinor.toString(),
      retainage_minor: draft.retainageMinor.toString(),
      retainage_entry_id: retainageEntry,
    });
  }

  private wipCtx(tenant: TenantId): WipContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  private purchasingCtx(tenant: TenantId): PurchasingContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  private workOrderCtx(tenant: TenantId): WorkOrderContext {
    return {
      backend: this.backend, tenant, currency: this.currency, now: this.opts.now,
    };
  }

  private salesOrderCtx(tenant: TenantId): SalesOrderContext {
    return { backend: this.backend, tenant, currency: this.currency };
  }

  private estimateCtx(tenant: TenantId): EstimateContext {
    return { backend: this.backend, tenant, currency: this.currency };
  }

  private jobCtx(tenant: TenantId): JobContext {
    return { backend: this.backend, tenant, currency: this.currency };
  }

  // --- attachments -----------------------------------------------------------

  private attachmentCtx(tenant: TenantId): AttachmentContext {
    return { backend: this.backend, tenant, now: this.opts.now };
  }

  // --- reporting dimensions --------------------------------------------------

  private dimensionCtx(tenant: TenantId): DimensionContext {
    return { backend: this.backend, tenant, currency: this.currency };
  }

  // --- reporting -------------------------------------------------------------

  private reportingCtx(tenant: TenantId): ReportingContext {
    return { backend: this.backend, tenant, currency: this.currency };
  }

  // --- payroll ---------------------------------------------------------------

  private payrollCtx(tenant: TenantId): PayrollContext {
    return {
      backend: this.backend,
      tenant,
      currency: this.currency,
      now: this.opts.now,
    };
  }

  // --- the bank feed review inbox -------------------------------------------

  private inboxCtx(tenant: TenantId): InboxContext {
    return {
      backend: this.backend,
      tenant,
      currency: this.currency,
      now: this.opts.now,
    };
  }

  private async feedDeliver(
    tenant: TenantId, accountCode: string, body: string,
  ): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const txns = data["transactions"];
    if (!Array.isArray(txns)) return bad("transactions must be an array");
    const report = await deliverFeed(
      this.inboxCtx(tenant), accountCode,
      txns as Parameters<typeof deliverFeed>[2],
      str(data["source"]) || "feed",
    );
    return created({ tenant, account_code: accountCode, ...report });
  }

  private async feedAction(
    tenant: TenantId, id: string, action: string, body: string,
  ): Promise<ServiceResponse> {
    const data = parseJson(body) ?? {};
    const ctx = this.inboxCtx(tenant);
    switch (action) {
      case "accept":
        return ok(await acceptTxn(
          ctx, id, str(data["category_code"]),
          dimensionsOf(data["dimensions"], "this transaction"),
        ));
      case "match":
        return ok(await matchTxn(ctx, id, str(data["doc_kind"]), str(data["doc_id"])));
      case "exclude":
        return ok(await excludeTxn(ctx, id, str(data["reason"])));
      case "undo":
        return ok(await undoTxn(ctx, id));
      default:
        return notFound(`unknown action ${action}`);
    }
  }

  private async feedBulkAccept(tenant: TenantId, body: string): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const raw = data["min_confidence"];
    const min = typeof raw === "number" ? raw : Number(str(raw));
    const account = str(data["account_code"]).trim();
    return ok(await bulkAccept(this.inboxCtx(tenant), min, account || undefined));
  }

  private async feedSaveRule(tenant: TenantId, body: string): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const ctx = this.inboxCtx(tenant);
    const rule = await saveRule(ctx, data as Parameters<typeof saveRule>[1]);
    return created({ tenant, rule: ruleJson(rule), would_match: await ruleImpact(ctx, rule) });
  }

  // --- bank reconciliation ---------------------------------------------------

  private reconCtx(tenant: TenantId): ReconcileContext {
    return {
      backend: this.backend,
      recon: this.backend.recon(),
      tenant,
      currency: this.currency,
    };
  }

  private async reconToggle(
    tenant: TenantId, code: string, body: string,
  ): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("body must be a JSON object");
    const cleared = data["cleared"];
    if (typeof cleared !== "boolean") return bad("cleared must be true or false");
    await toggleCleared(this.reconCtx(tenant), code, str(data["entry_id"]), cleared);
    return ok(
      await reconcileView(
        this.reconCtx(tenant), code, data["statement_date"], data["statement_balance_minor"],
      ),
    );
  }

  private async reconFinish(
    tenant: TenantId, code: string, body: string,
  ): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("body must be a JSON object");
    return ok(
      await finishReconciliation(
        this.reconCtx(tenant), code, data["statement_date"], data["statement_balance_minor"],
      ),
    );
  }

  // --- AR / AP --------------------------------------------------------------

  private async arapCtx(tenant: TenantId): Promise<ArApContext> {
    return {
      tenant: String(tenant),
      chart: await this.backend.chart(tenant),
      store: this.backend.store(tenant),
      periods: this.backend.periods(tenant),
      docs: this.backend.documents(),
      currency: this.currency,
      postedAt: this.opts.now(),
      validate: (command) => validateDimensions(
        { backend: this.backend, tenant, currency: this.currency }, command,
      ),
    };
  }

  private async listParties(tenant: TenantId, kind: PartyKind): Promise<ServiceResponse> {
    const parties = await this.backend.documents().listParties(String(tenant), kind);
    return ok({ tenant, kind, parties });
  }

  private async createParty(
    tenant: TenantId, kind: PartyKind, body: string,
  ): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const party = validateParty(data);
    await this.backend.documents().upsertParty(String(tenant), kind, party);
    return created({ tenant, kind, party });
  }

  private docJson(d: DocRecord): Record<string, unknown> {
    return {
      id: d.id,
      kind: d.kind,
      party_id: d.partyId,
      date: d.date,
      due_date: d.dueDate,
      net_minor: d.netMinor,
      tax_minor: d.taxMinor,
      tax_rate_ppm: d.taxRatePpm,
      total_minor: d.totalMinor,
      open_minor: d.openMinor,
      status: d.status,
      memo: d.memo,
      lines: d.lines.map((l) => ({
        description: l.description,
        quantity: l.quantity,
        unit_amount_minor: l.unitAmountMinor,
        account_code: l.accountCode,
        amount_minor: l.amountMinor,
        taxable: l.taxable,
      })),
    };
  }

  private async listDocs(tenant: TenantId, kind: DocKind): Promise<ServiceResponse> {
    const docs = await this.backend.documents().listDocs(String(tenant), kind);
    let openTotal = 0n;
    let overdueTotal = 0n;
    const today = this.opts.now().slice(0, 10);
    for (const d of docs) {
      const open = BigInt(d.openMinor);
      openTotal += open;
      if (open > 0n && d.dueDate < today) overdueTotal += open;
    }
    return ok({
      tenant,
      kind,
      documents: docs.map((d) => this.docJson(d)),
      open_total_minor: openTotal.toString(),
      overdue_total_minor: overdueTotal.toString(),
    });
  }

  private async getDoc(tenant: TenantId, kind: DocKind, id: string): Promise<ServiceResponse> {
    const doc = await this.backend.documents().getDoc(String(tenant), kind, id);
    if (!doc) return notFound(`unknown ${kind} ${id}`);
    const payments = await this.backend.documents().listPayments(String(tenant), kind, id);
    return ok({
      tenant,
      document: this.docJson(doc),
      payments: payments.map((p) => ({
        id: p.id, date: p.date, amount_minor: p.amountMinor, bank_code: p.bankCode,
      })),
    });
  }

  private async createDoc(
    tenant: TenantId, kind: DocKind, body: string,
  ): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const result = await createDocument(
      kind, data as unknown as CreateDocumentRequest, await this.arapCtx(tenant),
    );
    return created({ tenant, document: this.docJson(result.doc), entry_id: result.entryId });
  }

  private async payDoc(
    tenant: TenantId, kind: DocKind, id: string, body: string,
  ): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const result = await recordPayment(
      kind, id, data as unknown as RecordPaymentRequest, await this.arapCtx(tenant),
    );
    return created({
      tenant,
      document: this.docJson(result.doc),
      entry_id: result.entryId,
      applied_minor: result.appliedMinor,
    });
  }

  private creditJson(
    tenant: TenantId,
    result: { doc: DocRecord; entryId: string; appliedMinor: string; kind: string },
  ): ServiceResponse {
    return ok({
      tenant,
      document: this.docJson(result.doc),
      entry_id: result.entryId,
      applied_minor: result.appliedMinor,
      kind: result.kind,
    });
  }

  private async aging(
    tenant: TenantId, kind: DocKind, query: Readonly<Record<string, string>>,
  ): Promise<ServiceResponse> {
    const asOf = query["as_of"] ?? this.opts.now().slice(0, 10);
    return ok(await aging(kind, asOf, await this.arapCtx(tenant)));
  }

  // --- feed → ledger --------------------------------------------------------

  /**
   * Post a batch of bank/card/QBO feed transactions. This is what keeps the
   * books current between manual entries; it is idempotent, so a sync that
   * overlaps a previous window re-reports rather than double-posts.
   */
  private async ingest(tenant: TenantId, body: string): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const chart = await this.backend.chart(tenant);
    const report = await ingestTransactions(
      data as unknown as IngestRequest,
      chart,
      this.backend.store(tenant),
      this.backend.periods(tenant),
      tenant,
      this.currency,
      this.opts.now(),
    );
    return ok({
      tenant,
      posted: report.posted,
      already_posted: report.alreadyPosted,
      skipped_transfers: report.skippedTransfers,
      skipped_zero: report.skippedZero,
      blocked_by_closed_period: report.blockedByClosedPeriod,
      failed: report.failed.map((f) => ({ key: f.key, error: f.error })),
    });
  }

  // --- go-live --------------------------------------------------------------

  private async goLive(tenant: TenantId, body: string): Promise<ServiceResponse> {
    const data = parseJson(body);
    if (!data) return bad("invalid JSON body");
    const dto = { ...(data as unknown as GoLiveDto), tenant_id: String(tenant) };
    const request = goLiveFromDto(dto);
    const result = await executeGoLive(
      this.backend.store(tenant),
      this.backend.periods(tenant),
      request,
      this.opts.now(),
    );
    for (const account of result.chart.list()) await this.backend.saveAccount(tenant, account);
    return ok({
      tenant,
      opening_entry_id: result.cutover.entry.id,
      accounts: result.chart.list().length,
      created_accounts: result.createdAccounts.length,
      locked_period: result.cutover.record.lockedThroughPeriod,
      opening_total_minor: result.cutover.record.openingTotal.minorUnits.toString(),
    });
  }
}
