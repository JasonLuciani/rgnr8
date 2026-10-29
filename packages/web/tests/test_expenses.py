"""Expenses: money already spent, recorded as one balanced journal entry.

What is defended here: the entry the ledger receives (each line debited, the
paid-from account credited with the total, idempotent on the form's token, the
vendor on it), the refusals (paid from something that isn't a bank or a card,
zero amounts, lines that don't add up), the evidence (a file sent with the form
lands on the new entry, and so does a receipt kept on the Receipts screen), the
list of recent expenses with its paperclips, and the parallel-run lock.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

from test_attachments import multipart_body

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
from rgnr8_web.expense_screens import recent_expenses, vendor_of
from rgnr8_web.parallel_run import is_posting_path

SECRET = "exp-secret"
NOW = 1_760_000_000

ACCOUNTS = [
    {"code": "1000", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
    {"code": "1200", "name": "Accounts Receivable", "type": "ASSET",
     "subtype": "ACCOUNTS_RECEIVABLE"},
    {"code": "2100", "name": "Company Card", "type": "LIABILITY", "subtype": "CREDIT_CARD"},
    {"code": "6400", "name": "Supplies", "type": "EXPENSE", "subtype": "EXPENSE"},
    {"code": "6500", "name": "Software", "type": "EXPENSE", "subtype": "EXPENSE"},
]


class _Ledger:
    """Just enough ledger: a chart, vendors, a journal, attachments, a book of record."""

    def __init__(self, book: str = "rgnr8") -> None:
        self.book = book
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.entries: list[dict[str, Any]] = []
        self.vendors = [{"id": "adobe", "name": "Adobe"}]
        self.counts: dict[str, int] = {}

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        route = path.split("?")[0]
        payload = json.loads(body) if body else {}
        self.calls.append((method, route, payload))
        if route == "/t/acme/settings":
            return LedgerResponse(200, {"settings": {"book_of_record": self.book}})
        if route == "/t/acme/accounts":
            return LedgerResponse(200, {"accounts": ACCOUNTS})
        if route == "/t/acme/vendors":
            if method == "POST":
                self.vendors.append({"id": payload["id"], "name": payload["name"]})
                return LedgerResponse(201, {"party": payload})
            return LedgerResponse(200, {"parties": self.vendors})
        if route == "/t/acme/entries" and method == "POST":
            entry = {"id": f"acme:{len(self.entries) + 1}", "sequence": len(self.entries) + 1,
                     "date": payload["date"], "memo": payload.get("memo", ""),
                     "source": payload.get("source", ""),
                     "idempotency_key": payload.get("idempotency_key", ""),
                     "lines": [{"account_id": f"acct:{ln['code']}", "side": ln["side"],
                                "amount_minor": ln["amount_minor"]} for ln in payload["lines"]]}
            self.entries.append(entry)
            return LedgerResponse(201, {"entry": entry})
        if route == "/t/acme/entries":
            return LedgerResponse(200, {"entries": self.entries})
        if route == "/t/acme/attachments/counts":
            return LedgerResponse(200, {"counts": self.counts})
        if route == "/t/acme/attachments" and method == "POST":
            return LedgerResponse(201, {"attachment": {"id": "att-1", **payload}})
        if route.endswith("/file") and method == "POST":
            return LedgerResponse(200, {"attachment": {"id": route.split("/")[4], **payload}})
        if route == "/t/acme/dimensions":
            return LedgerResponse(200, {"dimensions": []})
        return LedgerResponse(404, {"error": "not found"})

    def posted(self) -> list[dict[str, Any]]:
        return [p for m, r, p in self.calls if m == "POST" and r == "/t/acme/entries"]


def _app(book: str = "rgnr8") -> tuple[WebApp, _Ledger, InMemoryAuditLog]:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-view", "acme", Role.VIEWER)
    audit = InMemoryAuditLog()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 audit=audit)
    app.add_tenant("acme", "Acme Co", ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("0.00"))),
        ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token="unused")
    ledger = _Ledger(book)
    app.set_ledger(LedgerClient(ledger, token="svc"))
    return app, ledger, audit


def _req(app: WebApp, path: str, method: str = "GET", body: str = "",
         content_type: str = "", sub: str = "u-owner") -> Any:
    tok = sign_jwt({"sub": sub, "tenant": "acme", "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    if method == "POST":
        headers["content-type"] = content_type or "application/x-www-form-urlencoded"
    return app.handle(Request(method, path, headers, body))


SUPPLIES = {
    "token": "tok123", "date": "2026-03-14", "vendor": "Home Depot", "paid_from": "1000",
    "code1": "6400", "desc1": "Plywood", "amount1": "96.00",
    "code2": "6400", "desc2": "Tax", "amount2": "21.18", "total": "117.18",
}


def _post(app: WebApp, form: dict[str, str]) -> Any:
    return _req(app, "/t/acme/expenses", "POST", urlencode(form))


def _query(r: Any) -> dict[str, str]:
    loc = str(r.headers.get("Location", ""))
    return {k: v[0] for k, v in parse_qs(urlsplit(loc).query).items()}


# --- the entry ---------------------------------------------------------------


def test_an_expense_debits_each_line_and_credits_what_it_was_paid_from() -> None:
    app, ledger, audit = _app()
    r = _post(app, SUPPLIES)
    assert r.status == 302, r.body
    assert "Expense recorded" in _query(r)["done"]
    [entry] = ledger.posted()
    assert entry["date"] == "2026-03-14"
    assert entry["lines"] == [
        {"code": "6400", "side": "DEBIT", "amount_minor": "9600", "memo": "Plywood"},
        {"code": "6400", "side": "DEBIT", "amount_minor": "2118", "memo": "Tax"},
        {"code": "1000", "side": "CREDIT", "amount_minor": "11718"},
    ]
    debits = sum(int(ln["amount_minor"]) for ln in entry["lines"] if ln["side"] == "DEBIT")
    credits = sum(int(ln["amount_minor"]) for ln in entry["lines"] if ln["side"] == "CREDIT")
    assert debits == credits == 11718
    assert entry["source"] == "expense"
    assert entry["memo"] == "Home Depot"
    # A vendor typed in is created, and the entry names it.
    assert {"id": "home-depot", "name": "Home Depot"} in ledger.vendors
    assert entry["idempotency_key"] == "exp:home-depot:tok123"
    assert any(e.action == "expense.recorded" for e in audit.events(tenant_id="acme"))


def test_a_card_is_a_place_to_pay_from_and_an_existing_vendor_is_used_as_is() -> None:
    app, ledger, _ = _app()
    r = _post(app, {"token": "t2", "date": "2026-03-01", "vendor_id": "adobe",
                    "paid_from": "2100", "code1": "6500", "amount1": "59.99",
                    "memo": "Annual plan"})
    assert r.status == 302 and "err" not in _query(r)
    [entry] = ledger.posted()
    assert entry["lines"][-1] == {"code": "2100", "side": "CREDIT", "amount_minor": "5999"}
    assert entry["memo"] == "Adobe — Annual plan"
    assert entry["idempotency_key"] == "exp:adobe:t2"
    assert not [c for c in ledger.calls if c[0] == "POST" and c[1] == "/t/acme/vendors"]


def test_the_same_form_sent_twice_carries_the_same_key() -> None:
    app, ledger, _ = _app()
    _post(app, SUPPLIES)
    _post(app, SUPPLIES)
    keys = {e["idempotency_key"] for e in ledger.posted()}
    assert keys == {"exp:home-depot:tok123"}, "the ledger replays a matching key: posted once"


def test_a_form_with_no_token_still_gets_a_stable_key() -> None:
    app, ledger, _ = _app()
    form = {k: v for k, v in SUPPLIES.items() if k != "token"}
    _post(app, form)
    _post(app, form)
    keys = [e["idempotency_key"] for e in ledger.posted()]
    assert len(set(keys)) == 1 and keys[0].startswith("exp:home-depot:")


# --- refusals ------------------------------------------------------------------


def test_only_a_bank_cash_or_card_account_can_be_paid_from() -> None:
    app, ledger, _ = _app()
    for code in ("1200", "6500", "9999", ""):
        r = _post(app, {**SUPPLIES, "paid_from": code})
        assert "bank, cash or credit card" in _query(r)["err"], code
    assert ledger.posted() == []


def test_zero_negative_and_unbalanced_amounts_are_refused() -> None:
    app, ledger, _ = _app()
    cases = [
        ({"amount1": "0", "amount2": "", "code2": "", "total": ""}, "greater than zero"),
        ({"amount1": "-5.00", "amount2": "", "code2": "", "total": ""}, "greater than zero"),
        ({"total": "120.00"}, "add up to $117.18 but the total is $120.00"),
        ({"code1": "", "code2": "", "amount1": "", "amount2": "", "total": ""},
         "at least one line"),
        ({"code1": ""}, "choose the account"),
        ({"code1": "1000"}, "bank or card account"),
        ({"amount1": "1.234"}, "more than cents"),
        ({"date": "14/03/2026"}, "YYYY-MM-DD"),
    ]
    for change, reason in cases:
        r = _post(app, {**SUPPLIES, **change})
        assert reason in _query(r)["err"], (change, _query(r))
    assert ledger.posted() == []


def test_a_refused_form_comes_back_filled_in() -> None:
    app, _ledger, _ = _app()
    r = _post(app, {**SUPPLIES, "total": "120.00"})
    back = _req(app, str(r.headers["Location"]))
    assert back.status == 200
    assert 'value="Plywood"' in back.body and 'value="120.00"' in back.body
    assert "but the total is $120.00" in back.body


def test_a_viewer_cannot_record_one() -> None:
    app, ledger, _ = _app()
    r = _req(app, "/t/acme/expenses", "POST", urlencode(SUPPLIES), sub="u-view")
    assert r.status == 403
    assert ledger.posted() == []
    page = _req(app, "/t/acme/expenses", sub="u-view")
    assert page.status == 200 and "Record an expense" not in page.body


# --- evidence --------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


def test_a_file_sent_with_the_expense_lands_on_its_entry() -> None:
    app, ledger, _ = _app()
    body = multipart_body("B", [("file", "till.png", "image/png", PNG)], SUPPLIES)
    r = _req(app, "/t/acme/expenses", "POST", body, "multipart/form-data; boundary=B")
    assert r.status == 302 and "err" not in _query(r), _query(r)
    [saved] = [p for m, rt, p in ledger.calls if m == "POST" and rt == "/t/acme/attachments"]
    assert saved["subject_kind"] == "entry"
    assert saved["subject_id"] == ledger.entries[0]["id"]
    assert base64.b64decode(saved["content_base64"]) == PNG


def test_a_kept_receipt_is_filed_onto_the_new_entry() -> None:
    app, ledger, _ = _app()
    _post(app, {**SUPPLIES, "receipt": "att-receipt-rcpt-1"})
    [filed] = [(rt, p) for m, rt, p in ledger.calls if rt.endswith("/file")]
    assert filed[0] == "/t/acme/attachments/att-receipt-rcpt-1/file"
    assert filed[1] == {"subject_kind": "entry", "subject_id": ledger.entries[0]["id"]}


def test_a_file_the_ledger_refuses_doesnt_undo_the_expense_and_says_so() -> None:
    app, ledger, _ = _app()
    real = ledger.request

    def refuse_files(method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        if method == "POST" and path.startswith("/t/acme/attachments"):
            return LedgerResponse(400, {"error": "the file is 9.0 MB — the limit is 8 MB"})
        return real(method, path, body, headers)

    ledger.request = refuse_files  # type: ignore[method-assign]
    body = multipart_body("B", [("file", "big.pdf", "application/pdf", b"%PDF")], SUPPLIES)
    q = _query(_req(app, "/t/acme/expenses", "POST", body, "multipart/form-data; boundary=B"))
    assert "Expense recorded" in q["done"]
    assert "the limit is 8 MB" in q["err"] and "paperclip" in q["err"]
    assert len(ledger.entries) == 1


# --- the screen ------------------------------------------------------------------


def test_the_list_shows_this_forms_expenses_with_a_paperclip_each() -> None:
    app, ledger, _ = _app()
    _post(app, SUPPLIES)
    ledger.entries.append({"id": "acme:99", "date": "2026-03-20", "memo": "Owner draw",
                           "source": "owner", "idempotency_key": "je:x", "lines": []})
    ledger.counts = {"acme:1": 2}
    page = _req(app, "/t/acme/expenses").body
    assert "Recent expenses" in page
    assert "Home Depot" in page and "$117.18" in page and "Business Checking" in page
    assert 'href="/t/acme/files/entry/acme:1"' in page and "📎 2" in page
    assert "Owner draw" not in page, "only entries this form recorded"


def test_recent_expenses_reads_vendor_amount_and_reversal_off_the_entries() -> None:
    entries = {"entries": [
        {"id": "acme:1", "sequence": 1, "date": "2026-03-01", "source": "expense",
         "idempotency_key": "exp:adobe:t1", "memo": "Adobe Inc — Annual plan",
         "lines": [{"account_id": "acct:6500", "side": "DEBIT", "amount_minor": "5999"},
                   {"account_id": "acct:2100", "side": "CREDIT", "amount_minor": "5999"}]},
        {"id": "acme:2", "sequence": 2, "date": "2026-03-02", "source": "manual",
         "idempotency_key": "reverse:acme:1", "lines": []},
    ]}
    [row] = recent_expenses(entries, {"accounts": ACCOUNTS},
                            {"parties": [{"id": "adobe", "name": "Adobe Inc"}]})
    assert row["vendor"] == "Adobe Inc"
    assert row["memo"] == "Annual plan", "the vendor has its own column"
    assert row["amount_minor"] == 5999
    assert row["paid_from"] == "2100 — Company Card"
    assert row["reversed"] is True
    assert vendor_of({"idempotency_key": "exp:home-depot:tok"}) == "home-depot"
    assert vendor_of({"idempotency_key": "je:2026"}) == ""


def test_paid_from_offers_only_bank_and_card_accounts() -> None:
    app, _ledger, _ = _app()
    page = _req(app, "/t/acme/expenses").body
    paid_from = page.split('name="paid_from"', 1)[1].split("</select>", 1)[0]
    assert "Business Checking" in paid_from and "Company Card" in paid_from
    assert "Accounts Receivable" not in paid_from and "Supplies" not in paid_from
    assert 'enctype="multipart/form-data"' in page and 'name="file"' in page


def test_a_drafted_receipt_arrives_pre_filled() -> None:
    app, _ledger, _ = _app()
    page = _req(app, "/t/acme/expenses?" + urlencode({
        "vendor": "HOME DEPOT", "date": "2026-03-14", "amount": "117.18",
        "expense_account_code": "6400", "receipt": "att-r1", "receipt_name": "till.png",
    })).body
    assert 'name="amount1" value="117.18"' in page
    assert 'name="total" value="117.18"' in page
    assert 'value="HOME DEPOT"' in page and 'value="2026-03-14"' in page
    assert '<option value="6400" selected>' in page
    assert 'name="receipt" value="att-r1"' in page and "till.png" in page


def test_expenses_sits_under_money_out() -> None:
    app, _ledger, _ = _app()
    page = _req(app, "/t/acme/expenses").body
    nav = page.split('<nav class="rg-nav"', 1)[1].split("</nav>", 1)[0]
    money_out = nav.split("Money out", 1)[1].split("<h2>", 1)[0]
    assert 'href="/t/acme/expenses"' in money_out and ">Expenses<" in money_out


# --- the parallel run ------------------------------------------------------------


def test_recording_an_expense_is_a_posting_route() -> None:
    assert is_posting_path(["t", "acme", "expenses"], "POST")
    assert not is_posting_path(["t", "acme", "expenses"], "GET")


def test_during_the_parallel_run_the_form_says_what_it_is_and_nothing_posts() -> None:
    app, ledger, _ = _app("quickbooks")
    page = _req(app, "/t/acme/expenses").body
    assert 'action="/t/acme/expenses"' not in page
    assert "<strong>Expense</strong>: this form opens here after the cutover" in page
    assert "+ New → Expense" in page
    r = _post(app, SUPPLIES)
    assert r.status == 409
    assert "Add this in QuickBooks" in r.body
    assert ledger.posted() == []
