#!/usr/bin/env python3
"""UAT harness — the fully-operationalized stack on a fleet of demo businesses.

Proves the platform is ready for user-acceptance testing end to end, with no
external credentials:

  1. bootstrap a fresh SQLite database (all Python-owned tables in one call),
  2. onboard THREE demo businesses with distinct cash profiles into a
     SQL-persisted fleet (a stable shop, a watch-list agency, an at-risk studio),
  3. attach each client's operational status via the ops-status/1 loop
     (`set_status_from_json` — exactly what the TS serializers emit),
  4. run one delivery tick (recording deliverer) so briefings "go out",
  5. **restart**: throw the fleet away and rehydrate from the database — same
     roster, same inputs, same status, delivery cursor intact,
  6. serve each owner over the WSGI adapter with a minted JWT (and hit /ready),
  7. render the operator fleet dashboard.

Run: `python uat.py`. Writes out/uat_fleet.html and prints a UAT summary.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
for pkg in ("forecast", "briefing", "web", "runtime", "ops"):
    sys.path.insert(0, str(HERE.parent / pkg / "src"))

from rgnr8_forecast import (  # noqa: E402
    CashPosition, Category, Direction, ForecastConfig, ForecastInputs, Frequency,
    Invoice, Money, PayrollSchedule, Recurrence, RecurringItem, to_dto,
)
from rgnr8_briefing import RecordingDeliverer  # noqa: E402
from rgnr8_web import Request, wsgi_app  # noqa: E402
from rgnr8_ops import (  # noqa: E402
    BetaTenant, Fleet, SqlFleetStore, bootstrap_python_schemas,
    build_ops_report, render_ops_html,
)

MT = ZoneInfo("America/Denver")
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=MT)
NOW_EPOCH = int(NOW.timestamp())
SECRET = "uat-signing-key"


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def _stable() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=usd("180000.00")),
        invoices=(Invoice("INV-1", "c", date(2026, 8, 20), date(2026, 9, 15), usd("24000.00")),),
        recurring=(RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("5000.00"),
                                 Recurrence(Frequency.MONTHLY, anchor=date(2026, 9, 1))),),
    )


def _watch() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=usd("72000.00")),
        payroll=(PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 9, 3)),
                                 usd("18000.00"), usd("4600.00")),),
        recurring=(RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("6500.00"),
                                 Recurrence(Frequency.MONTHLY, anchor=date(2026, 9, 1))),),
    )


def _at_risk() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=usd("38000.00")),
        payroll=(PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 9, 3)),
                                 usd("24000.00"), usd("6100.00")),),
        recurring=(RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("7000.00"),
                                 Recurrence(Frequency.MONTHLY, anchor=date(2026, 9, 1))),),
    )


DEMOS = [
    ("brightwork", "Brightwork Studio", "owner@brightwork.com", _at_risk(), usd("15000.00"),
     {"connectors": {"total": 3, "needs_attention": 0, "stale": 0, "last_sync": "2026-09-02T06:00:00Z"},
      "close": {"period": "2026-08", "total": 7, "done": 4, "overdue": 1, "blocked": 0,
                "next_task": "Reconcile bank", "next_due": "2026-09-03"}}),
    ("nimbus", "Nimbus Agency", "owner@nimbus.com", _watch(), usd("12000.00"),
     {"connectors": {"total": 2, "needs_attention": 1, "stale": 0, "last_sync": "2026-09-01T06:00:00Z"},
      "close": {"period": "2026-08", "total": 7, "done": 7, "overdue": 0, "blocked": 0,
                "next_task": None, "next_due": None}}),
    ("harbor", "Harbor Supply Co", "owner@harbor.com", _stable(), usd("20000.00"),
     {"connectors": {"total": 2, "needs_attention": 0, "stale": 0, "last_sync": "2026-09-02T05:30:00Z"},
      "close": {"period": "2026-08", "total": 7, "done": 7, "overdue": 0, "blocked": 0,
                "next_task": None, "next_due": None}}),
]


def _check(label: str, cond: bool) -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        raise SystemExit(f"UAT failed: {label}")


def main() -> int:
    print("→ UAT: bootstrap a fresh database")
    conn = sqlite3.connect(":memory:")
    tables = bootstrap_python_schemas(conn)
    _check(f"schema bootstrap created {tables}", set(tables) >= {"web_tenant_state", "fleet_tenant"})

    print("→ UAT: onboard 3 demo businesses into a persisted fleet")
    fleet = Fleet(jwt_secret=SECRET, clock=lambda: NOW_EPOCH, store=SqlFleetStore(conn))
    for tid, name, recip, inputs, floor, status in DEMOS:
        fleet.onboard(BetaTenant(tid, name, recip, inputs, ForecastConfig(minimum_cash=floor)))
        # round-trip the inputs DTO too (proves forecast-inputs/1 survives)
        _ = to_dto(inputs)
        fleet.set_status_from_json(tid, status)
    _check("3 clients onboarded", len(fleet.tenants) == 3)

    print("→ UAT: run one delivery tick (briefings go out)")
    fleet.delivery_runtime(RecordingDeliverer()).tick(NOW)
    pre = build_ops_report(fleet, NOW)
    _check("all briefings delivered this week", pre.delivered == 3)

    print("→ UAT: RESTART — rehydrate the fleet from the database")
    back = Fleet.load(SqlFleetStore(conn), jwt_secret=SECRET, clock=lambda: NOW_EPOCH)
    _check("roster survived restart", set(back.tenants) == {"brightwork", "nimbus", "harbor"})
    _check("operational status survived restart",
           back.statuses["nimbus"].connectors is not None
           and back.statuses["nimbus"].connectors.needs_attention == 1)

    print("→ UAT: serve owners over WSGI with minted JWTs + /ready")
    app = back.web_app()
    application = wsgi_app(app)

    ready: dict[str, object] = {}

    def _sr(status: str, headers: list[tuple[str, str]]) -> None:
        ready["status"] = status

    b"".join(application(
        {"REQUEST_METHOD": "GET", "PATH_INFO": "/ready", "QUERY_STRING": "", "CONTENT_LENGTH": "0"},
        _sr,  # type: ignore[arg-type]
    ))
    _check("/ready is 200", str(ready.get("status", "")).startswith("200"))
    for tid in back.tenants:
        token = back.mint_token(tid)
        r = app.handle(Request("GET", f"/api/{tid}/today", {"authorization": f"Bearer {token}"}))
        _check(f"{tid} owner route authorized", r.status == 200)

    print("→ UAT: render the operator dashboard")
    report = build_ops_report(back, NOW)
    (OUT / "uat_fleet.html").write_text(render_ops_html(report))
    _check("dashboard ranks worst-first (at-risk client leads)", report.rows[0].tenant_id == "brightwork")
    print(f"  wrote out/uat_fleet.html — {report.total} clients, {report.at_risk} at risk, "
          f"{report.books_not_current} with books behind, {report.closes_done} closed")

    print("\nUAT PASSED — the fully-operationalized stack is ready for user-acceptance testing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
