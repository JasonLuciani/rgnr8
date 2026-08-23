"""The guided commingled-account split screen (A-2).

The screen reads the tenant's imported bank feed and lets the owner author routing
rules in a form (no JSON). GET shows the statement + an empty rule builder; POST
runs the split and renders the proposed books plus a per-transaction audit.
Nothing is posted to the ledger.
"""

from datetime import date
from urllib.parse import urlencode

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)
from rgnr8_web.transactions import BankTransaction

SECRET = "split-secret"
NOW = 1_760_000_000


def _txns() -> list[BankTransaction]:
    return [
        BankTransaction("t1", "2026-08-03", "STRIPE PAYOUT", Money.from_decimal("2500.00"),
                        counterparty="STRIPE"),
        BankTransaction("t2", "2026-08-05", "GUSTO PAYROLL", Money.from_decimal("-1200.00"),
                        counterparty="GUSTO"),
        BankTransaction("t3", "2026-08-07", "WHOLE FOODS", Money.from_decimal("-80.00"),
                        counterparty="WHOLEFOODS"),
    ]


def _app(with_feed: bool = True) -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users)
    app.add_tenant("acme", "Acme Co",
                   ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                  available=Money.from_decimal("0.00"))),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    if with_feed:
        app.add_transactions("acme", _txns())
    return app


def _req(app: WebApp, path: str, method: str = "GET", form: dict | None = None):
    tok = sign_jwt({"sub": "u-owner", "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    body = ""
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
        body = urlencode(form or {})
    return app.handle(Request(method, path, headers, body))


# --- source: reads the imported statement ------------------------------------

def test_split_screen_reads_the_imported_statement() -> None:
    r = _req(_app(), "/t/acme/books/split")
    assert r.status == 200
    assert "Split a commingled account" in r.body
    # it shows the imported lines and the rule builder (not a JSON box)
    assert "STRIPE PAYOUT" in r.body and "GUSTO PAYROLL" in r.body
    assert 'name="rule_book_0"' in r.body
    assert 'name="spec"' not in r.body


def test_split_screen_without_a_feed_explains_how_to_import() -> None:
    r = _req(_app(with_feed=False), "/t/acme/books/split")
    assert r.status == 200
    assert "No imported transactions yet" in r.body


# --- the rule builder → a balanced split -------------------------------------

def _rule_form() -> dict:
    # Route business by counterparty/description; everything else → the default book.
    return {
        "default_book": "Personal",
        "rule_book_0": "Business", "rule_field_0": "counterparty",
        "rule_value_0": "GUSTO", "rule_dir_0": "", "rule_cat_0": "Wages",
        "rule_book_1": "Business", "rule_field_1": "description",
        "rule_value_1": "STRIPE", "rule_dir_1": "in", "rule_cat_1": "Sales",
    }


def test_rule_builder_produces_two_balanced_books() -> None:
    r = _req(_app(), "/t/acme/books/split", method="POST", form=_rule_form())
    assert r.status == 200
    assert "Proposed split" in r.body
    assert "Every set of books balances" in r.body
    assert "Business" in r.body and "Personal" in r.body
    # audit names the lines and where they landed
    assert "STRIPE PAYOUT" in r.body and "WHOLE FOODS" in r.body


def test_submitted_rules_persist_in_the_builder() -> None:
    r = _req(_app(), "/t/acme/books/split", method="POST", form=_rule_form())
    # the values the owner typed come back in the form so they can tweak
    assert 'value="Business"' in r.body
    assert 'value="GUSTO"' in r.body


def test_description_match_is_literal_not_regex() -> None:
    # A value with a regex metachar must match literally, not blow up or over-match.
    app = _app(with_feed=False)
    app.add_transactions("acme", [
        BankTransaction("t1", "2026-08-03", "ACME (A+B) LLC", Money.from_decimal("-50.00"),
                        counterparty="ACME"),
    ])
    form = {
        "default_book": "Personal",
        "rule_book_0": "Business", "rule_field_0": "description",
        "rule_value_0": "(A+B)", "rule_dir_0": "", "rule_cat_0": "Supplies",
    }
    r = _req(app, "/t/acme/books/split", method="POST", form=form)
    assert r.status == 200
    assert "Every set of books balances" in r.body
    # it landed in Business (literal match), not the default
    assert "Business" in r.body


def test_posting_with_no_rules_asks_for_one() -> None:
    r = _req(_app(), "/t/acme/books/split", method="POST", form={"default_book": "Personal"})
    assert r.status == 200
    assert "Add at least one routing rule" in r.body


# --- scope: which slice of the feed to route ---------------------------------

def _mixed_app() -> WebApp:
    app = _app(with_feed=False)
    app.add_transactions("acme", [
        # already reconciled + categorized → NOT needing review
        BankTransaction("m1", "2026-07-15", "OLD MATCHED", Money.from_decimal("100.00"),
                        counterparty="ACME", category="Sales", status="matched"),
        # needs review (uncategorized)
        BankTransaction("m2", "2026-08-10", "STRIPE PAYOUT", Money.from_decimal("2500.00"),
                        counterparty="STRIPE"),
        BankTransaction("m3", "2026-08-20", "GUSTO PAYROLL", Money.from_decimal("-1200.00"),
                        counterparty="GUSTO"),
    ])
    return app


def test_scope_shows_the_status_and_date_controls() -> None:
    r = _req(_mixed_app(), "/t/acme/books/split")
    assert r.status == 200
    assert 'name="scope_status"' in r.body
    assert 'name="scope_from"' in r.body and 'name="scope_to"' in r.body


def test_scope_review_only_routes_lines_needing_review() -> None:
    form = {
        "scope_status": "review",
        "default_book": "Personal",
        "rule_book_0": "Business", "rule_field_0": "counterparty",
        "rule_value_0": "STRIPE", "rule_dir_0": "", "rule_cat_0": "Sales",
    }
    r = _req(_mixed_app(), "/t/acme/books/split", method="POST", form=form)
    assert r.status == 200
    # the matched line is out of scope; only the two review lines are routed
    assert "OLD MATCHED" not in r.body
    assert "STRIPE PAYOUT" in r.body and "GUSTO PAYROLL" in r.body
    assert "in scope of 3 imported" in r.body


def test_scope_date_range_bounds_the_statement() -> None:
    form = {
        "scope_status": "all",
        "scope_from": "2026-08-01",
        "scope_to": "2026-08-15",
        "default_book": "Personal",
        "rule_book_0": "Business", "rule_field_0": "counterparty",
        "rule_value_0": "STRIPE", "rule_dir_0": "", "rule_cat_0": "Sales",
    }
    r = _req(_mixed_app(), "/t/acme/books/split", method="POST", form=form)
    assert r.status == 200
    # only m2 (2026-08-10) falls in the window
    assert "STRIPE PAYOUT" in r.body
    assert "OLD MATCHED" not in r.body and "GUSTO PAYROLL" not in r.body


def test_scope_persists_in_the_controls_after_submit() -> None:
    form = {"scope_status": "review", "scope_from": "2026-08-01", "default_book": "Personal"}
    r = _req(_mixed_app(), "/t/acme/books/split", method="POST", form=form)
    # the chosen scope comes back selected/filled
    assert 'value="review" selected' in r.body
    assert 'value="2026-08-01"' in r.body


def test_scope_that_matches_nothing_explains_how_to_widen() -> None:
    form = {"scope_status": "all", "scope_from": "2030-01-01", "default_book": "Personal"}
    r = _req(_mixed_app(), "/t/acme/books/split", method="POST", form=form)
    assert r.status == 200
    assert "No transactions match this scope" in r.body
