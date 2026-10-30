"""Editing the chart of accounts from the books screens.

The screens only offer what the ledger will allow: system and QuickBooks accounts
get no edit actions, and the reason is said in their place. Every change still goes
to the ledger, which enforces the rules, and the permission is checked on the server.
"""

from __future__ import annotations

import json
import re
from typing import Any

from test_books import DEFAULT_ROUTES, _app, _req

from rgnr8_web.books_screens import (
    DETAIL_TYPES,
    _STATEMENT_ORDER,
    render_account_form,
    render_chart_of_accounts,
)

# The ledger's chart as GET /accounts answers it: each account says whether the
# ledger lets it be edited (locked_by), and why not.
CHART: dict[str, Any] = {
    "tenant": "acme",
    "accounts": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK",
         "active": True, "parent_code": None, "origin": "quickbooks", "system": False,
         "locked_by": "quickbooks"},
        {"code": "1200", "name": "Accounts Receivable", "type": "ASSET",
         "subtype": "ACCOUNTS_RECEIVABLE", "active": True, "parent_code": None,
         "origin": "rgnr8", "system": True, "locked_by": "system"},
        {"code": "4000", "name": "Services Income", "type": "REVENUE", "subtype": "INCOME",
         "active": True, "parent_code": None, "origin": "rgnr8", "system": False,
         "locked_by": None},
        {"code": "6000", "name": "Advertising & Marketing", "type": "EXPENSE",
         "subtype": "EXPENSE", "active": True, "parent_code": None, "origin": "rgnr8",
         "system": False, "locked_by": None},
        {"code": "6300", "name": "Rent & Lease", "type": "EXPENSE", "subtype": "EXPENSE",
         "active": True, "parent_code": None, "origin": "rgnr8", "system": False,
         "locked_by": None},
        {"code": "6900", "name": "Old Subscriptions", "type": "EXPENSE", "subtype": "EXPENSE",
         "active": False, "parent_code": None, "origin": "rgnr8", "system": False,
         "locked_by": None},
    ],
}


def _routes(chart: dict[str, Any] | None = None) -> dict[str, tuple[int, dict[str, Any]]]:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/accounts"] = (200, chart if chart is not None else CHART)
    return routes


def _calls(transport: Any, method: str) -> list[tuple[str, str, str]]:
    return [c for c in transport.calls if c[0] == method]


def _row(html: str, code: str) -> str:
    """The table row for one account, found by its link to the account's register."""
    m = re.search(rf"<tr>(?:(?!</tr>).)*books/accounts/{re.escape(code)}\"(?:(?!</tr>).)*</tr>",
                  html, re.DOTALL)
    assert m, f"no row for {code}"
    return m.group(0)


# --- what the chart shows --------------------------------------------------------------

def test_an_rgnr8_account_shows_edit_and_inactivate() -> None:
    html = render_chart_of_accounts("acme", CHART, can_edit=True)
    row = _row(html, "6300")
    assert "/t/acme/books/accounts/6300/edit" in row
    assert 'action="/t/acme/books/accounts/6300/active"' in row
    assert "Inactivate" in row
    assert 'name="active" value="false"' in row


def test_a_system_account_has_no_edit_actions_and_says_why() -> None:
    html = render_chart_of_accounts("acme", CHART, can_edit=True)
    row = _row(html, "1200")
    assert "/books/accounts/1200/edit" not in row
    assert "/books/accounts/1200/active" not in row
    assert "System account" in row
    assert 'title="The books depend on this account' in row


def test_a_quickbooks_account_has_no_edit_actions_and_says_why() -> None:
    html = render_chart_of_accounts("acme", CHART, can_edit=True)
    row = _row(html, "1000")
    assert "/books/accounts/1000/edit" not in row
    assert "/books/accounts/1000/active" not in row
    assert "In QuickBooks" in row
    assert "kept in QuickBooks until the cutover" in row


def test_a_viewer_sees_the_chart_with_no_edit_actions_at_all() -> None:
    html = render_chart_of_accounts("acme", CHART, can_edit=False)
    assert "New account" not in html
    assert "/books/accounts/6300/edit" not in html
    assert "Inactivate" not in html
    assert "<th class=\"acts\">" not in html


def test_new_account_is_offered_to_an_editor() -> None:
    html = render_chart_of_accounts("acme", CHART, can_edit=True)
    assert 'href="/t/acme/books/accounts/new"' in html and "New account" in html


def test_inactive_accounts_are_hidden_behind_a_toggle() -> None:
    hidden = render_chart_of_accounts("acme", CHART, can_edit=True)
    assert "books/accounts/6900" not in hidden
    assert "Show inactive (1)" in hidden
    assert 'href="/t/acme/books/accounts?inactive=1"' in hidden

    shown = render_chart_of_accounts("acme", CHART, can_edit=True, show_inactive=True)
    row = _row(shown, "6900")
    assert "Inactive" in row
    assert "Reactivate" in row and 'name="active" value="true"' in row
    assert "Hide inactive" in shown


def test_locked_account_form_gives_the_reason_and_no_form() -> None:
    locked = next(a for a in CHART["accounts"] if a["code"] == "1000")
    html = render_account_form("acme", CHART, account=locked)
    assert "<form" not in html
    assert "kept in QuickBooks until the cutover" in html


def test_edit_form_is_prefilled_and_offers_only_the_same_type_as_parent() -> None:
    account = next(a for a in CHART["accounts"] if a["code"] == "6300")
    html = render_account_form("acme", CHART, account=account)
    assert 'action="/t/acme/books/accounts/6300/edit"' in html
    assert 'value="Rent &amp; Lease"' in html or 'value="Rent & Lease"' in html
    assert 'name="number" required value="6300"' in html
    # An expense account's parent choices are expense accounts only.
    assert "Services Income" not in html
    assert "Advertising" in html


def test_create_form_offers_every_detail_type_grouped_by_type() -> None:
    html = render_account_form("acme", CHART)
    assert 'action="/t/acme/books/accounts"' in html
    assert '<optgroup label="Expense">' in html and 'value="COST_OF_GOODS_SOLD"' in html
    assert 'value="ACCOUNTS_RECEIVABLE"' in html


def test_every_detail_type_is_known_to_the_screens() -> None:
    assert set(_STATEMENT_ORDER) == set(DETAIL_TYPES)


# --- the routes -------------------------------------------------------------------------

def test_the_chart_page_offers_actions_to_an_owner_and_none_to_a_viewer() -> None:
    app, _t, _a = _app(_routes())
    owner = _req(app, "/t/acme/books/accounts")
    assert owner.status == 200
    assert "New account" in owner.body
    assert "/t/acme/books/accounts/6300/edit" in owner.body

    viewer = _req(app, "/t/acme/books/accounts", sub="u-view")
    assert viewer.status == 200
    assert "New account" not in viewer.body
    assert "/t/acme/books/accounts/6300/edit" not in viewer.body


def test_inactivate_flows_through_to_the_ledger_as_a_patch() -> None:
    app, transport, _a = _app(_routes())
    transport.routes["PATCH /t/acme/accounts/6300"] = (200, {"tenant": "acme", "account": {"code": "6300"}})
    r = _req(app, "/t/acme/books/accounts/6300/active", method="POST", body="active=false")
    assert r.status == 302
    assert "ok=" in str(r.headers.get("Location", ""))
    patches = _calls(transport, "PATCH")
    assert patches == [("PATCH", "/t/acme/accounts/6300", json.dumps({"active": False}))]


def test_reactivate_sends_active_true() -> None:
    app, transport, _a = _app(_routes())
    transport.routes["PATCH /t/acme/accounts/6900"] = (200, {"tenant": "acme", "account": {"code": "6900"}})
    _req(app, "/t/acme/books/accounts/6900/active", method="POST", body="active=true")
    assert _calls(transport, "PATCH")[-1][2] == json.dumps({"active": True})


def test_an_edit_sends_only_the_fields_the_form_carried() -> None:
    app, transport, _a = _app(_routes())
    transport.routes["PATCH /t/acme/accounts/6300"] = (200, {"tenant": "acme", "account": {"code": "6300"}})
    _req(app, "/t/acme/books/accounts/6300/edit", method="POST",
         body="name=Premises+Rent&number=6300&subtype=EXPENSE&parent_code=")
    body = json.loads(_calls(transport, "PATCH")[-1][2])
    assert body == {"name": "Premises Rent", "number": "6300", "subtype": "EXPENSE", "parent_code": None}


def test_a_create_posts_the_number_name_detail_type_and_parent() -> None:
    app, transport, _a = _app(_routes())
    transport.routes["POST /t/acme/accounts"] = (201, {"tenant": "acme", "account": {"code": "6010"}})
    r = _req(app, "/t/acme/books/accounts", method="POST",
             body="number=6010&name=Travel&subtype=EXPENSE&parent_code=6300")
    assert r.status == 302
    sent = json.loads(_calls(transport, "POST")[-1][2])
    assert sent == {"code": "6010", "name": "Travel", "subtype": "EXPENSE", "parent_code": "6300"}


def test_a_ledger_refusal_comes_back_to_the_edit_form_with_its_reason() -> None:
    app, transport, _a = _app(_routes())
    transport.routes["PATCH /t/acme/accounts/6300"] = (
        409, {"error": "6300 has posted entries, so its type cannot change"})
    r = _req(app, "/t/acme/books/accounts/6300/edit", method="POST", body="name=X&subtype=EXPENSE")
    assert r.status == 302
    location = str(r.headers.get("Location", ""))
    assert "/books/accounts/6300/edit?err=" in location
    shown = _req(app, location)
    assert "has posted entries, so its type cannot change" in shown.body


def test_a_quickbooks_refusal_is_shown_plainly() -> None:
    app, transport, _a = _app(_routes())
    transport.routes["PATCH /t/acme/accounts/1000"] = (409, {"error": "edit this account in QuickBooks"})
    r = _req(app, "/t/acme/books/accounts/1000/active", method="POST", body="active=false")
    assert r.status == 302
    shown = _req(app, "/t/acme/books/accounts?err=edit+this+account+in+QuickBooks")
    assert "edit this account in QuickBooks" in shown.body


def test_a_viewer_cannot_change_the_chart_and_the_ledger_is_not_called() -> None:
    app, transport, _a = _app(_routes())
    for path, method, body in [
        ("/t/acme/books/accounts", "POST", "number=6010&name=Travel&subtype=EXPENSE"),
        ("/t/acme/books/accounts/6300/edit", "POST", "name=X"),
        ("/t/acme/books/accounts/6300/active", "POST", "active=false"),
    ]:
        assert _req(app, path, sub="u-view", method=method, body=body).status == 403, path
    for path in ["/t/acme/books/accounts/new", "/t/acme/books/accounts/6300/edit"]:
        assert _req(app, path, sub="u-view").status == 403, path
    assert not _calls(transport, "POST") and not _calls(transport, "PATCH")


def test_the_new_account_form_is_served_to_an_owner() -> None:
    app, _t, _a = _app(_routes())
    r = _req(app, "/t/acme/books/accounts/new")
    assert r.status == 200
    assert "New account" in r.body
    assert 'action="/t/acme/books/accounts"' in r.body
