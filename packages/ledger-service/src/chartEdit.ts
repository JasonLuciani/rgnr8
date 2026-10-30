/**
 * Editing the chart of accounts: the rules that decide whether one account may
 * be created, renamed, retyped, re-parented, renumbered or made inactive.
 *
 * They live here, not in the web app, so every client of the ledger gets the same
 * answer. The rules, each refused with a 400 (the request is wrong) or a 409 (the
 * books are in a state that forbids it):
 *
 *  - QuickBooks accounts (`origin: quickbooks`) take no change while QuickBooks is
 *    the book of record. The answer is 409 "edit this account in QuickBooks". After
 *    cutover RGNR8 is the system of record and they are ordinary accounts again.
 *  - System accounts (see `isSystemAccount`) can be renamed, but cannot be made
 *    inactive, retyped, re-subtyped or renumbered.
 *  - The type cannot change once the account has any posted line (a reversal
 *    counts), and cannot change while it has sub-accounts.
 *  - An account with a non-zero balance cannot be made inactive.
 *  - A parent must exist, be active, have the same type, not create a cycle, and
 *    keep the hierarchy within {@link MAX_CHART_DEPTH} levels.
 *  - The number (the account code) is unique within the chart.
 *
 * The rules read the chart and the journal through {@link ChartEditContext}, so
 * the function holds no storage of its own.
 */

import {
  AccountSubtype,
  AccountType,
  ChartOfAccounts,
  Money,
  accountTypeOfSubtype,
  isSystemAccount,
  parentsFirst,
  type Account,
  type AccountId,
  type Currency,
  type LedgerStore,
  type TenantId,
} from "@rgnr8/ledger-kernel";

/** Levels a chart may nest: a top-level account is level 1. */
export const MAX_CHART_DEPTH = 5;

/** A refused change. `status` is the HTTP status the service answers with. */
export class ChartEditError extends Error {
  constructor(readonly status: 400 | 409, message: string) {
    super(message);
    this.name = "ChartEditError";
  }
}

/** What a caller asked to change. `undefined` means "leave it alone". */
export interface AccountChanges {
  readonly name?: string;
  /** The new account number (the account code). */
  readonly number?: string;
  readonly type?: AccountType;
  /** `null` clears the detail type. */
  readonly subtype?: AccountSubtype | null;
  /** The parent's code; `null` makes the account top-level. */
  readonly parentCode?: string | null;
  readonly active?: boolean;
}

/** The facts the rules read. Lazy, so a request that doesn't need one doesn't load it. */
export interface ChartEditContext {
  readonly tenant: TenantId;
  readonly chart: ChartOfAccounts;
  readonly currency: Currency;
  /** True while QuickBooks is the book of record (the parallel run). */
  readonly quickBooksOfRecord: boolean;
  /** Whether any posted line references the account, reversals included. */
  hasPostings(account: Account): Promise<boolean>;
  /** The account's net balance, debit minus credit, in minor units. */
  balanceMinor(account: Account): Promise<bigint>;
}

/** The net debit position of every account, debit-positive, in minor units. */
export async function netByAccount(
  store: LedgerStore, tenant: TenantId,
): Promise<Map<AccountId, bigint>> {
  if (store.netByAccount) return store.netByAccount(tenant);
  const net = new Map<AccountId, bigint>();
  for (const entry of await store.list(tenant)) {
    for (const l of entry.lines) {
      const signed = l.side === "DEBIT" ? l.amount.minorUnits : -l.amount.minorUnits;
      net.set(l.accountId, (net.get(l.accountId) ?? 0n) + signed);
    }
  }
  return net;
}

/** The accounts that any posted line references, reversals included. */
export async function postedAccountIds(store: LedgerStore, tenant: TenantId): Promise<Set<AccountId>> {
  const ids = new Set<AccountId>();
  for (const entry of await store.list(tenant)) {
    for (const l of entry.lines) ids.add(l.accountId);
  }
  return ids;
}

/** The code of an account's parent in the chart, or null when it has none. */
export function parentCodeIn(chart: ChartOfAccounts, a: Account): string | null {
  if (a.parentId === undefined) return null;
  return chart.get(a.parentId)?.code ?? null;
}

/** Levels from the top of the chart down to and including `a`. */
function depthOf(chart: ChartOfAccounts, a: Account): number {
  let depth = 1;
  let cursor: Account | undefined = a;
  const seen = new Set<AccountId>([a.id]);
  while (cursor?.parentId !== undefined) {
    const parent: Account | undefined = chart.get(cursor.parentId);
    if (!parent || seen.has(parent.id)) break;
    seen.add(parent.id);
    depth += 1;
    cursor = parent;
  }
  return depth;
}

/** Levels below `a`: 0 for a leaf, 1 for an account whose children are leaves, and so on. */
function heightOf(chart: ChartOfAccounts, a: Account): number {
  const kids = chart.children(a.id);
  if (kids.length === 0) return 0;
  return 1 + Math.max(...kids.map((k) => heightOf(chart, k)));
}

/**
 * Check a parent for an account of `type`. `accountId` is the account being
 * changed, or undefined when it is being created. Returns the parent.
 */
export function checkParent(
  chart: ChartOfAccounts,
  parentCode: string,
  type: AccountType,
  accountId: AccountId | undefined,
  accountHeight: number,
): Account {
  const parent = chart.getByCode(parentCode);
  if (!parent) throw new ChartEditError(400, `parent account ${parentCode} does not exist`);
  if (accountId !== undefined && parent.id === accountId) {
    throw new ChartEditError(400, "an account cannot be its own parent");
  }
  if (accountId !== undefined) {
    // Walk up from the proposed parent: meeting the account itself is a cycle.
    let up: Account | undefined = parent;
    const seen = new Set<AccountId>();
    while (up !== undefined && !seen.has(up.id)) {
      if (up.id === accountId) {
        throw new ChartEditError(400, `${parentCode} is already a sub-account of this account, so this would be a cycle`);
      }
      seen.add(up.id);
      up = up.parentId !== undefined ? chart.get(up.parentId) : undefined;
    }
  }
  if (parent.active === false) {
    throw new ChartEditError(400, `parent account ${parentCode} is inactive`);
  }
  if (parent.type !== type) {
    throw new ChartEditError(
      400,
      `parent account ${parentCode} is ${parent.type}; a sub-account must have the same type as its parent (${type})`,
    );
  }
  const levels = depthOf(chart, parent) + 1 + accountHeight;
  if (levels > MAX_CHART_DEPTH) {
    throw new ChartEditError(400, `the chart may nest at most ${MAX_CHART_DEPTH} levels; this would be ${levels}`);
  }
  return parent;
}

/** Resolve the account's type and detail type from the request, enforcing that they agree. */
function resolveTypeAndSubtype(
  current: Account | undefined,
  changes: AccountChanges,
): { type: AccountType; subtype: AccountSubtype | undefined } {
  const subtype = changes.subtype === undefined
    ? current?.subtype
    : (changes.subtype ?? undefined);
  const fromSubtype = subtype !== undefined ? accountTypeOfSubtype(subtype) : undefined;
  if (changes.type !== undefined && fromSubtype !== undefined && changes.type !== fromSubtype) {
    throw new ChartEditError(400, `detail type ${subtype} belongs to ${fromSubtype}, not ${changes.type}`);
  }
  const type = changes.type ?? fromSubtype ?? current?.type;
  if (type === undefined) throw new ChartEditError(400, "an account needs a type or a detail type");
  if (subtype !== undefined && accountTypeOfSubtype(subtype) !== type) {
    throw new ChartEditError(400, `detail type ${subtype} does not belong to type ${type}; choose a detail type for it`);
  }
  return { type, subtype };
}

function cleanName(raw: string | undefined, current?: string): string {
  if (raw === undefined) return current ?? "";
  const name = raw.trim();
  if (!name) throw new ChartEditError(400, "an account needs a name");
  return name;
}

function cleanNumber(raw: string): string {
  const code = raw.trim();
  if (!code) throw new ChartEditError(400, "an account needs a number");
  return code;
}

/** Create a new account. The caller persists it and the chart validates it. */
export function planNewAccount(
  input: { code: string; name: string; type?: AccountType; subtype?: AccountSubtype; parentCode?: string | null },
  ctx: ChartEditContext,
): Account {
  const code = cleanNumber(input.code);
  const name = cleanName(input.name);
  if (ctx.chart.getByCode(code)) throw new ChartEditError(409, `account ${code} already exists`);
  const { type, subtype } = resolveTypeAndSubtype(undefined, {
    ...(input.type !== undefined ? { type: input.type } : {}),
    subtype: input.subtype ?? null,
  });
  const parent = input.parentCode
    ? checkParent(ctx.chart, input.parentCode, type, undefined, 0)
    : undefined;
  const account: Account = {
    id: `acct:${code}` as AccountId,
    code,
    name,
    type,
    currency: ctx.currency,
    ...(subtype ? { subtype } : {}),
    ...(parent ? { parentId: parent.id } : {}),
    active: true,
    origin: "rgnr8",
  };
  new ChartOfAccounts(parentsFirst([...ctx.chart.list(), account])); // kernel-validate
  return account;
}

/**
 * Apply `changes` to an existing account. Returns the account as it should be
 * stored, or the account unchanged when nothing differs. Throws ChartEditError.
 */
export async function planAccountEdit(
  current: Account,
  changes: AccountChanges,
  ctx: ChartEditContext,
): Promise<Account> {
  const chart = ctx.chart;
  const currentParent = parentCodeIn(chart, current);
  const currentActive = current.active !== false;

  // What would be different. Computed before any refusal, so a no-op is a no-op
  // even on a locked account, and a real change is refused for the right reason.
  const { type, subtype } = resolveTypeAndSubtype(current, changes);
  const nameChanged = changes.name !== undefined && cleanName(changes.name) !== current.name;
  const numberChanged = changes.number !== undefined && cleanNumber(changes.number) !== current.code;
  const typeChanged = type !== current.type;
  const subtypeChanged = subtype !== current.subtype;
  const parentChanged = changes.parentCode !== undefined && (changes.parentCode || null) !== currentParent;
  const activeChanged = changes.active !== undefined && changes.active !== currentActive;
  const anyChange = nameChanged || numberChanged || typeChanged || subtypeChanged
    || parentChanged || activeChanged;
  if (!anyChange) return current;

  if (current.origin === "quickbooks" && ctx.quickBooksOfRecord) {
    throw new ChartEditError(409, "edit this account in QuickBooks");
  }

  const system = isSystemAccount(current);
  if (system && (typeChanged || subtypeChanged)) {
    throw new ChartEditError(409, `${current.code} ${current.name} is a system account: its type and detail type cannot change`);
  }
  if (system && numberChanged) {
    throw new ChartEditError(409, `${current.code} ${current.name} is a system account: its number cannot change`);
  }
  if (system && activeChanged && changes.active === false) {
    throw new ChartEditError(409, `${current.code} ${current.name} is a system account and cannot be made inactive`);
  }

  if (typeChanged) {
    if (await ctx.hasPostings(current)) {
      throw new ChartEditError(409, `${current.code} has posted entries, so its type cannot change`);
    }
    const kids = chart.children(current.id).length;
    if (kids > 0) {
      throw new ChartEditError(409, `${current.code} has ${kids} sub-account(s); move them before changing its type`);
    }
  }

  if (changes.active === false && currentActive) {
    const balance = await ctx.balanceMinor(current);
    if (balance !== 0n) {
      const shown = Money.fromMinorUnits(balance < 0n ? -balance : balance, ctx.currency).toDecimalString();
      const side = balance > 0n ? "debit" : "credit";
      throw new ChartEditError(
        409,
        `${current.code} has a balance of ${shown} ${ctx.currency.code} (${side}); clear it before making it inactive`,
      );
    }
  }
  if (changes.active === true && !currentActive && current.parentId !== undefined) {
    const parent = chart.get(current.parentId);
    if (parent && parent.active === false) {
      throw new ChartEditError(409, `parent account ${parent.code} is inactive; reactivate it first`);
    }
  }

  const finalParentCode = changes.parentCode !== undefined ? changes.parentCode || null : currentParent;
  let parentId: AccountId | undefined = current.parentId;
  if (finalParentCode === null) {
    parentId = undefined;
  } else if (parentChanged || typeChanged || (activeChanged && changes.active === true)) {
    const parent = checkParent(chart, finalParentCode, type, current.id, heightOf(chart, current));
    parentId = parent.id;
  }

  if (numberChanged) {
    const number = cleanNumber(changes.number!);
    if (chart.getByCode(number)) throw new ChartEditError(409, `account ${number} already exists`);
  }

  const next: Account = {
    ...current,
    code: numberChanged ? cleanNumber(changes.number!) : current.code,
    name: cleanName(changes.name, current.name),
    type,
    active: changes.active !== undefined ? changes.active : currentActive,
    ...(subtype !== undefined ? { subtype } : {}),
    ...(parentId !== undefined ? { parentId } : {}),
  };
  if (subtype === undefined) delete (next as { subtype?: AccountSubtype }).subtype;
  if (parentId === undefined) delete (next as { parentId?: AccountId }).parentId;
  new ChartOfAccounts(parentsFirst([...chart.list().filter((a) => a.id !== current.id), next])); // kernel-validate
  return next;
}

/** Why an account is read-only, or null when it can be edited. */
export function lockedBy(a: Account, quickBooksOfRecord: boolean): "quickbooks" | "system" | null {
  if (a.origin === "quickbooks" && quickBooksOfRecord) return "quickbooks";
  if (isSystemAccount(a)) return "system";
  return null;
}
