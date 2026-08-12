import { test } from "node:test";
import assert from "node:assert/strict";
import { buildCloseCalendar, closeCalendarStatus, opsCloseStatus } from "../src/index.js";

const AUG_END = "2026-08-31";

test("opsCloseStatus emits the ops-status/1 close fragment mid-close", () => {
  const cal = buildCloseCalendar(AUG_END);
  const status = closeCalendarStatus(cal, "2026-09-02", []); // nothing done yet
  const s = opsCloseStatus(status, "2026-08");
  assert.equal(s.period, "2026-08");
  assert.equal(s.total, cal.length);
  assert.equal(s.done, 0);
  assert.ok(s.total > 0);
  assert.equal(typeof s.next_task, "string"); // there is a next actionable task
  assert.ok(s.next_due && /^\d{4}-\d{2}-\d{2}$/.test(s.next_due));
});

test("opsCloseStatus reflects a completed close and derives the period when omitted", () => {
  const cal = buildCloseCalendar(AUG_END);
  const allDone = cal.map((e) => e.id);
  const status = closeCalendarStatus(cal, "2026-09-10", allDone);
  const s = opsCloseStatus(status);
  assert.equal(s.done, s.total);
  assert.equal(s.next_task, null);
  assert.equal(s.next_due, null);
  assert.match(s.period, /^\d{4}-\d{2}$/); // derived from asOf/tasks
});
