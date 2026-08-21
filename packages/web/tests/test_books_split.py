"""The guided commingled-account split screen (A-2).

The screen is a thin renderer over the same split engine the JSON/MCP path uses:
GET shows a prefilled form; POST runs the split and renders the proposed books
plus a per-transaction audit. Nothing is posted to the ledger.
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

SECRET = "split-secret"
NOW = 1_760_000_000


def _app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users)
    app.add_tenant("acme", "Acme Co",
                   ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                  available=Money.from_decimal("0.00"))),
                   ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    return app


def _req(app: WebApp, path: str, method: str = "GET", form: dict | None = None):
    tok = sign_jwt({"sub": "u-owner", "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    body = ""
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
        body = urlencode(form or {})
    return app.handle(Request(method, path, headers, body))


_SPEC = """{
  "default_book": "personal",
  "rules": [
    {"book": "business", "name": "payroll", "category": "Wages", "counterparty": "GUSTO"},
    {"book": "business", "name": "sales", "category": "Sales", "description_regex": "STRIPE"}
  ],
  "transactions": [
    {"id": "t1", "description": "STRIPE PAYOUT", "counterparty": "STRIPE", "amount_minor": "250000"},
    {"id": "t2", "description": "GUSTO PAYROLL", "counterparty": "GUSTO", "amount_minor": "-120000"},
    {"id": "t3", "description": "WHOLE FOODS", "counterparty": "WHOLEFOODS", "amount_minor": "-8000"}
  ]
}"""


def test_split_screen_renders_the_guided_form() -> None:
    r = _req(_app(), "/t/acme/books/split")
    assert r.status == 200
    assert "Split a commingled account" in r.body
    assert 'name="spec"' in r.body            # the JSON input
    assert "GUSTO" in r.body                   # the prefilled worked example


def test_split_screen_posts_and_renders_two_balanced_books() -> None:
    r = _req(_app(), "/t/acme/books/split", method="POST", form={"spec": _SPEC})
    assert r.status == 200
    assert "Proposed split" in r.body
    assert "Every set of books balances" in r.body
    # both destination books are shown, each with its entries
    assert "business" in r.body and "personal" in r.body
    # the audit trail names each transaction and where it landed
    assert "STRIPE PAYOUT" in r.body and "WHOLE FOODS" in r.body


def test_split_screen_reports_bad_json_without_crashing() -> None:
    r = _req(_app(), "/t/acme/books/split", method="POST", form={"spec": "{not json"})
    assert r.status == 200
    assert "isn&#x27;t valid JSON" in r.body or "valid JSON" in r.body
