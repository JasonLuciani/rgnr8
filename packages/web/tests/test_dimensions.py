"""Classes and locations on the owner-facing screens.

The point of a dimension is that its value is *chosen*, never typed. So the
things worth defending here are: the form offers a dropdown of defined values
and nothing else; a business with no dimensions never sees the clutter; and the
selection actually reaches the ledger attached to the right line.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "dim-secret"
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


ACCOUNTS = {
    "accounts": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "4100", "name": "Food Sales", "type": "REVENUE"},
        {"code": "5000", "name": "Cost of Sales", "type": "EXPENSE"},
    ],
}

DIMENSIONS = {
    "dimensions": [
        {"key": "class", "label": "Line of business",
         "values": ["Dine-in", "Catering"], "required": True},
        {"key": "location", "label": "Site",
         "values": ["Denver, CO", "Boulder, CO"], "required": False},
    ],
}

TRIAL_BALANCE = {
    "currency": "USD", "in_balance": True, "rows": [],
    "total_debit_minor": "0", "total_credit_minor": "0",
}

REPORT = {
    "contract": "trial-balance-by-dimension/1", "dimension": "class",
    "label": "Line of business", "currency": "USD",
    "buckets": [
        {"value": "Catering", "unassigned": False, "in_balance": False,
         "total_debit_minor": "0", "total_credit_minor": "800000",
         "rows": [{"code": "4100", "name": "Food Sales", "type": "REVENUE",
                   "debit_minor": "0", "credit_minor": "800000"}]},
        {"value": "Dine-in", "unassigned": False, "in_balance": False,
         "total_debit_minor": "0", "total_credit_minor": "120000",
         "rows": [{"code": "4100", "name": "Food Sales", "type": "REVENUE",
                   "debit_minor": "0", "credit_minor": "120000"}]},
        {"value": "(unassigned)", "unassigned": True, "in_balance": False,
         "total_debit_minor": "0", "total_credit_minor": "5000",
         "rows": [{"code": "4100", "name": "Food Sales", "type": "REVENUE",
                   "debit_minor": "0", "credit_minor": "5000"}]},
    ],
}

INBOX = {
    "pending": 1, "posted": 0, "matched": 0, "excluded": 0,
    "pending_in_minor": "0", "pending_out_minor": "-40000",
    "items": [{
        "id": "bk-1", "account_code": "1000", "date": "2026-08-06",
        "amount_minor": "-40000", "description": "RESTAURANT DEPOT",
        "counterparty": "Restaurant Depot", "status": "PENDING",
        "category_code": "", "entry_id": "", "doc_kind": "", "doc_id": "", "note": "",
        "suggestion": {"account_code": "5000", "account_name": "Cost of Sales",
                       "confidence": 1.0, "reason": "Rule matched",
                       "source": "rule", "rule_id": "depot"},
        "matches": [],
    }],
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/accounts": (200, ACCOUNTS),
    "GET /t/acme/dimensions": (200, DIMENSIONS),
    "GET /t/acme/trial-balance": (200, TRIAL_BALANCE),
    "GET /t/acme/dimensions/class/report": (200, REPORT),
    "POST /t/acme/dimensions": (201, {"dimension": DIMENSIONS["dimensions"][0]}),
    "DELETE /t/acme/dimensions/class": (200, {"removed": "class"}),
    "POST /t/acme/entries": (201, {"entry": {"id": "acme:1"}}),
    "GET /t/acme/feed": (200, INBOX),
    "POST /t/acme/feed/txn/bk-1/accept": (200, {"id": "bk-1", "status": "POSTED"}),
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


# --- managing ----------------------------------------------------------------

def test_dimensions_are_listed_with_their_values_and_rule() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/dimensions")
    assert r.status == 200
    assert "Line of business" in r.body
    assert "Dine-in, Catering" in r.body
    assert "required on income and costs" in r.body
    assert "optional" in r.body


def test_an_empty_list_says_most_businesses_never_need_one() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/dimensions"] = (200, {"dimensions": []})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/dimensions")
    assert "Most single-line businesses never need one" in r.body


def test_adding_one_sends_the_values_and_the_rule() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/books/dimensions", method="POST",
             body="key=class&label=Line+of+business&values=Dine-in%0ACatering&required=1")
    assert r.status == 302
    sent = _sent(transport, "/dimensions")
    assert sent["key"] == "class"
    assert sent["values"] == "Dine-in\nCatering"
    assert sent["required"] is True
    assert any(e.action == "dimension.saved" for e in audit.events(tenant_id="acme"))


def test_the_services_refusal_reaches_the_owner() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/dimensions"] = (
        400, {"error": "a required dimension needs its allowed values listed"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/dimensions", method="POST", body="key=class&required=1")
    assert "needs%20its%20allowed%20values" in str(r.headers.get("Location", ""))


def test_one_can_be_removed() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/books/dimensions/class/delete", method="POST")
    assert r.status == 302
    assert any(c[0] == "DELETE" for c in transport.calls)
    assert any(e.action == "dimension.removed" for e in audit.events(tenant_id="acme"))


# --- choosing, not typing ----------------------------------------------------

def test_the_entry_form_offers_a_dropdown_of_defined_values_only() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books")
    assert 'name="dim_class1"' in r.body
    assert '<option value="Dine-in">' in r.body
    assert '<option value="Catering">' in r.body
    # a required dimension prompts for a choice; an optional one offers a dash
    assert "Line of business (required)" in r.body
    assert "Site</label>" in r.body


def test_a_business_with_no_dimensions_sees_no_clutter() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/dimensions"] = (200, {"dimensions": []})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books")
    assert "dim_" not in r.body
    assert "Record a transaction" in r.body


def test_a_class_chosen_on_the_entry_form_reaches_the_right_line() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/books/entries", method="POST",
         body="date=2026-08-05&memo=Dinner"
              "&code1=1000&debit1=1200.00"
              "&code2=4100&credit2=1200.00&dim_class2=Dine-in&dim_location2=Denver%2C+CO")
    sent = _sent(transport, "/entries")
    assert "dimensions" not in sent["lines"][0], "the bank line carries no class"
    assert sent["lines"][1]["dimensions"] == {"class": "Dine-in", "location": "Denver, CO"}


def test_a_blank_selection_is_left_off_entirely() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/books/entries", method="POST",
         body="date=2026-08-05&code1=1000&debit1=10.00&code2=4100&credit2=10.00"
              "&dim_class2=&dim_location2=")
    sent = _sent(transport, "/entries")
    assert all("dimensions" not in l for l in sent["lines"])


def test_a_feed_line_can_be_classified_as_it_is_accepted() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/inbox")
    assert 'name="dim_class"' in page.body
    _req(app, "/t/acme/inbox/bk-1/accept", method="POST",
         body="category_code=5000&dim_class=Catering")
    sent = _sent(transport, "/accept")
    assert sent["category_code"] == "5000"
    assert sent["dimensions"] == {"class": "Catering"}


# --- reporting ---------------------------------------------------------------

def test_the_report_shows_a_balance_per_value_with_unassigned_last() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/dimensions/class")
    assert r.status == 200
    assert "Line of business — by value" in r.body
    assert "$8,000.00" in r.body and "$1,200.00" in r.body
    # the unattributed bucket is called out rather than hidden
    assert "(unassigned)" in r.body
    assert "Activity nobody has attributed yet" in r.body
    assert r.body.index("Catering") < r.body.index("(unassigned)")


def test_the_report_window_reaches_the_service() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/books/dimensions/class?from=2026-08-01&to=2026-08-31")
    call = [c for c in transport.calls if "/report" in c[1]][0]
    assert "from=2026-08-01" in call[1] and "to=2026-08-31" in call[1]


def test_books_home_links_to_classes() -> None:
    app, _t, _a = _app()
    assert "/t/acme/books/dimensions" in _req(app, "/t/acme/books").body


# --- permissions -------------------------------------------------------------

def test_a_viewer_can_read_but_not_define() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/books/dimensions", sub="u-view")
    assert page.status == 200
    assert "Line of business" in page.body
    assert "Add one" not in page.body
    assert _req(app, "/t/acme/books/dimensions", sub="u-view",
                method="POST", body="key=x&values=A").status == 403
    assert _req(app, "/t/acme/books/dimensions/class/delete", sub="u-view",
                method="POST").status == 403
    assert _req(app, "/t/acme/books/dimensions/class", sub="u-view").status == 200
    assert not [c for c in transport.calls if c[0] in ("POST", "DELETE")]
