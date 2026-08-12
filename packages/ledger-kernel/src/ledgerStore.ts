import type { DraftEntry, EntryId, IdempotencyKey, PostedEntry, TenantId } from "./types.js";

/**
 * Append-only journal store. Posted entries are never updated or deleted; the
 * only write is `append`, and the store owns atomic assignment of the monotonic
 * per-tenant sequence and entry id. Concrete adapters (PostgreSQL, etc.)
 * implement this same contract behind the posting engine.
 *
 * All methods are async so persistent, concurrent backends fit the same shape.
 */
export interface LedgerStore {
  /**
   * Atomically assign the next per-tenant sequence + id, persist, and return
   * the finalized entry. If the draft's idempotency key was already used, the
   * store returns the existing entry and appends nothing (no sequence gap).
   */
  append(draft: DraftEntry): Promise<PostedEntry>;
  getByIdempotencyKey(tenant: TenantId, key: IdempotencyKey): Promise<PostedEntry | undefined>;
  getById(tenant: TenantId, id: EntryId): Promise<PostedEntry | undefined>;
  getBySequence(tenant: TenantId, sequence: number): Promise<PostedEntry | undefined>;
  /** All entries for a tenant, in posting order. */
  list(tenant: TenantId): Promise<readonly PostedEntry[]>;
}

interface TenantLog {
  entries: PostedEntry[];
  byKey: Map<IdempotencyKey, PostedEntry>;
  byId: Map<EntryId, PostedEntry>;
}

/**
 * In-memory reference implementation. Enforces append-only and contiguous,
 * monotonic per-tenant sequencing. Suitable for tests and for the Phase 0
 * shadow ledger before the PostgreSQL adapter is wired in.
 */
export class InMemoryLedgerStore implements LedgerStore {
  private readonly tenants = new Map<TenantId, TenantLog>();

  private log(tenant: TenantId): TenantLog {
    let l = this.tenants.get(tenant);
    if (!l) {
      l = { entries: [], byKey: new Map(), byId: new Map() };
      this.tenants.set(tenant, l);
    }
    return l;
  }

  append(draft: DraftEntry): Promise<PostedEntry> {
    const l = this.log(draft.tenantId);

    // Idempotent: a key already used returns the existing entry, appends nothing.
    const dup = l.byKey.get(draft.idempotencyKey);
    if (dup) return Promise.resolve(dup);

    const sequence = l.entries.length + 1;
    const id = `${draft.tenantId}:${sequence}` as EntryId;
    const entry = Object.freeze({ ...draft, id, sequence }) as PostedEntry;

    l.entries.push(entry);
    l.byKey.set(entry.idempotencyKey, entry);
    l.byId.set(entry.id, entry);
    return Promise.resolve(entry);
  }

  getByIdempotencyKey(tenant: TenantId, key: IdempotencyKey): Promise<PostedEntry | undefined> {
    return Promise.resolve(this.tenants.get(tenant)?.byKey.get(key));
  }

  getById(tenant: TenantId, id: EntryId): Promise<PostedEntry | undefined> {
    return Promise.resolve(this.tenants.get(tenant)?.byId.get(id));
  }

  getBySequence(tenant: TenantId, sequence: number): Promise<PostedEntry | undefined> {
    return Promise.resolve(this.tenants.get(tenant)?.entries[sequence - 1]);
  }

  list(tenant: TenantId): Promise<readonly PostedEntry[]> {
    return Promise.resolve(this.tenants.get(tenant)?.entries ?? []);
  }
}
