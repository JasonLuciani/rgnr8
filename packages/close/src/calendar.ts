/**
 * The close calendar — sequencing the *work* of a month-end close.
 *
 * `close.ts` is the **gate** (are the controls clean enough to lock the period?).
 * This is the **schedule**: the ordered tasks, who owns each, when each is due
 * (business days after period end, skipping weekends + holidays), and what
 * blocks what. Given the completed set and a `now`, it reports each task's state
 * — done / overdue / due-today / blocked / upcoming — so a bookkeeper (or the
 * owner) always knows what's next and what's late.
 *
 * Deterministic: all dates are ISO strings and business-day math is explicit;
 * holidays are injected, nothing is read from a wall clock.
 */

import { RG, RG_BASE_CSS, RG_TOKENS_CSS, brandBar } from "@rgnr8/ledger-kernel";

export type OwnerRole = "system" | "bookkeeper" | "controller" | "owner";

export interface CloseTaskTemplate {
  readonly id: string;
  readonly label: string;
  readonly owner: OwnerRole;
  /** Due this many business days after period end (0 = period-end day itself). */
  readonly offsetBusinessDays: number;
  /** Task ids that must be complete before this one can start. */
  readonly dependsOn?: readonly string[];
}

/**
 * The standard RGNR8 close, in order. Mirrors the pipeline: pull data, reconcile,
 * tie the subledgers, review, prove the trial balance, publish statements, seal
 * the immutable package, deliver.
 */
export const DEFAULT_CLOSE_TEMPLATE: readonly CloseTaskTemplate[] = [
  { id: "sync", label: "Sync bank, card & payroll feeds", owner: "system", offsetBusinessDays: 1 },
  { id: "reconcile_bank", label: "Reconcile all bank & card accounts", owner: "bookkeeper", offsetBusinessDays: 3, dependsOn: ["sync"] },
  { id: "reconcile_subledgers", label: "Tie AR/AP subledgers to the GL", owner: "bookkeeper", offsetBusinessDays: 3, dependsOn: ["sync"] },
  { id: "review_adjustments", label: "Review accruals & adjusting entries", owner: "controller", offsetBusinessDays: 4, dependsOn: ["reconcile_bank", "reconcile_subledgers"] },
  { id: "trial_balance", label: "Confirm the trial balance ties", owner: "controller", offsetBusinessDays: 5, dependsOn: ["review_adjustments"] },
  { id: "statements", label: "Publish income statement & balance sheet", owner: "controller", offsetBusinessDays: 5, dependsOn: ["trial_balance"] },
  { id: "seal_package", label: "Seal the immutable financial package", owner: "controller", offsetBusinessDays: 6, dependsOn: ["statements"] },
  { id: "deliver", label: "Deliver the close summary to the owner", owner: "system", offsetBusinessDays: 6, dependsOn: ["seal_package"] },
];

const DAY_MS = 86_400_000;

function isoDay(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/** Add `n` business days to an ISO date, skipping Sat/Sun and any holiday. */
export function addBusinessDays(
  fromIso: string,
  n: number,
  holidays: ReadonlySet<string> = new Set(),
): string {
  let d = new Date(`${fromIso}T00:00:00Z`);
  let added = 0;
  while (added < n) {
    d = new Date(d.getTime() + DAY_MS);
    const dow = d.getUTCDay();
    if (dow !== 0 && dow !== 6 && !holidays.has(isoDay(d))) added += 1;
  }
  return isoDay(d);
}

export interface CloseCalendarEntry extends CloseTaskTemplate {
  readonly dueDate: string; // ISO
}

export interface BuildCalendarOptions {
  readonly holidays?: readonly string[];
  readonly template?: readonly CloseTaskTemplate[];
}

/** Compute due dates for a period's close tasks (period end + business-day offset). */
export function buildCloseCalendar(
  periodEnd: string,
  opts: BuildCalendarOptions = {},
): CloseCalendarEntry[] {
  const holidays = new Set(opts.holidays ?? []);
  const template = opts.template ?? DEFAULT_CLOSE_TEMPLATE;
  return template.map((t) => ({ ...t, dueDate: addBusinessDays(periodEnd, t.offsetBusinessDays, holidays) }));
}

export type TaskState = "done" | "blocked" | "overdue" | "due_today" | "upcoming";

export interface CloseTaskStatus extends CloseCalendarEntry {
  readonly state: TaskState;
  /** For a blocked task, which dependencies are still outstanding. */
  readonly blockedBy: readonly string[];
}

export interface CloseCalendarStatus {
  readonly asOf: string;
  readonly tasks: readonly CloseTaskStatus[];
  readonly done: number;
  readonly overdue: number;
  readonly blocked: number;
  readonly remaining: number;
  readonly complete: boolean;
  /** The next actionable task (earliest due among not-done, not-blocked), if any. */
  readonly nextUp: CloseTaskStatus | null;
}

/**
 * Classify each task given the completed set and `asOf` (both ISO). A task is
 * `done` if completed; else `blocked` if any dependency isn't done; else
 * `overdue`/`due_today`/`upcoming` by comparing its due date to `asOf`.
 */
export function closeCalendarStatus(
  entries: readonly CloseCalendarEntry[],
  asOf: string,
  completed: readonly string[] = [],
): CloseCalendarStatus {
  const doneSet = new Set(completed);
  const tasks: CloseTaskStatus[] = entries.map((e) => {
    if (doneSet.has(e.id)) {
      return { ...e, state: "done", blockedBy: [] };
    }
    const blockedBy = (e.dependsOn ?? []).filter((d) => !doneSet.has(d));
    if (blockedBy.length > 0) {
      return { ...e, state: "blocked", blockedBy };
    }
    let state: TaskState;
    if (e.dueDate < asOf) state = "overdue";
    else if (e.dueDate === asOf) state = "due_today";
    else state = "upcoming";
    return { ...e, state, blockedBy: [] };
  });

  const done = tasks.filter((t) => t.state === "done").length;
  const overdue = tasks.filter((t) => t.state === "overdue").length;
  const blocked = tasks.filter((t) => t.state === "blocked").length;
  const actionable = tasks
    .filter((t) => t.state === "overdue" || t.state === "due_today" || t.state === "upcoming")
    .sort((a, b) => (a.dueDate < b.dueDate ? -1 : a.dueDate > b.dueDate ? 1 : 0));

  return {
    asOf,
    tasks,
    done,
    overdue,
    blocked,
    remaining: tasks.length - done,
    complete: done === tasks.length,
    nextUp: actionable[0] ?? null,
  };
}

// --- ops-status/1 serializer (closes the cross-language ops loop) -------------

/** The close fragment of a tenant's operational status, as the Python
 * `rgnr8_ops.CloseProgress` consumes it. `period` is the period key (e.g.
 * "2026-08"); if omitted it's derived from the next-up/first task's due month.
 * Pair with the connector fragment (`opsConnectorStatus` in @rgnr8/connectors)
 * under `{connectors, close}` for `Fleet.set_status_from_json`. */
export interface OpsCloseStatusJson {
  readonly period: string;
  readonly total: number;
  readonly done: number;
  readonly overdue: number;
  readonly blocked: number;
  readonly next_task: string | null;
  readonly next_due: string | null;
}

export function opsCloseStatus(status: CloseCalendarStatus, period?: string): OpsCloseStatusJson {
  const anchor = status.nextUp?.dueDate ?? status.tasks[0]?.dueDate ?? status.asOf;
  return {
    period: period ?? anchor.slice(0, 7),
    total: status.tasks.length,
    done: status.done,
    overdue: status.overdue,
    blocked: status.blocked,
    next_task: status.nextUp?.label ?? null,
    next_due: status.nextUp?.dueDate ?? null,
  };
}

// --- HTML render -------------------------------------------------------------

const STATE_COLOR: Readonly<Record<TaskState, string>> = {
  done: RG.positive,
  overdue: RG.risk,
  due_today: RG.watch,
  blocked: RG.muted,
  upcoming: RG.accent,
};

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function renderCloseCalendarHtml(status: CloseCalendarStatus): string {
  const rows = status.tasks
    .map(
      (t) => `<tr>
      <td>${esc(t.dueDate)}</td>
      <td>${esc(t.label)}</td>
      <td>${esc(t.owner)}</td>
      <td><span class="dot" style="background:${STATE_COLOR[t.state]}"></span>${t.state.replace("_", " ")}${t.blockedBy.length ? ` (needs ${t.blockedBy.map(esc).join(", ")})` : ""}</td>
    </tr>`,
    )
    .join("\n");
  const banner = status.complete
    ? `<div class="banner good">Close complete — all ${status.tasks.length} tasks done.</div>`
    : status.overdue > 0
      ? `<div class="banner warn">${status.overdue} task(s) overdue${status.nextUp ? `; next up: ${esc(status.nextUp.label)} (due ${esc(status.nextUp.dueDate)})` : ""}.</div>`
      : `<div class="banner ok">${status.remaining} of ${status.tasks.length} remaining${status.nextUp ? `; next up: ${esc(status.nextUp.label)} (due ${esc(status.nextUp.dueDate)})` : ""}.</div>`;
  return `<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Close calendar</title>
<style>
${RG_TOKENS_CSS}
${RG_BASE_CSS}
  .wrap{max-width:900px;margin:0 auto;padding:24px 20px 56px}
  h1{font-size:22px;margin:0 0 2px;font-weight:800;letter-spacing:-.01em} .sub{color:var(--rg-muted);margin:0 0 12px;font-size:13px}
  .banner{padding:12px 16px;border-radius:var(--rg-radius);margin-bottom:12px;font-weight:700;font-size:13px;border:1px solid transparent}
  .good{background:#E7F6EF;color:#0A6E4B;border-color:#BFE7D6}
  .warn{background:#FDECEC;color:#B02A2F;border-color:#F5C4C6}
  .ok{background:#EEF0FF;color:#3325D6;border-color:#D6CEFF}
  table{border-collapse:separate;border-spacing:0;width:100%;background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:var(--rg-radius);overflow:hidden;box-shadow:var(--rg-shadow)}
  th,td{text-align:left;padding:9px 13px;border-bottom:1px solid var(--rg-line);font-size:13.5px}
  thead th{background:var(--rg-surface);font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-muted)}
  tbody tr:last-child td{border-bottom:none}
  .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
</style></head>
<body>
${brandBar("Close calendar")}
<div class="wrap">
  <h1>Close calendar</h1>
  <p class="sub">as of ${esc(status.asOf)} · ${status.done}/${status.tasks.length} done · ${status.overdue} overdue · ${status.blocked} blocked</p>
  ${banner}
  <table>
    <thead><tr><th>Due</th><th>Task</th><th>Owner</th><th>Status</th></tr></thead>
    <tbody>
${rows}
    </tbody>
  </table>
</div>
</body></html>`;
}
