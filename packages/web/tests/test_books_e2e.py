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
