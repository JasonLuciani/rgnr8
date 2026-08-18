"""Work orders, sales orders and purchase orders on the owner-facing screens.

Three things these pages have to get right. A work order's time entry must not
read as a second wage — the screen says what it actually posts. A sales order's
backlog must be named as the thing a general ledger cannot tell you. And a
purchase order must show ordered, received and billed on the same row, because
a three-way match is only a control if a person can see which of the three
disagrees.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "orders-secret"
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


WORK_ORDER = {
    "id": "WO-1", "job_id": "harper", "sales_order_id": "", "title": "Hang the doors",
    "description": "", "status": "SCHEDULED", "scheduled_date": "2026-07-14",
    "completed_date": "", "assignee_id": "marco", "assignee": "Marco Diaz", "memo": "",
    "entries": [
        {"id": "WO-1-e1", "work_order_id": "WO-1", "kind": "LABOR", "date": "2026-07-14",
         "description": "Diagnose and repair", "cost_code": "LAB", "employee_id": "marco",
         "quantity_milli": "8000", "unit_cost_minor": "5200", "unit_bill_minor": "11000",
         "extended_cost_minor": "41600", "extended_bill_minor": "88000", "billable": True,
         "cost_account_code": "5500", "relieve_account_code": "6200",
         "entry_id": "acme:9", "invoice_id": "", "posted": True},
        {"id": "WO-1-e2", "work_order_id": "WO-1", "kind": "EQUIPMENT", "date": "2026-07-14",
         "description": "Lift hire", "cost_code": "EQP", "employee_id": "",
         "quantity_milli": "1000", "unit_cost_minor": "9000", "unit_bill_minor": "0",
         "extended_cost_minor": "9000", "extended_bill_minor": "0", "billable": False,
         "cost_account_code": "5300", "relieve_account_code": "",
         "entry_id": "", "invoice_id": "", "posted": False},
    ],
    "totals": {"hours_milli": "8000", "cost_minor": "50600", "billable_minor": "88000",
               "unbilled_minor": "88000", "margin_minor": "37400"},
}

SALES_ORDER = {
    "id": "SO-1", "customer_id": "harper", "job_id": "harper", "estimate_id": "",
    "date": "2026-06-01", "requested_date": "2026-08-15", "status": "PARTIAL",
    "tax_rate_ppm": 0, "memo": "Doors and windows",
    "lines": [
        {"line_no": 1, "description": "Interior doors", "cost_code": "MAT",
         "quantity_milli": "10000", "invoiced_milli": "4000", "remaining_milli": "6000",
         "unit_price_minor": "45000", "extended_price_minor": "450000",
         "account_code": "4100", "taxable": True},
    ],
    "totals": {"ordered_minor": "450000", "invoiced_minor": "180000",
               "remaining_minor": "270000", "tax_minor": "0", "total_minor": "450000"},
}

PURCHASE_ORDER = {
    "purchase_order": {
        "id": "PO-1", "vendor_id": "buildmart", "job_id": "harper", "date": "2026-06-10",
        "expected_date": "2026-06-20", "status": "PARTIAL", "memo": "",
        "lines": [
            {"line_no": 1, "description": "Plywood", "cost_code": "MAT",
             "account_code": "5100", "quantity_milli": "100000",
             "received_milli": "60000", "accrued_milli": "60000", "billed_milli": "0",
             "unit_price_minor": "4800", "ordered_minor": "480000",
             "received_minor": "288000", "billed_minor": "0"},
        ],
        "totals": {"ordered_minor": "480000", "received_minor": "288000",
                   "billed_minor": "0", "committed_minor": "480000"},
    },
    "receipts": [
        {"id": "PO-1-R1", "purchase_order_id": "PO-1", "date": "2026-06-20",
         "memo": "Half of it", "accrued": True, "entry_id": "acme:4",
         "lines": [{"line_no": 1, "quantity_milli": "60000"}]},
    ],
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/work-orders": (200, {"work_orders": [WORK_ORDER]}),
    "GET /t/acme/work-orders/WO-1": (200, {"work_order": WORK_ORDER}),
    "POST /t/acme/work-orders": (201, {"work_order": WORK_ORDER}),
    "POST /t/acme/work-orders/WO-1/entries": (201, {
        "entry": WORK_ORDER["entries"][0], "unposted_reason": "",  # type: ignore[index]
    }),
    "POST /t/acme/work-orders/WO-1/complete": (200, {"work_order": WORK_ORDER}),
    "GET /t/acme/sales-orders": (200, {"orders": [SALES_ORDER]}),
    "GET /t/acme/sales-orders/SO-1": (200, {"order": SALES_ORDER}),
    "GET /t/acme/sales-orders/backlog": (200, {
        "contract": "sales-backlog/1", "currency": "USD",
        "orders": [], "remaining_minor": "18000000",
    }),
    "POST /t/acme/sales-orders": (201, {"order": SALES_ORDER}),
    "POST /t/acme/sales-orders/SO-1/invoice": (201, {
        "order": SALES_ORDER, "invoice": {"id": "INV-1"},
    }),
    "GET /t/acme/purchase-orders": (200, {
        "purchase_orders": [PURCHASE_ORDER["purchase_order"]],
    }),
    "GET /t/acme/purchase-orders/PO-1": (200, PURCHASE_ORDER),
    "GET /t/acme/purchase-orders/committed": (200, {
        "contract": "purchase-commitments/1", "currency": "USD",
        "orders": [], "committed_minor": "480000",
    }),
    "POST /t/acme/purchase-orders": (201, {"purchase_order": PURCHASE_ORDER["purchase_order"]}),
    "POST /t/acme/purchase-orders/PO-1/receipts": (201, {
        "receipt": {}, "purchase_order": PURCHASE_ORDER["purchase_order"],
        "accrued_minor": "288000",
    }),
    "POST /t/acme/purchase-orders/PO-1/bill": (201, {
        "purchase_order": PURCHASE_ORDER["purchase_order"], "bill": {"id": "BILL-1"},
        "variances": [{"line_no": 1, "ordered_minor": "288000",
                       "billed_minor": "312000", "variance_minor": "24000"}],
        "variance_entry_id": "acme:12",
    }),
    "GET /t/acme/jobs": (200, {"jobs": [{"id": "harper", "name": "Harper kitchen"}]}),
    "GET /t/acme/customers": (200, {"parties": [{"id": "harper", "name": "Harper Residence"}]}),
    "GET /t/acme/vendors": (200, {"parties": [{"id": "buildmart", "name": "BuildMart"}]}),
    "GET /t/acme/cost-codes": (200, {"cost_codes": [
        {"code": "LAB", "name": "Labor", "category": "LABOR",
         "account_code": "5500", "active": True},
    ]}),
    "GET /t/acme/payroll/employees": (200, {"employees": [
        {"id": "marco", "name": "Marco Diaz", "active": True},
    ]}),
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


# --- work orders -------------------------------------------------------------

def test_work_orders_are_split_into_to_do_and_done() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/work-orders")
    assert r.status == 200
    assert "To do (1)" in r.body
    assert "Hang the doors" in r.body


def test_the_time_form_says_what_it_actually_posts() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/work-orders/WO-1")
    assert "do not book a second wage" in r.body
    assert "onto the job that used it" in r.body
    assert "about 70% of the truth" in r.body


def test_an_entry_that_could_not_be_posted_is_called_out() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/work-orders/WO-1")
    assert "not in the books" in r.body
    assert "nowhere honest to take the cost from" in r.body


def test_booking_time_parses_hours_with_decimals() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/work-orders/WO-1/entries", method="POST",
             body="kind=LABOR&date=2026-07-14&cost_code=LAB&employee_id=marco"
                  "&quantity=7.5&billable=1&description=Hanging")
    assert r.status == 302
    sent = _sent(transport, "/entries")
    assert sent["quantity_milli"] == "7500"
    assert sent["billable"] is True
    assert "unit_cost_minor" not in sent, "an unset rate falls back to the employee's"
    assert any(e.action == "work_order.entries" for e in audit.events(tenant_id="acme"))


def test_an_unpostable_entry_reports_why_rather_than_claiming_success() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/work-orders/WO-1/entries"] = (201, {
        "entry": {}, "unposted_reason": "nothing to take the cost from",
    })
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/work-orders/WO-1/entries", method="POST",
             body="kind=EQUIPMENT&date=2026-07-14&quantity=4")
    assert "not%20in%20the%20books" in str(r.headers.get("Location", ""))


def test_completing_a_work_order_says_its_costs_are_unaffected() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/work-orders/WO-1"] = (200, {
        "work_order": {**WORK_ORDER, "status": "COMPLETE", "completed_date": "2026-07-16"},
    })
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/work-orders/WO-1")
    assert "Completed 2026-07-16" in r.body
    assert "stay where they were booked" in r.body


# --- sales orders ------------------------------------------------------------

def test_the_backlog_is_named_as_what_a_ledger_cannot_tell_you() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/sales-orders")
    assert "$180,000.00" in r.body
    assert "work agreed is not revenue" in r.body


def test_an_order_shows_ordered_invoiced_and_left_per_line() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/sales-orders/SO-1")
    assert "Interior doors" in r.body
    assert "$2,700.00 of this order is still to bill" in r.body


def test_invoicing_an_order_sends_only_the_lines_asked_for() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/sales-orders/SO-1/invoice", method="POST",
             body="id=INV-2&date=2026-08-01&qty1=3")
    assert r.status == 302
    sent = _sent(transport, "/sales-orders/SO-1/invoice")
    assert sent["lines"] == [{"line_no": 1, "quantity_milli": "3000"}]
    assert any(e.action == "sales_order.invoiced" for e in audit.events(tenant_id="acme"))


def test_leaving_every_line_blank_bills_everything_outstanding() -> None:
    """An empty line list reads as "everything that is left", which is what an
    owner means when they fill nothing in."""
    app, transport, _a = _app()
    _req(app, "/t/acme/sales-orders/SO-1/invoice", method="POST",
         body="id=INV-2&date=2026-08-01&qty1=")
    assert _sent(transport, "/invoice")["lines"] == []


# --- purchase orders ---------------------------------------------------------

def test_committed_cost_leads_the_purchasing_page_and_says_why() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/purchase-orders")
    assert "$4,800.00" in r.body
    assert "already on a signed order" in r.body


def test_ordered_received_and_billed_are_on_the_same_row() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/purchase-orders")
    assert "Ordered" in r.body and "Received" in r.body and "Billed" in r.body
    assert "which of the three disagrees" in r.body


def test_an_accrued_delivery_explains_what_the_bill_will_do() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/purchase-orders/PO-1")
    assert "already on the job" in r.body
    assert "rather than book it twice" in r.body


def test_receiving_can_accrue_and_says_so_afterwards() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/purchase-orders/PO-1/receipts", method="POST",
             body="date=2026-06-20&accrue=1&qty1=40")
    assert r.status == 302
    assert "accrued%20onto%20the%20job" in str(r.headers.get("Location", ""))
    sent = _sent(transport, "/receipts")
    assert sent["accrue"] is True
    assert sent["lines"] == [{"line_no": 1, "quantity_milli": "40000"}]


def test_the_receiving_form_explains_why_accruing_matters() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/purchase-orders/PO-1")
    assert "looks cheap until the invoice arrives" in r.body


def test_matching_a_bill_reports_the_variances_it_posted() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/purchase-orders/PO-1/bill", method="POST",
             body="id=BILL-1&date=2026-06-25&price1=52.00&accept_variance=1")
    assert r.status == 302
    assert "1%20price%20variance" in str(r.headers.get("Location", ""))
    sent = _sent(transport, "/bill")
    assert sent["accept_variance"] is True
    assert sent["lines"] == [{"line_no": 1, "unit_price_minor": "5200"}]
    assert any(e.action == "purchase_order.bill" for e in audit.events(tenant_id="acme"))


def test_a_refused_match_reaches_the_owner_with_the_reason() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/purchase-orders/PO-1/bill"] = (
        400, {"error": "line 1: only 60000 of the 60000 received are unbilled"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/purchase-orders/PO-1/bill", method="POST",
             body="id=BILL-1&date=2026-06-25")
    assert "unbilled" in str(r.headers.get("Location", ""))


# --- permissions -------------------------------------------------------------

def test_a_viewer_reads_all_three_and_changes_none() -> None:
    app, transport, _a = _app()
    assert "Schedule work" not in _req(app, "/t/acme/work-orders", sub="u-view").body
    assert "Take an order" not in _req(app, "/t/acme/sales-orders", sub="u-view").body
    assert "Order materials" not in _req(app, "/t/acme/purchase-orders", sub="u-view").body
    for path, body in [
        ("/t/acme/work-orders", "title=X&job_id=harper"),
        ("/t/acme/work-orders/WO-1/entries", "date=2026-07-14&quantity=1"),
        ("/t/acme/sales-orders", "customer_id=harper&date=2026-06-01&price1=100"),
        ("/t/acme/sales-orders/SO-1/invoice", "id=X&date=2026-08-01"),
        ("/t/acme/purchase-orders", "vendor_id=buildmart&date=2026-06-10&price1=100"),
        ("/t/acme/purchase-orders/PO-1/receipts", "date=2026-06-20"),
    ]:
        assert _req(app, path, sub="u-view", method="POST", body=body).status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]
