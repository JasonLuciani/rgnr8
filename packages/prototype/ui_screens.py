"""Render every server-rendered UI screen to out/ — the whole owner + operator
surface, reproducibly. Each page is produced by the real WebApp routing (login →
session cookie → role-aware shell), so what lands in out/ is exactly what a
browser would load. Run: python ui_screens.py
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
for pkg in ("forecast", "briefing", "web", "runtime", "ops", "billing"):
    sys.path.insert(0, str(HERE.parent / pkg / "src"))

from rgnr8_forecast import (  # noqa: E402
    CashPosition, Category, Direction, ForecastConfig, ForecastInputs, Frequency,
    Invoice, Money, PayrollSchedule, Recurrence, RecurringItem,
)
from rgnr8_web import (  # noqa: E402
    BankTransaction, CloseBoard, CloseTask, InMemoryUserDirectory, JwtAuthenticator,
    Request, Role, User, WebApp, sign_jwt,
)
from rgnr8_ops import (  # noqa: E402
    BetaTenant, ConnectorHealth, Fleet, TenantOpsStatus, build_ops_report,
    render_operator_console,
)

SECRET = "prototype-secret"
NOW = 1_760_000_000


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=usd("128400.00")),
        invoices=(
            Invoice("INV-88", "northwind", date(2026, 8, 22), date(2026, 9, 12), usd("42000.00")),
            Invoice("INV-90", "contoso", date(2026, 8, 28), date(2026, 9, 26), usd("18500.00")),
        ),
        payroll=(
            PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 9, 3)),
                            usd("36000.00"), usd("9200.00")),
        ),
        recurring=(
            RecurringItem("Office lease", Category.RENT, Direction.OUTFLOW, usd("7800.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 9, 1))),
            RecurringItem("SaaS stack", Category.VENDOR_PAYMENT, Direction.OUTFLOW, usd("3100.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 9, 5))),
        ),
    )


def _feed() -> list[BankTransaction]:
    return [
        BankTransaction("t1", "2026-08-28", "Deposit — Northwind retainer", usd("42000.00"),
                        category="Sales income", status="matched", counterparty="Northwind LLC"),
        BankTransaction("t2", "2026-08-29", "GUSTO PAYROLL", usd("-36000.00"),
                        category="Payroll", status="matched"),
        BankTransaction("t3", "2026-08-30", "MERIDIAN OFFICE LEASE", usd("-7800.00"),
                        category="Rent", status="matched", counterparty="Meridian Props"),
        BankTransaction("t4", "2026-08-31", "SQ *COFFEE SUPPLY CO", usd("-186.40"),
                        category="Uncategorized", status="review"),
        BankTransaction("t5", "2026-09-01", "AMZN MKTPL — office", usd("-412.19"),
                        category="Uncategorized", status="review"),
        BankTransaction("t6", "2026-09-02", "CHECK 2041", usd("-5000.00"),
                        category="Uncategorized", status="unmatched"),
    ]


def _close_board() -> CloseBoard:
    return CloseBoard("2026-08", (
        CloseTask("feeds", "Bank & card feeds imported", "done", "Bookkeeper", "2026-08-01"),
        CloseTask("categorize", "All transactions categorized", "done", "Bookkeeper", "2026-08-03"),
        CloseTask("reconcile", "Bank reconciliations tie out", "open", "Bookkeeper", "2026-08-04"),
        CloseTask("payroll", "Payroll & benefits accrued", "overdue", "Controller", "2026-08-04"),
        CloseTask("review", "Owner/controller review", "open", "Controller", "2026-08-05"),
    ))


def _app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("owner@northwind.com", "owner@northwind.com", "Nora Owner"))
    users.set_membership("owner@northwind.com", "northwind", Role.OWNER)
    users.upsert_user(User("book@northwind.com", "book@northwind.com", "Ben Books"))
    users.set_membership("book@northwind.com", "northwind", Role.BOOKKEEPER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW)
    app.add_tenant("northwind", "Northwind LLC", _inputs(),
                   ForecastConfig(minimum_cash=usd("25000.00")), token="unused")
    app.add_transactions("northwind", _feed(), account_name="Operating · Checking ••4821")
    app.add_close("northwind", _close_board())
    return app


def _cookie(email: str, role: str) -> str:
    return "rgnr8_session=" + sign_jwt(
        {"sub": email, "tenant": "northwind", "exp": NOW + 3600}, SECRET)


def _get(app: WebApp, route: str, email: str, role: str) -> str:
    body = app.handle(Request("GET", route, {"cookie": _cookie(email, role)})).body
    return body


def main() -> None:
    app = _app()
    owner, book = "owner@northwind.com", "book@northwind.com"
    pages = {
        "ui_login.html": app.handle(Request("GET", "/login")).body,
        "ui_home_owner.html": _get(app, "/app", owner, "owner"),
        "ui_home_bookkeeper.html": _get(app, "/app", book, "bookkeeper"),
        "ui_cash.html": _get(app, "/t/northwind", owner, "owner"),
        "ui_briefing.html": _get(app, "/t/northwind/briefing", owner, "owner"),
        "ui_close.html": _get(app, "/t/northwind/close", owner, "owner"),
        "ui_transactions.html": _get(app, "/t/northwind/transactions", book, "bookkeeper"),
        "ui_packages.html": _get(app, "/t/northwind/packages", owner, "owner"),
        "ui_team.html": _get(app, "/t/northwind/team", owner, "owner"),
    }
    for name, html in pages.items():
        (OUT / name).write_text(html)
        print(f"  wrote out/{name} ({len(html):,} bytes)")

    # Operator console — the RGNR8-staff fleet surface
    fleet = Fleet(jwt_secret=SECRET, clock=lambda: NOW)
    fleet.onboard(BetaTenant("northwind", "Northwind LLC", "owner@northwind.com",
                             _inputs(), ForecastConfig(minimum_cash=usd("25000.00"))))
    fleet.onboard(BetaTenant("contoso", "Contoso Studio", "owner@contoso.com",
                             ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                            available=usd("31000.00")),
                                            payroll=(PayrollSchedule("Payroll",
                                                     Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 9, 3)),
                                                     usd("24000.00"), usd("6100.00")),)),
                             ForecastConfig(minimum_cash=usd("20000.00"))))
    fleet.set_status("northwind", TenantOpsStatus(
        connectors=ConnectorHealth(total=3, needs_attention=0, stale=0, last_sync="2026-09-02T06:00:00Z")))
    fleet.set_status("contoso", TenantOpsStatus(
        connectors=ConnectorHealth(total=2, needs_attention=1, stale=0)))
    report = build_ops_report(fleet, datetime(2026, 9, 2, 12, 0, tzinfo=ZoneInfo("America/Denver")))
    console = render_operator_console(report, operator="jason@rgnr8.co")
    (OUT / "operator_console.html").write_text(console)
    print(f"  wrote out/operator_console.html ({len(console):,} bytes)")


if __name__ == "__main__":
    main()
