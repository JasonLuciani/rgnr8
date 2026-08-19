"""The bank feed review inbox screens, against a fake ledger transport.

These cover routing, the permission gate, and the shape of what reaches the
service. The *rules* — what may be accepted, what a match is allowed to settle,
what undo does to the journal — live in the ledger service and are tested there.
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

SECRET = "inbox-secret"
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
    "tenant": "acme",
    "accounts": [
        {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
        {"code": "4100", "name": "Services Income", "type": "REVENUE", "subtype": "INCOME"},
        {"code": "6300", "name": "Rent & Lease", "type": "EXPENSE", "subtype": "EXPENSE"},
    ],
}

ITEM_RULE = {
    "id": "bk-3", "account_code": "1000", "date": "2026-08-09",
    "amount_minor": "-350000", "description": "RIVERSIDE PROPERTIES RENT",
    "counterparty": "Riverside Properties", "status": "PENDING",
    "category_code": "", "entry_id": "", "doc_kind": "", "doc_id": "", "note": "",
    "suggestion": {"account_code": "6300", "account_name": "Rent & Lease",
                   "confidence": 1.0, "reason": 'Rule "rent" matched',
                   "source": "rule", "rule_id": "rent"},
    "matches": [],
}
ITEM_BLANK = {
    "id": "bk-2", "account_code": "1000", "date": "2026-08-05",
    "amount_minor": "-24900", "description": "SQ *COFFEE 1187",
    "counterparty": "Square", "status": "PENDING",
    "category_code": "", "entry_id": "", "doc_kind": "", "doc_id": "", "note": "",
    "suggestion": {"account_code": "", "account_name": "", "confidence": 0.0,
                   "reason": "No rule matched and nothing like this has been categorized before",
                   "source": "none", "rule_id": ""},
    "matches": [],
}
ITEM_MATCHABLE = {
    "id": "bk-1", "account_code": "1000", "date": "2026-08-03",
    "amount_minor": "500000", "description": "DEPOSIT HALCYON LLC",
    "counterparty": "Halcyon LLC", "status": "PENDING",
    "category_code": "", "entry_id": "", "doc_kind": "", "doc_id": "", "note": "",
    "suggestion": {"account_code": "", "account_name": "", "confidence": 0.0,
                   "reason": "No rule matched", "source": "none", "rule_id": ""},
    "matches": [
        {"kind": "invoice", "id": "INV-1", "label": "August retainer",
         "date": "2026-08-01", "openMinor": "500000", "fit": "exact"},
    ],
}

INBOX = {
    "account_code": "", "pending": 3, "posted": 0, "matched": 0, "excluded": 0,
    "pending_in_minor": "500000", "pending_out_minor": "-374900",
    "items": [ITEM_MATCHABLE, ITEM_BLANK, ITEM_RULE],
}

POSTED_VIEW = {
    "account_code": "", "pending": 0, "posted": 1, "matched": 0, "excluded": 0,
    "pending_in_minor": "0", "pending_out_minor": "0",
    "items": [dict(ITEM_RULE, status="POSTED", category_code="6300", entry_id="acme:7",
                   matches=[])],
}

RULES = {
    "tenant": "acme",
    "rules": [
        {"id": "rent", "priority": 100, "account_code": "6300",
         "description_contains": "RIVERSIDE", "counterparty_equals": "",
         "sign": "out", "auto_post": False},
        {"id": "fees", "priority": 100, "account_code": "6050",
         "description_contains": "SERVICE CHARGE", "counterparty_equals": "",
         "sign": "out", "auto_post": True},
    ],
}

DEFAULT_ROUTES: dict[str, tuple[int, dict[str, Any]]] = {
    "GET /t/acme/accounts": (200, ACCOUNTS),
    "GET /t/acme/feed": (200, INBOX),
    "GET /t/acme/feed-rules": (200, RULES),
    "POST /t/acme/feed-rules": (201, {"rule": RULES["rules"][0], "would_match": 2}),
    "DELETE /t/acme/feed-rules/rent": (200, {"deleted": "rent"}),
    "POST /t/acme/feed/txn/bk-1/accept": (200, {"id": "bk-1", "status": "POSTED"}),
    "POST /t/acme/feed/txn/bk-1/match": (200, {"id": "bk-1", "status": "MATCHED"}),
    "POST /t/acme/feed/txn/bk-1/exclude": (200, {"id": "bk-1", "status": "EXCLUDED"}),
    "POST /t/acme/feed/txn/bk-1/undo": (200, {"id": "bk-1", "status": "PENDING"}),
    "POST /t/acme/feed/bulk-accept": (200, {"accepted": 2, "skipped": 1, "failures": []}),
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


def _posted(transport: FakeTransport) -> dict[str, Any]:
    body = [c for c in transport.calls if c[0] == "POST"][0][2]
    return json.loads(body)  # type: ignore[no-any-return]


# --- the queue ---------------------------------------------------------------

def test_the_queue_shows_every_waiting_line_with_its_amount() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inbox")
    assert r.status == 200
    assert "For review" in r.body
    assert "DEPOSIT HALCYON LLC" in r.body and "RIVERSIDE PROPERTIES RENT" in r.body
    assert "$5,000.00" in r.body and "-$3,500.00" in r.body
    assert "3 to review" in r.body


def test_a_rule_backed_suggestion_is_preselected_and_explains_itself() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inbox")
    assert '<option value="6300" selected>' in r.body
    assert "100% · from your rule" in r.body
    assert 'Rule &quot;rent&quot; matched' in r.body


def test_a_line_with_no_basis_shows_no_guess() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inbox")
    # the blank line's reason is shown, and no confidence chip is rendered for it
    assert "nothing like this has been categorized before" in r.body
    assert ">0% ·" not in r.body and "12px\">0%" not in r.body
    # exactly one confidence chip is rendered, for the one rule-backed line
    assert r.body.count("% · from") == 1


def test_a_line_can_never_be_categorized_to_its_own_bank_account() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inbox")
    # 1000 is the bank account every line sits on, so it is not an option
    assert '<option value="1000"' not in r.body
    assert '<option value="4100"' in r.body


def test_an_exact_invoice_match_is_offered_for_an_inflow() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inbox")
    assert "INV-1" in r.body and "August retainer" in r.body
    assert "(exact)" in r.body
    assert "/inbox/bk-1/match" in r.body


def test_a_transfer_is_flagged_as_a_hint_not_offered_as_a_payment() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/feed"] = (200, dict(INBOX, items=[
        dict(ITEM_BLANK, matches=[
            {"kind": "transfer", "id": "tf-in", "label": "Transfer to 1010 — TO SAVINGS",
             "date": "2026-08-14", "openMinor": "200000", "fit": "exact"},
        ]),
    ]))
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/inbox")
    assert "other side of a transfer" in r.body
    assert "/inbox/bk-2/match" not in r.body


def test_the_queue_surfaces_a_service_outage_honestly() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/feed"] = (503, {"error": "connection refused"})
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/inbox")
    assert r.status == 200
    assert "Bank feed unavailable" in r.body
    assert "Nothing has been lost" in r.body


# --- actions -----------------------------------------------------------------

def test_accepting_sends_the_chosen_account_and_logs_it() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/inbox/bk-1/accept", method="POST", body="category_code=4100")
    assert r.status == 302
    assert "done=" in str(r.headers.get("Location", ""))
    assert _posted(transport) == {"category_code": "4100"}
    assert any(e.action == "feed.accept" for e in audit.events(tenant_id="acme"))


def test_accepting_without_choosing_an_account_is_refused_before_the_service() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/inbox/bk-1/accept", method="POST", body="category_code=")
    assert "err=" in str(r.headers.get("Location", ""))
    assert "Choose+an+account" in str(r.headers.get("Location", "")) or \
           "Choose%20an%20account" in str(r.headers.get("Location", ""))
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_matching_splits_the_kind_and_document_id() -> None:
    app, transport, _a = _app()
    r = _req(app, "/t/acme/inbox/bk-1/match", method="POST", body="match=invoice%3AINV-1")
    assert r.status == 302
    assert _posted(transport) == {"doc_kind": "invoice", "doc_id": "INV-1"}


def test_a_malformed_match_selection_never_reaches_the_service() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/inbox/bk-1/match", method="POST", body="match=")
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_excluding_passes_the_reason_through() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/inbox/bk-1/exclude", method="POST",
         body="reason=Personal+card%2C+not+the+business")
    assert _posted(transport) == {"reason": "Personal card, not the business"}


def test_undo_says_a_reversing_entry_was_posted() -> None:
    app, _t, audit = _app()
    r = _req(app, "/t/acme/inbox/bk-1/undo", method="POST")
    loc = str(r.headers.get("Location", ""))
    assert "reversing%20entry" in loc
    assert any(e.action == "feed.undo" for e in audit.events(tenant_id="acme"))


def test_a_refused_action_surfaces_the_ledgers_reason_and_logs_nothing() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/feed/txn/bk-1/accept"] = (
        409, {"error": "period 2026-08 is closed"}
    )
    app, _t, audit = _app(routes)
    r = _req(app, "/t/acme/inbox/bk-1/accept", method="POST", body="category_code=4100")
    assert "period%202026-08%20is%20closed" in str(r.headers.get("Location", ""))
    assert not [e for e in audit.events(tenant_id="acme") if e.action == "feed.accept"]


# --- bulk accept -------------------------------------------------------------

def test_bulk_accept_reports_what_it_did_and_what_it_left() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/inbox/bulk-accept", method="POST", body="min_confidence=1")
    assert r.status == 302
    loc = str(r.headers.get("Location", ""))
    assert "Posted%202" in loc and "left%201" in loc
    assert _posted(transport) == {"min_confidence": 1.0}
    assert any(e.action == "feed.bulk_accept" for e in audit.events(tenant_id="acme"))


def test_bulk_accept_with_no_confidence_chosen_never_calls_the_service() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/inbox/bulk-accept", method="POST", body="min_confidence=")
    assert not [c for c in transport.calls if c[0] == "POST"]


# --- actioned lists ----------------------------------------------------------

def test_posted_lines_show_what_they_became_and_offer_undo() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/feed"] = (200, POSTED_VIEW)
    app, transport, _a = _app(routes)
    r = _req(app, "/t/acme/inbox?status=POSTED")
    assert r.status == 200
    assert "Posted to the books" in r.body
    assert "acme:7" in r.body           # the journal entry it produced
    assert "Posted to 6300" in r.body
    assert "/inbox/bk-3/undo" in r.body
    assert any("status=POSTED" in c[1] for c in transport.calls)


# --- rules -------------------------------------------------------------------

def test_rules_are_listed_in_plain_language() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inbox/rules")
    assert r.status == 200
    assert "description contains “RIVERSIDE” and money out" in r.body
    assert "suggests only" in r.body
    assert "posts automatically" in r.body     # the auto-post rule is called out


def test_saving_a_rule_reports_how_many_waiting_lines_it_matches() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/inbox/rules", method="POST",
             body="id=rent&description_contains=RIVERSIDE&sign=out&account_code=6300")
    assert r.status == 302
    assert "matches%202%20waiting" in str(r.headers.get("Location", ""))
    sent = _posted(transport)
    assert sent["id"] == "rent"
    assert sent["account_code"] == "6300"
    assert sent["auto_post"] is False
    assert any(e.action == "feed.rule_saved" for e in audit.events(tenant_id="acme"))


def test_auto_post_is_only_on_when_explicitly_chosen() -> None:
    app, transport, _a = _app()
    _req(app, "/t/acme/inbox/rules", method="POST",
         body="id=fees&description_contains=SERVICE&account_code=6300&auto_post=1")
    assert _posted(transport)["auto_post"] is True


def test_a_rule_the_service_rejects_surfaces_the_reason() -> None:
    routes = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/feed-rules"] = (
        400, {"error": "a rule needs at least one condition to match on"}
    )
    app, _t, _a = _app(routes)
    r = _req(app, "/t/acme/inbox/rules", method="POST", body="id=all&account_code=6300")
    assert "at%20least%20one%20condition" in str(r.headers.get("Location", ""))


def test_a_rule_can_be_removed() -> None:
    app, transport, audit = _app()
    r = _req(app, "/t/acme/inbox/rules/rent/delete", method="POST")
    assert r.status == 302
    assert any(c[0] == "DELETE" for c in transport.calls)
    assert any(e.action == "feed.rule_deleted" for e in audit.events(tenant_id="acme"))


# --- permissions -------------------------------------------------------------

def test_a_viewer_can_read_the_queue_but_not_action_it() -> None:
    app, transport, _a = _app()
    page = _req(app, "/t/acme/inbox", sub="u-view")
    assert page.status == 200
    assert "DEPOSIT HALCYON LLC" in page.body
    assert "/inbox/bk-1/accept" not in page.body
    assert "Accept the confident ones" not in page.body

    for path, body in [
        ("/t/acme/inbox/bk-1/accept", "category_code=4100"),
        ("/t/acme/inbox/bk-1/match", "match=invoice:INV-1"),
        ("/t/acme/inbox/bk-1/exclude", "reason=x"),
        ("/t/acme/inbox/bk-1/undo", ""),
        ("/t/acme/inbox/bulk-accept", "min_confidence=1"),
        ("/t/acme/inbox/rules", "id=x&account_code=6300&description_contains=y"),
        ("/t/acme/inbox/rules/rent/delete", ""),
    ]:
        assert _req(app, path, sub="u-view", method="POST", body=body).status == 403
    assert not [c for c in transport.calls if c[0] in ("POST", "DELETE")]


def test_a_viewer_sees_the_rules_read_only() -> None:
    app, _t, _a = _app()
    r = _req(app, "/t/acme/inbox/rules", sub="u-view")
    assert r.status == 200
    assert "RIVERSIDE" in r.body
    assert "Add a rule" not in r.body


def test_transactions_leads_to_the_real_inbox_when_a_ledger_is_configured() -> None:
    """The old in-memory register posted nothing. With a ledger behind it, the
    Transactions nav item is the real queue — not a second, fake one beside it."""
    app, _t, _a = _app()
    r = _req(app, "/t/acme/transactions")
    assert r.status == 302
    assert r.headers.get("Location") == "/t/acme/inbox"
    # and there is exactly one register in the nav
    page = _req(app, "/t/acme/inbox")
    assert page.body.count(">Transactions<") == 1


def test_the_review_queue_is_served_as_json_from_the_real_feed() -> None:
    app, transport, _a = _app()
    r = _req(app, "/api/acme/transactions")
    assert r.status == 200
    data = json.loads(r.body)
    assert data["summary"]["review"] == 3
    assert data["summary"]["inflow"] == "5000.00"
    assert data["summary"]["outflow"] == "-3749.00"
    ids = [x["id"] for x in data["for_review"]]
    assert ids == ["bk-1", "bk-2", "bk-3"]
    assert data["for_review"][2]["suggested_account"] == "6300"
    assert any("/feed" in c[1] for c in transport.calls)
