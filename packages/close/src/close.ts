import type { PeriodRegistry } from "@rgnr8/ledger-kernel";
import type { PeriodKey } from "@rgnr8/ledger-kernel";

export type TaskStatus = "PASSED" | "FAILED";

export interface CloseTask {
  readonly id: string;
  readonly label: string;
  readonly status: TaskStatus;
  readonly detail: string;
}

/** Minimal structural views of upstream results (no hard package deps). */
export interface ReconLike {
  readonly accountId: string;
  readonly status: string; // "BALANCED" | ...
  readonly signOff?: unknown;
}
export interface ControlLike {
  readonly name: string;
  readonly balanced: boolean;
}

export interface CloseGateInputs {
  readonly reconciliations: readonly ReconLike[];
  readonly controls: readonly ControlLike[];
  readonly trialBalanceBalanced: boolean;
  /** Require reviewer sign-off on every reconciliation, not just a clean status. */
  readonly requireSignOff?: boolean;
}

export interface ClosePackage {
  readonly periodKey: PeriodKey;
  readonly tasks: readonly CloseTask[];
  readonly ready: boolean; // all tasks passed
  readonly closed: boolean;
  readonly closedBy?: string;
  readonly closedAt?: string;
  readonly blockers: readonly string[];
}

/** Build the close checklist from the period's control results. */
export function buildCloseChecklist(inputs: CloseGateInputs): CloseTask[] {
  const tasks: CloseTask[] = [];

  const notClean = inputs.reconciliations.filter((r) => r.status !== "BALANCED");
  tasks.push({
    id: "bank_reconciliations",
    label: "All bank/card accounts reconciled",
    status: notClean.length === 0 ? "PASSED" : "FAILED",
    detail:
      notClean.length === 0
        ? `${inputs.reconciliations.length} account(s) reconciled`
        : `out of balance: ${notClean.map((r) => r.accountId).join(", ")}`,
  });

  if (inputs.requireSignOff) {
    const unsigned = inputs.reconciliations.filter((r) => r.signOff == null);
    tasks.push({
      id: "reconciliation_signoff",
      label: "Reconciliations signed off by reviewer",
      status: unsigned.length === 0 ? "PASSED" : "FAILED",
      detail:
        unsigned.length === 0
          ? "all reconciliations signed"
          : `unsigned: ${unsigned.map((r) => r.accountId).join(", ")}`,
    });
  }

  const drifted = inputs.controls.filter((c) => !c.balanced);
  tasks.push({
    id: "control_accounts",
    label: "Subledger control accounts tie to the GL",
    status: drifted.length === 0 ? "PASSED" : "FAILED",
    detail:
      drifted.length === 0 ? "AR/AP controls tied" : `drift: ${drifted.map((c) => c.name).join(", ")}`,
  });

  tasks.push({
    id: "trial_balance",
    label: "Trial balance is in balance",
    status: inputs.trialBalanceBalanced ? "PASSED" : "FAILED",
    detail: inputs.trialBalanceBalanced ? "debits = credits" : "trial balance does not tie",
  });

  return tasks;
}

/**
 * Attempt to close a period. If every checklist task passes, the period is
 * locked in the kernel's PeriodRegistry (which the posting engine enforces —
 * no further postings into a closed period). Otherwise the close is blocked and
 * the failing tasks are returned; nothing is locked.
 */
export function closePeriod(
  periods: PeriodRegistry,
  periodKey: PeriodKey,
  inputs: CloseGateInputs,
  meta: { closedBy: string; at: string },
): ClosePackage {
  const tasks = buildCloseChecklist(inputs);
  const blockers = tasks.filter((t) => t.status === "FAILED").map((t) => t.label);
  const ready = blockers.length === 0;

  if (ready) {
    periods.close(periodKey);
    return {
      periodKey,
      tasks,
      ready,
      closed: true,
      closedBy: meta.closedBy,
      closedAt: meta.at,
      blockers: [],
    };
  }

  return { periodKey, tasks, ready, closed: false, blockers };
}

/** Reopen a closed period (e.g., for a prior-period adjustment). */
export function reopenPeriod(periods: PeriodRegistry, periodKey: PeriodKey): void {
  periods.reopen(periodKey);
}
