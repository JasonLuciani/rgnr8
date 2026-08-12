import { test } from "node:test";
import assert from "node:assert/strict";
import {
  DEFAULT_CLOSE_TEMPLATE,
  addBusinessDays,
  buildCloseCalendar,
  closeCalendarStatus,
  renderCloseCalendarHtml,
} from "../src/index.js";

// 2026-08-31 is a Monday.
const AUG_END = "2026-08-31";

test("addBusinessDays skips weekends", () => {
  // +1 bd from Mon = Tue; +5 bd = next Mon (skips Sat/Sun)
  assert.equal(addBusinessDays("2026-08-31", 1), "2026-09-01");
  assert.equal(addBusinessDays("2026-08-31", 5), "2026-09-07");
  assert.equal(addBusinessDays("2026-08-31", 0), "2026-08-31");
});

test("addBusinessDays skips injected holidays", () => {
  // Labor Day 2026-09-07 (Mon) is a holiday → +5 bd from Mon 08-31 lands on Tue 09-08
  const holidays = new Set(["2026-09-07"]);
  assert.equal(addBusinessDays("2026-08-31", 5, holidays), "2026-09-08");
});

test("buildCloseCalendar computes due dates from the default template", () => {
  const cal = buildCloseCalendar(AUG_END);
  assert.equal(cal.length, DEFAULT_CLOSE_TEMPLATE.length);
  assert.equal(cal.find((c) => c.id === "sync")?.dueDate, "2026-09-01"); // +1 bd
  assert.equal(cal.find((c) => c.id === "seal_package")?.dueDate, "2026-09-08"); // +6 bd
});

test("status: nothing done yet → sync is due, downstream tasks blocked", () => {
  const cal = buildCloseCalendar(AUG_END);
  const s = closeCalendarStatus(cal, "2026-09-01", []);
  const sync = s.tasks.find((t) => t.id === "sync");
  const rec = s.tasks.find((t) => t.id === "reconcile_bank");
  assert.equal(sync?.state, "due_today"); // due 09-01, asOf 09-01
  assert.equal(rec?.state, "blocked");
  assert.deepEqual(rec?.blockedBy, ["sync"]);
  assert.equal(s.nextUp?.id, "sync");
  assert.equal(s.complete, false);
});

test("status: completing a dependency unblocks the next task", () => {
  const cal = buildCloseCalendar(AUG_END);
  const s = closeCalendarStatus(cal, "2026-09-03", ["sync"]);
  const rec = s.tasks.find((t) => t.id === "reconcile_bank");
  assert.equal(rec?.state, "due_today"); // due 09-03, now unblocked
  assert.equal(s.done, 1);
});

test("status: a past-due, unblocked task is overdue", () => {
  const cal = buildCloseCalendar(AUG_END);
  // asOf well past sync's due date, sync not completed
  const s = closeCalendarStatus(cal, "2026-09-15", []);
  assert.equal(s.tasks.find((t) => t.id === "sync")?.state, "overdue");
  assert.equal(s.overdue >= 1, true);
});

test("status: all tasks complete → close complete", () => {
  const cal = buildCloseCalendar(AUG_END);
  const allIds = cal.map((c) => c.id);
  const s = closeCalendarStatus(cal, "2026-09-08", allIds);
  assert.equal(s.complete, true);
  assert.equal(s.remaining, 0);
  assert.equal(s.nextUp, null);
});

test("renderCloseCalendarHtml shows the schedule and a banner", () => {
  const cal = buildCloseCalendar(AUG_END);
  const html = renderCloseCalendarHtml(closeCalendarStatus(cal, "2026-09-01", []));
  assert.match(html, /<!doctype html>/);
  assert.match(html, /Close calendar/);
  assert.match(html, /Reconcile all bank/);
  assert.match(html, /next up: Sync/);
});
