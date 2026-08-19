"""The payroll screens, against a fake ledger transport.

The thing these guard is that the screen tells the truth about cost: gross plus
the employer's own taxes, not the net that left the bank — and that the money
still owed is impossible to miss.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog,
    InMemoryUserDirectory,
    JwtAuthenticator,
    LedgerClient,
    LedgerResponse,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)

SECRET = "payroll-secret"
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


EMPLOYEES = {
    "tenant": "acme",
    "employees": [
        {"id": "ada", "name": "Ada Reyes", "active": True},
        {"id": "jo", "name": "Jo Okafor", "active": True},
    ],
}

TOTALS = {
    "gross_minor": "800000", "employee_taxes_minor": "170000",
    "deductions_minor": "20000", "net_minor": "610000",
    "employer_taxes_minor": "61200",
    "total_cost_minor": "861200", "liability_minor": "251200",
}

RUN = {
    "id": "PR-2026-08-15", "date": "2026-08-15", "status": "DRAFT",
    "memo": "August 1–15", "entry_id": "", "bank_code": "1000",
    "lines": [
        {"employee_id": "ada", "gross_minor": "500000", "employee_taxes_minor": "110000",
         "deductions_minor": "20000", "net_minor": "370000"},
        {"employee_id": "jo", "gross_minor": "300000", "employee_taxes_minor": "60000",
         "deductions_minor": "0", "net_minor": "240000"},
    ],
    "totals": TOTALS,
}

POSTED_RUN = dict(RUN, status="POSTED", entry_id="acme:12")

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/payroll/employees": (200, EMPLOYEES),
    "POST /t/acme/payroll/employees": (201, {"employee": {"id": "sam", "name": "Sam"}}),
    "GET /t/acme/payroll/runs": (200, {"tenant": "acme", "runs": [RUN]}),
    "GET /t/acme/payroll/runs/PR-2026-08-15": (200, {"run": POSTED_RUN}),
    "POST /t/acme/payroll/runs": (201, {"run": RUN}),
    "POST /t/acme/payroll/runs/PR-2026-08-15/post": (200, {"run": POSTED_RUN, "entry_id": "acme:12"}),
    "POST /t/acme/payroll/runs/PR-2026-08-15/void": (200, {"run": dict(RUN, status="VOID")}),
    "GET /t/acme/payroll/liabilities": (200, {
        "account_code": "2300", "account_name": "Payroll Liabilities",
        "owed_minor": "251200", "posted_runs": 1, "draft_runs": 0,
    }),
    "POST /t/acme/payroll/remit": (200, {
        "entry_id": "acme:13", "date": "2026-08-18",
        "amount_minor": "170000", "remaining_minor": "81200",
    }),
}


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("3000.00"))
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
                   ForecastConfig(minimum_cash=Money.from_decimal("1000.00")), token="unused")
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
    body = [c for c in transport.calls if c[0] == "POST" and needle in c[1]][0][2]
    return json.loads(body)  # type: ignore[no-any-return]


# --- the home screen ---------------------------------------------------------

def test_the_run_list_leads_with_what_it_cost_not_what_was_paid() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/payroll")
    assert r.status == 200
    assert "$8,612.00" in r.body   # real cost: gross + employer taxes
    assert "$6,100.00" in r.body   # net actually paid
    assert "$8,000.00" in r.body   # gross
    assert "Real cost" in r.body and "Owed after" in r.body


def test_what_is_still_owed_is_impossible_to_miss() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/payroll")
    assert "You owe $2,512.00 in payroll liabilities" in r.body
    assert "held until you deposit them" in r.body
    assert "Make a payroll tax deposit" in r.body


def test_no_liability_means_no_deposit_form() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/payroll/liabilities"] = (200, {
        "account_code": "2300", "account_name": "Payroll Liabilities",
        "owed_minor": "0", "posted_runs": 0, "draft_runs": 0,
    })
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/payroll")
    assert "No outstanding payroll liabilities" in r.body
    assert "Make a payroll tax deposit" not in r.body


def test_a_run_detail_spells_out_where_the_money_went() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/payroll/PR-2026-08-15")
    assert r.status == 200
    assert "Ada Reyes" in r.body and "Jo Okafor" in r.body
    assert "$3,700.00" in r.body and "$2,400.00" in r.body    # per-employee net
    assert "What this run actually cost" in r.body
    assert "acme:12" in r.body                                # the journal entry


def test_a_service_outage_is_reported_not_hidden() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/payroll/runs"] = (503, {"error": "connection refused"})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/payroll")
    assert "Payroll unavailable" in r.body


# --- drafting ----------------------------------------------------------------

def test_drafting_parses_amounts_exactly_and_skips_unpaid_employees() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/payroll", method="POST",
             body="id=PR-1&date=2026-08-15&memo=Aug&employer_taxes=612.00"
                  "&employee1=ada&gross1=5,000.00&taxes1=1100.00&deductions1=200.00"
                  "&employee2=jo&gross2=&taxes2=&deductions2=")
    assert r.status == 302
    sent = _sent(transport, "/payroll/runs")
    assert sent["employer_taxes_minor"] == "61200"
    assert len(sent["lines"]) == 1, "Jo had no gross pay this run"
    assert sent["lines"][0] == {
        "employee_id": "ada", "gross_minor": "500000",
        "employee_taxes_minor": "110000", "deductions_minor": "20000",
    }
    assert any(e.action == "payroll.drafted" for e in audit.events(tenant_id="acme"))


def test_a_run_with_nobody_paid_never_reaches_the_service() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/payroll", method="POST",
             body="date=2026-08-15&employee1=ada&gross1=")
    assert "err=" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if "/payroll/runs" in c[1]]


def test_a_malformed_amount_is_reported_not_guessed() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/payroll", method="POST",
             body="date=2026-08-15&employee1=ada&gross1=five+thousand")
    assert "not%20a%20valid%20amount" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if "/payroll/runs" in c[1]]


def test_a_rejected_draft_surfaces_the_ledgers_reason() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/payroll/runs"] = (
        400, {"error": "ada: withholdings and deductions come to more than gross pay"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/payroll", method="POST",
             body="date=2026-08-15&employee1=ada&gross1=100.00&taxes1=200.00")
    assert "more%20than%20gross%20pay" in str(r.headers.get("Location", ""))


# --- posting and voiding -----------------------------------------------------

def test_posting_says_what_it_booked() -> None:
    app, _t, audit = _app()
    r = _req(app, "/t/acme/payroll/PR-2026-08-15/post", method="POST")
    assert r.status == 302
    loc = str(r.headers.get("Location", ""))
    assert "gross%20wages" in loc and "liability" in loc
    assert any(e.action == "payroll.post" for e in audit.events(tenant_id="acme"))


def test_voiding_says_the_original_stays() -> None:
    app, _t, audit = _app()
    r = _req(app, "/t/acme/payroll/PR-2026-08-15/void", method="POST")
    loc = str(r.headers.get("Location", ""))
    assert "reversing%20entry" in loc and "original%20stays" in loc
    assert any(e.action == "payroll.void" for e in audit.events(tenant_id="acme"))


def test_a_closed_period_refusal_reaches_the_owner() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/payroll/runs/PR-2026-08-15/post"] = (
        409, {"error": "period 2026-08 is closed"}
    )
    app, _t, audit = _app(routes)
    r = _req(app, "/t/acme/payroll/PR-2026-08-15/post", method="POST")
    assert "period%202026-08%20is%20closed" in str(r.headers.get("Location", ""))
    assert not [e for e in audit.events(tenant_id="acme") if e.action == "payroll.post"]


# --- remittance --------------------------------------------------------------

def test_a_deposit_is_sent_exactly_and_reports_what_is_left() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/payroll/remit", method="POST",
             body="date=2026-08-18&amount=1,700.00&bank_code=1000")
    assert r.status == 302
    assert _sent(transport, "/payroll/remit")["amount_minor"] == "170000"
    assert "812.00%20still%20owed" in str(r.headers.get("Location", ""))
    assert any(e.action == "payroll.remitted" for e in audit.events(tenant_id="acme"))


def test_an_overpayment_refusal_is_shown_plainly() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/payroll/remit"] = (
        400, {"error": "payroll liabilities are 2512.00 — a payment of 3000.00 would overpay"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/payroll/remit", method="POST", body="date=2026-08-18&amount=3000.00")
    assert "would%20overpay" in str(r.headers.get("Location", ""))


# --- employees and permissions -----------------------------------------------

def test_an_employee_can_be_added() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/payroll/employees", method="POST", body="name=Sam+O%27Neil")
    assert r.status == 302
    assert _sent(transport, "/payroll/employees")["name"] == "Sam O'Neil"


def test_a_viewer_can_see_payroll_but_not_run_it() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/payroll", sub="u-view")
    assert page.status == 200
    assert "$8,612.00" in page.body          # they can see the cost
    assert "Run payroll" not in page.body
    assert "Make a payroll tax deposit" not in page.body

    for path, body in [
        ("/t/acme/payroll", "date=2026-08-15&employee1=ada&gross1=100"),
        ("/t/acme/payroll/PR-2026-08-15/post", ""),
        ("/t/acme/payroll/PR-2026-08-15/void", ""),
        ("/t/acme/payroll/remit", "date=2026-08-18&amount=10"),
        ("/t/acme/payroll/employees", "name=X"),
    ]:
        assert _req(app, path, sub="u-view", method="POST", body=body).status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_payroll_is_in_the_nav() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/payroll")
    assert ">Payroll<" in r.body
