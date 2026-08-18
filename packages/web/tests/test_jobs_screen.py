"""The job screen — the page a contractor actually lives on.

What it has to make impossible to miss: what the job will cost if the current
estimate holds, what has been spent, what has been *committed* on a signed
purchase order, and whether billing is ahead of or behind the work. A screen
that shows spend against budget and omits commitments tells a contractor their
framing budget is fine while the rest of it is already ordered.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "jobs-secret"
NOW = 1_760_000_000


class FakeTransport:
    def __init__(self, routes: dict[str, tuple[int, dict[str, Any]]]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, str]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path, body))
        status, payload = self.routes.get(
            f"{method} {path.split('?')[0]}", (404, {"error": "not found"})
        )
        return LedgerResponse(status, payload)


JOB = {
    "id": "harper", "customer_id": "harper-residence", "name": "Harper kitchen remodel",
    "status": "ACTIVE", "billing_method": "PROGRESS", "cost_method": "AS_INCURRED",
    "start_date": "2026-06-01", "end_date": "", "contract_minor": "8500000",
    "retainage_ppm": 100_000, "revenue_account_code": "4100", "memo": "",
}

COST = {
    "contract": "job-cost/1", "currency": "USD", "job": JOB, "through": "",
    "rows": [
        {"cost_code": "LAB", "name": "Labor", "category": "LABOR",
         "budget_cost_minor": "3000000", "revised_cost_minor": "3000000",
         "actual_cost_minor": "1200000", "committed_minor": "0",
         "remaining_minor": "1800000", "percent_spent_ppm": 400_000, "over_budget": False},
        {"cost_code": "MAT", "name": "Materials", "category": "MATERIAL",
         "budget_cost_minor": "2000000", "revised_cost_minor": "2400000",
         "actual_cost_minor": "1500000", "committed_minor": "1000000",
         "remaining_minor": "-100000", "percent_spent_ppm": 625_000, "over_budget": True},
    ],
    "totals": {
        "budget_cost_minor": "5000000", "revised_cost_minor": "5400000",
        "actual_cost_minor": "2700000", "committed_minor": "1000000",
        "remaining_minor": "1700000", "percent_spent_ppm": 500_000,
        "contract_minor": "8500000", "revenue_minor": "3000000",
        "margin_minor": "300000", "projected_margin_minor": "3100000",
    },
    "unbudgeted": ["SUB"], "uncoded_minor": "35000",
}

WORK_ORDERS = {
    "contract": "job-work-orders/1", "currency": "USD", "job_id": "harper",
    "work_orders": [
        {"id": "WO-1", "title": "Hang the doors", "status": "COMPLETE",
         "scheduled_date": "2026-07-14", "assignee": "Marco Diaz",
         "totals": {"hours_milli": "8000", "cost_minor": "41600",
                    "billable_minor": "88000", "unbilled_minor": "0",
                    "margin_minor": "46400"}},
        {"id": "WO-2", "title": "Trim out", "status": "SCHEDULED",
         "scheduled_date": "2026-07-20", "assignee": "Marco Diaz",
         "totals": {"hours_milli": "6000", "cost_minor": "31200",
                    "billable_minor": "66000", "unbilled_minor": "66000",
                    "margin_minor": "34800"}},
    ],
    "totals": {"hours_milli": "14000", "cost_minor": "72800",
               "unbilled_minor": "66000", "open": 1},
}

BILLING = {
    "contract": "job-billing/1", "currency": "USD", "job_id": "harper",
    "billing_method": "PROGRESS", "contract_minor": "8500000", "retainage_ppm": 100_000,
    "schedule": [
        {"line_no": 1, "description": "Demolition", "cost_code": "LAB",
         "scheduled_value_minor": "1500000", "billed_minor": "1500000",
         "remaining_minor": "0", "percent_billed_ppm": 1_000_000},
        {"line_no": 2, "description": "Cabinets", "cost_code": "MAT",
         "scheduled_value_minor": "7000000", "billed_minor": "1500000",
         "remaining_minor": "5500000", "percent_billed_ppm": 214_285},
    ],
    "milestones": [],
    "unbilled_work": [
        {"entry_id": "WO-2-e1", "work_order_id": "WO-2", "date": "2026-07-20",
         "description": "Trim out", "kind": "LABOR", "quantity_milli": "6000",
         "bill_minor": "66000"},
    ],
    "totals": {"scheduled_minor": "8500000", "billed_minor": "3000000",
               "remaining_minor": "5500000", "unbilled_work_minor": "66000",
               "retainage_held_minor": "300000", "deposit_held_minor": "2000000"},
}

WIP = {
    "contract": "wip-schedule/1", "currency": "USD", "through": "",
    "rows": [{
        "job_id": "harper", "name": "Harper kitchen remodel", "status": "ACTIVE",
        "billing_method": "PROGRESS", "cost_method": "AS_INCURRED",
        "contract_minor": "8500000", "cost_to_date_minor": "2700000",
        "estimated_cost_minor": "5400000", "cost_to_complete_minor": "2700000",
        "percent_complete_ppm": 500_000, "earned_revenue_minor": "4250000",
        "billed_minor": "3000000", "under_billed_minor": "1250000",
        "over_billed_minor": "0", "gross_profit_minor": "1550000",
        "costs_in_excess_minor": "0", "billings_in_excess_minor": "0",
        "work_in_progress_minor": "0", "adjustment_minor": "1250000",
        "estimate_exceeded": False, "projected_loss_minor": "0",
    }],
    "totals": {},
}

COST_CODES = {"cost_codes": [
    {"code": "LAB", "name": "Labor", "category": "LABOR",
     "account_code": "5500", "active": True},
    {"code": "MAT", "name": "Materials", "category": "MATERIAL",
     "account_code": "5100", "active": True},
]}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/jobs": (200, {"contract": "job-list/1", "currency": "USD", "jobs": [
        {**JOB, "cost_to_date_minor": "2700000", "revenue_to_date_minor": "3000000",
         "revised_cost_minor": "5400000", "percent_spent_ppm": 500_000},
    ]}),
    "GET /t/acme/jobs/harper": (200, {"job": JOB, "budget": []}),
    "GET /t/acme/jobs/harper/cost": (200, COST),
    "GET /t/acme/jobs/harper/work-orders": (200, WORK_ORDERS),
    "GET /t/acme/jobs/harper/billing": (200, BILLING),
    "GET /t/acme/cost-codes": (200, COST_CODES),
    "GET /t/acme/wip": (200, WIP),
    "GET /t/acme/customers": (200, {"parties": [
        {"id": "harper-residence", "name": "Harper Residence"},
    ]}),
    "GET /t/acme/accounts": (200, {"accounts": [
        {"code": "4100", "name": "Contract Income", "type": "REVENUE"},
        {"code": "5100", "name": "Job Materials", "type": "EXPENSE"},
    ]}),
    "POST /t/acme/jobs": (201, {"job": JOB}),
    "POST /t/acme/jobs/harper/budget": (200, {"budget": []}),
    "POST /t/acme/jobs/harper/bill/progress": (201, {
        "invoice": {"id": "APP-2"}, "gross_minor": "1000000", "retainage_minor": "100000",
    }),
    "POST /t/acme/jobs/harper/deposits": (201, {"deposit_held_minor": "2000000"}),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("0.00"))
    )


def _app(
    routes: dict[str, tuple[int, dict[str, Any]]] | None = None,
) -> tuple[WebApp, FakeTransport, InMemoryAuditLog]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-view", "acme", Role.VIEWER)
    audit = InMemoryAuditLog()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
                 users=users, audit=audit)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    transport = FakeTransport(routes if routes is not None else dict(DEFAULT_ROUTES))
    app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport, audit


def _req(app: WebApp, path: str, sub: str = "u-owner",
         method: str = "GET", body: str = "") -> Any:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
    return app.handle(Request(method, path, headers, body))


def _sent(transport: FakeTransport, needle: str) -> dict[str, Any]:
    return json.loads([c for c in transport.calls if c[0] == "POST" and needle in c[1]][0][2])


# --- the list ----------------------------------------------------------------

def test_the_job_list_shows_contract_spend_and_progress() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/jobs")
    assert r.status == 200
    assert "Harper kitchen remodel" in r.body
    assert "$85,000.00" in r.body and "$27,000.00" in r.body
    assert "50.0%" in r.body


def test_an_empty_list_explains_what_a_job_is_for() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/jobs"] = (200, {"jobs": []})
    app, _t, _a = _app(routes)
    assert "whether the work made money" in _req(app, "/t/acme/jobs").body


def test_a_service_outage_is_reported_rather_than_a_blank_page() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/jobs"] = (503, {"error": "connection refused"})
    app, _t, _a = _app(routes)
    assert "Jobs unavailable" in _req(app, "/t/acme/jobs").body


# --- the job page ------------------------------------------------------------

def test_the_headline_leads_with_committed_cost_not_just_spend() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/jobs/harper")
    assert r.status == 200
    assert "Committed" in r.body
    assert "$10,000.00" in r.body, "the committed figure is on the page"
    assert "Projected margin" in r.body and "$31,000.00" in r.body


def test_underbilling_is_said_in_words_because_it_is_a_phone_call() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/jobs/harper")
    assert "$12,500.00 of work has been done and not yet billed" in r.body
    assert "also a phone call" in r.body


def test_overbilling_is_not_described_as_profit() -> None:
    routes = dict(DEFAULT_ROUTES)
    row = dict(WIP["rows"][0])  # type: ignore[index,arg-type]
    row["under_billed_minor"] = "0"
    row["over_billed_minor"] = "800000"
    routes["GET /t/acme/wip"] = (200, {**WIP, "rows": [row]})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/jobs/harper")
    assert "$8,000.00 has been billed ahead of the work" in r.body
    assert "owed in labour, not profit" in r.body


def test_a_blown_estimate_says_so_rather_than_capping_silently() -> None:
    routes = dict(DEFAULT_ROUTES)
    row = dict(WIP["rows"][0])  # type: ignore[index,arg-type]
    row["estimate_exceeded"] = True
    routes["GET /t/acme/wip"] = (200, {**WIP, "rows": [row]})
    app, _t, _a = _app(routes)
    assert "Cost has passed the estimate" in _req(app, "/t/acme/jobs/harper").body


def test_a_job_expected_to_lose_money_says_it_loudly() -> None:
    routes = dict(DEFAULT_ROUTES)
    row = dict(WIP["rows"][0])  # type: ignore[index,arg-type]
    row["projected_loss_minor"] = "1200000"
    routes["GET /t/acme/wip"] = (200, {**WIP, "rows": [row]})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/jobs/harper")
    assert "expected to lose $12,000.00" in r.body


def test_the_cost_table_shows_bid_and_current_estimate_side_by_side() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/jobs/harper")
    assert "What we bid" in r.body or "Bid" in r.body
    assert "$20,000.00" in r.body and "$24,000.00" in r.body


def test_spend_with_no_budget_line_and_spend_with_no_cost_code_are_surfaced() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/jobs/harper")
    assert "SUB" in r.body and "no budget line" in r.body
    assert "$350.00 is on this job with no cost code" in r.body


def test_work_orders_roll_up_onto_the_job_page() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/jobs/harper")
    assert "Hang the doors" in r.body and "Trim out" in r.body
    assert "1 still open" in r.body
    assert "$660.00 billable and not yet invoiced" in r.body


def test_retainage_and_deposits_are_explained_not_just_totalled() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/jobs/harper")
    assert "$3,000.00 is held as retainage" in r.body
    assert "aging report stays true" in r.body
    assert "$20,000.00 of deposit is held" in r.body
    assert "liability until an invoice draws it down" in r.body


# --- doing things ------------------------------------------------------------

def test_opening_a_job_parses_money_and_percentages_exactly() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/jobs", method="POST",
             body="name=Harper+kitchen&customer_id=harper-residence"
                  "&billing_method=PROGRESS&contract=85,000.00&retainage=10"
                  "&start_date=2026-06-01")
    assert r.status == 302
    sent = _sent(transport, "/jobs")
    assert sent["contract_minor"] == "8500000"
    assert sent["retainage_ppm"] == 100_000
    assert any(e.action == "job.saved" for e in audit.events(tenant_id="acme"))


def test_the_budget_form_sends_the_bid_and_the_revision_separately() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/jobs/harper/budget", method="POST",
         body="bid_LAB=30,000.00&revised_LAB=&bid_MAT=20,000.00&revised_MAT=24,000.00")
    sent = _sent(transport, "/budget")
    by_code = {line["cost_code"]: line for line in sent["lines"]}
    assert by_code["MAT"]["budget_cost_minor"] == "2000000"
    assert by_code["MAT"]["revised_cost_minor"] == "2400000"
    assert "revised_cost_minor" not in by_code["LAB"], "an untouched line keeps its estimate"


def test_a_progress_application_sends_cumulative_percentages() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/jobs/harper/bill/progress", method="POST",
             body="id=APP-2&date=2026-07-31&pct1=100&pct2=25")
    assert r.status == 302
    sent = _sent(transport, "/bill/progress")
    assert sent["lines"] == [
        {"line_no": 1, "percent_ppm": 1_000_000},
        {"line_no": 2, "percent_ppm": 250_000},
    ]
    assert any(e.action == "job.billed" for e in audit.events(tenant_id="acme"))


def test_the_retained_amount_is_reported_back_to_the_owner() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/jobs/harper/bill/progress", method="POST",
             body="id=APP-2&date=2026-07-31&pct1=100")
    assert "retainage" in str(r.headers.get("Location", ""))


def test_the_services_refusal_reaches_the_owner() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/jobs/harper/bill/progress"] = (
        400, {"error": "line 1: that is a change order, not a progress bill"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/jobs/harper/bill/progress", method="POST",
             body="id=APP-2&date=2026-07-31&pct1=100")
    assert "change%20order" in str(r.headers.get("Location", ""))


def test_a_deposit_is_recorded_as_a_liability_and_said_to_be_one() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/jobs/harper/deposits", method="POST",
             body="amount=20,000.00&date=2026-05-15")
    assert r.status == 302
    assert "liability" in str(r.headers.get("Location", ""))
    assert _sent(transport, "/deposits")["amount_minor"] == "2000000"


# --- permissions -------------------------------------------------------------

def test_a_viewer_sees_the_job_but_cannot_change_anything() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/jobs/harper", sub="u-view")
    assert page.status == 200
    assert "Harper kitchen remodel" in page.body
    assert "Save the budget" not in page.body
    assert "Raise the application" not in page.body

    for path, body in [
        ("/t/acme/jobs", "name=X&customer_id=harper-residence"),
        ("/t/acme/jobs/harper/budget", "bid_LAB=1"),
        ("/t/acme/jobs/harper/bill/progress", "id=X&date=2026-07-31&pct1=100"),
        ("/t/acme/jobs/harper/deposits", "amount=100&date=2026-07-31"),
    ]:
        assert _req(app, path, sub="u-view", method="POST", body=body).status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_the_nav_carries_jobs() -> None:
    app, _t, _a = _app()
    assert "/t/acme/jobs" in _req(app, "/t/acme/jobs").body
