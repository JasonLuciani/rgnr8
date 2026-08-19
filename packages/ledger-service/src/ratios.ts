import { AccountSubtype, type Currency, type TenantId } from "@rgnr8/ledger-kernel";
import type { LedgerBackend } from "./backend.js";
import { debtDashboard, type DebtContext } from "./debt.js";

/**
 * Management KPIs and financial ratios — the "am I healthy?" answer, in numbers
 * a lender recognises and a plain-language read an owner does.
 *
 * Xero shipped an analytics tier for exactly this; QuickBooks Online barely
 * attempts it. Everything here is derived from the same account balances the
 * statements use (classified by subtype), plus the debt module's annual service
 * for the coverage ratios — so the health read and the financial statements can
 * never disagree. Ratios are carried as micro (×1e6) integers with a formatted
 * string beside them; a division by zero yields null rather than a fake number.
 */

export class RatiosError extends Error {}

const MICRO = 1_000_000n;

function ratioMicro(num: bigint, den: bigint): string | null {
  if (den === 0n) return null;
  // round to the nearest micro
  const neg = (num < 0n) !== (den < 0n);
  const a = num < 0n ? -num : num;
  const b = den < 0n ? -den : den;
  const q = (a * MICRO + b / 2n) / b;
  return (neg ? -q : q).toString();
}

function fmt(micro: string | null, places = 2): string | null {
  if (micro === null) return null;
  const v = BigInt(micro);
  const neg = v < 0n;
  const abs = neg ? -v : v;
  const whole = abs / MICRO;
  const frac = abs % MICRO;
  const fracStr = frac.toString().padStart(6, "0").slice(0, places);
  return `${neg ? "-" : ""}${whole}${places > 0 ? `.${fracStr}` : ""}`;
}

function pct(micro: string | null): string | null {
  if (micro === null) return null;
  // micro is a fraction ×1e6; as a percent that's ×100
  const v = (BigInt(micro) * 100n);
  return fmt(v.toString(), 2);
}

const CURRENT_ASSET_SUBTYPES = new Set<AccountSubtype>([
  AccountSubtype.BANK,
  AccountSubtype.ACCOUNTS_RECEIVABLE,
  AccountSubtype.UNDEPOSITED_FUNDS,
  AccountSubtype.INVENTORY,
  AccountSubtype.OTHER_CURRENT_ASSET,
]);

const CURRENT_LIABILITY_SUBTYPES = new Set<AccountSubtype>([
  AccountSubtype.ACCOUNTS_PAYABLE,
  AccountSubtype.CREDIT_CARD,
  AccountSubtype.SALES_TAX_PAYABLE,
  AccountSubtype.OTHER_CURRENT_LIABILITY,
]);

export interface RatiosContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

interface Buckets {
  currentAssets: bigint;
  inventory: bigint;
  totalAssets: bigint;
  currentLiabilities: bigint;
  longTermLiabilities: bigint;
  totalLiabilities: bigint;
  equity: bigint;
  revenue: bigint;
  cogs: bigint;
  opex: bigint;
  interest: bigint;
  depreciation: bigint;
}

/**
 * Compute the ratio set. Balances are summed from the journal and classified by
 * the chart's subtype; current-year net income is folded into equity (it has not
 * been closed to retained earnings). Interest and depreciation are recognised by
 * account code/name so the coverage ratios can add them back.
 */
export async function financialRatios(
  ctx: RatiosContext, asOf?: string,
): Promise<Record<string, unknown>> {
  const asOfDate = asOf && /^\d{4}-\d{2}-\d{2}$/.test(asOf) ? asOf : ctx.now().slice(0, 10);
  const chart = await ctx.backend.chart(ctx.tenant);

  // signed balance per account (debit − credit)
  const signed = new Map<string, bigint>();
  for (const entry of await ctx.backend.store(ctx.tenant).list(ctx.tenant)) {
    if (entry.entryDate > asOfDate) continue;
    for (const line of entry.lines) {
      const delta = line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
      signed.set(String(line.accountId), (signed.get(String(line.accountId)) ?? 0n) + delta);
    }
  }

  const b: Buckets = {
    currentAssets: 0n, inventory: 0n, totalAssets: 0n,
    currentLiabilities: 0n, longTermLiabilities: 0n, totalLiabilities: 0n,
    equity: 0n, revenue: 0n, cogs: 0n, opex: 0n, interest: 0n, depreciation: 0n,
  };

  for (const account of chart.list()) {
    const raw = signed.get(String(account.id)) ?? 0n;
    if (raw === 0n) continue;
    const subtype = account.subtype;
    const name = account.name.toLowerCase();
    switch (account.type) {
      case "ASSET": {
        b.totalAssets += raw; // debit-normal
        if (subtype && CURRENT_ASSET_SUBTYPES.has(subtype)) b.currentAssets += raw;
        if (subtype === AccountSubtype.INVENTORY) b.inventory += raw;
        break;
      }
      case "LIABILITY": {
        const amt = -raw; // credit-normal
        b.totalLiabilities += amt;
        if (subtype === AccountSubtype.LONG_TERM_LIABILITY) b.longTermLiabilities += amt;
        else if (subtype && CURRENT_LIABILITY_SUBTYPES.has(subtype)) b.currentLiabilities += amt;
        else b.currentLiabilities += amt; // unknown liability subtype → treat as current
        break;
      }
      case "EQUITY":
        b.equity += -raw;
        break;
      case "REVENUE":
        b.revenue += -raw;
        break;
      case "EXPENSE": {
        const amt = raw; // debit-normal
        if (subtype === AccountSubtype.COST_OF_GOODS_SOLD) b.cogs += amt;
        else b.opex += amt;
        if (account.code === "6950" || name.includes("interest")) b.interest += amt;
        if (account.code === "6900" || name.includes("depreciation")) b.depreciation += amt;
        break;
      }
    }
  }

  const netIncome = b.revenue - b.cogs - b.opex;
  const equityWithIncome = b.equity + netIncome; // current earnings not yet closed
  const grossProfit = b.revenue - b.cogs;
  const ebit = netIncome + b.interest; // add interest back
  const ebitda = ebit + b.depreciation;

  // annual debt service from the debt module (0 if no loans)
  const debtCtx: DebtContext = {
    backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency, now: ctx.now,
  };
  const dash = await debtDashboard(debtCtx, asOfDate);
  const annualDebtService = BigInt(String((dash["totals"] as Record<string, unknown>)["annual_debt_service_minor"] ?? "0"));

  const currentRatio = ratioMicro(b.currentAssets, b.currentLiabilities);
  const quickRatio = ratioMicro(b.currentAssets - b.inventory, b.currentLiabilities);
  const debtToEquity = ratioMicro(b.totalLiabilities, equityWithIncome);
  const debtToAssets = ratioMicro(b.totalLiabilities, b.totalAssets);
  const grossMargin = ratioMicro(grossProfit, b.revenue);
  const netMargin = ratioMicro(netIncome, b.revenue);
  const roa = ratioMicro(netIncome, b.totalAssets);
  const roe = ratioMicro(netIncome, equityWithIncome);
  const interestCoverage = ratioMicro(ebit, b.interest);
  const dscr = ratioMicro(ebitda, annualDebtService);

  // covenant checks against per-loan minimum DSCR
  const loans = (await ctx.backend.debt().listLoans(String(ctx.tenant))).filter((l) => l.active);
  const covenants = loans
    .filter((l) => BigInt(l.minDscrMicro) > 0n)
    .map((l) => {
      const min = BigInt(l.minDscrMicro);
      const actual = dscr === null ? null : BigInt(dscr);
      return {
        loan_id: l.id,
        min_dscr_micro: l.minDscrMicro,
        actual_dscr_micro: dscr,
        breached: actual !== null && actual < min,
      };
    });

  return {
    contract: "financial-ratios/1",
    as_of: asOfDate,
    currency: ctx.currency.code,
    inputs: {
      current_assets_minor: b.currentAssets.toString(),
      inventory_minor: b.inventory.toString(),
      total_assets_minor: b.totalAssets.toString(),
      current_liabilities_minor: b.currentLiabilities.toString(),
      total_liabilities_minor: b.totalLiabilities.toString(),
      equity_minor: equityWithIncome.toString(),
      revenue_minor: b.revenue.toString(),
      cogs_minor: b.cogs.toString(),
      operating_expense_minor: b.opex.toString(),
      net_income_minor: netIncome.toString(),
      interest_expense_minor: b.interest.toString(),
      depreciation_minor: b.depreciation.toString(),
      ebitda_minor: ebitda.toString(),
      annual_debt_service_minor: annualDebtService.toString(),
    },
    liquidity: {
      current_ratio_micro: currentRatio,
      current_ratio: fmt(currentRatio),
      quick_ratio_micro: quickRatio,
      quick_ratio: fmt(quickRatio),
      working_capital_minor: (b.currentAssets - b.currentLiabilities).toString(),
      health: healthLiquidity(currentRatio),
    },
    leverage: {
      debt_to_equity_micro: debtToEquity,
      debt_to_equity: fmt(debtToEquity),
      debt_to_assets_micro: debtToAssets,
      debt_to_assets: fmt(debtToAssets),
      interest_coverage_micro: interestCoverage,
      interest_coverage: fmt(interestCoverage),
      dscr_micro: dscr,
      dscr: fmt(dscr),
      health: healthLeverage(debtToEquity, dscr),
    },
    profitability: {
      gross_margin_pct: pct(grossMargin),
      net_margin_pct: pct(netMargin),
      return_on_assets_pct: pct(roa),
      return_on_equity_pct: pct(roe),
      health: healthProfitability(netMargin),
    },
    covenants,
  };
}

function healthLiquidity(currentRatioMicro: string | null): string {
  if (currentRatioMicro === null) return "no current liabilities";
  const v = BigInt(currentRatioMicro);
  if (v >= 1_500_000n) return "healthy — comfortably covers short-term obligations";
  if (v >= 1_000_000n) return "adequate — current assets just cover current liabilities";
  return "tight — current liabilities exceed current assets";
}

function healthLeverage(debtToEquityMicro: string | null, dscrMicro: string | null): string {
  const dscr = dscrMicro === null ? null : BigInt(dscrMicro);
  if (dscr !== null && dscr < 1_000_000n) return "at risk — earnings do not cover debt service";
  if (debtToEquityMicro === null) return "unlevered";
  const de = BigInt(debtToEquityMicro);
  if (de < 1_000_000n) return "conservative — more equity than debt";
  if (de <= 2_000_000n) return "moderate — debt roughly one to two times equity";
  return "high — debt well above equity";
}

function healthProfitability(netMarginMicro: string | null): string {
  if (netMarginMicro === null) return "no revenue yet";
  const v = BigInt(netMarginMicro);
  if (v < 0n) return "unprofitable — expenses exceed revenue";
  if (v >= 100_000n) return "strong — over 10% of revenue reaches the bottom line";
  return "thin — a small share of revenue reaches the bottom line";
}
