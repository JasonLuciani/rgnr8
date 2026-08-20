import {
  asPeriodKey,
  asTenantId,
  type PeriodKey,
  type PeriodStore,
} from "@rgnr8/ledger-kernel";
import type { Migration, SqlExecutor } from "@rgnr8/migrations";
import { buildCloseChecklist, type CloseGateInputs, type CloseTask } from "./close.js";
import type { FinancialPackage } from "./financialPackage.js";
import type { FinancialPackageStore } from "./packageStore.js";

/**
 * The single authoritative close state machine.
 *
 * Before this, "closed" lived in three disconnected places — an in-memory board
 * flag on the web tier, the kernel's period lock, and the sealed financial
 * package — none of which was the source of truth and none of which the publish
 * path actually set together. This module makes ONE durable state the authority,
 * and every transition does the whole job atomically-in-intent:
 *
 *   OPEN ──publish──▶ PUBLISHED ──requestReopen──▶ REOPEN_REQUESTED
 *                          ▲                              │
 *                          └──────── approveReopen ───────┘  (→ REOPENED)
 *
 *  - **publish** runs the close gate (real recon/control results, not a flag),
 *    LOCKS the period durably (kernel `PeriodStore`), and BUILDS+PERSISTS the
 *    immutable `FinancialPackage` — all three, or none.
 *  - **separation of duties**: the publisher may not be the preparer; a reopen
 *    approver may not be the requester.
 *  - **reopen is a two-step, approved workflow**: a request records intent but
 *    changes nothing; only an approval by a *different* person unlocks the
 *    period. The published package is never deleted — it is preserved as evidence
 *    across a reopen, and every transition is appended to an immutable history.
 */

export type CloseStatus = "OPEN" | "PUBLISHED" | "REOPEN_REQUESTED" | "REOPENED";

export type CloseAction = "PUBLISH" | "REQUEST_REOPEN" | "APPROVE_REOPEN";

export interface CloseTransition {
  readonly action: CloseAction;
  readonly by: string;
  readonly at: string;
  readonly note?: string;
}

/** The durable, authoritative close state for one (tenant, period). */
export interface CloseState {
  readonly tenantId: string;
  readonly periodKey: string;
  readonly status: CloseStatus;
  /** The gate evidence captured at publish time (which controls passed). */
  readonly checklist: readonly CloseTask[];
  readonly publishedBy?: string;
  readonly publishedAt?: string;
  /** Fingerprint of the sealed package (its record stays in the package store). */
  readonly packageFingerprint?: string;
  readonly reopenRequestedBy?: string;
  readonly reopenReason?: string;
  /** Append-only audit of every transition. */
  readonly history: readonly CloseTransition[];
}

export class CloseStateError extends Error {
  override readonly name: string = "CloseStateError";
}
/** The close gate failed — some control did not pass. Carries the blockers. */
export class CloseGateError extends CloseStateError {
  override readonly name = "CloseGateError";
  constructor(readonly blockers: readonly string[]) {
    super(`close is blocked: ${blockers.join("; ")}`);
  }
}
/** A single person tried to perform two roles that must be separated. */
export class SeparationOfDutiesError extends CloseStateError {
  override readonly name = "SeparationOfDutiesError";
  constructor(message: string) {
    super(message);
  }
}

export interface CloseStateStore {
  get(tenantId: string, periodKey: string): Promise<CloseState | null>;
  put(state: CloseState): Promise<void>;
  list(tenantId: string): Promise<readonly CloseState[]>;
}

export class InMemoryCloseStateStore implements CloseStateStore {
  private readonly byTenant = new Map<string, Map<string, CloseState>>();

  get(tenantId: string, periodKey: string): Promise<CloseState | null> {
    return Promise.resolve(this.byTenant.get(tenantId)?.get(periodKey) ?? null);
  }

  put(state: CloseState): Promise<void> {
    const periods = this.byTenant.get(state.tenantId) ?? new Map<string, CloseState>();
    periods.set(state.periodKey, state);
    this.byTenant.set(state.tenantId, periods);
    return Promise.resolve();
  }

  list(tenantId: string): Promise<readonly CloseState[]> {
    return Promise.resolve(
      [...(this.byTenant.get(tenantId)?.values() ?? [])].sort((a, b) =>
        a.periodKey.localeCompare(b.periodKey),
      ),
    );
  }
}

/** Table for the durable SQL close-state store — register through @rgnr8/migrations. */
export const CLOSE_STATE_MIGRATIONS: readonly Migration[] = [
  {
    version: 1,
    name: "close_state",
    sql: `CREATE TABLE close_state (
      tenant_id  text NOT NULL,
      period_key text NOT NULL,
      status     text NOT NULL,
      state_json text NOT NULL,
      CONSTRAINT close_state_pk PRIMARY KEY (tenant_id, period_key)
    );`,
  },
];

/**
 * PostgreSQL close-state store. The authoritative close state must outlive the
 * process, so a period sealed before a deploy is still sealed after it. The full
 * state (checklist evidence + transition history) is stored as JSON, with the
 * status mirrored to a column for querying.
 */
export class SqlCloseStateStore implements CloseStateStore {
  constructor(private readonly db: SqlExecutor) {}

  async get(tenantId: string, periodKey: string): Promise<CloseState | null> {
    const res = await this.db.query(
      "SELECT state_json FROM close_state WHERE tenant_id = $1 AND period_key = $2",
      [tenantId, periodKey],
    );
    const row = res.rows[0];
    return row === undefined ? null : (JSON.parse(String(row["state_json"])) as CloseState);
  }

  async put(state: CloseState): Promise<void> {
    await this.db.query(
      `INSERT INTO close_state (tenant_id, period_key, status, state_json)
       VALUES ($1, $2, $3, $4)
       ON CONFLICT (tenant_id, period_key)
       DO UPDATE SET status = EXCLUDED.status, state_json = EXCLUDED.state_json`,
      [state.tenantId, state.periodKey, state.status, JSON.stringify(state)],
    );
  }

  async list(tenantId: string): Promise<readonly CloseState[]> {
    const res = await this.db.query(
      "SELECT state_json FROM close_state WHERE tenant_id = $1 ORDER BY period_key",
      [tenantId],
    );
    return res.rows.map((r) => JSON.parse(String(r["state_json"])) as CloseState);
  }
}

/** The durable seams a close engine acts through. */
export interface CloseEngineDeps {
  readonly periods: PeriodStore;
  readonly packages: FinancialPackageStore;
  readonly states: CloseStateStore;
}

export interface PublishCloseInput {
  readonly tenantId: string;
  /** The gate inputs — real reconciliation and control results for the period. */
  readonly inputs: CloseGateInputs;
  /** The sealed package to persist; its `periodKey` is the period being closed. */
  readonly pkg: FinancialPackage;
  /** Who prepared the close (ran the controls). Publisher must differ from this. */
  readonly preparedBy?: string;
  readonly publishedBy: string;
  readonly at: string;
}

function brand(tenantId: string, periodKey: string): { tenant: ReturnType<typeof asTenantId>; period: PeriodKey } {
  return { tenant: asTenantId(tenantId), period: asPeriodKey(periodKey) };
}

/**
 * Publish (seal) a period. Runs the gate, and only if every control passes:
 * locks the period durably, persists the immutable package, and records the
 * authoritative PUBLISHED state with its checklist evidence. Idempotent: an
 * already-published period with the same package fingerprint returns its state.
 */
export async function publishClose(deps: CloseEngineDeps, input: PublishCloseInput): Promise<CloseState> {
  const periodKey = input.pkg.periodKey;
  const existing = await deps.states.get(input.tenantId, periodKey);
  if (existing && existing.status === "PUBLISHED") {
    if (existing.packageFingerprint === input.pkg.fingerprint) return existing; // idempotent
    throw new CloseStateError(
      `${input.tenantId} ${periodKey} is already published with a different package`,
    );
  }

  // Gate on REAL control results — not an in-memory "done" flag.
  const checklist = buildCloseChecklist(input.inputs);
  const blockers = checklist.filter((t) => t.status === "FAILED").map((t) => t.label);
  if (blockers.length > 0) throw new CloseGateError(blockers);

  // Separation of duties: the publisher may not have prepared the close.
  if (input.preparedBy && input.preparedBy === input.publishedBy) {
    throw new SeparationOfDutiesError(
      `${input.publishedBy} prepared this close and cannot also publish it — a second person must sign off`,
    );
  }

  const { tenant, period } = brand(input.tenantId, periodKey);
  // Lock the period durably, then persist the immutable package. The package
  // store verifies integrity + balance before it accepts the record.
  await deps.periods.lock(tenant, period);
  await deps.packages.save(input.tenantId, input.pkg);

  const state: CloseState = {
    tenantId: input.tenantId,
    periodKey,
    status: "PUBLISHED",
    checklist,
    publishedBy: input.publishedBy,
    publishedAt: input.at,
    packageFingerprint: input.pkg.fingerprint,
    history: [
      ...(existing?.history ?? []),
      { action: "PUBLISH", by: input.publishedBy, at: input.at },
    ],
  };
  await deps.states.put(state);
  return state;
}

export interface RequestReopenInput {
  readonly tenantId: string;
  readonly periodKey: string;
  readonly requestedBy: string;
  readonly reason: string;
  readonly at: string;
}

/**
 * Request that a published period be reopened. Records intent and the reason,
 * but changes nothing else — the period stays locked until an approval by a
 * different person.
 */
export async function requestReopen(deps: CloseEngineDeps, input: RequestReopenInput): Promise<CloseState> {
  const state = await deps.states.get(input.tenantId, input.periodKey);
  if (!state || state.status === "OPEN") {
    throw new CloseStateError(`${input.tenantId} ${input.periodKey} is not published — nothing to reopen`);
  }
  if (state.status === "REOPEN_REQUESTED") {
    throw new CloseStateError(`a reopen is already pending approval for ${input.periodKey}`);
  }
  if (state.status === "REOPENED") {
    throw new CloseStateError(`${input.periodKey} is already reopened`);
  }
  const reason = input.reason.trim();
  if (!reason) throw new CloseStateError("a reopen must state a reason");

  const next: CloseState = {
    ...state,
    status: "REOPEN_REQUESTED",
    reopenRequestedBy: input.requestedBy,
    reopenReason: reason,
    history: [
      ...state.history,
      { action: "REQUEST_REOPEN", by: input.requestedBy, at: input.at, note: reason },
    ],
  };
  await deps.states.put(next);
  return next;
}

export interface ApproveReopenInput {
  readonly tenantId: string;
  readonly periodKey: string;
  readonly approvedBy: string;
  readonly at: string;
}

/**
 * Approve a pending reopen. Separation of duties: the approver may not be the
 * requester. On approval the period is unlocked durably; the published package
 * is preserved (never deleted) as the evidence of what was sealed.
 */
export async function approveReopen(deps: CloseEngineDeps, input: ApproveReopenInput): Promise<CloseState> {
  const state = await deps.states.get(input.tenantId, input.periodKey);
  if (!state || state.status !== "REOPEN_REQUESTED") {
    throw new CloseStateError(`${input.tenantId} ${input.periodKey} has no reopen awaiting approval`);
  }
  if (state.reopenRequestedBy && state.reopenRequestedBy === input.approvedBy) {
    throw new SeparationOfDutiesError(
      `${input.approvedBy} requested this reopen and cannot approve it — a second person must approve`,
    );
  }

  const { tenant, period } = brand(input.tenantId, input.periodKey);
  await deps.periods.unlock(tenant, period);

  const next: CloseState = {
    ...state,
    status: "REOPENED",
    history: [
      ...state.history,
      { action: "APPROVE_REOPEN", by: input.approvedBy, at: input.at },
    ],
  };
  await deps.states.put(next);
  return next;
}

/** Serialize a close state for a JSON API. */
export function closeStateJson(state: CloseState): Record<string, unknown> {
  return {
    tenant_id: state.tenantId,
    period: state.periodKey,
    status: state.status,
    checklist: state.checklist.map((t) => ({
      id: t.id,
      label: t.label,
      status: t.status,
      detail: t.detail,
    })),
    ...(state.publishedBy ? { published_by: state.publishedBy } : {}),
    ...(state.publishedAt ? { published_at: state.publishedAt } : {}),
    ...(state.packageFingerprint ? { package_fingerprint: state.packageFingerprint } : {}),
    ...(state.reopenRequestedBy ? { reopen_requested_by: state.reopenRequestedBy } : {}),
    ...(state.reopenReason ? { reopen_reason: state.reopenReason } : {}),
    history: state.history.map((h) => ({
      action: h.action,
      by: h.by,
      at: h.at,
      ...(h.note ? { note: h.note } : {}),
    })),
  };
}
