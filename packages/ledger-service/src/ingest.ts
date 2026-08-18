import {
  Money,
  PostingEngine,
  type AccountId,
  type ChartOfAccounts,
  type Currency,
  type LedgerStore,
  type PeriodStore,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import {
  RuleSet,
  TransactionKind,
  postCanonicalToLedger,
  type AccountMap,
  type CanonicalTransaction,
  type CategoryRule,
  type Direction,
  type PostingReport,
} from "@rgnr8/ingestion";

/**
 * The feed → ledger path: how a client's books stay **current** without anyone
 * typing entries.
 *
 * A bank/card/QBO sync hands this a batch of transactions; each becomes a
 * balanced journal entry against the cash account and a counter account chosen
 * by categorization rules (falling back to a kind-based default). The heavy
 * lifting is `postCanonicalToLedger`, which is idempotent (keyed by transaction
 * id, so re-syncing an overlapping window never double-posts), period-safe (a
 * transaction dated into a closed month is deferred, not force-posted), and
 * failure-isolated (one bad row doesn't sink the batch).
 *
 * The wire format speaks **account codes**; ids are resolved against the
 * tenant's chart here so callers never see internal ids.
 */

/** One transaction as a caller sends it. Amount is signed: + into the account. */
export interface IngestTransactionInput {
  readonly id: string;
  readonly date: string;
  /** Signed integer minor units: positive = money in, negative = money out. */
  readonly amount_minor: string | number;
  readonly description?: string;
  readonly counterparty?: string;
  /** DEPOSIT | PURCHASE | FEE | INTEREST | REFUND | TRANSFER | ... */
  readonly kind?: string;
  readonly is_transfer?: boolean;
}

export interface IngestRuleInput {
  readonly id: string;
  readonly priority: number;
  readonly account_code: string;
  readonly description_contains?: string;
  readonly counterparty_equals?: string;
  readonly kind?: string;
}

export interface IngestRequest {
  readonly source?: string;
  /** Account codes the mapping posts to; each falls back to a sensible default. */
  readonly accounts?: Readonly<Record<string, string>>;
  readonly rules?: readonly IngestRuleInput[];
  readonly transactions: readonly IngestTransactionInput[];
}

export class IngestError extends Error {}

function kindOf(raw: string | undefined, inflow: boolean): TransactionKind {
  if (raw) {
    const upper = raw.toUpperCase();
    if ((Object.values(TransactionKind) as string[]).includes(upper)) {
      return upper as TransactionKind;
    }
    throw new IngestError(`unknown transaction kind ${raw}`);
  }
  return inflow ? TransactionKind.DEPOSIT : TransactionKind.PURCHASE;
}

/** Resolve an account code to its id, insisting the account actually exists. */
function idFor(chart: ChartOfAccounts, code: string, role: string): AccountId {
  const account = chart.getByCode(code);
  if (!account) throw new IngestError(`${role}: unknown account code ${code}`);
  return account.id;
}

/**
 * The GL accounts the feed maps into. Callers may override any of them by code;
 * the defaults follow the standard chart the COA templates ship (1000 checking,
 * 4000 services income, 6400 office supplies as the catch-all expense, …).
 */
const DEFAULT_CODES: Readonly<Record<keyof AccountMapCodes, string>> = {
  cash: "1000",
  income: "4000",
  expense: "6400",
  fees: "6050",
  payrollExpense: "6200",
  payrollTaxExpense: "6210",
  interestIncome: "4900",
};

interface AccountMapCodes {
  cash: string;
  income: string;
  expense: string;
  fees: string;
  payrollExpense: string;
  payrollTaxExpense: string;
  interestIncome: string;
}

function buildAccountMap(
  chart: ChartOfAccounts,
  overrides: Readonly<Record<string, string>> | undefined,
): AccountMap {
  const code = (k: keyof AccountMapCodes): string => overrides?.[k] ?? DEFAULT_CODES[k];
  return {
    cash: idFor(chart, code("cash"), "cash"),
    income: idFor(chart, code("income"), "income"),
    expense: idFor(chart, code("expense"), "expense"),
    fees: idFor(chart, code("fees"), "fees"),
    payrollExpense: idFor(chart, code("payrollExpense"), "payrollExpense"),
    payrollTaxExpense: idFor(chart, code("payrollTaxExpense"), "payrollTaxExpense"),
    interestIncome: idFor(chart, code("interestIncome"), "interestIncome"),
    mappingVersion: "ledger-service/ingest-1",
  };
}

function buildRules(
  chart: ChartOfAccounts,
  inputs: readonly IngestRuleInput[] | undefined,
): RuleSet | undefined {
  if (!inputs || inputs.length === 0) return undefined;
  const rules: CategoryRule[] = inputs.map((r) => {
    const kind = r.kind ? kindOf(r.kind, false) : undefined;
    return {
      id: r.id,
      priority: r.priority,
      accountId: idFor(chart, r.account_code, `rule ${r.id}`),
      match: {
        ...(r.description_contains ? { descriptionContains: r.description_contains } : {}),
        ...(r.counterparty_equals ? { counterpartyEquals: r.counterparty_equals } : {}),
        ...(kind ? { kind } : {}),
      },
    };
  });
  return new RuleSet(rules);
}

function toCanonical(
  input: IngestTransactionInput,
  tenant: TenantId,
  currency: Currency,
  source: string,
  fetchedAt: string,
): CanonicalTransaction {
  let minor: bigint;
  try {
    minor = BigInt(input.amount_minor);
  } catch {
    throw new IngestError(`transaction ${input.id}: amount_minor is not an integer`);
  }
  const inflow = minor >= 0n;
  const direction: Direction = inflow ? "INFLOW" : "OUTFLOW";
  return {
    id: input.id,
    tenantId: String(tenant),
    accountId: "feed",
    externalId: input.id,
    status: "POSTED",
    date: input.date,
    amount: Money.fromMinorUnits(minor, currency),
    direction,
    description: input.description ?? "",
    ...(input.counterparty ? { counterparty: input.counterparty } : {}),
    kind: kindOf(input.kind, inflow),
    isInternalTransfer: input.is_transfer === true,
    dedupeKey: `${source}:${input.id}`,
    source: {
      provider: source,
      sourceType: "bank.transaction",
      sourceId: input.id,
      sourceVersion: "1",
      fetchedAt,
    },
  };
}

/**
 * Post a batch of feed transactions into the tenant's ledger. Returns the
 * posting report so a caller can show exactly what happened — posted, already
 * there, skipped as a transfer, or deferred because the month is closed.
 */
export async function ingestTransactions(
  request: IngestRequest,
  chart: ChartOfAccounts,
  store: LedgerStore,
  periods: PeriodStore,
  tenant: TenantId,
  currency: Currency,
  postedAt: string,
): Promise<PostingReport> {
  if (!Array.isArray(request.transactions) || request.transactions.length === 0) {
    throw new IngestError("no transactions to ingest");
  }
  for (const t of request.transactions) {
    if (!t || typeof t.id !== "string" || !t.id) throw new IngestError("each transaction needs an id");
    if (!/^\d{4}-\d{2}-\d{2}$/.test(String(t.date))) {
      throw new IngestError(`transaction ${t.id}: date must be YYYY-MM-DD`);
    }
  }
  const source = request.source ?? "feed";
  const map = buildAccountMap(chart, request.accounts);
  const rules = buildRules(chart, request.rules);
  const txns = request.transactions.map((t) => toCanonical(t, tenant, currency, source, postedAt));
  const engine = new PostingEngine(chart, store, periods);
  return postCanonicalToLedger(txns, map, engine, store, {
    postedAt,
    ...(rules ? { rules } : {}),
  });
}
