"""Estimates and the pipeline on the owner-facing screens.

The estimate builder's job is to keep the cost side of the document alive. A
quote that stores only prices cannot seed a job budget and cannot answer "we bid
this at 22% and finished at 9%".

The pipeline's job is to be worth two numbers at once — what is on the table and
what to plan around — and to touch the books with neither of them.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "est-secret"
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


ESTIMATE = {
    "id": "EST-1", "root_id": "EST-1", "revision": 1, "customer_id": "harper",
    "job_id": "", "date": "2026-05-01", "expiry_date": "2026-06-30", "status": "SENT",
    "tax_rate_ppm": 0, "memo": "Harper kitchen remodel",
    "lines": [
        {"line_no": 1, "description": "Framing labor", "cost_code": "LAB",
         "quantity_milli": "40000", "unit_cost_minor": "6500", "markup_ppm": 200_000,
         "unit_price_minor": "7800", "extended_cost_minor": "260000",
         "extended_price_minor": "312000", "margin_ppm": 166_667,
         "account_code": "4100", "taxable": True},
        {"line_no": 2, "description": "Cabinets", "cost_code": "MAT",
         "quantity_milli": "1000", "unit_cost_minor": "1800000", "markup_ppm": 150_000,
         "unit_price_minor": "2070000", "extended_cost_minor": "1800000",
         "extended_price_minor": "2070000", "margin_ppm": 130_435,
         "account_code": "4100", "taxable": True},
    ],
    "totals": {"cost_minor": "2060000", "price_minor": "2382000", "tax_minor": "0",
               "total_minor": "2382000", "margin_minor": "322000",
               "margin_ppm": 135_180, "markup_ppm": 156_310},
}

PIPELINE = {
    "contract": "sales-pipeline/1", "currency": "USD",
    "stages": [
        {"stage": "NEW", "count": 1, "value_minor": "8500000",
         "weighted_value_minor": "850000"},
        {"stage": "QUALIFIED", "count": 0, "value_minor": "0",
         "weighted_value_minor": "0"},
        {"stage": "PROPOSAL", "count": 1, "value_minor": "2000000",
         "weighted_value_minor": "1000000"},
        {"stage": "NEGOTIATION", "count": 0, "value_minor": "0",
         "weighted_value_minor": "0"},
        {"stage": "WON", "count": 1, "value_minor": "4000000",
         "weighted_value_minor": "4000000"},
        {"stage": "LOST", "count": 1, "value_minor": "1000000",
         "weighted_value_minor": "0"},
    ],
    "opportunities": [
        {"id": "OPP-1", "lead_id": "L-1", "customer_id": "harper",
         "name": "Kitchen remodel", "stage": "NEW", "value_minor": "8500000",
         "probability_ppm": 100_000, "weighted_value_minor": "850000",
         "expected_close_date": "2026-06-15", "owner": "jason", "source": "Referral",
         "estimate_id": "", "job_id": "", "lost_reason": "", "created_date": "2026-05-02"},
    ],
    "totals": {"open_value_minor": "10500000", "open_weighted_minor": "1850000",
               "won_count": 1, "lost_count": 1, "won_value_minor": "4000000",
               "win_rate_ppm": 500_000},
    "lost_reasons": [{"reason": "Price — went with a cheaper bid", "count": 1}],
}

LEADS = {"leads": [
    {"id": "L-2", "name": "Sam Okonkwo", "company": "Okonkwo Property",
     "email": "", "phone": "", "source": "Website", "status": "NEW", "owner": "jason",
     "created_date": "2026-05-10", "notes": "", "customer_id": ""},
]}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/estimates": (200, {"estimates": [ESTIMATE]}),
    "GET /t/acme/estimates/EST-1": (200, {"estimate": ESTIMATE}),
    "POST /t/acme/estimates": (201, {"estimate": ESTIMATE}),
    "POST /t/acme/estimates/EST-1/accept": (200, {
        "estimate": {**ESTIMATE, "status": "ACCEPTED"}, "job_id": "harper-kitchen",
        "job_created": True, "budget_lines_seeded": 2,
    }),
    "POST /t/acme/estimates/EST-1/status": (200, {"estimate": ESTIMATE}),
    "POST /t/acme/estimates/EST-1/revise": (201, {
        "estimate": {**ESTIMATE, "id": "EST-1-r2", "revision": 2, "status": "DRAFT"},
    }),
    "POST /t/acme/estimates/EST-1/invoice": (201, {"invoice": {"id": "INV-1"}}),
    "GET /t/acme/jobs": (200, {"jobs": []}),
    "GET /t/acme/customers": (200, {"parties": [{"id": "harper", "name": "Harper Residence"}]}),
    "GET /t/acme/cost-codes": (200, {"cost_codes": [
        {"code": "LAB", "name": "Labor", "category": "LABOR",
         "account_code": "5500", "active": True},
    ]}),
    "GET /t/acme/accounts": (200, {"accounts": [
        {"code": "4100", "name": "Contract Income", "type": "REVENUE"},
    ]}),
    "GET /t/acme/pipeline": (200, PIPELINE),
    "GET /t/acme/leads": (200, LEADS),
    "POST /t/acme/leads": (201, {"lead": LEADS["leads"][0]}),
    "POST /t/acme/leads/L-2/convert": (201, {"customer_id": "okonkwo-property"}),
    "POST /t/acme/opportunities": (201, {"opportunity": PIPELINE["opportunities"][0]}),
    "POST /t/acme/opportunities/OPP-1/win": (200, {
        "opportunity": {**PIPELINE["opportunities"][0], "stage": "WON"},  # type: ignore[dict-item]
    }),
    "POST /t/acme/opportunities/OPP-1/lose": (200, {
        "opportunity": {**PIPELINE["opportunities"][0], "stage": "LOST"},  # type: ignore[dict-item]
    }),
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


# --- estimates ---------------------------------------------------------------

def test_the_estimate_list_shows_cost_price_and_margin() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/estimates")
    assert r.status == 200
    assert "$20,600.00" in r.body and "$23,820.00" in r.body
    assert "13.5%" in r.body


def test_an_estimate_shows_markup_and_margin_on_every_line() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/estimates/EST-1")
    assert "16.7%" in r.body, "the framing line's margin"
    assert "20.0%" in r.body, "…beside its markup"
    assert "They are never equal" in r.body


def test_sales_tax_is_named_as_the_states_money() -> None:
    routes = dict(DEFAULT_ROUTES)
    taxed = dict(ESTIMATE)
    taxed["totals"] = {**ESTIMATE["totals"], "tax_minor": "196515",  # type: ignore[dict-item]
                       "total_minor": "2578515"}
    routes["GET /t/acme/estimates/EST-1"] = (200, {"estimate": taxed})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/estimates/EST-1")
    assert "$1,965.15 of sales tax" in r.body
    assert "state's money" in r.body or "state&#x27;s money" in r.body


def test_the_builder_sends_cost_and_markup_exactly() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/estimates", method="POST",
             body="customer_id=harper&date=2026-05-01&memo=Harper"
                  "&desc1=Framing&code1=LAB&qty1=40&cost1=65.00&markup1=20&account1=4100")
    assert r.status == 302
    sent = _sent(transport, "/estimates")
    assert sent["lines"] == [{
        "description": "Framing", "quantity_milli": "40000",
        "unit_cost_minor": "6500", "cost_code": "LAB", "account_code": "4100",
        "markup_ppm": 200_000,
    }]
    assert any(e.action == "estimate.saved" for e in audit.events(tenant_id="acme"))


def test_a_price_typed_directly_is_sent_instead_of_a_markup() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/estimates", method="POST",
         body="customer_id=harper&date=2026-05-01"
              "&desc1=Cabinets&cost1=4000.00&price1=5000.00&account1=4100")
    line = _sent(transport, "/estimates")["lines"][0]
    assert line["unit_price_minor"] == "500000"
    assert "markup_ppm" not in line


def test_a_fractional_quantity_stays_exact() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/estimates", method="POST",
         body="customer_id=harper&date=2026-05-01&desc1=Days&qty1=2.5&cost1=450.00")
    assert _sent(transport, "/estimates")["lines"][0]["quantity_milli"] == "2500"


def test_a_blank_row_is_not_a_zero_line() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/estimates", method="POST",
         body="customer_id=harper&date=2026-05-01&desc1=Framing&cost1=100.00&desc2=&cost2=")
    assert len(_sent(transport, "/estimates")["lines"]) == 1


def test_accepting_says_the_budget_came_from_the_estimate() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/estimates/EST-1/accept", method="POST",
             body="job_name=Harper+kitchen&billing_method=PROGRESS&retainage=10")
    assert r.status == 302
    assert "budget" in str(r.headers.get("Location", ""))
    assert _sent(transport, "/accept")["retainage_ppm"] == 100_000
    assert any(e.action == "estimate.accept" for e in audit.events(tenant_id="acme"))


def test_revising_lands_on_the_new_revision_and_says_the_old_one_survives() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/estimates/EST-1/revise", method="POST", body="memo=Upgraded")
    location = str(r.headers.get("Location", ""))
    assert "/estimates/EST-1-r2" in location
    assert "superseded" in location


def test_a_superseded_estimate_offers_no_revision() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/estimates/EST-1"] = (
        200, {"estimate": {**ESTIMATE, "status": "SUPERSEDED"}}
    )
    app, _t, _a = _app(routes)
    assert "Start a revision" not in _req(app, "/t/acme/estimates/EST-1").body


def test_only_an_accepted_estimate_offers_to_be_invoiced() -> None:
    app, _t, _a = _app()
    assert "Invoice the whole thing" not in _req(app, "/t/acme/estimates/EST-1").body
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/estimates/EST-1"] = (
        200, {"estimate": {**ESTIMATE, "status": "ACCEPTED"}}
    )
    app2, _t2, _a2 = _app(routes)
    assert "Invoice the whole thing" in _req(app2, "/t/acme/estimates/EST-1").body


def test_the_services_refusal_reaches_the_owner() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/estimates/EST-1/accept"] = (
        400, {"error": "estimate EST-1 expired on 2026-06-30"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/estimates/EST-1/accept", method="POST", body="job_name=X")
    assert "expired%20on" in str(r.headers.get("Location", ""))


# --- the pipeline ------------------------------------------------------------

def test_the_pipeline_reports_both_numbers_and_says_why() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/pipeline")
    assert r.status == 200
    assert "$105,000.00 is on the table" in r.body
    assert "$18,500.00 is what to plan around" in r.body
    assert "hiring too early or turning work away" in r.body
    assert "win rate 50.0%" in r.body


def test_lost_reasons_are_ranked_and_explained() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/pipeline")
    assert "Price — went with a cheaper bid" in r.body
    assert "a reason is required" in r.body


def test_winning_says_nothing_is_booked_yet() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/opportunities/OPP-1/win", method="POST")
    assert "nothing%20is%20booked" in str(r.headers.get("Location", ""))


def test_losing_sends_the_reason() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/opportunities/OPP-1/lose", method="POST", body="reason=Timing")
    assert _sent(transport, "/lose")["reason"] == "Timing"


def test_a_lead_becomes_a_customer_from_the_pipeline() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/leads/L-2/convert", method="POST")
    assert r.status == 302
    assert any(c[1].endswith("/convert") for c in transport.calls)
    assert any(e.action == "lead.converted" for e in audit.events(tenant_id="acme"))


def test_the_new_opportunity_form_says_it_does_not_touch_the_books() -> None:
    app, _t, _a = _app()
    assert "money appears when the job is billed" in _req(app, "/t/acme/pipeline").body


# --- permissions -------------------------------------------------------------

def test_a_viewer_reads_both_screens_and_changes_neither() -> None:
    app, transport, _a = _app()
    assert "Build an estimate" not in _req(app, "/t/acme/estimates", sub="u-view").body
    assert "Add the lead" not in _req(app, "/t/acme/pipeline", sub="u-view").body
    for path, body in [
        ("/t/acme/estimates", "customer_id=harper&date=2026-05-01&cost1=1"),
        ("/t/acme/estimates/EST-1/accept", "job_name=X"),
        ("/t/acme/leads", "name=X"),
        ("/t/acme/opportunities", "name=X"),
        ("/t/acme/opportunities/OPP-1/lose", "reason=X"),
    ]:
        assert _req(app, path, sub="u-view", method="POST", body=body).status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]
