"""END-TO-END: the Python web app driving the REAL TypeScript ledger service.

Everything else in the suite uses fakes. This test boots the actual built
`@rgnr8/ledger-service` as a subprocess, points the web app's `UrllibTransport`
at it over a real socket, and walks a client's whole lifecycle: seed a chart,
post transactions from the owner form, read the trial balance, register, and
statements — then proves a second client's books are completely isolated.

It is the check that the two halves of the system are actually connected, rather
than merely sharing contract shapes. Skipped (not silently passed) if the service
hasn't been built.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryUserDirectory, JwtAuthenticator, LedgerClient, Request, Role, User,
    UrllibTransport, WebApp, sign_jwt,
)

SECRET = "e2e-secret"
NOW = 1_760_000_000
TOKEN = "e2e-service-token"
REPO = Path(__file__).resolve().parents[3]
SERVICE_BIN = REPO / "packages" / "ledger-service" / "dist" / "bin.js"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def ledger_service() -> Any:
    if not SERVICE_BIN.exists():
        pytest.skip("ledger service not built (run: npm run build -w @rgnr8/ledger-service)")
    port = _free_port()
    env = {**os.environ, "RGNR8_LEDGER_PORT": str(port), "RGNR8_LEDGER_TOKEN": TOKEN}
    proc = subprocess.Popen(
        ["node", str(SERVICE_BIN)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):  # wait for the port to answer
        try:
            with urllib.request.urlopen(f"{base}/health", timeout=0.5) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.05)
    else:
        proc.kill()
        pytest.skip("ledger service did not start")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("0.00"))
    )


def _app(base: str, tenants: list[str]) -> WebApp:
    users = InMemoryUserDirectory()
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users)
    for t in tenants:
        users.upsert_user(User(f"u-{t}", f"owner@{t}.com", "Owner"))
        users.set_membership(f"u-{t}", t, Role.OWNER)
        app.add_tenant(t, f"{t.title()} Co", _inputs(),
                       ForecastConfig(minimum_cash=Money.from_decimal("0.00")), token=f"tok-{t}")
    app.set_ledger(LedgerClient(UrllibTransport(base), token=TOKEN))
    return app


def _req(app: WebApp, tenant: str, path: str, method: str = "GET", body: str = "") -> Any:
    tok = sign_jwt({"sub": f"u-{tenant}", "tenant": tenant, "exp": NOW + 3600}, SECRET)
    headers = {"authorization": f"Bearer {tok}"}
    if method == "POST":
        headers["content-type"] = "application/x-www-form-urlencoded"
    return app.handle(Request(method, path, headers, body))


def _seed(base: str, tenant: str, category: str) -> None:
    req = urllib.request.Request(
        f"{base}/t/{tenant}/accounts/seed",
        data=json.dumps({"category": category}).encode(),
        headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 201


def _service_get(base: str, path: str) -> Any:
    """Read the service directly — used only to learn entry ids the UI renders."""
    req = urllib.request.Request(
        f"{base}{path}", headers={"authorization": f"Bearer {TOKEN}"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _service_post(base: str, path: str, payload: dict[str, Any]) -> Any:
    """Write to the service directly — used to stand in for the bank sync."""
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(),
        headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode())


def _service_post_raw(base: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the service, returning the status even when it refuses."""
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(),
        headers={"authorization": f"Bearer {TOKEN}", "content-type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return {"status": r.status, **json.loads(r.read().decode())}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, **json.loads(exc.read().decode())}


def _signed(base: str, tenant: str) -> dict[str, int]:
    """Signed trial-balance amounts by account code (debit-positive)."""
    tb = _service_get(base, f"/t/{tenant}/trial-balance")
    return {r["code"]: int(r["debit_minor"]) - int(r["credit_minor"]) for r in tb["rows"]}


def test_owner_runs_their_books_end_to_end_against_the_real_service(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["northwind"])
    _seed(base, "northwind", "SERVICE_GENERAL")

    # the chart is real, served by the ledger service
    coa = _req(app, "northwind", "/t/northwind/books/accounts")
    assert coa.status == 200
    assert "Business Checking" in coa.body

    # the owner posts two transactions through the web form
    sale = _req(app, "northwind", "/t/northwind/books/entries", "POST",
                "date=2026-08-05&memo=Cash+sale&code1=1000&debit1=5,000.00"
                "&code2=4000&credit2=5000.00")
    assert sale.status == 302 and "posted=" in str(sale.headers.get("Location", ""))
    rent = _req(app, "northwind", "/t/northwind/books/entries", "POST",
                "date=2026-08-10&memo=Pay+rent&code1=6300&debit1=2000.00"
                "&code2=1000&credit2=2000.00")
    assert rent.status == 302 and "posted=" in str(rent.headers.get("Location", ""))

    # the trial balance reflects them and is in balance
    books = _req(app, "northwind", "/t/northwind/books")
    assert books.status == 200
    assert "In balance" in books.body
    assert "$3,000.00" in books.body   # cash: 5,000 in − 2,000 rent

    # the register shows the running balance
    reg = _req(app, "northwind", "/t/northwind/books/accounts/1000")
    assert "Cash sale" in reg.body and "Pay rent" in reg.body
    assert "$5,000.00" in reg.body and "$3,000.00" in reg.body

    # statements are computed from those posted entries
    st = _req(app, "northwind", "/t/northwind/books/statements?from=2026-08-01&to=2026-08-31")
    assert st.status == 200
    assert "Income Statement" in st.body and "Balanced" in st.body
    assert "$5,000.00" in st.body and "$2,000.00" in st.body and "$3,000.00" in st.body


def test_the_real_ledger_refuses_an_unbalanced_entry(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["refuseco"])
    _seed(base, "refuseco", "SERVICE_GENERAL")
    r = _req(app, "refuseco", "/t/refuseco/books/entries", "POST",
             "date=2026-08-05&code1=1000&debit1=100.00&code2=4000&credit2=90.00")
    assert r.status == 302
    assert "err=" in str(r.headers.get("Location", ""))
    # nothing was written
    books = _req(app, "refuseco", "/t/refuseco/books")
    assert "Nothing posted yet" in books.body


def test_two_clients_books_are_isolated_through_the_running_stack(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["alpha", "bravo"])
    _seed(base, "alpha", "CONTRACTOR_TRADES")
    _seed(base, "bravo", "RESTAURANT")

    _req(app, "alpha", "/t/alpha/books/entries", "POST",
         "date=2026-08-05&memo=Job+deposit&code1=1000&debit1=7,777.00&code2=4100&credit2=7777.00")
    _req(app, "bravo", "/t/bravo/books/entries", "POST",
         "date=2026-08-06&memo=Food+sales&code1=1000&debit1=1,234.00&code2=4100&credit2=1234.00")

    alpha = _req(app, "alpha", "/t/alpha/books")
    bravo = _req(app, "bravo", "/t/bravo/books")
    assert "$7,777.00" in alpha.body and "$7,777.00" not in bravo.body
    assert "$1,234.00" in bravo.body and "$1,234.00" not in alpha.body

    # each got its own industry chart
    alpha_coa = _req(app, "alpha", "/t/alpha/books/accounts").body
    bravo_coa = _req(app, "bravo", "/t/bravo/books/accounts").body
    assert "Job Materials" in alpha_coa and "Job Materials" not in bravo_coa
    assert "Tips Payable" in bravo_coa and "Tips Payable" not in alpha_coa

    # a member of alpha cannot read bravo's books
    tok = sign_jwt({"sub": "u-alpha", "tenant": "alpha", "exp": NOW + 3600}, SECRET)
    cross = app.handle(Request("GET", "/t/bravo/books", {"authorization": f"Bearer {tok}"}, ""))
    assert cross.status == 403


def test_owner_invoices_a_customer_and_collects_it(ledger_service: str) -> None:
    """The AR loop an owner lives in: add a customer, invoice them, watch what's
    owed, collect it — with the ledger staying balanced the whole way."""
    base = ledger_service
    app = _app(base, ["invoiceco"])
    _seed(base, "invoiceco", "PROFESSIONAL_SERVICES")

    # add a customer through the inline form
    r = _req(app, "invoiceco", "/t/invoiceco/customers", "POST",
             "name=Northwind+Ltd&terms_days=30")
    assert r.status == 302 and "ok=" in str(r.headers.get("Location", ""))

    # raise an invoice
    inv = _req(app, "invoiceco", "/t/invoiceco/invoices", "POST",
               "id=INV-1001&party_id=northwind-ltd&date=2026-08-01&memo=August+work"
               "&desc1=Consulting&amount1=3,500.00&code1=4100"
               "&desc2=Travel&amount2=425.00&code2=4000")
    assert inv.status == 302, inv.body
    assert "ok=" in str(inv.headers.get("Location", "")), inv.headers

    # it shows as owed
    page = _req(app, "invoiceco", "/t/invoiceco/invoices")
    assert page.status == 200
    assert "INV-1001" in page.body and "Northwind Ltd" in page.body
    assert "$3,925.00" in page.body      # total owed
    assert "Owed to you" in page.body

    # the ledger reflects it: AR debited, income credited, still balanced
    books = _req(app, "invoiceco", "/t/invoiceco/books")
    assert "In balance" in books.body
    assert "$3,925.00" in books.body

    # collect part of it
    part = _req(app, "invoiceco", "/t/invoiceco/invoices/INV-1001/payments", "POST",
                "date=2026-08-20&amount=1,000.00")
    assert part.status == 302 and "ok=" in str(part.headers.get("Location", ""))
    page2 = _req(app, "invoiceco", "/t/invoiceco/invoices")
    assert "Part paid" in page2.body
    assert "$2,925.00" in page2.body     # still open

    # collect the rest → paid
    _req(app, "invoiceco", "/t/invoiceco/invoices/INV-1001/payments", "POST",
         "date=2026-08-25&amount=2925.00")
    page3 = _req(app, "invoiceco", "/t/invoiceco/invoices")
    assert "Paid" in page3.body

    # books still balance, and cash rose by the full amount
    final = _req(app, "invoiceco", "/t/invoiceco/books")
    assert "In balance" in final.body
    assert "$3,925.00" in final.body     # cash


def test_owner_enters_a_bill_and_pays_it(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["billco"])
    _seed(base, "billco", "SERVICE_GENERAL")

    _req(app, "billco", "/t/billco/vendors", "POST", "name=Copyshop&terms_days=15")
    bill = _req(app, "billco", "/t/billco/bills", "POST",
                "id=BILL-1&party_id=copyshop&date=2026-08-02&memo=Print+run"
                "&desc1=Brochures&amount1=360.00&code1=6400")
    assert bill.status == 302 and "ok=" in str(bill.headers.get("Location", ""))

    page = _req(app, "billco", "/t/billco/bills")
    assert "BILL-1" in page.body and "Copyshop" in page.body
    assert "You owe" in page.body and "$360.00" in page.body

    paid = _req(app, "billco", "/t/billco/bills/BILL-1/payments", "POST",
                "date=2026-08-16&amount=360.00")
    assert paid.status == 302 and "ok=" in str(paid.headers.get("Location", ""))
    after = _req(app, "billco", "/t/billco/bills")
    assert "Paid" in after.body

    books = _req(app, "billco", "/t/billco/books")
    assert "In balance" in books.body


def test_aging_shows_who_is_late(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["agingco"])
    _seed(base, "agingco", "PROFESSIONAL_SERVICES")
    _req(app, "agingco", "/t/agingco/customers", "POST", "name=Late+Payer&terms_days=30")

    # one long overdue, one not yet due (tenant "today" is 2026-08-31)
    _req(app, "agingco", "/t/agingco/invoices", "POST",
         "id=OLD&party_id=late-payer&date=2026-04-01&due_date=2026-04-30&amount1=500.00&code1=4100")
    _req(app, "agingco", "/t/agingco/invoices", "POST",
         "id=NEW&party_id=late-payer&date=2026-08-25&due_date=2026-09-24&amount1=250.00&code1=4100")

    page = _req(app, "agingco", "/t/agingco/invoices")
    assert "Overdue" in page.body           # the old one is flagged
    assert "$750.00" in page.body           # total owed

    aging = _req(app, "agingco", "/t/agingco/receivables/aging")
    assert aging.status == 200
    assert "Receivables Aging" in aging.body
    assert "Late Payer" in aging.body
    assert "$500.00" in aging.body and "$250.00" in aging.body


def test_a_viewer_cannot_invoice_or_collect(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["gated"])
    _seed(base, "gated", "SERVICE_GENERAL")
    # demote the member to viewer
    tok = sign_jwt({"sub": "u-gated", "tenant": "gated", "exp": NOW + 3600}, SECRET)
    app._users.set_membership("u-gated", "gated", Role.VIEWER)  # type: ignore[attr-defined]

    create = app.handle(Request("POST", "/t/gated/invoices",
                                {"authorization": f"Bearer {tok}",
                                 "content-type": "application/x-www-form-urlencoded"},
                                "id=X&party_id=y&date=2026-08-01&amount1=10&code1=4100"))
    assert create.status == 403
    # but they can still look
    assert app.handle(Request("GET", "/t/gated/invoices",
                              {"authorization": f"Bearer {tok}"}, "")).status == 200


def test_owner_reconciles_a_bank_account_against_the_real_service(ledger_service: str) -> None:
    """The month-end ritual: three transactions on the books, two on the bank
    statement. The owner ticks those two, the difference falls to zero, and
    finishing locks them in. The third is the outstanding check."""
    base = ledger_service
    app = _app(base, ["reconco"])
    _seed(base, "reconco", "PROFESSIONAL_SERVICES")

    _req(app, "reconco", "/t/reconco/books/entries", "POST",
         "date=2026-08-03&memo=Client+deposit&code1=1000&debit1=5,000.00"
         "&code2=4100&credit2=5000.00")
    _req(app, "reconco", "/t/reconco/books/entries", "POST",
         "date=2026-08-05&memo=Software&code1=6400&debit1=200.00&code2=1000&credit2=200.00")
    _req(app, "reconco", "/t/reconco/books/entries", "POST",
         "date=2026-08-30&memo=Check+not+cashed&code1=6400&debit1=50.00&code2=1000&credit2=50.00")

    # the account shows up as reconcilable
    pick = _req(app, "reconco", "/t/reconco/books/reconcile")
    assert pick.status == 200 and "Business Checking" in pick.body

    # the statement says $4,800.00 on 2026-08-31 — the check hasn't cleared
    url = "/t/reconco/books/reconcile/1000?statement_date=2026-08-31&statement_balance=4,800.00"
    page = _req(app, "reconco", url)
    assert page.status == 200
    assert "Client deposit" in page.body and "Check not cashed" in page.body
    assert "$4,800.00" in page.body          # still the whole difference
    assert "Finish reconciliation" not in page.body

    # find the two entry ids the statement covers and tick them
    entries = _service_get(base, "/t/reconco/accounts/1000/reconcile"
                                 "?statement_date=2026-08-31&statement_balance_minor=480000")
    to_tick = [ln["entry_id"] for ln in entries["lines"] if ln["memo"] != "Check not cashed"]
    assert len(to_tick) == 2
    for entry_id in to_tick:
        r = _req(app, "reconco", "/t/reconco/books/reconcile/1000/toggle", "POST",
                 f"entry_id={entry_id}&cleared=1&statement_date=2026-08-31"
                 "&statement_balance_minor=480000")
        assert r.status == 302 and "err=" not in str(r.headers.get("Location", ""))

    balanced = _req(app, "reconco", url)
    assert "Finish reconciliation" in balanced.body
    assert "Ticked" in balanced.body

    done = _req(app, "reconco", "/t/reconco/books/reconcile/1000/finish", "POST",
                "statement_date=2026-08-31&statement_balance_minor=480000")
    assert done.status == 302
    assert "done=" in str(done.headers.get("Location", ""))

    after = _req(app, "reconco", url)
    assert "Reconciled" in after.body
    assert "2026-08-31" in after.body
    # the outstanding check is still open, so September starts $50 short
    assert "-$50.00" in after.body


def test_the_real_ledger_refuses_an_out_of_balance_reconciliation(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["offbyco"])
    _seed(base, "offbyco", "PROFESSIONAL_SERVICES")
    _req(app, "offbyco", "/t/offbyco/books/entries", "POST",
         "date=2026-08-03&memo=Deposit&code1=1000&debit1=500.00&code2=4100&credit2=500.00")
    view = _service_get(base, "/t/offbyco/accounts/1000/reconcile"
                              "?statement_date=2026-08-31&statement_balance_minor=45000")
    entry_id = view["lines"][0]["entry_id"]
    _req(app, "offbyco", "/t/offbyco/books/reconcile/1000/toggle", "POST",
         f"entry_id={entry_id}&cleared=1&statement_date=2026-08-31"
         "&statement_balance_minor=45000")
    r = _req(app, "offbyco", "/t/offbyco/books/reconcile/1000/finish", "POST",
             "statement_date=2026-08-31&statement_balance_minor=45000")
    assert "err=" in str(r.headers.get("Location", ""))
    assert "off%20by" in str(r.headers.get("Location", ""))


def test_the_bank_feed_inbox_end_to_end_against_the_real_service(ledger_service: str) -> None:
    """A week of bank activity, reviewed the way an owner actually would: write a
    rule, accept the suggestion, match a deposit to the invoice it settles,
    exclude a personal charge, and undo one mistake."""
    base = ledger_service
    app = _app(base, ["feedco"])
    _seed(base, "feedco", "PROFESSIONAL_SERVICES")

    # an invoice already raised — the deposit below should settle it, not book
    # revenue a second time
    _req(app, "feedco", "/t/feedco/customers", "POST", "name=Halcyon+LLC&terms_days=30")
    _req(app, "feedco", "/t/feedco/invoices", "POST",
         "id=INV-1&party_id=halcyon-llc&date=2026-08-01&memo=August+retainer"
         "&amount1=5000.00&code1=4100")

    _service_post(base, "/t/feedco/feed/1000", {
        "source": "plaid-like",
        "transactions": [
            {"id": "bk-1", "date": "2026-08-03", "amount_minor": "500000",
             "description": "DEPOSIT HALCYON LLC", "counterparty": "Halcyon LLC"},
            {"id": "bk-2", "date": "2026-08-05", "amount_minor": "-24900",
             "description": "SQ *COFFEE 1187", "counterparty": "Square"},
            {"id": "bk-3", "date": "2026-08-09", "amount_minor": "-350000",
             "description": "RIVERSIDE PROPERTIES RENT", "counterparty": "Riverside Properties"},
            {"id": "bk-4", "date": "2026-08-11", "amount_minor": "-8900",
             "description": "NETFLIX.COM", "counterparty": "Netflix"},
        ],
    })

    # nothing from the FEED is in the books yet — a bank claim is not an
    # accounting fact until somebody says what it was. Only the invoice has
    # posted, and it moved no cash.
    tb = _service_get(base, "/t/feedco/trial-balance")
    cash = [r for r in tb["rows"] if r["code"] == "1000"]
    assert not cash, "no cash has moved in the books yet"
    assert any(r["code"] == "1200" for r in tb["rows"])   # only the invoice's AR

    queue = _req(app, "feedco", "/t/feedco/inbox")
    assert queue.status == 200
    assert "4 to review" in queue.body
    assert "DEPOSIT HALCYON LLC" in queue.body
    assert "INV-1" in queue.body and "(exact)" in queue.body   # the match is offered

    # a rule for the rent, written once
    rule = _req(app, "feedco", "/t/feedco/inbox/rules", "POST",
                "id=rent&description_contains=RIVERSIDE&sign=out&account_code=6300")
    assert "matches%201%20waiting" in str(rule.headers.get("Location", ""))

    # the rule pre-fills the rent line, and accepting posts it
    with_rule = _req(app, "feedco", "/t/feedco/inbox")
    assert '<option value="6300" selected>' in with_rule.body
    assert "100% · from your rule" in with_rule.body
    assert _req(app, "feedco", "/t/feedco/inbox/bk-3/accept", "POST",
                "category_code=6300").status == 302

    # the deposit settles the invoice rather than booking revenue twice
    assert _req(app, "feedco", "/t/feedco/inbox/bk-1/match", "POST",
                "match=invoice%3AINV-1").status == 302
    inv = _req(app, "feedco", "/t/feedco/invoices")
    assert "Paid" in inv.body

    # coffee is a real cost; Netflix on the business card is not
    _req(app, "feedco", "/t/feedco/inbox/bk-2/accept", "POST", "category_code=6400")
    _req(app, "feedco", "/t/feedco/inbox/bk-4/exclude", "POST", "reason=Personal")

    empty = _req(app, "feedco", "/t/feedco/inbox")
    assert "All caught up" in empty.body

    # the books now reflect exactly the four decisions
    books = _req(app, "feedco", "/t/feedco/books")
    assert "In balance" in books.body
    tb = _service_get(base, "/t/feedco/trial-balance")
    signed = {r["code"]: int(r["debit_minor"]) - int(r["credit_minor"]) for r in tb["rows"]}
    assert signed["1000"] == 500000 - 350000 - 24900, "cash: deposit less rent and coffee"
    assert signed["6300"] == 350000
    assert signed["6400"] == 24900
    assert signed.get("1200", 0) == 0, "the matched deposit cleared AR"
    assert signed["4100"] == -500000, "revenue was booked once, by the invoice"

    # one was miscategorized: undo it and it comes back to the queue
    undo = _req(app, "feedco", "/t/feedco/inbox/bk-2/undo", "POST")
    assert "reversing%20entry" in str(undo.headers.get("Location", ""))
    again = _req(app, "feedco", "/t/feedco/inbox")
    assert "1 to review" in again.body
    # the original entry AND its reversal are both still in the journal
    entries = _service_get(base, "/t/feedco/entries")["entries"]
    assert any(e["status"] == "REVERSAL" for e in entries)


def test_the_real_service_learns_from_what_was_categorized_before(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["learnco"])
    _seed(base, "learnco", "PROFESSIONAL_SERVICES")
    _service_post(base, "/t/learnco/feed/1000", {"transactions": [
        {"id": "c-1", "date": "2026-08-04", "amount_minor": "-1200",
         "description": "SQ *DAILY GRIND 4417", "counterparty": "Daily Grind"},
    ]})
    _req(app, "learnco", "/t/learnco/inbox/c-1/accept", "POST", "category_code=6400")

    # a different card reference from the same vendor
    _service_post(base, "/t/learnco/feed/1000", {"transactions": [
        {"id": "c-2", "date": "2026-08-18", "amount_minor": "-1450",
         "description": "SQ *DAILY GRIND 9902", "counterparty": "Daily Grind"},
    ]})
    page = _req(app, "learnco", "/t/learnco/inbox")
    assert '<option value="6400" selected>' in page.body
    assert "from your history" in page.body


def test_a_feed_line_cannot_be_posted_into_a_closed_month(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["lockedco"])
    _seed(base, "lockedco", "PROFESSIONAL_SERVICES")
    _service_post(base, "/t/lockedco/feed/1000", {"transactions": [
        {"id": "old-1", "date": "2026-07-15", "amount_minor": "-5000", "description": "LATE ARRIVAL"},
    ]})
    _service_post(base, "/t/lockedco/periods/2026-07/lock", {})
    r = _req(app, "lockedco", "/t/lockedco/inbox/old-1/accept", "POST", "category_code=6400")
    assert "err=" in str(r.headers.get("Location", ""))
    # and it is still waiting rather than silently lost
    assert "1 to review" in _req(app, "lockedco", "/t/lockedco/inbox").body


def test_payroll_end_to_end_against_the_real_service(ledger_service: str) -> None:
    """Payroll as an accrual: the run costs more than the cash that leaves, and
    the difference is a real liability until the deposit is made."""
    base = ledger_service
    app = _app(base, ["payco"])
    _seed(base, "payco", "PROFESSIONAL_SERVICES")

    _req(app, "payco", "/t/payco/payroll/employees", "POST", "name=Ada+Reyes")
    _req(app, "payco", "/t/payco/payroll/employees", "POST", "name=Jo+Okafor")

    draft = _req(app, "payco", "/t/payco/payroll", "POST",
                 "id=PR-2026-08-15&date=2026-08-15&memo=August+1-15&employer_taxes=612.00"
                 "&employee1=ada-reyes&gross1=5,000.00&taxes1=1100.00&deductions1=200.00"
                 "&employee2=jo-okafor&gross2=3000.00&taxes2=600.00")
    assert draft.status == 302 and "err=" not in str(draft.headers.get("Location", ""))

    # a draft posts nothing
    assert not _service_get(base, "/t/payco/trial-balance")["rows"]

    page = _req(app, "payco", "/t/payco/payroll")
    assert "$8,612.00" in page.body      # what it really costs
    assert "$6,100.00" in page.body      # what actually leaves the bank

    assert _req(app, "payco", "/t/payco/payroll/PR-2026-08-15/post", "POST").status == 302

    tb = _service_get(base, "/t/payco/trial-balance")
    signed = {r["code"]: int(r["debit_minor"]) - int(r["credit_minor"]) for r in tb["rows"]}
    assert signed["6200"] == 800000, "wages expense is gross"
    assert signed["6210"] == 61200, "the employer's own taxes are a cost"
    assert signed["1000"] == -610000, "only net pay left the bank"
    assert -signed["2300"] == 251200, "the rest is owed"
    assert tb["in_balance"] is True

    owed = _req(app, "payco", "/t/payco/payroll")
    assert "You owe $2,512.00 in payroll liabilities" in owed.body

    # deposit it in two goes
    _req(app, "payco", "/t/payco/payroll/remit", "POST", "date=2026-08-18&amount=1700.00")
    part = _req(app, "payco", "/t/payco/payroll")
    assert "You owe $812.00" in part.body
    _req(app, "payco", "/t/payco/payroll/remit", "POST", "date=2026-08-20&amount=812.00")
    clear = _req(app, "payco", "/t/payco/payroll")
    assert "No outstanding payroll liabilities" in clear.body

    tb = _service_get(base, "/t/payco/trial-balance")
    signed = {r["code"]: int(r["debit_minor"]) - int(r["credit_minor"]) for r in tb["rows"]}
    assert signed.get("2300", 0) == 0
    assert signed["1000"] == -861200, "in the end the full cost left the bank"


def test_the_real_service_refuses_to_overpay_payroll_liabilities(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["overpayco"])
    _seed(base, "overpayco", "PROFESSIONAL_SERVICES")
    _req(app, "overpayco", "/t/overpayco/payroll/employees", "POST", "name=Ada")
    _req(app, "overpayco", "/t/overpayco/payroll", "POST",
         "id=PR-1&date=2026-08-15&employee1=ada&gross1=1000.00&taxes1=200.00")
    _req(app, "overpayco", "/t/overpayco/payroll/PR-1/post", "POST")
    r = _req(app, "overpayco", "/t/overpayco/payroll/remit", "POST",
             "date=2026-08-18&amount=500.00")
    assert "overpay" in str(r.headers.get("Location", ""))


def test_sales_tax_credits_and_refunds_against_the_real_service(ledger_service: str) -> None:
    """The three adjustments a real business needs, end to end: tax collected as
    a liability, a credit for over-billing, and a refund for money already taken."""
    base = ledger_service
    app = _app(base, ["taxco"])
    _seed(base, "taxco", "PROFESSIONAL_SERVICES")
    _req(app, "taxco", "/t/taxco/customers", "POST", "name=Halcyon+LLC&terms_days=30")

    # $1,000 of work at 8.25%
    r = _req(app, "taxco", "/t/taxco/invoices", "POST",
             "id=INV-1&party_id=halcyon-llc&date=2026-08-01&tax_rate=8.25"
             "&amount1=1000.00&code1=4100&desc1=Consulting")
    assert r.status == 302 and "err=" not in str(r.headers.get("Location", ""))

    page = _req(app, "taxco", "/t/taxco/invoices")
    assert "$1,000.00 + $82.50 sales tax (8.25%)" in page.body
    assert "$1,082.50" in page.body

    signed = _signed(base, "taxco")
    assert signed["1200"] == 108250, "AR is what they owe, tax included"
    assert signed["4100"] == -100000, "revenue is the NET"
    assert -signed["2200"] == 8250, "the tax is a liability, not income"

    # over-billed by $250 — credit it, don't edit the invoice
    _req(app, "taxco", "/t/taxco/invoices/INV-1/credits", "POST",
         "amount=250.00&date=2026-08-05&memo=Overbilled")
    signed = _signed(base, "taxco")
    assert signed["1200"] == 83250
    assert signed["4100"] == -75000, "revenue came back down"
    inv = _service_get(base, "/t/taxco/invoices/INV-1")["document"]
    assert inv["total_minor"] == "108250", "the invoice still says what it said"
    assert inv["open_minor"] == "83250"

    # they pay the rest, then cancel — the money physically goes back
    _req(app, "taxco", "/t/taxco/invoices/INV-1/payments", "POST",
         "amount=832.50&date=2026-08-12")
    assert _signed(base, "taxco")["1000"] == 83250
    refunded = _req(app, "taxco", "/t/taxco/invoices/INV-1/refunds", "POST",
                    "amount=300.00&date=2026-08-20")
    assert "Refunded%20300.00" in str(refunded.headers.get("Location", ""))
    signed = _signed(base, "taxco")
    assert signed["1000"] == 53250, "cash left the bank"
    assert signed["4100"] == -45000, "and the revenue went with it"

    tb = _service_get(base, "/t/taxco/trial-balance")
    assert tb["in_balance"] is True
    # every adjustment is its own entry — nothing was edited
    assert len(_service_get(base, "/t/taxco/entries")["entries"]) == 4


def test_the_real_service_refuses_the_wrong_adjustment(ledger_service: str) -> None:
    base = ledger_service
    app = _app(base, ["adjco"])
    _seed(base, "adjco", "PROFESSIONAL_SERVICES")
    _req(app, "adjco", "/t/adjco/customers", "POST", "name=Halcyon+LLC")
    _req(app, "adjco", "/t/adjco/invoices", "POST",
         "id=INV-1&party_id=halcyon-llc&date=2026-08-01&amount1=100.00&code1=4100")

    # nothing collected yet, so a refund is the wrong tool
    r = _req(app, "adjco", "/t/adjco/invoices/INV-1/refunds", "POST", "amount=50.00")
    assert "credit%20instead" in str(r.headers.get("Location", ""))

    # settle it, and now a credit is the wrong tool
    _req(app, "adjco", "/t/adjco/invoices/INV-1/payments", "POST", "amount=100.00")
    r = _req(app, "adjco", "/t/adjco/invoices/INV-1/credits", "POST", "amount=50.00")
    assert "refund%2C%20not%20a%20credit" in str(r.headers.get("Location", ""))


def test_general_ledger_and_budget_against_the_real_service(ledger_service: str) -> None:
    """The reports an accountant asks for, computed from the client's own books."""
    base = ledger_service
    app = _app(base, ["repco"])
    _seed(base, "repco", "PROFESSIONAL_SERVICES")

    for date_, memo, dr, cr, amount in [
        ("2026-07-20", "July retainer", "1000", "4100", "4,000.00"),
        ("2026-08-03", "August retainer", "1000", "4100", "12,000.00"),
        ("2026-08-09", "Studio rent", "6300", "1000", "3,500.00"),
        ("2026-08-21", "Software", "6500", "1000", "249.00"),
    ]:
        r = _req(app, "repco", "/t/repco/books/entries", "POST",
                 f"date={date_}&memo={memo.replace(' ', '+')}"
                 f"&code1={dr}&debit1={amount}&code2={cr}&credit2={amount}")
        assert r.status == 302 and "err=" not in str(r.headers.get("Location", ""))

    # the whole ledger, and it balances on screen
    gl = _req(app, "repco", "/t/repco/books/gl")
    assert gl.status == 200
    assert "Debits equal credits across every account" in gl.body
    assert "August retainer" in gl.body and "Studio rent" in gl.body
    assert "$12,251.00" in gl.body            # closing cash

    # August only: July folds into the opening balance rather than disappearing
    august = _req(app, "repco", "/t/repco/books/gl?from=2026-08-01&to=2026-08-31")
    assert "July retainer" not in august.body
    assert "$4,000.00" in august.body         # ...but it is the opening

    # budget the month, then compare
    saved = _req(app, "repco", "/t/repco/books/budget", "POST",
                 "period=2026-08&amount_4100=15,000.00&amount_6300=3500.00"
                 "&amount_6500=400.00")
    assert "Budget%20saved" in str(saved.headers.get("Location", ""))

    page = _req(app, "repco", "/t/repco/books/budget?period=2026-08")
    assert page.status == 200
    assert "$15,000.00" in page.body and "$12,000.00" in page.body
    assert "worse than planned" in page.body   # revenue missed
    assert "better than planned" in page.body  # software under-spent
    assert "on plan" in page.body              # rent exactly on budget

    # the budget is the period's activity, not the balance carried into it
    data = _service_get(base, "/t/repco/budget/2026-08")
    revenue = [l for l in data["lines"] if l["code"] == "4100"][0]
    assert revenue["actual_minor"] == "1200000", "July's 4,000 is not in August"
    assert revenue["favorable"] is False


def test_classes_end_to_end_against_the_real_service(ledger_service: str) -> None:
    """A restaurant with two lines of business: what did each one actually earn?"""
    base = ledger_service
    app = _app(base, ["classco"])
    _seed(base, "classco", "RESTAURANT")

    made = _req(app, "classco", "/t/classco/books/dimensions", "POST",
                "key=class&label=Line+of+business&values=Dine-in%0ACatering&required=1")
    assert made.status == 302 and "err=" not in str(made.headers.get("Location", ""))

    # a class is now demanded on income and cost lines
    untagged = _req(app, "classco", "/t/classco/books/entries", "POST",
                    "date=2026-08-05&memo=Dinner&code1=1000&debit1=1200.00"
                    "&code2=4100&credit2=1200.00")
    assert "err=" in str(untagged.headers.get("Location", ""))
    assert "required" in str(untagged.headers.get("Location", ""))

    for date_, memo, amount, klass in [
        ("2026-08-05", "Dinner+service", "1200.00", "Dine-in"),
        ("2026-08-12", "Wedding", "8000.00", "Catering"),
    ]:
        r = _req(app, "classco", "/t/classco/books/entries", "POST",
                 f"date={date_}&memo={memo}&code1=1000&debit1={amount}"
                 f"&code2=4100&credit2={amount}&dim_class2={klass}")
        assert r.status == 302 and "err=" not in str(r.headers.get("Location", "")), klass

    # a typo is refused at the door, not silently accepted
    typo = _service_post_raw(base, "/t/classco/entries", {
        "date": "2026-08-14", "memo": "Typo",
        "lines": [
            {"code": "1000", "side": "DEBIT", "amount_minor": "100"},
            {"code": "4100", "side": "CREDIT", "amount_minor": "100",
             "dimensions": {"class": "Dinein"}},
        ],
    })
    assert typo["status"] == 400
    assert "not allowed" in typo["error"]

    report = _req(app, "classco", "/t/classco/books/dimensions/class")
    assert report.status == 200
    assert "$1,200.00" in report.body and "$8,000.00" in report.body
    assert "Dine-in" in report.body and "Catering" in report.body

    data = _service_get(base, "/t/classco/dimensions/class/report")
    by = {b["value"]: b for b in data["buckets"]}
    assert by["Dine-in"]["total_credit_minor"] == "120000"
    assert by["Catering"]["total_credit_minor"] == "800000"


def test_the_forecast_reads_the_real_books_end_to_end(ledger_service: str) -> None:
    """The hybrid, against the running service: cash and open items come from
    the books, the owner's pipeline stays theirs, and posting changes the
    forecast immediately."""
    base = ledger_service
    app = _app(base, ["hybridco"])
    _seed(base, "hybridco", "PROFESSIONAL_SERVICES")

    # money in the bank, an invoice owed to us, a bill we owe
    _req(app, "hybridco", "/t/hybridco/books/entries", "POST",
         "date=2026-08-03&memo=Opening&code1=1000&debit1=10,000.00"
         "&code2=3000&credit2=10000.00")
    _req(app, "hybridco", "/t/hybridco/customers", "POST", "name=Halcyon+LLC&terms_days=30")
    _req(app, "hybridco", "/t/hybridco/invoices", "POST",
         "id=INV-1&party_id=halcyon-llc&date=2026-08-10&due_date=2026-09-09"
         "&amount1=4,000.00&code1=4100")
    _req(app, "hybridco", "/t/hybridco/vendors", "POST", "name=Copyshop&terms_days=15")
    _req(app, "hybridco", "/t/hybridco/bills", "POST",
         "id=BILL-1&party_id=copyshop&date=2026-08-12&due_date=2026-08-27"
         "&amount1=350.00&code1=6400")

    cash = _req(app, "hybridco", "/t/hybridco")
    assert cash.status == 200
    assert "$10,000.00" in cash.body, "opening cash is what the books say"
    assert "come straight from your books" in cash.body

    # spend some of it and the forecast follows without a refresh of anything
    _req(app, "hybridco", "/t/hybridco/books/entries", "POST",
         "date=2026-08-20&memo=Rent&code1=6300&debit1=1,500.00"
         "&code2=1000&credit2=1500.00")
    after = _req(app, "hybridco", "/t/hybridco")
    assert "$8,500.00" in after.body


def test_attaching_a_receipt_end_to_end_against_the_real_service(ledger_service: str) -> None:
    """A receipt uploaded as multipart, stored beside the books, and served back
    byte-for-byte — including bytes that are not valid UTF-8."""
    base = ledger_service
    app = _app(base, ["fileco"])
    _seed(base, "fileco", "PROFESSIONAL_SERVICES")
    _service_post(base, "/t/fileco/feed/1000", {"transactions": [
        {"id": "bk-1", "date": "2026-08-21", "amount_minor": "-24900",
         "description": "ADOBE SUBSCRIPTION"},
    ]})

    binary = bytes([0x89, 0x50, 0x4E, 0x47]) + bytes(range(256))
    boundary = "E2EBOUNDARY"
    body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="note"\r\n\r\nAdobe renewal\r\n'
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="receipt.png"\r\n'
        "Content-Type: image/png\r\n\r\n"
    ).encode() + binary + f"\r\n--{boundary}--\r\n".encode()

    tok = sign_jwt({"sub": "u-fileco", "tenant": "fileco", "exp": NOW + 3600}, SECRET)
    up = app.handle(Request(
        "POST", "/t/fileco/files/feed/bk-1",
        {"authorization": f"Bearer {tok}",
         "content-type": f"multipart/form-data; boundary={boundary}"},
        body.decode("utf-8", "surrogateescape"),
    ))
    assert up.status == 302
    assert "err=" not in str(up.headers.get("Location", ""))

    listing = _req(app, "fileco", "/t/fileco/files/feed/bk-1")
    assert "receipt.png" in listing.body and "Adobe renewal" in listing.body

    stored = _service_get(base, "/t/fileco/attachments?subject_kind=feed&subject_id=bk-1")
    att_id = stored["attachments"][0]["id"]
    assert stored["attachments"][0]["bytes"] == len(binary)

    down = _req(app, "fileco", f"/t/fileco/files/{att_id}/download")
    assert down.status == 200
    assert down.body == binary, "every byte survived the round trip"
    assert down.headers["Content-Disposition"].startswith("attachment;")

    # the queue shows the line now has evidence
    queue = _req(app, "fileco", "/t/fileco/inbox")
    assert "/t/fileco/files/feed/bk-1" in queue.body


def test_importing_a_statement_end_to_end_against_the_real_service(ledger_service: str) -> None:
    """Upload the bank's CSV, watch it tick off what matches, and see what it
    couldn't — the transaction the books don't know about."""
    base = ledger_service
    app = _app(base, ["stmtco"])
    _seed(base, "stmtco", "PROFESSIONAL_SERVICES")

    for date_, memo, dr, cr, amount in [
        ("2026-08-03", "Client+deposit", "1000", "4100", "5,000.00"),
        ("2026-08-05", "Software", "6400", "1000", "200.00"),
        ("2026-08-30", "Cheque+not+cashed", "6400", "1000", "50.00"),
    ]:
        _req(app, "stmtco", "/t/stmtco/books/entries", "POST",
             f"date={date_}&memo={memo}&code1={dr}&debit1={amount}"
             f"&code2={cr}&credit2={amount}")

    # the bank's export: the two that cleared, plus one nobody recorded
    csv = (
        "Date,Description,Amount\r\n"
        "2026-08-03,ACH CREDIT CLIENT,5000.00\r\n"
        "2026-08-06,ADOBE SUBSCRIPTION,-200.00\r\n"
        "2026-08-20,MYSTERY DEBIT,-125.00\r\n"
    )
    body = (
        "--B\r\n"
        'Content-Disposition: form-data; name="statement_date"\r\n\r\n2026-08-31\r\n'
        "--B\r\n"
        'Content-Disposition: form-data; name="statement_balance_minor"\r\n\r\n467500\r\n'
        "--B\r\n"
        'Content-Disposition: form-data; name="file"; filename="statement.csv"\r\n'
        "Content-Type: text/csv\r\n\r\n" + csv + "\r\n--B--\r\n"
    )
    tok = sign_jwt({"sub": "u-stmtco", "tenant": "stmtco", "exp": NOW + 3600}, SECRET)
    r = app.handle(Request(
        "POST", "/t/stmtco/books/reconcile/1000/import",
        {"authorization": f"Bearer {tok}",
         "content-type": "multipart/form-data; boundary=B"}, body,
    ))
    assert r.status == 200
    assert "Read 3 transactions and ticked off 2" in r.body
    assert "MYSTERY DEBIT" in r.body, "the books don't know about this one"
    assert "Cheque not cashed" in r.body, "and this one hasn't cleared yet"

    # the ticks are durable and the worksheet agrees
    view = _service_get(base, "/t/stmtco/accounts/1000/reconcile"
                              "?statement_date=2026-08-31&statement_balance_minor=467500")
    assert len([l for l in view["lines"] if l["status"] == "CLEARED"]) == 2
    # it does NOT tie out, because the books really are missing 125.00
    assert view["difference_minor"] == "-12500"
    assert view["can_finish"] is False


def test_recurring_transactions_end_to_end_against_the_real_service(ledger_service: str) -> None:
    """Memorize rent, see three months due, post them, and confirm running again
    cannot post them twice."""
    base = ledger_service
    app = _app(base, ["recurco"])
    _seed(base, "recurco", "PROFESSIONAL_SERVICES")

    made = _req(app, "recurco", "/t/recurco/books/recurring", "POST",
                "name=Studio+rent&frequency=MONTHLY&interval=1&start_date=2026-06-01"
                "&memo=Riverside&code1=6300&debit1=3,500.00&code2=1000&credit2=3500.00")
    assert made.status == 302 and "err=" not in str(made.headers.get("Location", ""))

    page = _req(app, "recurco", "/t/recurco/books/recurring?as_of=2026-08-31")
    assert "3 due as of 2026-08-31" in page.body
    assert "every month from 2026-06-01" in page.body
    # nothing has posted just from looking
    assert not _service_get(base, "/t/recurco/trial-balance")["rows"]

    ran = _req(app, "recurco", "/t/recurco/books/recurring/run", "POST",
               "as_of=2026-08-31")
    assert ran.status == 200
    assert "Posted 3" in ran.body

    signed = _signed(base, "recurco")
    assert signed["6300"] == 1050000, "three months of rent"
    assert signed["1000"] == -1050000

    # running again is safe
    again = _req(app, "recurco", "/t/recurco/books/recurring/run", "POST",
                 "as_of=2026-08-31")
    assert "Posted 0" in again.body
    assert _signed(base, "recurco")["6300"] == 1050000, "still three, not six"

    empty = _req(app, "recurco", "/t/recurco/books/recurring?as_of=2026-08-31")
    assert "Nothing due" in empty.body


def test_1099_contractors_end_to_end_against_the_real_service(ledger_service: str) -> None:
    """Flag two contractors, pay them, and read the January report: who you owe a
    form to and cannot file for, who is one invoice away from the list, and what
    money left the bank to a contractor without going through a bill."""
    base = ledger_service
    app = _app(base, ["ten99co"])
    _seed(base, "ten99co", "PROFESSIONAL_SERVICES")

    # flagged before the W-9 arrives — waiting until January is how the tax id
    # never gets collected at all
    _req(app, "ten99co", "/t/ten99co/vendors", "POST",
         "name=Dana+Ruiz+Design&is_1099=1")
    _req(app, "ten99co", "/t/ten99co/vendors", "POST",
         "name=Sam+Okonkwo&is_1099=1&tax_id=98-7654321")
    _req(app, "ten99co", "/t/ten99co/vendors", "POST",
         "name=Riverside+Properties")           # a landlord, not a contractor

    for doc, party, when, amount in [
        ("B-1", "dana-ruiz-design", "2026-03-10", "4000.00"),
        ("B-2", "sam-okonkwo", "2026-05-01", "540.00"),
        ("B-3", "riverside-properties", "2026-03-01", "3500.00"),
    ]:
        made = _req(app, "ten99co", "/t/ten99co/bills", "POST",
                    f"id={doc}&party_id={party}&date={when}&amount1={amount}&code1=6600")
        assert "err=" not in str(made.headers.get("Location", "")), made.headers
        paid = _req(app, "ten99co", f"/t/ten99co/bills/{doc}/payments", "POST",
                    f"date={when}&amount={amount}")
        assert "err=" not in str(paid.headers.get("Location", "")), paid.headers

    # and one payment straight out of the bank, categorized but never billed
    _service_post(base, "/t/ten99co/feed/1000", {"transactions": [
        {"id": "bk-1", "date": "2026-09-04", "amount_minor": "-120000",
         "description": "TRANSFER TO DANA", "counterparty": "Dana Ruiz Design"},
    ]})
    _req(app, "ten99co", "/t/ten99co/inbox/bk-1/accept", "POST", "category_code=6600")

    page = _req(app, "ten99co", "/t/ten99co/books/1099?year=2026")
    assert page.status == 200

    # the form you owe and cannot file, with the amount that makes it urgent
    assert "You owe a 1099 to Dana Ruiz Design and have no tax id on file" in page.body
    assert "$4,000.00" in page.body
    assert "W-9 needed" in page.body

    # the landlord is not a contractor and never appears
    assert "Riverside Properties" not in page.body

    # $60 from needing a form
    assert "Tracked, but under the threshold" in page.body
    assert "Sam Okonkwo" in page.body and "$540.00" in page.body and "$60.00" in page.body

    # the bank payment is a prompt, not a number folded into the total
    assert "Paid from the bank, never billed" in page.body
    assert "TRANSFER TO DANA" in page.body and "-$1,200.00" in page.body

    report = _service_get(base, "/t/ten99co/1099/2026")
    assert [r["vendor_name"] for r in report["rows"]] == ["Dana Ruiz Design"]
    assert report["total_minor"] == "400000", "the unbilled 1,200 is NOT in the total"
    assert report["missing_tax_id"] == ["dana-ruiz-design"]

    # collect the W-9 and the warning turns into a filable form
    _req(app, "ten99co", "/t/ten99co/vendors", "POST",
         "id=dana-ruiz-design&name=Dana+Ruiz+Design&is_1099=1&tax_id=12-3456789")
    after = _req(app, "ten99co", "/t/ten99co/books/1099?year=2026")
    assert "no tax id on file" not in after.body
    assert "1 form(s) to file for 2026, all with a tax id" in after.body

    # another company's contractors are none of this company's business
    app2 = _app(base, ["othercorp"])
    _seed(base, "othercorp", "PROFESSIONAL_SERVICES")
    other = _req(app2, "othercorp", "/t/othercorp/books/1099?year=2026")
    assert "Dana" not in other.body
    assert "Nobody was paid enough to need a form this year" in other.body
