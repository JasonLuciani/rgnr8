import { Money, USD, sumMoney } from "@rgnr8/ledger-kernel";
import type { CanonicalTransaction } from "@rgnr8/ingestion";
import { isCleanForPublish, type Reconciliation } from "@rgnr8/reconciliation";
import { CONTRACT_VERSION, moneyToDto, type ForecastInputsDTO } from "./dto.js";
import { detectRecurring, type DetectOptions } from "./recurring.js";

export interface BuildOptions {
  readonly currency?: string;
  readonly reconciliations: readonly Reconciliation[];
  readonly transactions: readonly CanonicalTransaction[]; // active canonical
  /** Override the as-of date; defaults to the latest clean statement period end. */
  readonly asOf?: string;
  readonly restrictedMinor?: bigint;
  readonly detect?: DetectOptions;
  /**
   * AR/AP subledger contributions (from @rgnr8/subledger's
   * subledgerForecastParts) — open invoices, customer payment histories, open
   * bills. Merged into the DTO so the forecast gets invoice-level timing.
   */
  readonly subledger?: {
    readonly invoices?: unknown[];
    readonly customer_histories?: unknown[];
    readonly bills?: unknown[];
  };
}

export interface ExcludedAccount {
  readonly accountId: string;
  readonly status: string;
  readonly residual: string;
}

export interface BuildResult {
  readonly dto: ForecastInputsDTO;
  readonly excludedAccounts: readonly ExcludedAccount[];
  readonly notes: readonly string[];
}

/**
 * Build the ForecastInputs DTO from reconciled activity.
 *
 * Only accounts with a clean (BALANCED) reconciliation contribute their balance
 * to opening cash and their transactions to recurring detection. Any account
 * that is out of balance is excluded and reported, and the opening position is
 * marked unverified — the reconciliation gate, enforced at the boundary.
 */
export function buildForecastInputs(opts: BuildOptions): BuildResult {
  const clean = opts.reconciliations.filter(isCleanForPublish);
  const excludedAccounts: ExcludedAccount[] = opts.reconciliations
    .filter((r) => !isCleanForPublish(r))
    .map((r) => ({
      accountId: r.accountId,
      status: r.status,
      residual: r.unreconciledResidual.toDecimalString(),
    }));

  const ccyObj = clean[0]?.statementClosing.currency ?? USD;
  const currency = opts.currency ?? ccyObj.code;

  const asOf =
    opts.asOf ??
    clean.map((r) => r.periodEnd).sort().at(-1);
  if (!asOf) {
    throw new Error("buildForecastInputs: no clean reconciliations and no asOf provided");
  }

  const available =
    clean.length > 0
      ? sumMoney(clean.map((r) => r.statementClosing), ccyObj)
      : Money.zero(ccyObj);

  const cleanAccountIds = new Set(clean.map((r) => r.accountId));
  const cleanTxns = opts.transactions.filter((t) => cleanAccountIds.has(t.accountId));
  const recurring = detectRecurring(cleanTxns, opts.detect);

  const notes: string[] = [];
  if (excludedAccounts.length > 0) {
    notes.push(
      `${excludedAccounts.length} account(s) excluded — not clean-for-publish: ` +
        excludedAccounts.map((e) => e.accountId).join(", "),
    );
  }
  notes.push(`${recurring.length} recurring flow(s) detected from ${cleanTxns.length} reconciled transactions`);
  const sub = opts.subledger;
  if (sub && (sub.invoices?.length || sub.customer_histories?.length || sub.bills?.length)) {
    notes.push(
      `subledger: ${sub.invoices?.length ?? 0} open invoice(s), ` +
        `${sub.customer_histories?.length ?? 0} customer history(ies), ${sub.bills?.length ?? 0} open bill(s)`,
    );
  }

  const restrictedMinor = opts.restrictedMinor ?? 0n;

  const dto: ForecastInputsDTO = {
    contract: CONTRACT_VERSION,
    currency,
    opening: {
      as_of: asOf.slice(0, 10),
      available: moneyToDto(available),
      restricted: { minor: Number(restrictedMinor), currency },
      verified: excludedAccounts.length === 0 && clean.length > 0,
    },
    invoices: opts.subledger?.invoices ?? [],
    customer_histories: opts.subledger?.customer_histories ?? [],
    bills: opts.subledger?.bills ?? [],
    recurring,
    payroll: [],
    debt: [],
    one_time: [],
    pipeline: [],
  };

  return { dto, excludedAccounts, notes };
}

export function toJson(dto: ForecastInputsDTO, pretty = false): string {
  return JSON.stringify(dto, null, pretty ? 2 : undefined);
}
