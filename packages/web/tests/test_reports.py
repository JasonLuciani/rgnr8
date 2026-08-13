"""The rgnr8-reports package wired into the web UI: a reports index (baseline +
saved), in-shell rendering of a baseline or saved report, JSON/CSV exports, and a
custom-report save endpoint. Viewing is gated on VIEW_CASH (everyone who can see
the business); saving on RECORD_DECISION (everyone but a read-only viewer).

All self-contained (no external assets) and tenant-scoped through the same RBAC as
every other route."""

import json
from datetime import date

from rgnr8_forecast import (
    CashPosition,
    Category,
    CustomerHistory,
    Direction,
    ForecastConfig,
    ForecastInputs,
    Frequency,
    Invoice,
    Money,
    PayrollSchedule,
    Recurrence,
    RecurringItem,
)
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "reports-secret"
NOW = 1_760_000_000


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def _inputs(available: str) -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=usd(available)),
        invoices=(Invoice("INV-1", "acme", date(2026, 7, 1), date(2026, 7, 20), usd("15000.00")),),
        customer_histories=(CustomerHistory("acme", override_days_late=6),),
        payroll=(
            PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                            usd("16000.00"), usd("4200.00")),
        ),
        recurring=(
            RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("4000.00"),
                          Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),
        ),
    )


def _users(members: dict[str, Role]) -> InMemoryUserDirectory:
    d = InMemoryUserDirectory()
    for email, role in members.items():
        d.upsert_user(User(email, email, email.split("@")[0]))
        d.set_membership(email, "acme", role)
    return d


def _app() -> WebApp:
    users = _users({"owner@acme.com": Role.OWNER, "view@acme.com": Role.VIEWER})
    # a second tenant, for the isolation check
    users.upsert_user(User("owner@bright.com", "owner@bright.com", "bright"))
    users.set_membership("owner@bright.com", "bright", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW)
    app.add_tenant("acme", "Acme Co", _inputs("80000.00"),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="unused")
    app.add_tenant("bright", "Bright Agency", _inputs("50000.00"),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="unused-bright")
    return app


def _h(sub: str, tenant: str = "acme") -> dict[str, str]:
    tok = sign_jwt({"sub": sub, "tenant": tenant, "exp": NOW + 3600}, SECRET)
    return {"authorization": f"Bearer {tok}"}


def _hj(sub: str, tenant: str = "acme") -> dict[str, str]:
    return {**_h(sub, tenant), "content-type": "application/json"}


# --- 1. reports index lists the baseline reports ----------------------------
def test_reports_index_lists_baseline_titles() -> None:
    r = _app().handle(Request("GET", "/t/acme/reports", _h("owner@acme.com")))
    assert r.status == 200
    assert "text/html" in r.content_type
    # a spread of the ten baseline reports, by title (ampersands are HTML-escaped)
    for title in ("Cash Flow Outlook", "AR Aging &amp; Collections", "Runway",
                  "Executive Board Pack", "Financial Statements Pack"):
        assert title in r.body
    # links point at the render + export routes
    assert 'href="/t/acme/reports/cash_flow_outlook"' in r.body
    assert 'href="/api/acme/reports/cash_flow_outlook.json"' in r.body
    assert 'href="/api/acme/reports/cash_flow_outlook.csv"' in r.body
    # self-contained
    assert "http://" not in r.body and "https://" not in r.body


# --- 2. the Reports nav item shows for VIEW_CASH (i.e. everyone) -------------
def test_reports_nav_item_present_for_viewer() -> None:
    r = _app().handle(Request("GET", "/t/acme/reports", _h("view@acme.com")))
    assert r.status == 200
    assert 'href="/t/acme/reports"' in r.body and ">Reports<" in r.body


# --- 3. rendering a baseline report in-shell, with a spot-checked number -----
def test_render_baseline_cash_flow_outlook() -> None:
    r = _app().handle(Request("GET", "/t/acme/reports/cash_flow_outlook", _h("owner@acme.com")))
    assert r.status == 200
    assert "text/html" in r.content_type
    assert "Cash Flow Outlook" in r.body
    # Cash today = the opening available, formatted through the shared formatter
    assert "$80,000.00" in r.body
    assert "http://" not in r.body and "https://" not in r.body


def test_render_unknown_report_is_404() -> None:
    r = _app().handle(Request("GET", "/t/acme/reports/not_a_report", _h("owner@acme.com")))
    assert r.status == 404


# --- 4. JSON + CSV exports: right content-type + shape ----------------------
def test_report_json_export() -> None:
    r = _app().handle(Request("GET", "/api/acme/reports/cash_flow_outlook.json", _h("owner@acme.com")))
    assert r.status == 200
    assert r.content_type == "application/json"
    payload = json.loads(r.body)
    assert payload["title"] == "Cash Flow Outlook"
    assert isinstance(payload["sections"], list) and payload["sections"]
    # a KPI item carries the cash-today figure
    flat = json.dumps(payload)
    assert "$80,000.00" in flat


def test_report_csv_export() -> None:
    r = _app().handle(Request("GET", "/api/acme/reports/cash_flow_outlook.csv", _h("owner@acme.com")))
    assert r.status == 200
    assert r.content_type.startswith("text/csv")
    # the flattened cash-outlook table header
    assert "Week,Starting,Closing cash,Status" in r.body


def test_report_export_unknown_suffix_is_404() -> None:
    r = _app().handle(Request("GET", "/api/acme/reports/cash_flow_outlook.pdf", _h("owner@acme.com")))
    assert r.status == 404


# --- 5. custom builder: POST a valid section list, save, then render ---------
def test_post_custom_report_saves_and_renders() -> None:
    app = _app()
    spec = {
        "id": "board_snapshot",
        "title": "Board Snapshot",
        "description": "A custom pick of sections.",
        "sections": [
            {"kind": "kpi_row", "title": "Highlights",
             "params": {"items": [["Headcount", "12", False]]}},
            {"kind": "cash_outlook", "title": "Cash"},
        ],
    }
    r = app.handle(Request("POST", "/api/acme/reports", _hj("owner@acme.com"), json.dumps(spec)))
    assert r.status == 201
    body = json.loads(r.body)
    assert body["id"] == "board_snapshot"
    assert body["sections"] == ["kpi_row", "cash_outlook"]

    # it now appears on the index and renders
    idx = app.handle(Request("GET", "/t/acme/reports", _h("owner@acme.com")))
    assert "Board Snapshot" in idx.body
    rendered = app.handle(Request("GET", "/t/acme/reports/board_snapshot", _h("owner@acme.com")))
    assert rendered.status == 200
    assert "Board Snapshot" in rendered.body and "Highlights" in rendered.body
    assert "$80,000.00" in rendered.body  # the cash_outlook section computed live


def test_post_custom_report_unknown_kind_is_400() -> None:
    app = _app()
    spec = {"id": "bad", "title": "Bad", "sections": [{"kind": "not_a_kind", "title": "X"}]}
    r = app.handle(Request("POST", "/api/acme/reports", _hj("owner@acme.com"), json.dumps(spec)))
    assert r.status == 400
    assert "not_a_kind" in r.body


# --- 6. RBAC: viewer can view but not save; tenants are isolated -------------
def test_viewer_can_view_but_not_save() -> None:
    app = _app()
    # viewer may render + export (VIEW_CASH)
    assert app.handle(Request("GET", "/t/acme/reports/runway", _h("view@acme.com"))).status == 200
    assert app.handle(Request("GET", "/api/acme/reports/runway.json", _h("view@acme.com"))).status == 200
    # but not save a custom report (needs RECORD_DECISION)
    spec = {"id": "x", "title": "X", "sections": [{"kind": "runway"}]}
    save = app.handle(Request("POST", "/api/acme/reports", _hj("view@acme.com"), json.dumps(spec)))
    assert save.status == 403


def test_tenant_isolation_on_reports() -> None:
    app = _app()
    # an acme token cannot reach bright's reports
    r = app.handle(Request("GET", "/t/bright/reports", _h("owner@acme.com", "acme")))
    assert r.status == 403
    r2 = app.handle(Request("GET", "/api/bright/reports/cash_flow_outlook.json", _h("owner@acme.com", "acme")))
    assert r2.status == 403


def test_saved_report_is_per_tenant() -> None:
    app = _app()
    spec = {"id": "mine", "title": "Mine", "sections": [{"kind": "runway"}]}
    app.handle(Request("POST", "/api/acme/reports", _hj("owner@acme.com"), json.dumps(spec)))
    # bright never saved it → 404 for bright, 200 for acme
    assert app.handle(Request("GET", "/t/bright/reports/mine", _h("owner@bright.com", "bright"))).status == 404
    assert app.handle(Request("GET", "/t/acme/reports/mine", _h("owner@acme.com"))).status == 200
