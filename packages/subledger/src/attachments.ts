/**
 * Attachments on transactions.
 *
 * Source documents — a scanned receipt, a vendor PDF invoice, a signed contract
 * — need to hang off the transaction they support so an owner or auditor can see
 * *why* an entry exists. This tracks attachment metadata (never the bytes: those
 * live in object storage, referenced by `storageKey`) linked to a target by
 * kind + id (a journal entry, an invoice, a bill…). Records are append-only with
 * a caller-supplied `uploadedAt` (no wall clock), and a soft `removed` flag so a
 * mistaken attachment can be hidden without erasing the trail.
 */

export type AttachmentTargetKind = "entry" | "invoice" | "bill" | "expense" | "payment" | "other";

export interface Attachment {
  readonly id: string;
  readonly targetKind: AttachmentTargetKind;
  readonly targetId: string;
  readonly filename: string;
  readonly contentType: string;
  readonly byteSize: number;
  /** Object-storage key/URL where the bytes actually live. */
  readonly storageKey: string;
  /** Content hash for integrity/dedupe (e.g. sha256 hex), optional. */
  readonly checksum?: string;
  readonly uploadedBy: string;
  readonly uploadedAt: string; // ISO, caller-supplied
  readonly removed?: boolean;
}

export type AttachmentInput = Omit<Attachment, "removed">;

export class AttachmentError extends Error {}

/** Per-tenant attachment metadata registry. In-memory reference implementation. */
export class AttachmentStore {
  private readonly byId = new Map<string, Attachment>();

  private key(tenant: string, id: string): string {
    return `${tenant}::${id}`;
  }

  /** Attach a document to a transaction. Ids must be unique per tenant. */
  add(tenant: string, input: AttachmentInput): Attachment {
    const k = this.key(tenant, input.id);
    if (this.byId.has(k)) throw new AttachmentError(`duplicate attachment ${input.id}`);
    if (input.byteSize < 0) throw new AttachmentError("byteSize must be non-negative");
    const att = Object.freeze({ ...input, removed: false }) as Attachment;
    this.byId.set(k, att);
    return att;
  }

  /** All (non-removed) attachments for a target, oldest first. */
  forTarget(tenant: string, kind: AttachmentTargetKind, targetId: string): Attachment[] {
    return [...this.byId.entries()]
      .filter(([k]) => k.startsWith(`${tenant}::`))
      .map(([, a]) => a)
      .filter((a) => !a.removed && a.targetKind === kind && a.targetId === targetId)
      .sort((a, b) => (a.uploadedAt < b.uploadedAt ? -1 : a.uploadedAt > b.uploadedAt ? 1 : a.id.localeCompare(b.id)));
  }

  get(tenant: string, id: string): Attachment | undefined {
    return this.byId.get(this.key(tenant, id));
  }

  /** Soft-remove an attachment (kept for the audit trail). */
  remove(tenant: string, id: string): void {
    const k = this.key(tenant, id);
    const a = this.byId.get(k);
    if (!a) throw new AttachmentError(`unknown attachment ${id}`);
    this.byId.set(k, Object.freeze({ ...a, removed: true }));
  }
}
