/**
 * Master-data / user-action audit log.
 *
 * The journal is already immutable, but master-data edits (renaming a customer,
 * changing an account's default, deactivating an item) and user actions (who
 * closed the period, who ran a sync) need their own trail. This is an
 * append-only log of {actor, action, entity, before/after, at} — the timestamp
 * is always caller-supplied (no wall clock in-core), so it's deterministic and
 * testable, and entries are frozen on write.
 */

export interface AuditEntry {
  readonly id: number; // monotonic per log
  readonly at: string; // ISO instant, caller-supplied
  readonly actor: string; // user/system id
  readonly action: string; // e.g. "customer.update", "period.close"
  readonly entityType: string; // e.g. "customer", "account"
  readonly entityId: string;
  /** State before/after the action (redacted/plain objects), optional. */
  readonly before?: unknown;
  readonly after?: unknown;
  readonly note?: string;
}

export type AuditEntryInput = Omit<AuditEntry, "id">;

export interface AuditQuery {
  readonly actor?: string;
  readonly entityType?: string;
  readonly entityId?: string;
  readonly action?: string;
  readonly from?: string; // ISO, inclusive
  readonly to?: string; // ISO, inclusive
}

/** Append-only audit log. In-memory reference; a SQL adapter mirrors it. */
export class AuditLog {
  private readonly entries: AuditEntry[] = [];
  private seq = 0;

  record(input: AuditEntryInput): AuditEntry {
    const entry = Object.freeze({ id: ++this.seq, ...input }) as AuditEntry;
    this.entries.push(entry);
    return entry;
  }

  /** All entries in append order (optionally filtered). */
  query(q: AuditQuery = {}): readonly AuditEntry[] {
    return this.entries.filter((e) => {
      if (q.actor !== undefined && e.actor !== q.actor) return false;
      if (q.entityType !== undefined && e.entityType !== q.entityType) return false;
      if (q.entityId !== undefined && e.entityId !== q.entityId) return false;
      if (q.action !== undefined && e.action !== q.action) return false;
      if (q.from !== undefined && e.at < q.from) return false;
      if (q.to !== undefined && e.at > q.to) return false;
      return true;
    });
  }

  /** The full history of one entity, oldest first. */
  history(entityType: string, entityId: string): readonly AuditEntry[] {
    return this.query({ entityType, entityId });
  }

  size(): number {
    return this.entries.length;
  }
}
