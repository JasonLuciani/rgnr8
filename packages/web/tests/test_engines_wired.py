"""The three new owner engines wired into the web UI: auto-categorize suggestions
in the register, the scenario-planning screen + endpoint, and the AR / collections
screen. All self-contained (no external assets), all behind VIEW_CASH / the
existing categorize gate."""

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
    BankTransaction,
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "engines-secret"
NOW = 1_760_000_000


def usd(s: str) -> Money:
    return Money.from_decimal(s)


def _users(members: dict[str, Role]) -> InMemoryUserDirectory:
    d = InMemoryUserDirectory()
    for email, role in members.items():
        d.upsert_user(User(email, email, email.split("@")[0]))
        d.set_membership(email, "acme", role)
    return d


def _app(inputs: ForecastInputs, *, members: dict[str, Role] | None = None) -> WebApp:
    members = members or {"owner@acme.com": Role.OWNER, "view@acme.com": Role.VIEWER}
    users = _users(members)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW)
    app.add_tenant("acme", "Acme Co", inputs,
                   ForecastConfig(minimum_cash=usd("5000.00")), token="unused")
    return app


def _h(sub: str) -> dict[str, str]:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    return {"authorization": f"Bearer {tok}"}


def _hj(sub: str) -> dict[str, str]:
    return {**_h(sub), "content-type": "application/json"}


# --- 1. auto-categorize in the register -------------------------------------
def _register_feed() -> list[BankTransaction]:
    return [
        # already-categorized recurring vendor history (trains the learned model)
        BankTransaction("h1", "2026-08-01", "GUSTO PAYROLL 8811", usd("-12000.00"),
                        category="Payroll", status="matched", counterparty="Gusto"),
        BankTransaction("h2", "2026-08-15", "GUSTO PAYROLL 8899", usd("-12000.00"),
                        category="Payroll", status="matched", counterparty="Gusto"),
        # a NEW uncategorized line from the same recurring vendor → should be suggested
        BankTransaction("new-gusto", "2026-08-29", "GUSTO PAYROLL 9002", usd("-12000.00"),
                        category="Uncategorized", status="review", counterparty="Gusto"),
        # a novel line the engine has never seen → no suggestion
        BankTransaction("novel", "2026-08-30", "PMT ZZ MYSTERY VENDOR", usd("-777.10"),
                        category="Uncategorized", status="review", counterparty="Zzz Mystery Co"),
    ]


def _register_app() -> WebApp:
    inputs = ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=usd("50000.00")))
    app = _app(inputs)
    app.add_transactions("acme", _register_feed(), account_name="Checking")
    app.add_categorizer("acme")  # learned-only (no hand-authored ruleset)
    return app


def test_register_suggests_for_recurring_vendor_only() -> None:
    app = _register_app()
    r = app.handle(Request("GET", "/t/acme/transactions", _h("owner@acme.com")))
    assert r.status == 200 and "Bank transactions" in r.body
    # exactly one suggestion: the recurring Gusto line, not the novel one
    assert r.body.count("Apply suggestion") == 1
    assert "% confident" in r.body
    # the suggestion is Payroll, tied to the new Gusto txn id
    assert "applySuggestion(this,'new-gusto','Payroll')" in r.body
    # the novel line gets no suggestion
    assert "applySuggestion(this,'novel'" not in r.body
    assert "http://" not in r.body and "https://" not in r.body


def test_register_suggestion_apply_uses_categorize_post() -> None:
    app = _register_app()
    # applying the suggestion is the same categorize POST — sets the category & matches
    r = app.handle(Request("POST", "/api/acme/transactions", _hj("owner@acme.com"),
                           '{"id":"new-gusto","category":"Payroll"}'))
    assert r.status == 200
    body = json.loads(r.body)
    assert body["category"] == "Payroll" and body["status"] == "matched"


def test_register_no_suggestions_without_history_or_rules() -> None:
    # a feed with nothing categorized yet + no ruleset → nothing to learn from,
    # so the register renders exactly as before, no hints.
    inputs = ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=usd("50000.00")))
    app = _app(inputs)
    app.add_transactions("acme", [
        BankTransaction("u1", "2026-08-29", "PMT ZZ MYSTERY VENDOR", usd("-777.10"),
                        category="Uncategorized", status="review", counterparty="Zzz Mystery Co"),
    ], account_name="Checking")
    r = app.handle(Request("GET", "/t/acme/transactions", _h("owner@acme.com")))
    assert r.status == 200 and "Apply suggestion" not in r.body


# --- 2. scenario planning ----------------------------------------------------
def _scenario_inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=usd("60000.00")),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 10), usd("20000.00")),),
        customer_histories=(CustomerHistory("acme", override_days_late=0),),
        payroll=(PayrollSchedule("Payroll", Recurrence(Frequency.BIWEEKLY, anchor=date(2026, 8, 7)),
                                 usd("12000.00"), usd("3000.00")),),
        recurring=(RecurringItem("Rent", Category.RENT, Direction.OUTFLOW, usd("4000.00"),
                                 Recurrence(Frequency.MONTHLY, anchor=date(2026, 8, 1))),),
    )


def test_scenario_page_offers_templates_in_shell() -> None:
    app = _app(_scenario_inputs())
    r = app.handle(Request("GET", "/t/acme/scenarios", _h("owner@acme.com")))
    assert r.status == 200 and "Scenario planning" in r.body
    assert 'class="rg-nav"' in r.body                     # wrapped in the shell
    for label in ("Hire someone", "Customer pays late", "Take a loan", "One-time expense"):
        assert label in r.body
    assert "http://" not in r.body and "https://" not in r.body


def test_scenario_customer_pays_late_worsens_trough() -> None:
    app = _app(_scenario_inputs())
    spec = {"template": "customer_pays_late", "params": {"customer_id": "acme", "days": 90}}
    r = app.handle(Request("POST", "/api/acme/scenario", _hj("owner@acme.com"), json.dumps(spec)))
    assert r.status == 200
    d = json.loads(r.body)
    # paying late drops the projected low point (trough delta is negative)
    assert float(d["trough_delta"]) < 0
    assert d["scenario"] == "Customer acme pays 90d late"
    assert len(d["weekly_closing_deltas"]) == 13


def test_scenario_rejects_bad_spec() -> None:
    app = _app(_scenario_inputs())
    r = app.handle(Request("POST", "/api/acme/scenario", _hj("owner@acme.com"),
                           '{"template":"customer_pays_late","params":{}}'))
    assert r.status == 400


def test_scenario_endpoint_gated_by_view_cash() -> None:
    # viewer HAS view_cash, so they can run a scenario; tenant isolation still holds
    app = _app(_scenario_inputs())
    r = app.handle(Request("POST", "/api/acme/scenario", _hj("view@acme.com"),
                           '{"template":"one_time_expense","params":{"amount":"1000.00","on":"2026-09-01"}}'))
    assert r.status == 200


# --- 3. AR / collections -----------------------------------------------------
def _ar_inputs() -> ForecastInputs:
    # three overdue invoices of escalating age (as of 2026-08-31)
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=usd("40000.00")),
        invoices=(
            Invoice("INV-A", "alpha", date(2026, 5, 1), date(2026, 6, 1), usd("20000.00")),   # 91d → FINAL
            Invoice("INV-B", "bravo", date(2026, 6, 25), date(2026, 7, 25), usd("10000.00")),  # 37d → FIRM
            Invoice("INV-C", "charlie", date(2026, 8, 1), date(2026, 8, 20), usd("5000.00")),   # 11d → FRIENDLY
        ),
    )


def test_ar_page_worst_first_with_escalating_tone() -> None:
    app = _app(_ar_inputs())
    r = app.handle(Request("GET", "/t/acme/receivables", _h("owner@acme.com")))
    assert r.status == 200 and "Receivables" in r.body
    # chase list is worst-first: oldest/biggest invoice ranks first
    assert r.body.index("INV-A") < r.body.index("INV-B") < r.body.index("INV-C")
    # nudge tone escalates with age, most-overdue first
    assert r.body.index("FINAL") < r.body.index("FIRM") < r.body.index("FRIENDLY")
    assert "http://" not in r.body and "https://" not in r.body


def test_ar_json_endpoint() -> None:
    app = _app(_ar_inputs())
    r = app.handle(Request("GET", "/api/acme/receivables", _h("owner@acme.com")))
    assert r.status == 200
    d = json.loads(r.body)
    assert d["total_ar"] == "35000.00"
    assert [c["invoice"] for c in d["chase_list"]] == ["INV-A", "INV-B", "INV-C"]
    assert [n["tone"] for n in d["nudges"]] == ["FINAL", "FIRM", "FRIENDLY"]


# --- 4. nav items ------------------------------------------------------------
def test_nav_shows_scenarios_and_receivables_for_view_cash() -> None:
    app = _app(ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=usd("40000.00"))))
    # a plain viewer has VIEW_CASH → both nav items appear
    r = app.handle(Request("GET", "/t/acme/receivables", _h("view@acme.com")))
    assert r.status == 200
    assert 'href="/t/acme/scenarios">Scenarios</a>' in r.body
    # The page you're on is the active item, so it also carries aria-current.
    assert 'href="/t/acme/receivables" aria-current="page">Receivables</a>' in r.body
    # ...and both sit under their groups rather than in one flat row.
    assert "<h2>Plan</h2>" in r.body and "<h2>Customers</h2>" in r.body
