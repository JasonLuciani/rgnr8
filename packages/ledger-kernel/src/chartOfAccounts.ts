import { accountTypeOfSubtype, type Account, type AccountId } from "./types.js";

/**
 * A registry of accounts with lookups by id/code and a real parent/sub-account
 * hierarchy. Validates on insert:
 *  - unique id and code;
 *  - a declared `subtype` must belong to the account's `type`;
 *  - a `parentId` must reference a known account of the **same type** (QBO
 *    disallows sub-accounts under a different account type), with no cycles.
 *
 * The registry is append-structured, but `deactivate`/`activate` toggle an
 * account's `active` flag (history is never removed — accounts are made inactive,
 * not deleted).
 */
export class ChartOfAccounts {
  private readonly byId = new Map<AccountId, Account>();
  private readonly byCode = new Map<string, Account>();

  constructor(accounts: readonly Account[] = []) {
    for (const a of accounts) this.add(a);
  }

  add(account: Account): this {
    if (this.byId.has(account.id)) {
      throw new Error(`Duplicate account id: ${account.id}`);
    }
    if (this.byCode.has(account.code)) {
      throw new Error(`Duplicate account code: ${account.code}`);
    }
    if (account.subtype !== undefined && accountTypeOfSubtype(account.subtype) !== account.type) {
      throw new Error(
        `Account ${account.code}: subtype ${account.subtype} does not belong to type ${account.type}`,
      );
    }
    if (account.parentId !== undefined) {
      const parent = this.byId.get(account.parentId);
      if (parent === undefined) {
        throw new Error(`Account ${account.code}: unknown parentId ${account.parentId}`);
      }
      if (parent.type !== account.type) {
        throw new Error(
          `Account ${account.code}: sub-account type ${account.type} must match parent type ${parent.type}`,
        );
      }
      if (account.parentId === account.id) {
        throw new Error(`Account ${account.code}: cannot be its own parent`);
      }
    }
    const stored = Object.freeze({ active: true, ...account });
    this.byId.set(account.id, stored);
    this.byCode.set(account.code, stored);
    return this;
  }

  has(id: AccountId): boolean {
    return this.byId.has(id);
  }

  get(id: AccountId): Account | undefined {
    return this.byId.get(id);
  }

  getByCode(code: string): Account | undefined {
    return this.byCode.get(code);
  }

  list(): readonly Account[] {
    return [...this.byId.values()];
  }

  /** Only active accounts (for pickers/new documents). */
  listActive(): readonly Account[] {
    return this.list().filter((a) => a.active !== false);
  }

  /** Direct children of an account. */
  children(id: AccountId): readonly Account[] {
    return this.list()
      .filter((a) => a.parentId === id)
      .sort((a, b) => a.code.localeCompare(b.code));
  }

  /** All descendants (children, grandchildren, …), depth-first by code. */
  descendants(id: AccountId): readonly Account[] {
    const out: Account[] = [];
    for (const child of this.children(id)) {
      out.push(child);
      out.push(...this.descendants(child.id));
    }
    return out;
  }

  /** An account plus all its descendants — the set that rolls up into it. */
  subtree(id: AccountId): readonly Account[] {
    const self = this.get(id);
    return self ? [self, ...this.descendants(id)] : [];
  }

  /** Deactivate an account (kept for history; hidden from pickers). */
  deactivate(id: AccountId): this {
    return this.setActive(id, false);
  }

  activate(id: AccountId): this {
    return this.setActive(id, true);
  }

  private setActive(id: AccountId, active: boolean): this {
    const a = this.byId.get(id);
    if (a === undefined) throw new Error(`Unknown account: ${id}`);
    const updated = Object.freeze({ ...a, active });
    this.byId.set(id, updated);
    this.byCode.set(a.code, updated);
    return this;
  }
}
