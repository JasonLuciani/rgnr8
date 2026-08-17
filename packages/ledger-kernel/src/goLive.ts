import { Money, getCurrency, type Currency } from "./money.js";
import { ChartOfAccounts } from "./chartOfAccounts.js";
import { PostingEngine } from "./postingEngine.js";
import { PeriodRegistry } from "./periods.js";
import type { LedgerStore } from "./ledgerStore.js";
import {
  BusinessCategory,
  templateAccounts,
} from "./coaTemplates.js";
import { CutoverError, executeCutover, type CutoverResult, type OpeningBalance, type SourceSystem } from "./cutover.js";
import {
  AccountType,
  AccountSubtype,
  accountTypeOfSubtype,
  normalBalanceOf,
  type Account,
  type AccountId,
  type Provenance,
} from "./types.js";

/**
 * Go-live — the one call that takes a client from "mirroring QBO/Xero" to
 * "RGNR8 is the system of record."
 *
 * It composes the pieces that already exist into the real end-to-end flow:
 *   1. build the chart of accounts (seed a business-category template, and/or
 *      bring over the source system's own accounts by code),
 *   2. map the source's as-of trial balance into opening balances,
 *   3. post the opening-balance conversion journal and lock the cutover period
 *      (via {@link executeCutover}) — RGNR8 is now authoritative.
 *
 * The request is a serializable contract (`go-live/1`, see {@link goLiveFromDto})
 * so the Python operator control-plane can hand a fully-specified go-live to the
 * TS accounting core without sharing types.
 */

export const GO_LIVE_CONTRACT = "go-live/1";

/** One account from the source system's trial balance (its chart + as-of balance). */
export interface SourceAccount {
  readonly code: string;
  readonly name: string;
  /** Signed, debit-positive as-of balance (debit balance +, credit balance −). */
  readonly balance: Money;
  /** Optional subtype; when set it also determines the type. */
  readonly subtype?: AccountSubtype;
  /** Used only when no subtype is given (to place a brought-over account). */
  readonly type?: AccountType;
}

export interface GoLiveRequest {
  readonly tenantId: string;
  readonly sourceSystem: SourceSystem;
  readonly cutoverDate: string; // ISO
  readonly currency: Currency;
  /** Seed this template chart first (optional). */
  readonly coaCategory?: BusinessCategory;
  /** The source's chart + as-of trial balance (drives opening balances). */
  readonly sourceAccounts: readonly SourceAccount[];
  /** Account code the opening-balance residual posts to (created if absent). */
  readonly openingBalanceEquityCode: string;
  readonly provenance: Provenance;
}

export interface GoLiveResult {
  readonly chart: ChartOfAccounts;
  /** Accounts created beyond the template (brought over from the source + OBE). */
  readonly createdAccounts: readonly Account[];
  readonly cutover: CutoverResult;
}

function accountIdFor(code: string): AccountId {
  return `acct:${code}` as AccountId;
}

/**
 * Build the go-live chart: start from a category template (if given), then bring
 * over every source account not already present (matched by code), then ensure
 * the Opening Balance Equity account exists. Returns the chart plus the accounts
 * that were newly created (so the caller can persist just those, or all).
 */
function buildGoLiveChart(request: GoLiveRequest): { chart: ChartOfAccounts; created: Account[] } {
  const accounts: Account[] = request.coaCategory
    ? [...templateAccounts(request.coaCategory, request.currency)]
    : [];
  const byCode = new Map(accounts.map((a) => [a.code, a]));
  const created: Account[] = [];

  for (const src of request.sourceAccounts) {
    if (byCode.has(src.code)) continue; // template already has it
    const type = src.subtype ? accountTypeOfSubtype(src.subtype) : src.type;
    if (type === undefined) {
      throw new CutoverError(`source account ${src.code} has neither a subtype nor a type`);
    }
    const account: Account = {
      id: accountIdFor(src.code),
      code: src.code,
      name: src.name,
      type,
      currency: request.currency,
      ...(src.subtype ? { subtype: src.subtype } : {}),
      active: true,
    };
    accounts.push(account);
    byCode.set(src.code, account);
    created.push(account);
  }

  // Opening Balance Equity must exist for the conversion residual.
  if (!byCode.has(request.openingBalanceEquityCode)) {
    const obe: Account = {
      id: accountIdFor(request.openingBalanceEquityCode),
      code: request.openingBalanceEquityCode,
      name: "Opening Balance Equity",
      type: AccountType.EQUITY,
      currency: request.currency,
      subtype: AccountSubtype.EQUITY,
      active: true,
    };
    accounts.push(obe);
    byCode.set(obe.code, obe);
    created.push(obe);
  }

  return { chart: new ChartOfAccounts(accounts), created };
}

/**
 * Execute a client's go-live end to end. `store` + `periods` must be a fresh (or
 * this tenant's) ledger; the flow builds the chart, constructs the posting
 * engine over it, posts the opening balances, and locks the cutover period.
 * Idempotent through {@link executeCutover}. `postedAt` is injected.
 */
export async function executeGoLive(
  store: LedgerStore,
  periods: PeriodRegistry,
  request: GoLiveRequest,
  postedAt: string,
): Promise<GoLiveResult> {
  if (request.sourceAccounts.length === 0) {
    throw new CutoverError("go-live has no source accounts to open with");
  }
  const { chart, created } = buildGoLiveChart(request);
  const engine = new PostingEngine(chart, store, periods);

  const balances: OpeningBalance[] = request.sourceAccounts.map((src) => ({
    accountId: accountIdFor(src.code),
    balance: src.balance,
  }));

  const cutover = await executeCutover(engine, periods, store, {
    tenantId: request.tenantId,
    sourceSystem: request.sourceSystem,
    cutoverDate: request.cutoverDate,
    currency: request.currency,
    balances,
    openingBalanceEquityId: accountIdFor(request.openingBalanceEquityCode),
    provenance: request.provenance,
    memo: `Go-live from ${request.sourceSystem}`,
  }, postedAt);

  return { chart, createdAccounts: created, cutover };
}

// --- go-live/1 serializable contract ----------------------------------------

export interface GoLiveDto {
  readonly contract: string;
  readonly tenant_id: string;
  readonly source_system: string;
  readonly cutover_date: string;
  readonly currency: string;
  readonly coa_category?: string;
  readonly opening_balance_equity_code: string;
  readonly source_accounts: ReadonlyArray<{
    readonly code: string;
    readonly name: string;
    readonly balance_minor: string;
    readonly subtype?: string;
    readonly type?: string;
  }>;
  /** Optional; the TS core synthesizes a default when a caller (e.g. the Python
   * control plane) omits it. */
  readonly provenance?: Provenance;
}

/** A default provenance stamped when a go-live/1 DTO omits one. */
function defaultProvenance(dto: GoLiveDto): Provenance {
  return {
    sourceSystem: dto.source_system,
    sourceObject: "trial_balance",
    sourceVersion: "1",
    effectiveDate: dto.cutover_date,
    postedDate: dto.cutover_date,
    ingestedAt: dto.cutover_date,
    normalizationVersion: "go-live/1",
    mappingVersion: "go-live/1",
  };
}

/** Serialize a go-live request to the `go-live/1` contract (minor-unit strings). */
export function goLiveToDto(request: GoLiveRequest): GoLiveDto {
  return {
    contract: GO_LIVE_CONTRACT,
    tenant_id: request.tenantId,
    source_system: request.sourceSystem,
    cutover_date: request.cutoverDate,
    currency: request.currency.code,
    ...(request.coaCategory ? { coa_category: request.coaCategory } : {}),
    opening_balance_equity_code: request.openingBalanceEquityCode,
    source_accounts: request.sourceAccounts.map((s) => ({
      code: s.code,
      name: s.name,
      balance_minor: s.balance.minorUnits.toString(),
      ...(s.subtype ? { subtype: s.subtype } : {}),
      ...(s.type ? { type: s.type } : {}),
    })),
    provenance: request.provenance,
  };
}

/** Parse a `go-live/1` contract into a typed request. Throws on a bad shape. */
export function goLiveFromDto(dto: GoLiveDto): GoLiveRequest {
  if (dto.contract !== GO_LIVE_CONTRACT) {
    throw new CutoverError(`expected ${GO_LIVE_CONTRACT}, got ${dto.contract}`);
  }
  const currency = getCurrency(dto.currency);
  const coaCategory =
    dto.coa_category !== undefined ? (dto.coa_category as BusinessCategory) : undefined;
  if (coaCategory !== undefined && !Object.values(BusinessCategory).includes(coaCategory)) {
    throw new CutoverError(`unknown coa_category ${dto.coa_category}`);
  }
  return {
    tenantId: dto.tenant_id,
    sourceSystem: dto.source_system as SourceSystem,
    cutoverDate: dto.cutover_date,
    currency,
    ...(coaCategory ? { coaCategory } : {}),
    openingBalanceEquityCode: dto.opening_balance_equity_code,
    provenance: dto.provenance ?? defaultProvenance(dto),
    sourceAccounts: dto.source_accounts.map((s) => ({
      code: s.code,
      name: s.name,
      balance: Money.fromMinorUnits(BigInt(s.balance_minor), currency),
      ...(s.subtype ? { subtype: s.subtype as AccountSubtype } : {}),
      ...(s.type ? { type: s.type as AccountType } : {}),
    })),
  };
}
