import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  type Currency,
  type JournalLineInput,
  type PostCommand,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import { validateDimensions } from "./dimensions.js";
import { mulDiv } from "./estimates.js";

/**
 * Work orders — the unit of work that actually gets scheduled, done, costed and
 * billed, and the piece the accounting stack normally has no place for.
 *
 * A job spans months. Nobody works on a job; they work on "go back Tuesday and
 * hang the doors". That is a work order: it belongs to a job, it has a day, a
 * person, and when it is done it has hours and materials attached to it. Those
 * hours are simultaneously three things — a cost to the job, a line on a
 * time-and-materials invoice, and a fact about what the crew did — and systems
 * that pick one of the three make the other two manual.
 *
 * ## The double-counting problem, and what is done about it
 *
 * The naive design posts every time entry as a cost: debit Job Labor, credit
 * cash. That books the labor **twice**, because the wage is already in the
 * books through payroll. A business running both would see its labor cost
 * double and its margin vanish.
 *
 * So a labor entry here posts an **allocation**, not a cost: it debits the job's
 * labor account and credits the payroll expense account it came from. The P&L
 * total is unchanged; the cost simply moves from an undifferentiated "Payroll —
 * Wages" line onto the job that consumed it. That is what job costing is, and
 * doing it any other way is how a contractor ends up with two labor numbers and
 * no idea which is real.
 *
 * A material entry works the same way against inventory: debit job materials,
 * credit the inventory asset. Material that was never in stock is not a work
 * order entry at all — it is a bill coded to the job, which already works.
 *
 * An entry with nowhere honest to take the cost **from** is recorded but not
 * posted, and says so. A cost invented from nothing is worse than a cost
 * missing.
 *
 * ## Cost rate, not wage
 *
 * An hour is costed at the employee's *burdened* rate — wage plus payroll
 * taxes, insurance, and the rest. A job costed at the bare wage is costed at
 * roughly 70% of the truth, and every margin computed from it is wrong in the
 * same direction, which is the direction that loses money.
 */

export class WorkOrderError extends Error {}

export type WorkOrderStatus =
  | "DRAFT" | "SCHEDULED" | "IN_PROGRESS" | "COMPLETE" | "CANCELLED";
export type EntryKind = "LABOR" | "MATERIAL" | "EQUIPMENT" | "OTHER";

const STATUSES: readonly WorkOrderStatus[] = [
  "DRAFT", "SCHEDULED", "IN_PROGRESS", "COMPLETE", "CANCELLED",
];
const KINDS: readonly EntryKind[] = ["LABOR", "MATERIAL", "EQUIPMENT", "OTHER"];
const MILLI = 1000n;

export interface WorkOrderRecord {
  readonly id: string;
  readonly jobId: string;
  readonly salesOrderId: string;
  readonly title: string;
  readonly description: string;
  readonly status: WorkOrderStatus;
  readonly scheduledDate: string;
  readonly completedDate: string;
  /** Who is doing it — an employee id when known, free text otherwise. */
  readonly assigneeId: string;
  readonly assignee: string;
  readonly memo: string;
}

export interface WorkOrderEntryRecord {
  readonly id: string;
  readonly workOrderId: string;
  readonly kind: EntryKind;
  readonly date: string;
  readonly description: string;
  readonly costCode: string;
  readonly employeeId: string;
  /** Hours or units, in thousandths. */
  readonly quantityMilli: string;
  readonly unitCostMinor: string;
  readonly unitBillMinor: string;
  readonly billable: boolean;
  /** Where the cost lands on the job. */
  readonly costAccountCode: string;
  /** Where it comes from — the account being relieved. Empty means unposted. */
  readonly relieveAccountCode: string;
  /** The journal entry that allocated it, if any. */
  readonly entryId: string;
  /** The invoice that billed it, if any. */
  readonly invoiceId: string;
}

export interface WorkOrderStore {
  migrate(): Promise<void>;
  list(tenant: string): Promise<WorkOrderRecord[]>;
  get(tenant: string, id: string): Promise<WorkOrderRecord | undefined>;
  save(tenant: string, record: WorkOrderRecord): Promise<void>;
  remove(tenant: string, id: string): Promise<void>;
  listEntries(tenant: string, workOrderId?: string): Promise<WorkOrderEntryRecord[]>;
  getEntry(tenant: string, id: string): Promise<WorkOrderEntryRecord | undefined>;
  saveEntry(tenant: string, entry: WorkOrderEntryRecord): Promise<void>;
  removeEntry(tenant: string, id: string): Promise<void>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryWorkOrderStore implements WorkOrderStore {
  private readonly orders = new Map<string, WorkOrderRecord>();
  private readonly entries = new Map<string, WorkOrderEntryRecord>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  list(tenant: string): Promise<WorkOrderRecord[]> {
    const out: WorkOrderRecord[] = [];
    for (const [k, v] of this.orders) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => (
      a.scheduledDate === b.scheduledDate
        ? a.id.localeCompare(b.id)
        : a.scheduledDate.localeCompare(b.scheduledDate)
    )));
  }

  get(tenant: string, id: string): Promise<WorkOrderRecord | undefined> {
    return Promise.resolve(this.orders.get(`${tenant}::${id}`));
  }

  save(tenant: string, record: WorkOrderRecord): Promise<void> {
    this.orders.set(`${tenant}::${record.id}`, record);
    return Promise.resolve();
  }

  remove(tenant: string, id: string): Promise<void> {
    this.orders.delete(`${tenant}::${id}`);
    for (const [k, v] of [...this.entries]) {
      if (k.startsWith(`${tenant}::`) && v.workOrderId === id) this.entries.delete(k);
    }
    return Promise.resolve();
  }

  listEntries(tenant: string, workOrderId?: string): Promise<WorkOrderEntryRecord[]> {
    const out: WorkOrderEntryRecord[] = [];
    for (const [k, v] of this.entries) {
      if (!k.startsWith(`${tenant}::`)) continue;
      if (workOrderId && v.workOrderId !== workOrderId) continue;
      out.push(v);
    }
    return Promise.resolve(out.sort((a, b) => (
      a.date === b.date ? a.id.localeCompare(b.id) : a.date.localeCompare(b.date)
    )));
  }

  getEntry(tenant: string, id: string): Promise<WorkOrderEntryRecord | undefined> {
    return Promise.resolve(this.entries.get(`${tenant}::${id}`));
  }

  saveEntry(tenant: string, entry: WorkOrderEntryRecord): Promise<void> {
    this.entries.set(`${tenant}::${entry.id}`, entry);
    return Promise.resolve();
  }

  removeEntry(tenant: string, id: string): Promise<void> {
    this.entries.delete(`${tenant}::${id}`);
    return Promise.resolve();
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const WORK_ORDER_DDL = `
CREATE TABLE IF NOT EXISTS work_order (
  tenant_id      text NOT NULL,
  id             text NOT NULL,
  job_id         text NOT NULL,
  sales_order_id text NOT NULL DEFAULT '',
  title          text NOT NULL DEFAULT '',
  description    text NOT NULL DEFAULT '',
  status         text NOT NULL DEFAULT 'DRAFT',
  scheduled_date text NOT NULL DEFAULT '',
  completed_date text NOT NULL DEFAULT '',
  assignee_id    text NOT NULL DEFAULT '',
  assignee       text NOT NULL DEFAULT '',
  memo           text NOT NULL DEFAULT '',
  CONSTRAINT work_order_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS work_order_entry (
  tenant_id            text NOT NULL,
  id                   text NOT NULL,
  work_order_id        text NOT NULL,
  kind                 text NOT NULL DEFAULT 'LABOR',
  entry_date           text NOT NULL,
  description          text NOT NULL DEFAULT '',
  cost_code            text NOT NULL DEFAULT '',
  employee_id          text NOT NULL DEFAULT '',
  quantity_milli       text NOT NULL DEFAULT '0',
  unit_cost_minor      text NOT NULL DEFAULT '0',
  unit_bill_minor      text NOT NULL DEFAULT '0',
  billable             boolean NOT NULL DEFAULT true,
  cost_account_code    text NOT NULL DEFAULT '',
  relieve_account_code text NOT NULL DEFAULT '',
  entry_id             text NOT NULL DEFAULT '',
  invoice_id           text NOT NULL DEFAULT '',
  CONSTRAINT work_order_entry_pk PRIMARY KEY (tenant_id, id)
);
`;

export class PgWorkOrderStore implements WorkOrderStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(WORK_ORDER_DDL);
  }

  private async tx<T>(tenant: string, fn: (db: Queryable) => Promise<T>): Promise<T> {
    const client = await this.pool.connect();
    try {
      await client.query("BEGIN");
      await client.query("SELECT set_config('app.tenant_id', $1, true)", [tenant]);
      const result = await fn(client);
      await client.query("COMMIT");
      return result;
    } catch (err) {
      try {
        await client.query("ROLLBACK");
      } catch {
        /* ignore */
      }
      throw err;
    } finally {
      client.release();
    }
  }

  private orderFrom(r: Record<string, unknown>): WorkOrderRecord {
    return {
      id: String(r["id"]),
      jobId: String(r["job_id"]),
      salesOrderId: String(r["sales_order_id"] ?? ""),
      title: String(r["title"] ?? ""),
      description: String(r["description"] ?? ""),
      status: String(r["status"]) as WorkOrderStatus,
      scheduledDate: String(r["scheduled_date"] ?? ""),
      completedDate: String(r["completed_date"] ?? ""),
      assigneeId: String(r["assignee_id"] ?? ""),
      assignee: String(r["assignee"] ?? ""),
      memo: String(r["memo"] ?? ""),
    };
  }

  private entryFrom(r: Record<string, unknown>): WorkOrderEntryRecord {
    return {
      id: String(r["id"]),
      workOrderId: String(r["work_order_id"]),
      kind: String(r["kind"]) as EntryKind,
      date: String(r["entry_date"]),
      description: String(r["description"] ?? ""),
      costCode: String(r["cost_code"] ?? ""),
      employeeId: String(r["employee_id"] ?? ""),
      quantityMilli: String(r["quantity_milli"] ?? "0"),
      unitCostMinor: String(r["unit_cost_minor"] ?? "0"),
      unitBillMinor: String(r["unit_bill_minor"] ?? "0"),
      billable: r["billable"] !== false && r["billable"] !== "f",
      costAccountCode: String(r["cost_account_code"] ?? ""),
      relieveAccountCode: String(r["relieve_account_code"] ?? ""),
      entryId: String(r["entry_id"] ?? ""),
      invoiceId: String(r["invoice_id"] ?? ""),
    };
  }

  async list(tenant: string): Promise<WorkOrderRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM work_order WHERE tenant_id=$1 ORDER BY scheduled_date, id", [tenant],
      );
      return res.rows.map((r) => this.orderFrom(r));
    });
  }

  async get(tenant: string, id: string): Promise<WorkOrderRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM work_order WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      return row ? this.orderFrom(row) : undefined;
    });
  }

  async save(tenant: string, record: WorkOrderRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO work_order (tenant_id, id, job_id, sales_order_id, title, description,
         status, scheduled_date, completed_date, assignee_id, assignee, memo)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         job_id=EXCLUDED.job_id, sales_order_id=EXCLUDED.sales_order_id,
         title=EXCLUDED.title, description=EXCLUDED.description, status=EXCLUDED.status,
         scheduled_date=EXCLUDED.scheduled_date, completed_date=EXCLUDED.completed_date,
         assignee_id=EXCLUDED.assignee_id, assignee=EXCLUDED.assignee, memo=EXCLUDED.memo`,
      [
        tenant, record.id, record.jobId, record.salesOrderId, record.title,
        record.description, record.status, record.scheduledDate, record.completedDate,
        record.assigneeId, record.assignee, record.memo,
      ],
    ));
  }

  async remove(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, async (db) => {
      await db.query(
        "DELETE FROM work_order_entry WHERE tenant_id=$1 AND work_order_id=$2", [tenant, id],
      );
      await db.query("DELETE FROM work_order WHERE tenant_id=$1 AND id=$2", [tenant, id]);
    });
  }

  async listEntries(tenant: string, workOrderId?: string): Promise<WorkOrderEntryRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = workOrderId
        ? await db.query(
          `SELECT * FROM work_order_entry WHERE tenant_id=$1 AND work_order_id=$2
           ORDER BY entry_date, id`, [tenant, workOrderId],
        )
        : await db.query(
          "SELECT * FROM work_order_entry WHERE tenant_id=$1 ORDER BY entry_date, id", [tenant],
        );
      return res.rows.map((r) => this.entryFrom(r));
    });
  }

  async getEntry(tenant: string, id: string): Promise<WorkOrderEntryRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM work_order_entry WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      return row ? this.entryFrom(row) : undefined;
    });
  }

  async saveEntry(tenant: string, entry: WorkOrderEntryRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO work_order_entry (tenant_id, id, work_order_id, kind, entry_date,
         description, cost_code, employee_id, quantity_milli, unit_cost_minor,
         unit_bill_minor, billable, cost_account_code, relieve_account_code,
         entry_id, invoice_id)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         work_order_id=EXCLUDED.work_order_id, kind=EXCLUDED.kind,
         entry_date=EXCLUDED.entry_date, description=EXCLUDED.description,
         cost_code=EXCLUDED.cost_code, employee_id=EXCLUDED.employee_id,
         quantity_milli=EXCLUDED.quantity_milli, unit_cost_minor=EXCLUDED.unit_cost_minor,
         unit_bill_minor=EXCLUDED.unit_bill_minor, billable=EXCLUDED.billable,
         cost_account_code=EXCLUDED.cost_account_code,
         relieve_account_code=EXCLUDED.relieve_account_code,
         entry_id=EXCLUDED.entry_id, invoice_id=EXCLUDED.invoice_id`,
      [
        tenant, entry.id, entry.workOrderId, entry.kind, entry.date, entry.description,
        entry.costCode, entry.employeeId, entry.quantityMilli, entry.unitCostMinor,
        entry.unitBillMinor, entry.billable, entry.costAccountCode,
        entry.relieveAccountCode, entry.entryId, entry.invoiceId,
      ],
    ));
  }

  async removeEntry(tenant: string, id: string): Promise<void> {
    await this.tx(tenant, (db) =>
      db.query("DELETE FROM work_order_entry WHERE tenant_id=$1 AND id=$2", [tenant, id]));
  }
}

// --- the flow ----------------------------------------------------------------

export interface WorkOrderContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

function requireDate(raw: unknown, label: string, required = true): string {
  const date = String(raw ?? "").trim();
  if (!date) {
    if (required) throw new WorkOrderError(`${label} is required`);
    return "";
  }
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new WorkOrderError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

function milliOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^\d+$/.test(value)) {
    throw new WorkOrderError(`${label} must be a whole number of thousandths`);
  }
  return BigInt(value);
}

function minorOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^\d+$/.test(value)) {
    throw new WorkOrderError(`${label} must be a whole number of minor units`);
  }
  return BigInt(value);
}

export interface WorkOrderInput {
  readonly id?: string;
  readonly job_id?: string;
  readonly sales_order_id?: string;
  readonly title?: string;
  readonly description?: string;
  readonly status?: string;
  readonly scheduled_date?: string;
  readonly completed_date?: string;
  readonly assignee_id?: string;
  readonly assignee?: string;
  readonly memo?: string;
}

export async function saveWorkOrder(
  ctx: WorkOrderContext, input: WorkOrderInput,
): Promise<WorkOrderRecord> {
  const jobId = String(input.job_id ?? "").trim();
  if (!jobId) throw new WorkOrderError("a work order belongs to a job");
  const job = await ctx.backend.jobs().getJob(String(ctx.tenant), jobId);
  if (!job) throw new WorkOrderError(`unknown job ${jobId}`);

  const title = String(input.title ?? "").trim();
  if (!title) throw new WorkOrderError("a work order needs a title");

  const salesOrderId = String(input.sales_order_id ?? "").trim();
  if (salesOrderId) {
    const order = await ctx.backend.salesOrders().get(String(ctx.tenant), salesOrderId);
    if (!order) throw new WorkOrderError(`unknown sales order ${salesOrderId}`);
    if (order.jobId && order.jobId !== jobId) {
      throw new WorkOrderError(
        `sales order ${salesOrderId} belongs to job ${order.jobId}, not ${jobId}`,
      );
    }
  }

  const status = String(input.status ?? "SCHEDULED").trim().toUpperCase() as WorkOrderStatus;
  if (!STATUSES.includes(status)) {
    throw new WorkOrderError(`status must be one of ${STATUSES.join(", ")}`);
  }

  const assigneeId = String(input.assignee_id ?? "").trim();
  let assignee = String(input.assignee ?? "").trim();
  if (assigneeId) {
    const employees = await ctx.backend.payroll().listEmployees(String(ctx.tenant));
    const employee = employees.find((e) => e.id === assigneeId);
    if (!employee) throw new WorkOrderError(`unknown employee ${assigneeId}`);
    assignee = assignee || employee.name;
  }

  const existing = input.id
    ? await ctx.backend.workOrders().get(String(ctx.tenant), String(input.id).trim())
    : undefined;
  const id = String(input.id ?? "").trim()
    || `WO-${(await ctx.backend.workOrders().list(String(ctx.tenant))).length + 1}`;

  const record: WorkOrderRecord = {
    id,
    jobId,
    salesOrderId,
    title,
    description: String(input.description ?? "").trim(),
    status,
    scheduledDate: requireDate(input.scheduled_date, "scheduled date", false),
    completedDate: requireDate(input.completed_date, "completed date", false)
      || (status === "COMPLETE" ? existing?.completedDate ?? "" : ""),
    assigneeId,
    assignee,
    memo: String(input.memo ?? "").trim(),
  };
  await ctx.backend.workOrders().save(String(ctx.tenant), record);
  return record;
}

/** Mark a work order complete on a date. Its costs are unaffected. */
export async function completeWorkOrder(
  ctx: WorkOrderContext, id: string, date: unknown,
): Promise<WorkOrderRecord> {
  const store = ctx.backend.workOrders();
  const order = await store.get(String(ctx.tenant), id);
  if (!order) throw new WorkOrderError(`unknown work order ${id}`);
  if (order.status === "CANCELLED") {
    throw new WorkOrderError(`work order ${id} was cancelled`);
  }
  const updated: WorkOrderRecord = {
    ...order,
    status: "COMPLETE",
    completedDate: requireDate(date, "completion date"),
  };
  await store.save(String(ctx.tenant), updated);
  return updated;
}

export interface EntryInput {
  readonly id?: string;
  readonly kind?: string;
  readonly date?: string;
  readonly description?: string;
  readonly cost_code?: string;
  readonly employee_id?: string;
  readonly quantity_milli?: string | number;
  readonly unit_cost_minor?: string | number;
  readonly unit_bill_minor?: string | number;
  readonly billable?: boolean;
  readonly cost_account_code?: string;
  /** Where the cost is taken from. Defaults by kind; "" means don't post. */
  readonly relieve_account_code?: string;
  /** Post the allocation immediately. Default true when an account is known. */
  readonly post?: boolean;
}

/** The account a kind of cost is relieved from, if the chart has one. */
async function defaultRelieveCode(
  ctx: WorkOrderContext, kind: EntryKind,
): Promise<string> {
  const chart = await ctx.backend.chart(ctx.tenant);
  if (kind === "LABOR") return chart.getByCode("6200") ? "6200" : "";
  if (kind === "MATERIAL") return chart.getByCode("1300") ? "1300" : "";
  return "";
}

function provenanceFor(entryId: string, date: string, at: string): Provenance {
  return {
    sourceSystem: "work-order",
    sourceObject: entryId,
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/work-order-1",
    mappingVersion: "ledger-service/work-order-1",
  };
}

export interface AddEntryResult {
  readonly entry: WorkOrderEntryRecord;
  /** Why nothing posted, when nothing posted. */
  readonly unposted_reason: string;
}

/**
 * Record time, material or another cost against a work order — and, when there
 * is somewhere honest to take it from, post the allocation that moves it onto
 * the job.
 */
export async function addEntry(
  ctx: WorkOrderContext, workOrderId: string, input: EntryInput,
): Promise<AddEntryResult> {
  const store = ctx.backend.workOrders();
  const order = await store.get(String(ctx.tenant), workOrderId);
  if (!order) throw new WorkOrderError(`unknown work order ${workOrderId}`);
  if (order.status === "CANCELLED") {
    throw new WorkOrderError(`work order ${workOrderId} was cancelled`);
  }

  const kind = String(input.kind ?? "LABOR").trim().toUpperCase() as EntryKind;
  if (!KINDS.includes(kind)) {
    throw new WorkOrderError(`kind must be one of ${KINDS.join(", ")}`);
  }
  const date = requireDate(input.date, "date");
  const quantityMilli = milliOf(input.quantity_milli, "quantity");
  if (quantityMilli === 0n) throw new WorkOrderError("quantity must be more than zero");

  const employeeId = String(input.employee_id ?? "").trim();
  let employee;
  if (employeeId) {
    const employees = await ctx.backend.payroll().listEmployees(String(ctx.tenant));
    employee = employees.find((e) => e.id === employeeId);
    if (!employee) throw new WorkOrderError(`unknown employee ${employeeId}`);
  }

  // Rates fall back to the employee's, which is the whole reason they are on
  // the employee: nobody types a burdened rate twice a day.
  const unitCost = input.unit_cost_minor === undefined || String(input.unit_cost_minor).trim() === ""
    ? BigInt(employee?.costRateMinor ?? "0")
    : minorOf(input.unit_cost_minor, "unit cost");
  const unitBill = input.unit_bill_minor === undefined || String(input.unit_bill_minor).trim() === ""
    ? BigInt(employee?.billRateMinor ?? "0")
    : minorOf(input.unit_bill_minor, "bill rate");

  const costCodes = await ctx.backend.jobs().listCostCodes(String(ctx.tenant));
  const costCode = String(input.cost_code ?? "").trim().toUpperCase();
  const code = costCodes.find((c) => c.code === costCode);
  if (costCode && !code) throw new WorkOrderError(`unknown cost code ${costCode}`);

  const chart = await ctx.backend.chart(ctx.tenant);
  const costAccountCode = String(input.cost_account_code ?? "").trim()
    || code?.accountCode
    || (kind === "LABOR" && chart.getByCode("5500") ? "5500" : "")
    || (chart.getByCode("5000") ? "5000" : "");
  const costAccount = chart.getByCode(costAccountCode);
  if (!costAccount) throw new WorkOrderError(`unknown account code ${costAccountCode || "(none)"}`);
  if (costAccount.type !== "EXPENSE") {
    throw new WorkOrderError(`${costAccountCode} is not a cost account`);
  }

  const relieveAccountCode = input.relieve_account_code === undefined
    ? await defaultRelieveCode(ctx, kind)
    : String(input.relieve_account_code).trim();
  if (relieveAccountCode && !chart.getByCode(relieveAccountCode)) {
    throw new WorkOrderError(`unknown account code ${relieveAccountCode}`);
  }

  const id = String(input.id ?? "").trim()
    || `${workOrderId}-e${(await store.listEntries(String(ctx.tenant), workOrderId)).length + 1}`;
  const existing = await store.getEntry(String(ctx.tenant), id);
  if (existing?.invoiceId) {
    throw new WorkOrderError(`entry ${id} has been invoiced — reverse the invoice to change it`);
  }

  const extendedCost = mulDiv(quantityMilli, unitCost, MILLI);
  let entryId = existing?.entryId ?? "";
  let unpostedReason = "";

  const wantsPost = input.post !== false;
  if (wantsPost && extendedCost > 0n && relieveAccountCode && !entryId) {
    const command: PostCommand = {
      tenantId: ctx.tenant,
      idempotencyKey: asIdempotencyKey(`wo-entry:${id}`),
      periodKey: asPeriodKey(date.slice(0, 7)),
      currency: ctx.currency,
      entryDate: date,
      memo: String(input.description ?? "").trim() || `${order.title} — ${kind.toLowerCase()}`,
      provenance: provenanceFor(id, date, ctx.now()),
      lines: [
        {
          accountId: costAccount.id,
          side: "DEBIT",
          amount: Money.fromMinorUnits(extendedCost, ctx.currency),
          memo: order.title,
          dimensions: {
            job: order.jobId,
            ...(costCode ? { cost_code: costCode } : {}),
          },
        },
        {
          accountId: chart.getByCode(relieveAccountCode)!.id,
          side: "CREDIT",
          amount: Money.fromMinorUnits(extendedCost, ctx.currency),
          memo: `Allocated to ${order.jobId}`,
        },
      ] satisfies JournalLineInput[],
    };
    await validateDimensions(
      { backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency }, command,
    );
    const engine = new PostingEngine(
      chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
    );
    const posted = await engine.post(command, { postedAt: ctx.now() });
    entryId = String(posted.id);
  } else if (!relieveAccountCode) {
    unpostedReason = extendedCost === 0n
      ? "no cost to allocate"
      : `nothing to take the cost from — say which account it comes from, or enter it as a bill coded to ${order.jobId}`;
  } else if (extendedCost === 0n) {
    unpostedReason = "no cost to allocate";
  }

  const entry: WorkOrderEntryRecord = {
    id,
    workOrderId,
    kind,
    date,
    description: String(input.description ?? "").trim(),
    costCode,
    employeeId,
    quantityMilli: quantityMilli.toString(),
    unitCostMinor: unitCost.toString(),
    unitBillMinor: unitBill.toString(),
    billable: input.billable !== false,
    costAccountCode,
    relieveAccountCode,
    entryId,
    invoiceId: existing?.invoiceId ?? "",
  };
  await store.saveEntry(String(ctx.tenant), entry);
  return { entry, unposted_reason: unpostedReason };
}

export function entryJson(e: WorkOrderEntryRecord): Record<string, unknown> {
  const quantity = BigInt(e.quantityMilli);
  return {
    id: e.id,
    work_order_id: e.workOrderId,
    kind: e.kind,
    date: e.date,
    description: e.description,
    cost_code: e.costCode,
    employee_id: e.employeeId,
    quantity_milli: e.quantityMilli,
    unit_cost_minor: e.unitCostMinor,
    unit_bill_minor: e.unitBillMinor,
    extended_cost_minor: mulDiv(quantity, BigInt(e.unitCostMinor), MILLI).toString(),
    extended_bill_minor: mulDiv(quantity, BigInt(e.unitBillMinor), MILLI).toString(),
    billable: e.billable,
    cost_account_code: e.costAccountCode,
    relieve_account_code: e.relieveAccountCode,
    entry_id: e.entryId,
    invoice_id: e.invoiceId,
    posted: e.entryId !== "",
  };
}

export function workOrderJson(
  o: WorkOrderRecord, entries: readonly WorkOrderEntryRecord[] = [],
): Record<string, unknown> {
  let cost = 0n;
  let billable = 0n;
  let unbilled = 0n;
  let hoursMilli = 0n;
  for (const e of entries) {
    const quantity = BigInt(e.quantityMilli);
    cost += mulDiv(quantity, BigInt(e.unitCostMinor), MILLI);
    const bill = mulDiv(quantity, BigInt(e.unitBillMinor), MILLI);
    if (e.billable) {
      billable += bill;
      if (!e.invoiceId) unbilled += bill;
    }
    if (e.kind === "LABOR") hoursMilli += quantity;
  }
  return {
    id: o.id,
    job_id: o.jobId,
    sales_order_id: o.salesOrderId,
    title: o.title,
    description: o.description,
    status: o.status,
    scheduled_date: o.scheduledDate,
    completed_date: o.completedDate,
    assignee_id: o.assigneeId,
    assignee: o.assignee,
    memo: o.memo,
    entries: entries.map(entryJson),
    totals: {
      hours_milli: hoursMilli.toString(),
      cost_minor: cost.toString(),
      billable_minor: billable.toString(),
      unbilled_minor: unbilled.toString(),
      margin_minor: (billable - cost).toString(),
    },
  };
}

/**
 * What a job's work orders have cost and what they are worth — the roll-up that
 * makes a work order more than a to-do.
 */
export async function workOrdersForJob(
  ctx: WorkOrderContext, jobId: string,
): Promise<Record<string, unknown>> {
  const store = ctx.backend.workOrders();
  const orders = (await store.list(String(ctx.tenant))).filter((o) => o.jobId === jobId);
  const entries = await store.listEntries(String(ctx.tenant));
  const rows = orders.map((o) =>
    workOrderJson(o, entries.filter((e) => e.workOrderId === o.id)));

  let cost = 0n;
  let unbilled = 0n;
  let hours = 0n;
  for (const row of rows) {
    const totals = row["totals"] as Record<string, string>;
    cost += BigInt(totals["cost_minor"]!);
    unbilled += BigInt(totals["unbilled_minor"]!);
    hours += BigInt(totals["hours_milli"]!);
  }
  return {
    contract: "job-work-orders/1",
    currency: ctx.currency.code,
    job_id: jobId,
    work_orders: rows,
    totals: {
      hours_milli: hours.toString(),
      cost_minor: cost.toString(),
      unbilled_minor: unbilled.toString(),
      open: rows.filter((r) => r["status"] !== "COMPLETE" && r["status"] !== "CANCELLED").length,
    },
  };
}

export interface UnbilledEntry {
  readonly entry: WorkOrderEntryRecord;
  readonly order: WorkOrderRecord;
  readonly billMinor: bigint;
}

/**
 * Billable work that hasn't been billed — the raw material of a time-and-
 * materials invoice, and the number a business most often loses money on by
 * never quite getting round to.
 */
export async function unbilledWork(
  ctx: WorkOrderContext, jobId: string, through?: string,
): Promise<UnbilledEntry[]> {
  const store = ctx.backend.workOrders();
  const orders = new Map(
    (await store.list(String(ctx.tenant)))
      .filter((o) => o.jobId === jobId)
      .map((o) => [o.id, o]),
  );
  const out: UnbilledEntry[] = [];
  for (const entry of await store.listEntries(String(ctx.tenant))) {
    const order = orders.get(entry.workOrderId);
    if (!order) continue;
    if (!entry.billable || entry.invoiceId) continue;
    if (through && entry.date > through) continue;
    const billMinor = mulDiv(BigInt(entry.quantityMilli), BigInt(entry.unitBillMinor), MILLI);
    if (billMinor === 0n) continue;
    out.push({ entry, order, billMinor });
  }
  return out.sort((a, b) => a.entry.date.localeCompare(b.entry.date));
}
