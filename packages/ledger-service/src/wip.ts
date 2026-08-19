import {
  Money,
  PostingEngine,
  asIdempotencyKey,
  asPeriodKey,
  type Currency,
  type JournalLineInput,
  type PostCommand,
  type Provenance,
  type TenantId,
} from "@rgnr8/ledger-kernel";
import type { LedgerBackend } from "./backend.js";
import { validateDimensions } from "./dimensions.js";
import { COST_CODE_DIMENSION, JOB_DIMENSION, type JobRecord } from "./jobs.js";

/**
 * Work in progress: percent complete, and the two balance-sheet accounts that
 * exist because billing and building never happen on the same day.
 *
 * A contractor bills a job on a schedule the customer agreed to, and incurs
 * cost on a schedule the weather agreed to. Those never match. Left alone, a
 * job that is 70% built and 40% billed reports a loss, and the same job next
 * month — 75% built, 90% billed — reports a windfall. Neither is true, and a
 * business run off that P&L will hire in the wrong month and panic in the
 * other.
 *
 * Percent complete fixes it by measuring *earning* against cost rather than
 * against invoices:
 *
 *     percent complete = cost to date ÷ estimated total cost
 *     earned revenue   = contract × percent complete
 *
 * and then posting the difference between earned and billed to one of two
 * accounts: **costs in excess of billings** (an asset — work done and not yet
 * invoiced) or **billings in excess of costs** (a liability — money taken for
 * work not yet done). Every month's entry is the *delta* to the correct
 * balance, so the schedule is idempotent and a re-run cannot compound.
 *
 * ## Two methods, because two kinds of job
 *
 * `AS_INCURRED` is the above: cost hits the P&L as it happens, revenue is
 * recognized by percent complete. Right for jobs that span periods.
 *
 * `CAPITALIZE` is the completed-contract method: cost is deferred to Work in
 * Progress and billings to Billings in Excess until the job is finished, at
 * which point both release at once. Right for short jobs where a monthly
 * percent-complete entry is more ceremony than the numbers deserve.
 *
 * ## What is deliberately not automatic
 *
 * A job whose estimated cost exceeds its contract is a loss, and accounting
 * standards say the whole loss is recognized as soon as it is foreseen — not
 * spread out. But "we now think this will cost more than it pays" is a
 * judgement about the future, not a fact in the ledger, and posting it silently
 * would be inventing an opinion. So the schedule reports the projected loss
 * loudly and posts the provision only when asked to.
 */

export class WipError extends Error {}

export const COSTS_IN_EXCESS_CODE = "1250";
export const BILLINGS_IN_EXCESS_CODE = "2450";
export const WORK_IN_PROGRESS_CODE = "1270";
const PPM = 1_000_000n;
const WIP_SOURCE = "wip-adjustment";

export interface WipContext {
  readonly backend: LedgerBackend;
  readonly tenant: TenantId;
  readonly currency: Currency;
  readonly now: () => string;
}

interface JobLedgerView {
  /** Cost on the job, excluding anything a WIP entry itself moved. */
  readonly costToDate: bigint;
  readonly costByAccount: ReadonlyMap<string, bigint>;
  /** Revenue invoiced on the job, excluding WIP revenue adjustments. */
  readonly billed: bigint;
  /** Current balance of the WIP accounts for this job (debit positive). */
  readonly costsInExcess: bigint;
  readonly billingsInExcess: bigint;
  readonly workInProgress: bigint;
}

/**
 * One scan of the journal, from which everything else is derived.
 *
 * WIP entries are excluded from cost and billing on purpose: they are the
 * correction, not the facts being corrected, and counting them would make the
 * next month's adjustment chase its own tail.
 */
async function ledgerView(
  ctx: WipContext, jobId: string, through: string,
): Promise<JobLedgerView> {
  const chart = await ctx.backend.chart(ctx.tenant);
  const entries = await ctx.backend.store(ctx.tenant).list(ctx.tenant);

  let costToDate = 0n;
  let billed = 0n;
  let costsInExcess = 0n;
  let billingsInExcess = 0n;
  let workInProgress = 0n;
  const costByAccount = new Map<string, bigint>();

  for (const entry of entries) {
    if (through && entry.entryDate > through) continue;
    const isWip = entry.provenance.sourceSystem === WIP_SOURCE;
    for (const line of entry.lines) {
      if (line.dimensions?.[JOB_DIMENSION] !== jobId) continue;
      const account = chart.get(line.accountId);
      if (!account) continue;
      const signed = line.side === "DEBIT" ? line.amount.minorUnits : -line.amount.minorUnits;
      if (account.code === COSTS_IN_EXCESS_CODE) costsInExcess += signed;
      else if (account.code === BILLINGS_IN_EXCESS_CODE) billingsInExcess += -signed;
      else if (account.code === WORK_IN_PROGRESS_CODE) workInProgress += signed;
      else if (account.type === "EXPENSE" && !isWip) {
        costToDate += signed;
        costByAccount.set(account.code, (costByAccount.get(account.code) ?? 0n) + signed);
      } else if (account.type === "EXPENSE" && isWip) {
        // A completed-contract deferral credits the cost accounts. It is not a
        // cost fact, but it does have to be remembered so the deferral is not
        // posted twice; that is what the WIP balance is for.
        costByAccount.set(account.code, costByAccount.get(account.code) ?? 0n);
      } else if (account.type === "REVENUE" && !isWip) {
        billed += -signed;
      }
    }
  }
  return { costToDate, costByAccount, billed, costsInExcess, billingsInExcess, workInProgress };
}

export interface WipRow {
  readonly job_id: string;
  readonly name: string;
  readonly status: string;
  readonly billing_method: string;
  readonly cost_method: string;
  readonly contract_minor: string;
  readonly cost_to_date_minor: string;
  readonly estimated_cost_minor: string;
  readonly cost_to_complete_minor: string;
  readonly percent_complete_ppm: number;
  readonly earned_revenue_minor: string;
  readonly billed_minor: string;
  /** Work done and not yet invoiced — an asset. */
  readonly under_billed_minor: string;
  /** Invoiced ahead of the work — a liability. */
  readonly over_billed_minor: string;
  readonly gross_profit_minor: string;
  /** What the balance sheet currently says, before any adjustment. */
  readonly costs_in_excess_minor: string;
  readonly billings_in_excess_minor: string;
  readonly work_in_progress_minor: string;
  /** What this month's entry would move. */
  readonly adjustment_minor: string;
  /** Cost has passed the estimate: the percentage is capped and this is set. */
  readonly estimate_exceeded: boolean;
  /** Estimated cost is above the contract — the job is expected to lose money. */
  readonly projected_loss_minor: string;
}

export interface WipSchedule {
  readonly contract: "wip-schedule/1";
  readonly currency: string;
  readonly through: string;
  readonly rows: readonly WipRow[];
  readonly totals: Record<string, string>;
}

function ppmOf(part: bigint, whole: bigint): number {
  if (whole <= 0n) return 0;
  return Number((part * PPM) / whole);
}

interface JobMath {
  readonly job: JobRecord;
  readonly view: JobLedgerView;
  readonly estimated: bigint;
  readonly percentPpm: number;
  readonly earned: bigint;
  readonly targetCostsInExcess: bigint;
  readonly targetBillingsInExcess: bigint;
  readonly targetWorkInProgress: bigint;
  readonly targetDeferredBillings: bigint;
}

async function jobMath(ctx: WipContext, job: JobRecord, through: string): Promise<JobMath> {
  const view = await ledgerView(ctx, job.id, through);
  const budget = await ctx.backend.jobs().listBudget(String(ctx.tenant), job.id);
  const budgeted = budget.reduce((acc, b) => acc + BigInt(b.revisedCostMinor), 0n);
  // No budget means the only estimate available is what has been spent, which
  // makes the job 100% complete by definition. That is the honest answer: a job
  // with no estimate cannot have a percent complete.
  const estimated = budgeted > 0n ? budgeted : view.costToDate;
  const contract = BigInt(job.contractMinor);

  const rawPpm = ppmOf(view.costToDate, estimated);
  const percentPpm = Math.min(rawPpm, 1_000_000);
  const earned = contract * BigInt(percentPpm) / PPM;

  const complete = job.status === "COMPLETE" || job.status === "CLOSED";
  const capitalize = job.costMethod === "CAPITALIZE";

  return {
    job,
    view,
    estimated,
    percentPpm,
    earned,
    // Percent-complete jobs carry the difference between earned and billed.
    targetCostsInExcess: capitalize ? 0n : (earned > view.billed ? earned - view.billed : 0n),
    targetBillingsInExcess: capitalize ? 0n : (view.billed > earned ? view.billed - earned : 0n),
    // Completed-contract jobs defer everything until they are done.
    targetWorkInProgress: capitalize && !complete ? view.costToDate : 0n,
    targetDeferredBillings: capitalize && !complete ? view.billed : 0n,
  };
}

/**
 * The WIP schedule — one line per job, and the single most useful page a
 * contractor's accountant will ask for.
 */
export async function wipSchedule(
  ctx: WipContext, throughRaw?: unknown,
): Promise<WipSchedule> {
  const through = String(throughRaw ?? "").trim();
  if (through && !/^\d{4}-\d{2}-\d{2}$/.test(through)) {
    throw new WipError("through must be YYYY-MM-DD");
  }
  const jobs = await ctx.backend.jobs().listJobs(String(ctx.tenant));
  const rows: WipRow[] = [];
  let contractTotal = 0n;
  let costTotal = 0n;
  let earnedTotal = 0n;
  let billedTotal = 0n;
  let underTotal = 0n;
  let overTotal = 0n;

  for (const job of jobs) {
    if (job.status === "ESTIMATING" || job.status === "CLOSED") continue;
    const m = await jobMath(ctx, job, through);
    const contract = BigInt(job.contractMinor);
    const capitalize = job.costMethod === "CAPITALIZE";

    // The P&L impact: what the entry would add to (or take off) revenue. An
    // asset going up and a liability going down both mean revenue recognized,
    // which is why the two deltas subtract rather than add.
    const adjustment = capitalize
      ? (m.targetWorkInProgress - m.view.workInProgress)
      : (m.targetCostsInExcess - m.view.costsInExcess)
        - (m.targetBillingsInExcess - m.view.billingsInExcess);

    contractTotal += contract;
    costTotal += m.view.costToDate;
    earnedTotal += m.earned;
    billedTotal += m.view.billed;
    underTotal += m.targetCostsInExcess;
    overTotal += m.targetBillingsInExcess;

    rows.push({
      job_id: job.id,
      name: job.name,
      status: job.status,
      billing_method: job.billingMethod,
      cost_method: job.costMethod,
      contract_minor: job.contractMinor,
      cost_to_date_minor: m.view.costToDate.toString(),
      estimated_cost_minor: m.estimated.toString(),
      cost_to_complete_minor: (m.estimated > m.view.costToDate
        ? m.estimated - m.view.costToDate
        : 0n).toString(),
      percent_complete_ppm: m.percentPpm,
      earned_revenue_minor: m.earned.toString(),
      billed_minor: m.view.billed.toString(),
      under_billed_minor: m.targetCostsInExcess.toString(),
      over_billed_minor: m.targetBillingsInExcess.toString(),
      gross_profit_minor: (m.earned - m.view.costToDate).toString(),
      costs_in_excess_minor: m.view.costsInExcess.toString(),
      billings_in_excess_minor: m.view.billingsInExcess.toString(),
      work_in_progress_minor: m.view.workInProgress.toString(),
      adjustment_minor: adjustment.toString(),
      estimate_exceeded: m.view.costToDate > m.estimated,
      projected_loss_minor: (m.estimated > contract ? m.estimated - contract : 0n).toString(),
    });
  }

  return {
    contract: "wip-schedule/1",
    currency: ctx.currency.code,
    through,
    rows,
    totals: {
      contract_minor: contractTotal.toString(),
      cost_to_date_minor: costTotal.toString(),
      earned_revenue_minor: earnedTotal.toString(),
      billed_minor: billedTotal.toString(),
      under_billed_minor: underTotal.toString(),
      over_billed_minor: overTotal.toString(),
      gross_profit_minor: (earnedTotal - costTotal).toString(),
    },
  };
}

function provenanceFor(date: string, at: string): Provenance {
  return {
    sourceSystem: WIP_SOURCE,
    sourceObject: "wip",
    sourceVersion: "1",
    effectiveDate: date,
    postedDate: date,
    ingestedAt: at,
    normalizationVersion: "ledger-service/wip-1",
    mappingVersion: "ledger-service/wip-1",
  };
}

/** Split an amount across accounts in proportion to their share, exactly. */
function allocate(
  total: bigint, weights: ReadonlyMap<string, bigint>,
): Map<string, bigint> {
  const out = new Map<string, bigint>();
  let weightTotal = 0n;
  for (const w of weights.values()) weightTotal += w;
  if (weightTotal <= 0n) return out;
  let assigned = 0n;
  let largest = "";
  let largestWeight = -1n;
  for (const [code, weight] of weights) {
    const share = total * weight / weightTotal;
    out.set(code, share);
    assigned += share;
    if (weight > largestWeight) {
      largestWeight = weight;
      largest = code;
    }
  }
  // The rounding remainder goes to the biggest account, so the entry balances
  // to the cent rather than to the nearest plausible number.
  if (assigned !== total && largest) {
    out.set(largest, (out.get(largest) ?? 0n) + (total - assigned));
  }
  return out;
}

export interface WipPostInput {
  readonly date?: string;
  readonly through?: string;
  readonly job_id?: string;
  readonly memo?: string;
  /**
   * Also post the full provision on jobs expected to lose money. Off by
   * default: a forecast loss is a judgement about the future, not a fact in the
   * ledger.
   */
  readonly include_loss_provision?: boolean;
}

export interface WipPostResult {
  readonly entry_id: string;
  readonly posted: boolean;
  readonly jobs: ReadonlyArray<{
    readonly job_id: string;
    readonly adjustment_minor: string;
    readonly loss_provision_minor: string;
  }>;
  readonly reason: string;
}

/**
 * Post the adjustment that makes the balance sheet agree with the schedule.
 *
 * The entry is the **delta** to the correct balance, never a fresh accrual, so
 * running it twice in a month is a no-op rather than a doubling — and running
 * it after a late cost lands simply corrects the difference.
 */
export async function postWipAdjustment(
  ctx: WipContext, input: WipPostInput,
): Promise<WipPostResult> {
  const date = String(input.date ?? input.through ?? "").trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) throw new WipError("date must be YYYY-MM-DD");
  const through = String(input.through ?? date).trim();

  const chart = await ctx.backend.chart(ctx.tenant);
  const need = (code: string) => {
    const account = chart.getByCode(code);
    if (!account) {
      throw new WipError(
        `this chart has no account ${code} — create a job to have the job-costing accounts added`,
      );
    }
    return account;
  };

  const jobs = (await ctx.backend.jobs().listJobs(String(ctx.tenant)))
    .filter((j) => j.status !== "ESTIMATING")
    .filter((j) => !input.job_id || j.id === input.job_id);

  const lines: JournalLineInput[] = [];
  const summary: { job_id: string; adjustment_minor: string; loss_provision_minor: string }[] = [];

  for (const job of jobs) {
    const m = await jobMath(ctx, job, through);
    const dims = { [JOB_DIMENSION]: job.id };
    const money = (minor: bigint) => Money.fromMinorUnits(minor < 0n ? -minor : minor, ctx.currency);
    let adjustment = 0n;
    let lossProvision = 0n;

    if (job.costMethod === "CAPITALIZE") {
      // Completed contract: defer cost to WIP and billings to a liability until
      // the job is finished, then let both go at once.
      const costDelta = m.targetWorkInProgress - m.view.workInProgress;
      if (costDelta !== 0n) {
        adjustment += costDelta;
        lines.push({
          accountId: need(WORK_IN_PROGRESS_CODE).id,
          side: costDelta > 0n ? "DEBIT" : "CREDIT",
          amount: money(costDelta),
          memo: `${job.name} — cost deferred`,
          dimensions: dims,
        });
        const weights = new Map<string, bigint>();
        for (const [code, amount] of m.view.costByAccount) {
          if (amount > 0n) weights.set(code, amount);
        }
        if (weights.size === 0) {
          throw new WipError(`${job.id}: no cost accounts to defer against`);
        }
        for (const [code, share] of allocate(costDelta, weights)) {
          if (share === 0n) continue;
          lines.push({
            accountId: need(code).id,
            side: costDelta > 0n ? "CREDIT" : "DEBIT",
            amount: money(share),
            memo: `${job.name} — cost deferred`,
            dimensions: { ...dims, [COST_CODE_DIMENSION]: "" },
          });
        }
      }
      const billDelta = m.targetDeferredBillings - m.view.billingsInExcess;
      if (billDelta !== 0n) {
        lines.push({
          accountId: need(BILLINGS_IN_EXCESS_CODE).id,
          side: billDelta > 0n ? "CREDIT" : "DEBIT",
          amount: money(billDelta),
          memo: `${job.name} — billings deferred`,
          dimensions: dims,
        });
        lines.push({
          accountId: need(job.revenueAccountCode).id,
          side: billDelta > 0n ? "DEBIT" : "CREDIT",
          amount: money(billDelta),
          memo: `${job.name} — billings deferred`,
          dimensions: dims,
        });
      }
    } else {
      const underDelta = m.targetCostsInExcess - m.view.costsInExcess;
      const overDelta = m.targetBillingsInExcess - m.view.billingsInExcess;
      // Debits from the asset line, credits from the liability line: the
      // revenue side is what balances them, so the deltas subtract.
      adjustment = underDelta - overDelta;
      if (underDelta !== 0n) {
        lines.push({
          accountId: need(COSTS_IN_EXCESS_CODE).id,
          side: underDelta > 0n ? "DEBIT" : "CREDIT",
          amount: money(underDelta),
          memo: `${job.name} — earned, not yet billed`,
          dimensions: dims,
        });
      }
      if (overDelta !== 0n) {
        lines.push({
          accountId: need(BILLINGS_IN_EXCESS_CODE).id,
          side: overDelta > 0n ? "CREDIT" : "DEBIT",
          amount: money(overDelta),
          memo: `${job.name} — billed ahead of the work`,
          dimensions: dims,
        });
      }
      if (adjustment !== 0n) {
        lines.push({
          accountId: need(job.revenueAccountCode).id,
          side: adjustment > 0n ? "CREDIT" : "DEBIT",
          amount: money(adjustment),
          memo: `${job.name} — percent complete`,
          dimensions: dims,
        });
      }

      const contract = BigInt(job.contractMinor);
      if (input.include_loss_provision === true && m.estimated > contract && contract > 0n) {
        // The whole foreseen loss, less the part already in the numbers.
        const total = m.estimated - contract;
        const recognized = m.earned - m.view.costToDate < 0n
          ? m.view.costToDate - m.earned
          : 0n;
        lossProvision = total - recognized;
        if (lossProvision > 0n) {
          lines.push({
            accountId: need(COSTS_IN_EXCESS_CODE).id,
            side: "CREDIT",
            amount: money(lossProvision),
            memo: `${job.name} — provision for loss on contract`,
            dimensions: dims,
          });
          lines.push({
            accountId: need(job.revenueAccountCode).id,
            side: "DEBIT",
            amount: money(lossProvision),
            memo: `${job.name} — provision for loss on contract`,
            dimensions: dims,
          });
        }
      }
    }

    if (adjustment !== 0n || lossProvision !== 0n) {
      summary.push({
        job_id: job.id,
        adjustment_minor: adjustment.toString(),
        loss_provision_minor: lossProvision.toString(),
      });
    }
  }

  if (lines.length === 0) {
    return {
      entry_id: "",
      posted: false,
      jobs: [],
      reason: "the balance sheet already agrees with the schedule — nothing to post",
    };
  }

  // The cost-deferral lines carry an empty cost code, which is meaningless on a
  // journal line; strip it rather than validate a blank.
  const cleaned = lines.map((l) => {
    if (!l.dimensions) return l;
    const dimensions = Object.fromEntries(
      Object.entries(l.dimensions).filter(([, v]) => v !== ""),
    );
    return { ...l, dimensions };
  });

  // Re-running WIP for the same period-end after a late cost must post the
  // *incremental* correction, not throw. A pure date key made the second run
  // collide (same key, different payload → the engine rejects it), which broke
  // the module's own promise that a late cost "simply corrects the difference".
  // Suffix the key with how many WIP entries already exist for this date/scope:
  // a genuine retry computes the same count (idempotent no-op), while a fresh
  // correction after a real change gets the next number (a new delta entry).
  const priorRuns = (await ctx.backend.store(ctx.tenant).list(ctx.tenant)).filter(
    (e) => e.provenance.sourceSystem === WIP_SOURCE && e.entryDate === date
      && (!input.job_id || e.lines.some((l) => l.dimensions?.[JOB_DIMENSION] === input.job_id)),
  ).length;

  const command: PostCommand = {
    tenantId: ctx.tenant,
    idempotencyKey: asIdempotencyKey(
      `wip:${input.job_id ? `${input.job_id}:` : ""}${date}#${priorRuns}`,
    ),
    periodKey: asPeriodKey(date.slice(0, 7)),
    currency: ctx.currency,
    entryDate: date,
    memo: String(input.memo ?? "") || `Work in progress through ${through}`,
    provenance: provenanceFor(date, ctx.now()),
    lines: cleaned,
  };
  await validateDimensions(
    { backend: ctx.backend, tenant: ctx.tenant, currency: ctx.currency }, command,
  );
  const engine = new PostingEngine(
    chart, ctx.backend.store(ctx.tenant), ctx.backend.periods(ctx.tenant),
  );
  const entry = await engine.post(command, { postedAt: ctx.now() });
  return {
    entry_id: String(entry.id),
    posted: true,
    jobs: summary,
    reason: "",
  };
}
