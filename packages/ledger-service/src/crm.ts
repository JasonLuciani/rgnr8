import { type Currency, type TenantId } from "@rgnr8/ledger-kernel";
import type { Pool, Queryable } from "@rgnr8/ledger-postgres";
import type { LedgerBackend } from "./backend.js";
import { mulDiv } from "./estimates.js";
import { slug } from "./jobs.js";

/**
 * The pipeline — leads, opportunities, and the seam an external CRM plugs into.
 *
 * A contractor with no CRM still has a pipeline; it is in a notebook, and the
 * follow-up that never happened is the most expensive thing in the business. So
 * the pipeline lives here: a lead becomes a customer, an opportunity becomes an
 * estimate, an accepted estimate becomes a job, and the same objects are
 * readable and writable over the API so a business that already runs HubSpot or
 * ServiceTitan can drive them from there instead.
 *
 * ## Why weighted value is reported and never posted
 *
 * A pipeline is worth `value × probability`, and that number belongs on a
 * screen where somebody decides whether to hire. It does not belong anywhere
 * near the ledger. Nothing in this module posts a journal entry — winning an
 * opportunity creates an estimate, and an accepted estimate creates a job, and
 * only when that job is billed does any money exist. A pipeline that leaks into
 * the accounts is how a business books revenue for work it never won.
 *
 * ## Integration by event feed, not webhooks
 *
 * Every change appends to an event log that an external system reads with a
 * cursor. Push delivery is deliberately not built: a webhook without a durable
 * retry queue drops events silently the first time the receiver is down, and a
 * silent drop in a CRM integration is a customer nobody called back. A feed the
 * consumer polls cannot lose anything — it can only be behind, and being behind
 * is visible.
 */

export class CrmError extends Error {}

export type LeadStatus = "NEW" | "WORKING" | "QUALIFIED" | "DISQUALIFIED" | "CONVERTED";
export type Stage = "NEW" | "QUALIFIED" | "PROPOSAL" | "NEGOTIATION" | "WON" | "LOST";

const LEAD_STATUSES: readonly LeadStatus[] = [
  "NEW", "WORKING", "QUALIFIED", "DISQUALIFIED", "CONVERTED",
];
export const STAGES: readonly Stage[] = [
  "NEW", "QUALIFIED", "PROPOSAL", "NEGOTIATION", "WON", "LOST",
];
/** A starting guess per stage, in parts per million. Editable per opportunity. */
const DEFAULT_PROBABILITY: Readonly<Record<Stage, number>> = {
  NEW: 100_000,
  QUALIFIED: 250_000,
  PROPOSAL: 500_000,
  NEGOTIATION: 750_000,
  WON: 1_000_000,
  LOST: 0,
};

export interface LeadRecord {
  readonly id: string;
  readonly name: string;
  readonly company: string;
  readonly email: string;
  readonly phone: string;
  readonly source: string;
  readonly status: LeadStatus;
  readonly owner: string;
  readonly createdDate: string;
  readonly notes: string;
  readonly customerId: string;
}

export interface OpportunityRecord {
  readonly id: string;
  readonly leadId: string;
  readonly customerId: string;
  readonly name: string;
  readonly stage: Stage;
  readonly valueMinor: string;
  readonly probabilityPpm: number;
  readonly expectedCloseDate: string;
  readonly owner: string;
  readonly source: string;
  readonly estimateId: string;
  readonly jobId: string;
  readonly lostReason: string;
  readonly createdDate: string;
}

export interface CrmEventRecord {
  readonly sequence: number;
  readonly at: string;
  readonly kind: string;
  readonly subject: string;
  readonly payload: string;
}

export interface CrmStore {
  migrate(): Promise<void>;
  listLeads(tenant: string): Promise<LeadRecord[]>;
  getLead(tenant: string, id: string): Promise<LeadRecord | undefined>;
  saveLead(tenant: string, lead: LeadRecord): Promise<void>;
  listOpportunities(tenant: string): Promise<OpportunityRecord[]>;
  getOpportunity(tenant: string, id: string): Promise<OpportunityRecord | undefined>;
  saveOpportunity(tenant: string, opportunity: OpportunityRecord): Promise<void>;
  appendEvent(tenant: string, event: Omit<CrmEventRecord, "sequence">): Promise<CrmEventRecord>;
  listEvents(tenant: string, since: number, limit: number): Promise<CrmEventRecord[]>;
}

// --- in-memory ---------------------------------------------------------------

export class InMemoryCrmStore implements CrmStore {
  private readonly leads = new Map<string, LeadRecord>();
  private readonly opportunities = new Map<string, OpportunityRecord>();
  private readonly events = new Map<string, CrmEventRecord[]>();

  migrate(): Promise<void> {
    return Promise.resolve();
  }

  listLeads(tenant: string): Promise<LeadRecord[]> {
    const out: LeadRecord[] = [];
    for (const [k, v] of this.leads) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.id.localeCompare(b.id)));
  }

  getLead(tenant: string, id: string): Promise<LeadRecord | undefined> {
    return Promise.resolve(this.leads.get(`${tenant}::${id}`));
  }

  saveLead(tenant: string, lead: LeadRecord): Promise<void> {
    this.leads.set(`${tenant}::${lead.id}`, lead);
    return Promise.resolve();
  }

  listOpportunities(tenant: string): Promise<OpportunityRecord[]> {
    const out: OpportunityRecord[] = [];
    for (const [k, v] of this.opportunities) if (k.startsWith(`${tenant}::`)) out.push(v);
    return Promise.resolve(out.sort((a, b) => a.id.localeCompare(b.id)));
  }

  getOpportunity(tenant: string, id: string): Promise<OpportunityRecord | undefined> {
    return Promise.resolve(this.opportunities.get(`${tenant}::${id}`));
  }

  saveOpportunity(tenant: string, opportunity: OpportunityRecord): Promise<void> {
    this.opportunities.set(`${tenant}::${opportunity.id}`, opportunity);
    return Promise.resolve();
  }

  appendEvent(
    tenant: string, event: Omit<CrmEventRecord, "sequence">,
  ): Promise<CrmEventRecord> {
    const log = this.events.get(tenant) ?? [];
    const record: CrmEventRecord = { ...event, sequence: log.length + 1 };
    log.push(record);
    this.events.set(tenant, log);
    return Promise.resolve(record);
  }

  listEvents(tenant: string, since: number, limit: number): Promise<CrmEventRecord[]> {
    const log = this.events.get(tenant) ?? [];
    return Promise.resolve(log.filter((e) => e.sequence > since).slice(0, limit));
  }
}

// --- PostgreSQL --------------------------------------------------------------

export const CRM_DDL = `
CREATE TABLE IF NOT EXISTS crm_lead (
  tenant_id    text NOT NULL,
  id           text NOT NULL,
  name         text NOT NULL,
  company      text NOT NULL DEFAULT '',
  email        text NOT NULL DEFAULT '',
  phone        text NOT NULL DEFAULT '',
  source       text NOT NULL DEFAULT '',
  status       text NOT NULL DEFAULT 'NEW',
  owner        text NOT NULL DEFAULT '',
  created_date text NOT NULL DEFAULT '',
  notes        text NOT NULL DEFAULT '',
  customer_id  text NOT NULL DEFAULT '',
  CONSTRAINT crm_lead_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS crm_opportunity (
  tenant_id           text NOT NULL,
  id                  text NOT NULL,
  lead_id             text NOT NULL DEFAULT '',
  customer_id         text NOT NULL DEFAULT '',
  name                text NOT NULL,
  stage               text NOT NULL DEFAULT 'NEW',
  value_minor         text NOT NULL DEFAULT '0',
  probability_ppm     integer NOT NULL DEFAULT 0,
  expected_close_date text NOT NULL DEFAULT '',
  owner               text NOT NULL DEFAULT '',
  source              text NOT NULL DEFAULT '',
  estimate_id         text NOT NULL DEFAULT '',
  job_id              text NOT NULL DEFAULT '',
  lost_reason         text NOT NULL DEFAULT '',
  created_date        text NOT NULL DEFAULT '',
  CONSTRAINT crm_opportunity_pk PRIMARY KEY (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS crm_event (
  tenant_id text NOT NULL,
  sequence  bigint NOT NULL,
  at        text NOT NULL,
  kind      text NOT NULL,
  subject   text NOT NULL DEFAULT '',
  payload   text NOT NULL DEFAULT '{}',
  CONSTRAINT crm_event_pk PRIMARY KEY (tenant_id, sequence)
);
`;

export class PgCrmStore implements CrmStore {
  constructor(private readonly pool: Pool) {}

  async migrate(): Promise<void> {
    await this.pool.query(CRM_DDL);
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

  private leadFrom(r: Record<string, unknown>): LeadRecord {
    return {
      id: String(r["id"]),
      name: String(r["name"]),
      company: String(r["company"] ?? ""),
      email: String(r["email"] ?? ""),
      phone: String(r["phone"] ?? ""),
      source: String(r["source"] ?? ""),
      status: String(r["status"]) as LeadStatus,
      owner: String(r["owner"] ?? ""),
      createdDate: String(r["created_date"] ?? ""),
      notes: String(r["notes"] ?? ""),
      customerId: String(r["customer_id"] ?? ""),
    };
  }

  private opportunityFrom(r: Record<string, unknown>): OpportunityRecord {
    return {
      id: String(r["id"]),
      leadId: String(r["lead_id"] ?? ""),
      customerId: String(r["customer_id"] ?? ""),
      name: String(r["name"]),
      stage: String(r["stage"]) as Stage,
      valueMinor: String(r["value_minor"] ?? "0"),
      probabilityPpm: Number(r["probability_ppm"] ?? 0),
      expectedCloseDate: String(r["expected_close_date"] ?? ""),
      owner: String(r["owner"] ?? ""),
      source: String(r["source"] ?? ""),
      estimateId: String(r["estimate_id"] ?? ""),
      jobId: String(r["job_id"] ?? ""),
      lostReason: String(r["lost_reason"] ?? ""),
      createdDate: String(r["created_date"] ?? ""),
    };
  }

  async listLeads(tenant: string): Promise<LeadRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM crm_lead WHERE tenant_id=$1 ORDER BY id", [tenant],
      );
      return res.rows.map((r) => this.leadFrom(r));
    });
  }

  async getLead(tenant: string, id: string): Promise<LeadRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM crm_lead WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      return row ? this.leadFrom(row) : undefined;
    });
  }

  async saveLead(tenant: string, lead: LeadRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO crm_lead (tenant_id, id, name, company, email, phone, source,
         status, owner, created_date, notes, customer_id)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         name=EXCLUDED.name, company=EXCLUDED.company, email=EXCLUDED.email,
         phone=EXCLUDED.phone, source=EXCLUDED.source, status=EXCLUDED.status,
         owner=EXCLUDED.owner, created_date=EXCLUDED.created_date,
         notes=EXCLUDED.notes, customer_id=EXCLUDED.customer_id`,
      [
        tenant, lead.id, lead.name, lead.company, lead.email, lead.phone, lead.source,
        lead.status, lead.owner, lead.createdDate, lead.notes, lead.customerId,
      ],
    ));
  }

  async listOpportunities(tenant: string): Promise<OpportunityRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM crm_opportunity WHERE tenant_id=$1 ORDER BY id", [tenant],
      );
      return res.rows.map((r) => this.opportunityFrom(r));
    });
  }

  async getOpportunity(tenant: string, id: string): Promise<OpportunityRecord | undefined> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        "SELECT * FROM crm_opportunity WHERE tenant_id=$1 AND id=$2", [tenant, id],
      );
      const row = res.rows[0];
      return row ? this.opportunityFrom(row) : undefined;
    });
  }

  async saveOpportunity(tenant: string, o: OpportunityRecord): Promise<void> {
    await this.tx(tenant, (db) => db.query(
      `INSERT INTO crm_opportunity (tenant_id, id, lead_id, customer_id, name, stage,
         value_minor, probability_ppm, expected_close_date, owner, source,
         estimate_id, job_id, lost_reason, created_date)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
       ON CONFLICT (tenant_id, id) DO UPDATE SET
         lead_id=EXCLUDED.lead_id, customer_id=EXCLUDED.customer_id, name=EXCLUDED.name,
         stage=EXCLUDED.stage, value_minor=EXCLUDED.value_minor,
         probability_ppm=EXCLUDED.probability_ppm,
         expected_close_date=EXCLUDED.expected_close_date, owner=EXCLUDED.owner,
         source=EXCLUDED.source, estimate_id=EXCLUDED.estimate_id, job_id=EXCLUDED.job_id,
         lost_reason=EXCLUDED.lost_reason, created_date=EXCLUDED.created_date`,
      [
        tenant, o.id, o.leadId, o.customerId, o.name, o.stage, o.valueMinor,
        o.probabilityPpm, o.expectedCloseDate, o.owner, o.source, o.estimateId,
        o.jobId, o.lostReason, o.createdDate,
      ],
    ));
  }

  async appendEvent(
    tenant: string, event: Omit<CrmEventRecord, "sequence">,
  ): Promise<CrmEventRecord> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `INSERT INTO crm_event (tenant_id, sequence, at, kind, subject, payload)
         SELECT $1, COALESCE(MAX(sequence), 0) + 1, $2, $3, $4, $5
         FROM crm_event WHERE tenant_id=$1
         RETURNING sequence`,
        [tenant, event.at, event.kind, event.subject, event.payload],
      );
      return { ...event, sequence: Number(res.rows[0]?.["sequence"] ?? 0) };
    });
  }

  async listEvents(tenant: string, since: number, limit: number): Promise<CrmEventRecord[]> {
    return this.tx(tenant, async (db) => {
      const res = await db.query(
        `SELECT sequence, at, kind, subject, payload FROM crm_event
         WHERE tenant_id=$1 AND sequence > $2 ORDER BY sequence LIMIT $3`,
        [tenant, since, limit],
      );
      return res.rows.map((r) => ({
        sequence: Number(r["sequence"]),
        at: String(r["at"]),
        kind: String(r["kind"]),
        subject: String(r["subject"] ?? ""),
        payload: String(r["payload"] ?? "{}"),
      }));
    });
  }
}

// --- the flow ----------------------------------------------------------------

export interface CrmContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

function optionalDate(raw: unknown, label: string): string {
  const date = String(raw ?? "").trim();
  if (date && !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    throw new CrmError(`${label} must be YYYY-MM-DD`);
  }
  return date;
}

function minorOf(raw: unknown, label: string): bigint {
  const value = typeof raw === "number" ? String(raw) : String(raw ?? "").trim();
  if (!value) return 0n;
  if (!/^\d+$/.test(value)) {
    throw new CrmError(`${label} must be a whole number of minor units`);
  }
  return BigInt(value);
}

/** Append to the feed an external system reads. Never throws the caller off. */
export async function recordEvent(
  ctx: CrmContext, kind: string, subject: string, payload: Record<string, unknown>,
): Promise<void> {
  await ctx.backend.crm().appendEvent(String(ctx.tenant), {
    at: ctx.now(),
    kind,
    subject,
    payload: JSON.stringify(payload),
  });
}

export interface LeadInput {
  readonly id?: string;
  readonly name?: string;
  readonly company?: string;
  readonly email?: string;
  readonly phone?: string;
  readonly source?: string;
  readonly status?: string;
  readonly owner?: string;
  readonly created_date?: string;
  readonly notes?: string;
}

export async function saveLead(ctx: CrmContext, input: LeadInput): Promise<LeadRecord> {
  const name = String(input.name ?? "").trim();
  if (!name) throw new CrmError("a lead needs a name");
  const status = String(input.status ?? "NEW").trim().toUpperCase() as LeadStatus;
  if (!LEAD_STATUSES.includes(status)) {
    throw new CrmError(`status must be one of ${LEAD_STATUSES.join(", ")}`);
  }
  const id = String(input.id ?? "").trim() || slug(`${name}-${input.company ?? ""}`) || slug(name);
  const existing = await ctx.backend.crm().getLead(String(ctx.tenant), id);
  if (existing?.status === "CONVERTED" && status !== "CONVERTED") {
    throw new CrmError(`lead ${id} has already been converted to customer ${existing.customerId}`);
  }
  const lead: LeadRecord = {
    id,
    name,
    company: String(input.company ?? "").trim(),
    email: String(input.email ?? "").trim(),
    phone: String(input.phone ?? "").trim(),
    source: String(input.source ?? "").trim(),
    status,
    owner: String(input.owner ?? "").trim(),
    createdDate: optionalDate(input.created_date, "created date") || existing?.createdDate || "",
    notes: String(input.notes ?? "").trim(),
    customerId: existing?.customerId ?? "",
  };
  await ctx.backend.crm().saveLead(String(ctx.tenant), lead);
  await recordEvent(ctx, existing ? "lead.updated" : "lead.created", lead.id, {
    name: lead.name, status: lead.status, source: lead.source,
  });
  return lead;
}

export interface ConvertInput {
  readonly customer_id?: string;
  readonly terms_days?: number;
  /** Open an opportunity at the same time, which is usually the point. */
  readonly opportunity?: OpportunityInput;
}

export interface ConvertResult {
  readonly lead: LeadRecord;
  readonly customerId: string;
  readonly opportunity?: OpportunityRecord;
}

/**
 * Turn a lead into a customer. The customer is a real party in the ledger, so
 * everything downstream — estimates, jobs, invoices, aging — works on it with
 * no special case for "came from a lead".
 */
export async function convertLead(
  ctx: CrmContext, id: string, input: ConvertInput,
): Promise<ConvertResult> {
  const store = ctx.backend.crm();
  const lead = await store.getLead(String(ctx.tenant), id);
  if (!lead) throw new CrmError(`unknown lead ${id}`);
  if (lead.status === "CONVERTED") {
    throw new CrmError(`lead ${id} is already customer ${lead.customerId}`);
  }
  if (lead.status === "DISQUALIFIED") {
    throw new CrmError(`lead ${id} was disqualified — reopen it before converting it`);
  }

  const customerId = String(input.customer_id ?? "").trim()
    || slug(lead.company || lead.name);
  if (!customerId) throw new CrmError("the customer needs an id");
  const existing = await ctx.backend.documents()
    .getParty(String(ctx.tenant), "customer", customerId);
  if (!existing) {
    await ctx.backend.documents().upsertParty(String(ctx.tenant), "customer", {
      id: customerId,
      name: lead.company || lead.name,
      ...(lead.email ? { email: lead.email } : {}),
      ...(input.terms_days !== undefined ? { termsDays: Number(input.terms_days) } : {}),
    });
  }

  const converted: LeadRecord = { ...lead, status: "CONVERTED", customerId };
  await store.saveLead(String(ctx.tenant), converted);
  await recordEvent(ctx, "lead.converted", lead.id, { customer_id: customerId });

  let opportunity: OpportunityRecord | undefined;
  if (input.opportunity) {
    opportunity = await saveOpportunity(ctx, {
      ...input.opportunity,
      lead_id: lead.id,
      customer_id: customerId,
      ...(input.opportunity.name ? {} : { name: lead.company || lead.name }),
    });
  }
  return { lead: converted, customerId, ...(opportunity ? { opportunity } : {}) };
}

export interface OpportunityInput {
  readonly id?: string;
  readonly lead_id?: string;
  readonly customer_id?: string;
  readonly name?: string;
  readonly stage?: string;
  readonly value_minor?: string | number;
  readonly probability_ppm?: number;
  readonly expected_close_date?: string;
  readonly owner?: string;
  readonly source?: string;
  readonly created_date?: string;
}

export async function saveOpportunity(
  ctx: CrmContext, input: OpportunityInput,
): Promise<OpportunityRecord> {
  const store = ctx.backend.crm();
  const name = String(input.name ?? "").trim();
  if (!name) throw new CrmError("an opportunity needs a name");

  const stage = String(input.stage ?? "NEW").trim().toUpperCase() as Stage;
  if (!STAGES.includes(stage)) throw new CrmError(`stage must be one of ${STAGES.join(", ")}`);

  const customerId = String(input.customer_id ?? "").trim();
  if (customerId
    && !await ctx.backend.documents().getParty(String(ctx.tenant), "customer", customerId)) {
    throw new CrmError(`unknown customer ${customerId}`);
  }
  const leadId = String(input.lead_id ?? "").trim();
  if (leadId && !await store.getLead(String(ctx.tenant), leadId)) {
    throw new CrmError(`unknown lead ${leadId}`);
  }
  if (!customerId && !leadId) {
    throw new CrmError("an opportunity belongs to a lead or a customer");
  }

  const id = String(input.id ?? "").trim() || slug(name);
  const existing = await store.getOpportunity(String(ctx.tenant), id);
  if (existing && (existing.stage === "WON" || existing.stage === "LOST")
      && stage !== existing.stage) {
    throw new CrmError(
      `opportunity ${id} is ${existing.stage} — reopen it explicitly rather than editing it back`,
    );
  }

  const probability = input.probability_ppm === undefined
    ? (existing && existing.stage === stage ? existing.probabilityPpm : DEFAULT_PROBABILITY[stage])
    : Number(input.probability_ppm);
  if (!Number.isInteger(probability) || probability < 0 || probability > 1_000_000) {
    throw new CrmError("probability must be between 0 and 1,000,000 parts per million");
  }

  const record: OpportunityRecord = {
    id,
    leadId,
    customerId,
    name,
    stage,
    valueMinor: minorOf(input.value_minor, "value").toString(),
    probabilityPpm: probability,
    expectedCloseDate: optionalDate(input.expected_close_date, "expected close date"),
    owner: String(input.owner ?? "").trim(),
    source: String(input.source ?? "").trim(),
    estimateId: existing?.estimateId ?? "",
    jobId: existing?.jobId ?? "",
    lostReason: existing?.lostReason ?? "",
    createdDate: optionalDate(input.created_date, "created date") || existing?.createdDate || "",
  };
  await store.saveOpportunity(String(ctx.tenant), record);
  if (existing && existing.stage !== record.stage) {
    await recordEvent(ctx, "opportunity.stage_changed", record.id, {
      from: existing.stage, to: record.stage, value_minor: record.valueMinor,
    });
  } else {
    await recordEvent(ctx, existing ? "opportunity.updated" : "opportunity.created", record.id, {
      name: record.name, stage: record.stage, value_minor: record.valueMinor,
    });
  }
  return record;
}

/** Link an estimate to the opportunity it was raised for. */
export async function attachEstimate(
  ctx: CrmContext, id: string, estimateId: string,
): Promise<OpportunityRecord> {
  const store = ctx.backend.crm();
  const opportunity = await store.getOpportunity(String(ctx.tenant), id);
  if (!opportunity) throw new CrmError(`unknown opportunity ${id}`);
  if (!await ctx.backend.estimates().get(String(ctx.tenant), estimateId)) {
    throw new CrmError(`unknown estimate ${estimateId}`);
  }
  const updated: OpportunityRecord = {
    ...opportunity,
    estimateId,
    stage: opportunity.stage === "NEW" || opportunity.stage === "QUALIFIED"
      ? "PROPOSAL"
      : opportunity.stage,
    probabilityPpm: opportunity.stage === "NEW" || opportunity.stage === "QUALIFIED"
      ? DEFAULT_PROBABILITY.PROPOSAL
      : opportunity.probabilityPpm,
  };
  await store.saveOpportunity(String(ctx.tenant), updated);
  await recordEvent(ctx, "opportunity.estimate_attached", id, { estimate_id: estimateId });
  return updated;
}

export interface CloseInput {
  readonly job_id?: string;
  readonly reason?: string;
  readonly value_minor?: string | number;
}

/**
 * Win an opportunity.
 *
 * Winning records that the work was won; it does not book anything. The money
 * appears when the job is billed, which is the only moment it exists.
 */
export async function winOpportunity(
  ctx: CrmContext, id: string, input: CloseInput,
): Promise<OpportunityRecord> {
  const store = ctx.backend.crm();
  const opportunity = await store.getOpportunity(String(ctx.tenant), id);
  if (!opportunity) throw new CrmError(`unknown opportunity ${id}`);
  if (opportunity.stage === "WON") throw new CrmError(`opportunity ${id} is already won`);

  const jobId = String(input.job_id ?? "").trim();
  if (jobId && !await ctx.backend.jobs().getJob(String(ctx.tenant), jobId)) {
    throw new CrmError(`unknown job ${jobId}`);
  }
  const updated: OpportunityRecord = {
    ...opportunity,
    stage: "WON",
    probabilityPpm: 1_000_000,
    jobId: jobId || opportunity.jobId,
    ...(input.value_minor !== undefined
      ? { valueMinor: minorOf(input.value_minor, "value").toString() }
      : {}),
    lostReason: "",
  };
  await store.saveOpportunity(String(ctx.tenant), updated);
  await recordEvent(ctx, "opportunity.won", id, {
    value_minor: updated.valueMinor, job_id: updated.jobId,
  });
  return updated;
}

/**
 * Lose an opportunity, with a reason.
 *
 * The reason is required. "Lost" with no reason is a row nobody can learn
 * anything from, and the pattern in those reasons — price, timing, never called
 * back — is the most useful thing the pipeline produces.
 */
export async function loseOpportunity(
  ctx: CrmContext, id: string, input: CloseInput,
): Promise<OpportunityRecord> {
  const store = ctx.backend.crm();
  const opportunity = await store.getOpportunity(String(ctx.tenant), id);
  if (!opportunity) throw new CrmError(`unknown opportunity ${id}`);
  const reason = String(input.reason ?? "").trim();
  if (!reason) {
    throw new CrmError("say why it was lost — the pattern in those reasons is the point");
  }
  const updated: OpportunityRecord = {
    ...opportunity, stage: "LOST", probabilityPpm: 0, lostReason: reason,
  };
  await store.saveOpportunity(String(ctx.tenant), updated);
  await recordEvent(ctx, "opportunity.lost", id, {
    reason, value_minor: updated.valueMinor,
  });
  return updated;
}

export function leadJson(l: LeadRecord): Record<string, unknown> {
  return {
    id: l.id,
    name: l.name,
    company: l.company,
    email: l.email,
    phone: l.phone,
    source: l.source,
    status: l.status,
    owner: l.owner,
    created_date: l.createdDate,
    notes: l.notes,
    customer_id: l.customerId,
  };
}

export function opportunityJson(o: OpportunityRecord): Record<string, unknown> {
  return {
    id: o.id,
    lead_id: o.leadId,
    customer_id: o.customerId,
    name: o.name,
    stage: o.stage,
    value_minor: o.valueMinor,
    probability_ppm: o.probabilityPpm,
    weighted_value_minor: mulDiv(
      BigInt(o.valueMinor), BigInt(o.probabilityPpm), 1_000_000n,
    ).toString(),
    expected_close_date: o.expectedCloseDate,
    owner: o.owner,
    source: o.source,
    estimate_id: o.estimateId,
    job_id: o.jobId,
    lost_reason: o.lostReason,
    created_date: o.createdDate,
  };
}

/**
 * The pipeline by stage, with the weighted value beside the raw one.
 *
 * Both are shown because they answer different questions: the raw value is what
 * is on the table, the weighted value is what to plan around, and a business
 * that reads one as the other either hires too early or turns work away.
 */
export async function pipeline(
  ctx: CrmContext, filter: { readonly owner?: string } = {},
): Promise<Record<string, unknown>> {
  const opportunities = (await ctx.backend.crm().listOpportunities(String(ctx.tenant)))
    .filter((o) => !filter.owner || o.owner === filter.owner);

  const byStage = new Map<Stage, { count: number; value: bigint; weighted: bigint }>();
  for (const stage of STAGES) byStage.set(stage, { count: 0, value: 0n, weighted: 0n });
  let openValue = 0n;
  let openWeighted = 0n;
  let won = 0;
  let lost = 0;
  let wonValue = 0n;

  for (const o of opportunities) {
    const bucket = byStage.get(o.stage)!;
    const value = BigInt(o.valueMinor);
    const weighted = mulDiv(value, BigInt(o.probabilityPpm), 1_000_000n);
    bucket.count += 1;
    bucket.value += value;
    bucket.weighted += weighted;
    if (o.stage === "WON") {
      won += 1;
      wonValue += value;
    } else if (o.stage === "LOST") {
      lost += 1;
    } else {
      openValue += value;
      openWeighted += weighted;
    }
  }

  const closed = won + lost;
  const lostReasons = new Map<string, number>();
  for (const o of opportunities) {
    if (o.stage !== "LOST" || !o.lostReason) continue;
    lostReasons.set(o.lostReason, (lostReasons.get(o.lostReason) ?? 0) + 1);
  }

  return {
    contract: "sales-pipeline/1",
    currency: ctx.currency.code,
    stages: STAGES.map((stage) => ({
      stage,
      count: byStage.get(stage)!.count,
      value_minor: byStage.get(stage)!.value.toString(),
      weighted_value_minor: byStage.get(stage)!.weighted.toString(),
    })),
    opportunities: opportunities.map(opportunityJson),
    totals: {
      open_value_minor: openValue.toString(),
      open_weighted_minor: openWeighted.toString(),
      won_count: won,
      lost_count: lost,
      won_value_minor: wonValue.toString(),
      // In parts per million, like every other rate here — never a float.
      win_rate_ppm: closed > 0 ? Math.round((won / closed) * 1_000_000) : 0,
    },
    lost_reasons: [...lostReasons.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([reason, count]) => ({ reason, count })),
  };
}

export async function events(
  ctx: CrmContext, since: unknown, limit: unknown,
): Promise<Record<string, unknown>> {
  const from = Number(String(since ?? "0").trim() || "0");
  if (!Number.isInteger(from) || from < 0) throw new CrmError("since must be a whole number");
  const size = Math.min(Math.max(Number(String(limit ?? "100").trim() || "100"), 1), 1000);
  const rows = await ctx.backend.crm().listEvents(String(ctx.tenant), from, size);
  return {
    contract: "crm-events/1",
    events: rows.map((e) => ({
      sequence: e.sequence,
      at: e.at,
      kind: e.kind,
      subject: e.subject,
      payload: JSON.parse(e.payload) as unknown,
    })),
    cursor: rows.length > 0 ? rows[rows.length - 1]!.sequence : from,
  };
}
