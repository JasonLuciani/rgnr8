"""Inventory, the WIP schedule, and the consolidation worksheet.

Each of these pages exists to show a number that a general ledger cannot show
on its own, and each has one thing it must refuse to fudge:

* inventory shows the gap between the shelf and the balance sheet rather than
  picking a side,
* the WIP schedule caps a blown estimate and says so instead of quietly earning
  more than the contract,
* and consolidation refuses to print a total when the entities disagree about
  what they owe each other.
"""

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryAuditLog, InMemoryUserDirectory, JwtAuthenticator, LedgerClient,
    LedgerResponse, Request, Role, User, WebApp, sign_jwt,
)

SECRET = "inv-secret"
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


VALUATION = {
    "contract": "inventory-valuation/1", "currency": "USD",
    "items": [
        {"sku": "PLY-34", "name": "3/4in plywood", "kind": "STOCK", "unit": "sheet",
         "quantity_milli": "18000", "unit_cost_minor": "5000", "value_minor": "90000",
         "inventory_account_code": "1300", "cost_account_code": "5100",
         "income_account_code": "4100", "reorder_point_milli": "20000",
         "active": True, "below_reorder_point": True},
    ],
    "accounts": [{"account_code": "1300", "items_value_minor": "90000",
                  "ledger_balance_minor": "90000", "difference_minor": "0"}],
    "totals": {"items_value_minor": "90000", "ledger_balance_minor": "90000",
               "difference_minor": "0", "ties_out": True},
    "reorder": [{"sku": "PLY-34", "name": "3/4in plywood",
                 "quantity_milli": "18000", "reorder_point_milli": "20000"}],
}

WIP = {
    "contract": "wip-schedule/1", "currency": "USD", "through": "2026-08-31",
    "rows": [{
        "job_id": "harper", "name": "Harper kitchen", "status": "ACTIVE",
        "billing_method": "PROGRESS", "cost_method": "AS_INCURRED",
        "contract_minor": "10000000", "cost_to_date_minor": "4000000",
        "estimated_cost_minor": "8000000", "cost_to_complete_minor": "4000000",
        "percent_complete_ppm": 500_000, "earned_revenue_minor": "5000000",
        "billed_minor": "3000000", "under_billed_minor": "2000000",
        "over_billed_minor": "0", "gross_profit_minor": "1000000",
        "costs_in_excess_minor": "0", "billings_in_excess_minor": "0",
        "work_in_progress_minor": "0", "adjustment_minor": "2000000",
        "estimate_exceeded": False, "projected_loss_minor": "0",
    }],
    "totals": {"contract_minor": "10000000", "cost_to_date_minor": "4000000",
               "earned_revenue_minor": "5000000", "billed_minor": "3000000",
               "under_billed_minor": "2000000", "over_billed_minor": "0",
               "gross_profit_minor": "1000000"},
}

CONSOLIDATED = {
    "contract": "consolidation/1", "currency": "USD",
    "group": {"id": "group", "name": "Harper Holdings",
              "intercompany_codes": ["1900", "2900"],
              "members": [{"tenant_id": "parent", "label": "Holdings", "ownership_ppm": 1000000},
                          {"tenant_id": "sub", "label": "Build", "ownership_ppm": 1000000}]},
    "through": "", "entities": [
        {"tenant_id": "parent", "label": "Holdings", "ownership_ppm": 1000000},
        {"tenant_id": "sub", "label": "Build", "ownership_ppm": 1000000},
    ],
    "rows": [
        {"account_code": "1000", "name": "Business Checking",
         "by_entity": {"parent": "-5000000", "sub": "5000000"},
         "combined_minor": "0", "elimination_minor": "0", "consolidated_minor": "0"},
        {"account_code": "1900", "name": "Due from affiliates",
         "by_entity": {"parent": "5000000", "sub": "0"},
         "combined_minor": "5000000", "elimination_minor": "-5000000",
         "consolidated_minor": "0"},
    ],
    "intercompany": [], "eliminations": [
        {"account_code": "1900", "signed_minor": "-5000000", "reason": "intercompany"},
    ],
    "mismatch_minor": "0", "balanced": True,
    "consolidated": {"rows": []},
}

REFUSED = {
    "contract": "consolidation/1", "currency": "USD",
    "group": CONSOLIDATED["group"], "through": "", "consolidated": None,
    "intercompany": [
        {"account_code": "1900", "by_entity": {"parent": "5000000", "sub": "0"},
         "net_minor": "5000000"},
        {"account_code": "2900", "by_entity": {"parent": "0", "sub": "-4958800"},
         "net_minor": "-4958800"},
    ],
    "mismatch_minor": "41200",
    "refused": "intercompany balances don't agree, so a consolidated total would be a plug. "
               "Find the difference — it is usually a payment in transit at the period end.",
    "entities": [
        {"tenant_id": "parent", "label": "Holdings"},
        {"tenant_id": "sub", "label": "Build"},
    ],
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/inventory": (200, VALUATION),
    "POST /t/acme/inventory/items": (201, {"item": VALUATION["items"][0]}),
    "POST /t/acme/inventory/receipts": (201, {"item": VALUATION["items"][0]}),
    "POST /t/acme/inventory/issues": (201, {"item": VALUATION["items"][0],
                                            "value_minor": "50000"}),
    "POST /t/acme/inventory/counts": (201, {"item": VALUATION["items"][0],
                                            "difference_milli": "-2000"}),
    "GET /t/acme/wip": (200, WIP),
    "POST /t/acme/wip/post": (200, {"posted": True, "entry_id": "acme:22",
                                    "jobs": [], "reason": ""}),
    "GET /t/acme/consolidation/groups": (200, {"groups": [CONSOLIDATED["group"]]}),
    "GET /t/acme/consolidation/groups/group/report": (200, CONSOLIDATED),
    "POST /t/acme/consolidation/groups": (201, {"group": CONSOLIDATED["group"]}),
    "POST /t/acme/consolidation/groups/group/eliminations": (201, {"elimination": {}}),
    "GET /t/acme/jobs": (200, {"jobs": [{"id": "harper", "name": "Harper kitchen"}]}),
    "GET /t/acme/cost-codes": (200, {"cost_codes": [
        {"code": "MAT", "name": "Materials", "category": "MATERIAL",
         "account_code": "5100", "active": True},
    ]}),
    "GET /t/acme/accounts": (200, {"accounts": [
        {"code": "1300", "name": "Materials Inventory", "type": "ASSET"},
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


# --- inventory ---------------------------------------------------------------

def test_inventory_leads_with_the_tie_out() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inventory")
    assert r.status == 200
    assert "$900.00 on the shelf" in r.body
    assert "$900.00 on the balance sheet" in r.body
    assert "They agree." in r.body


def test_a_gap_between_the_shelf_and_the_books_is_shown_not_hidden() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/inventory"] = (200, {
        **VALUATION,
        "totals": {"items_value_minor": "90000", "ledger_balance_minor": "140000",
                   "difference_minor": "-50000", "ties_out": False},
    })
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/inventory")
    assert "They differ by $500.00" in r.body
    assert "coded straight to the inventory account" in r.body


def test_items_below_their_reorder_point_are_listed() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inventory")
    assert "Running low" in r.body
    assert "PLY-34" in r.body


def test_issuing_stock_sends_the_job_and_says_what_it_was_costed_at() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/inventory/issues", method="POST",
             body="sku=PLY-34&date=2026-07-01&quantity=10&job_id=harper&cost_code=MAT")
    assert r.status == 302
    assert "moving%20average" in str(r.headers.get("Location", ""))
    sent = _sent(transport, "/issues")
    assert sent["quantity_milli"] == "10000"
    assert sent["job_id"] == "harper"
    assert any(e.action == "inventory.issues" for e in audit.events(tenant_id="acme"))


def test_a_count_that_already_agreed_says_so() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/inventory/counts"] = (201, {"item": {}, "difference_milli": "0"})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/inventory/counts", method="POST",
             body="sku=PLY-34&date=2026-07-31&counted=18")
    assert "already%20agreed" in str(r.headers.get("Location", ""))


def test_a_refusal_from_the_service_reaches_the_owner() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/inventory/issues"] = (
        400, {"error": "PLY-34: count it before you cost it"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/inventory/issues", method="POST",
             body="sku=PLY-34&date=2026-07-01&quantity=100&job_id=harper")
    assert "count%20it%20before" in str(r.headers.get("Location", ""))


# --- work in progress --------------------------------------------------------

def test_the_wip_schedule_shows_earned_against_billed() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/books/wip")
    assert r.status == 200
    assert "Harper kitchen" in r.body
    assert "$50,000.00 earned against $30,000.00 billed" in r.body
    assert "work done and not invoiced" in r.body


def test_a_blown_estimate_is_a_banner_not_a_capped_number() -> None:
    routes = dict(DEFAULT_ROUTES)
    row = dict(WIP["rows"][0])  # type: ignore[index,arg-type]
    row["estimate_exceeded"] = True
    routes["GET /t/acme/wip"] = (200, {**WIP, "rows": [row]})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/wip")
    assert "cost has passed the estimate" in r.body
    assert "cannot say anything useful about that job" in r.body


def test_a_foreseen_loss_is_called_out_on_the_schedule() -> None:
    routes = dict(DEFAULT_ROUTES)
    row = dict(WIP["rows"][0])  # type: ignore[index,arg-type]
    row["projected_loss_minor"] = "900000"
    routes["GET /t/acme/wip"] = (200, {**WIP, "rows": [row]})
    app, _t, _a = _app(routes)
    assert "expected to lose $9,000.00" in _req(app, "/t/acme/books/wip").body


def test_posting_the_adjustment_says_it_cannot_compound() -> None:
    app, transport, audit = _app()
    page = _req(app, "/t/acme/books/wip")
    assert "running it twice changes nothing" in page.body
    r = _req(app, "/t/acme/books/wip", method="POST",
             body="date=2026-08-31&through=2026-08-31")
    assert r.status == 302
    assert _sent(transport, "/wip/post")["include_loss_provision"] is False
    assert any(e.action == "wip.posted" for e in audit.events(tenant_id="acme"))


def test_a_schedule_already_agreed_with_reports_that_rather_than_claiming_a_post() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/wip/post"] = (200, {
        "posted": False, "entry_id": "", "jobs": [],
        "reason": "the balance sheet already agrees with the schedule — nothing to post",
    })
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/books/wip", method="POST", body="date=2026-08-31")
    assert "already%20agrees" in str(r.headers.get("Location", ""))


def test_the_loss_provision_is_opt_in_and_says_why() -> None:
    app, transport, _a = _app()
    assert "judgement about the future" in _req(app, "/t/acme/books/wip").body
    _req(app, "/t/acme/books/wip", method="POST",
         body="date=2026-08-31&include_loss_provision=1")
    assert _sent(transport, "/wip/post")["include_loss_provision"] is True


# --- consolidation -----------------------------------------------------------

def test_a_group_lists_the_entities_it_is_allowed_to_read() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/consolidation")
    assert r.status == 200
    assert "Harper Holdings" in r.body
    assert "Holdings, Build" in r.body


def test_the_worksheet_shows_an_entity_per_column_and_the_eliminations() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/consolidation/group")
    assert "Due from affiliates" in r.body
    assert "-$50,000.00" in r.body
    assert "what the group owed itself" in r.body


def test_disagreeing_entities_get_no_total_and_an_explanation() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/consolidation/groups/group/report"] = (200, REFUSED)
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/consolidation/group")
    assert "not consolidated" in r.body
    assert "payment in transit" in r.body
    assert "Out by $412.00" in r.body
    assert "Consolidate anyway" in r.body


def test_consolidating_anyway_warns_that_the_statements_still_refuse() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/consolidation/groups/group/report"] = (200, REFUSED)
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/consolidation/group")
    assert "not a worksheet you can annotate" in r.body


def test_an_unexplained_difference_is_still_flagged_when_it_is_shown() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/consolidation/groups/group/report"] = (
        200, {**CONSOLIDATED, "mismatch_minor": "41200"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/consolidation/group")
    assert "$412.00 of this is an intercompany difference nobody has explained" in r.body


def test_creating_a_group_sends_the_named_entities_only() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/consolidation", method="POST",
             body="name=Harper+Holdings&members=parent,+sub&intercompany_codes=1900,+2900")
    assert r.status == 302
    sent = _sent(transport, "/consolidation/groups")
    assert sent["members"] == [{"tenant_id": "parent"}, {"tenant_id": "sub"}]
    assert sent["intercompany_codes"] == ["1900", "2900"]
    assert any(e.action == "consolidation.group" for e in audit.events(tenant_id="acme"))


def test_an_elimination_that_is_half_an_entry_is_refused_before_it_is_sent() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/consolidation/group/eliminations", method="POST",
             body="description=X&code1=4100&debit1=100&credit1=100")
    assert "not%20both" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if "eliminations" in c[1]]


def test_the_elimination_form_says_why_it_has_to_balance() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/consolidation/group")
    assert "moves value into or out of the group" in r.body


# --- permissions -------------------------------------------------------------

def test_a_viewer_reads_and_changes_nothing() -> None:
    app, transport, _a = _app()
    assert "Add an item" not in _req(app, "/t/acme/inventory", sub="u-view").body
    assert "Post the adjustment" not in _req(app, "/t/acme/books/wip", sub="u-view").body
    for path, body in [
        ("/t/acme/inventory/items", "sku=X&name=X"),
        ("/t/acme/inventory/issues", "sku=X&date=2026-07-01&quantity=1"),
        ("/t/acme/books/wip", "date=2026-08-31"),
        ("/t/acme/consolidation", "name=X&members=a,b"),
    ]:
        assert _req(app, path, sub="u-view", method="POST", body=body).status == 403
    assert not [c for c in transport.calls if c[0] == "POST"]
