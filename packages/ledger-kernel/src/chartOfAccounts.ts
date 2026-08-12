import type { Account, AccountId } from "./types.js";

/** An immutable registry of accounts. Lookups by id or code. */
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
    this.byId.set(account.id, Object.freeze({ ...account }));
    this.byCode.set(account.code, this.byId.get(account.id)!);
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
}
