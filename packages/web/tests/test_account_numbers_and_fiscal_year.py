"""Account numbers that are really QuickBooks ids, and the fiscal year (the rest
of issues 25 and 27).

Books mirrored from a QuickBooks company that doesn't number its accounts carry
QuickBooks' internal ids as codes ("57"), and the Ledger showed and sorted by
them. And Business health's revenue covered everything in the books, where an
owner reads "this year" as their fiscal year, which QuickBooks knows.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlencode

from test_books import ACCOUNTS, DEFAULT_ROUTES, TRIAL_BALANCE, FakeTransport, _inputs, _req

from rgnr8_forecast import ForecastConfig, Money
from rgnr8_web import (
    InMemoryAuditLog,
    InMemoryUserDirectory,
    JwtAuthenticator,
    LedgerClient,
    Request,
    Role,
    User,
    WebApp,
)
from rgnr8_web.books_screens import account_label, account_number, account_order
from rgnr8_web.health_screens import fiscal_year_start, period_words
from rgnr8_web.store import InMemoryTenantStore, JsonFileTenantStore, TenantState, TenantStore

SECRET = "books-secret"
NOW = 1_760_000_000

# A QuickBooks company with no account numbers: every code is QuickBooks' id.
ID_ACCOUNTS = {"tenant": "acme", "accounts": [
    {"code": "57", "name": "Business Checking", "type": "ASSET", "subtype": "BANK"},
    {"code": "9", "name": "Accounts Receivable", "type": "ASSET", "subtype": "RECEIVABLE"},
    {"code": "1", "name": "Services", "type": "REVENUE", "subtype": "INCOME"},
    {"code": "103", "name": "Advertising", "type": "EXPENSE", "subtype": "EXPENSE"},
    {"code": "12", "name": "Rent", "type": "EXPENSE", "subtype": "EXPENSE"},
]}
ID_TB = {
    "tenant": "acme", "currency": "USD", "in_balance": True,
    "total_debit_minor": "500000", "total_credit_minor": "500000",
    "rows": [
        {"code": "1", "name": "Services", "type": "REVENUE",
         "debit_minor": "0", "credit_minor": "500000"},
        {"code": "103", "name": "Advertising", "type": "EXPENSE",
         "debit_minor": "50000", "credit_minor": "0"},
        {"code": "12", "name": "Rent", "type": "EXPENSE",
         "debit_minor": "150000", "credit_minor": "0"},
        {"code": "57", "name": "Business Checking", "type": "ASSET",
         "debit_minor": "300000", "credit_minor": "0"},
    ],
}
ID_CODES = ["1", "103", "12", "57", "9"]


def _app(routes: "dict[str, Any] | None" = None, *, store: "TenantStore | None" = None,
         state: "TenantState | None" = None) -> "tuple[WebApp, FakeTransport]":
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    store = store if store is not None else InMemoryTenantStore()
    if state is not None:
        store.save("acme", state)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
                 users=users, audit=InMemoryAuditLog(), store=store)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("1000.00")), token="unused")
    transport = FakeTransport(routes if routes is not None else dict(DEFAULT_ROUTES))
    app.set_ledger(LedgerClient(transport, token="svc-token"))
    return app, transport


def _id_coded_app() -> "tuple[WebApp, FakeTransport]":
    routes = {**DEFAULT_ROUTES, "GET /t/acme/accounts": (200, ID_ACCOUNTS),
              "GET /t/acme/trial-balance": (200, ID_TB)}
    return _app(routes, state=TenantState(qbo_id_codes=list(ID_CODES)))


def _main(html: str) -> str:
    return html[html.index('<main class="shell">'):]


def _shown(html: str) -> str:
    """The page's text as a person sees it: tags (and their hrefs) removed."""
    return re.sub(r"<[^>]+>", "|", html)


# --- how an account reads --------------------------------------------------------

def test_an_id_code_reads_as_the_account_name_and_a_number_keeps_showing() -> None:
    ids = frozenset({"57"})
    assert account_label("57", "Business Checking", ids) == "Business Checking"
    assert account_label("4000", "Sales", ids) == "4000 — Sales"
    assert account_number("57", ids) == "" and account_number("4000", ids) == "4000"
    # Nothing recorded (a chart RGNR8 set up, or a mirror not refreshed since):
    # every code is shown, as before.
    assert account_label("57", "Business Checking") == "57 — Business Checking"


def test_ids_sort_by_type_then_name_and_numbers_numerically() -> None:
    accounts = [("12", "Rent", "EXPENSE"), ("103", "Advertising", "EXPENSE"),
                ("57", "Checking", "ASSET"), ("9", "Accounts Receivable", "ASSET"),
                ("6500", "Utilities", "EXPENSE"), ("600", "Payroll", "EXPENSE")]
    ids = frozenset({"12", "103", "57", "9"})
    order = [c for c, n, t in sorted(accounts, key=lambda a: account_order(*a, ids))]
    assert order == ["9", "57", "600", "6500", "103", "12"]
    # A fully numbered chart sorts by number alone, as it always has.
    assert [c for c, n, t in sorted(accounts, key=lambda a: account_order(*a))] == \
        ["9", "12", "57", "103", "600", "6500"]


# --- the Ledger screens ------------------------------------------------------------

def test_the_ledger_shows_an_id_coded_chart_by_name_in_type_then_name_order() -> None:
    app, _ = _id_coded_app()
    main = _main(_req(app, "/t/acme/books").body)
    accounts = main[:main.index("Record a transaction")]
    shown = _shown(accounts)
    for code in ID_CODES:
        assert f"|{code}|" not in shown, code     # no QuickBooks id shown as a number
    assert "<th>No.</th>" not in accounts          # nothing numbered: no number column
    assert (accounts.index("Accounts Receivable") < accounts.index("Business Checking")
            < accounts.index(">Services<") < accounts.index("Advertising") < accounts.index(">Rent<"))
    # The link still goes by the stored code: that's how the books join QuickBooks.
    assert 'href="/t/acme/books/accounts/57">Business Checking</a>' in accounts
    # And the picker on the same page names the account, not the id.
    picker = main[main.index("Record a transaction"):]
    assert '<option value="57">Business Checking</option>' in picker
    assert "57 — " not in picker


def test_the_trial_balance_and_chart_of_accounts_do_the_same() -> None:
    app, _ = _id_coded_app()
    tb = _main(_req(app, "/t/acme/books/trial-balance").body)
    names = re.findall(r'books/accounts/\d+">([^<]+)</a>', tb)
    assert names == ["Business Checking", "Services", "Advertising", "Rent"]
    assert not any(f"|{c}|" in _shown(tb) for c in ID_CODES)
    assert "<th>No.</th>" not in tb                # no numbers: no empty number column

    routes = {**DEFAULT_ROUTES, "GET /t/acme/accounts": (200, ID_ACCOUNTS)}
    app, _ = _app(routes, state=TenantState(qbo_id_codes=list(ID_CODES)))
    chart = _main(_req(app, "/t/acme/books/accounts").body)
    names = re.findall(r'books/accounts/\d+">([^<]+)</a>', chart)
    assert names == ["Accounts Receivable", "Business Checking", "Services", "Advertising", "Rent"]
    assert not any(f"|{c}|" in _shown(chart) for c in ID_CODES)
    assert "<th>No.</th>" not in chart


def test_a_numbered_chart_keeps_its_numbers_in_numeric_order() -> None:
    accounts = {"tenant": "acme", "accounts": [
        *ACCOUNTS["accounts"], {"code": "900", "name": "Petty Cash", "type": "ASSET"}]}
    app, _ = _app({**DEFAULT_ROUTES, "GET /t/acme/accounts": (200, accounts)},
                  state=TenantState(qbo_id_codes=[]))
    main = _main(_req(app, "/t/acme/books").body)
    assert "<th>No.</th>" in main
    assert '<td class="muted">1000</td>' in main and '<td class="muted">900</td>' in main
    assert main.index("Petty Cash") < main.index("Business Checking")      # 900 before 1000
    assert '<option value="4000">4000 — Services Income</option>' in main
    tb = _main(_req(app, "/t/acme/books/trial-balance").body)
    assert '<td class="muted">1000</td>' in tb and '<td class="muted">4000</td>' in tb


def test_a_partly_numbered_chart_shows_numbers_first_then_names() -> None:
    accounts = {"tenant": "acme", "accounts": [
        {"code": "12", "name": "Rent", "type": "EXPENSE"},
        {"code": "6000", "name": "Wages", "type": "EXPENSE"},
        {"code": "103", "name": "Advertising", "type": "EXPENSE"},
    ]}
    app, _ = _app({**DEFAULT_ROUTES, "GET /t/acme/accounts": (200, accounts),
                   "GET /t/acme/trial-balance": (200, {**TRIAL_BALANCE, "rows": []})},
                  state=TenantState(qbo_id_codes=["12", "103"]))
    main = _main(_req(app, "/t/acme/books").body)
    expenses = main[main.index(">Expenses<"):main.index("Total expenses")]
    assert expenses.index("Wages") < expenses.index("Advertising") < expenses.index(">Rent<")
    assert '<td class="muted">6000</td>' in expenses and "|12|" not in _shown(expenses)


# --- where the record comes from: go-live and every Refresh ------------------------

def test_go_live_records_which_codes_are_ids_and_the_fiscal_year(monkeypatch) -> None:
    import rgnr8_web.qbo_migrate as qm
    import rgnr8_web.qbo_mirror as mirror_mod
    from rgnr8_web.qbo_mirror import MirrorResult
    from test_qbo_connect import RecordingLedgerTransport, _connected_app, _h, _qr

    monkeypatch.setattr(qm, "migrate_qbo_to_ledger",
                        lambda *a, **k: qm.MigrationResult(True, 3, opening_entry_id="t:1"))
    monkeypatch.setattr(mirror_mod, "refresh_mirror", lambda *a, **k: MirrorResult(
        True, accounts_moved=3, numbered={"57": False, "4000": True, "12": False}))
    app = _connected_app(ledger=LedgerClient(RecordingLedgerTransport()), queries=[
        _qr({"CompanyInfo": [{"CompanyName": "Sandbox Co", "FiscalYearStartMonth": "July"}]}),
        # The whole chart, read too: an account at zero today isn't on the trial
        # balance, and it still needs its name shown rather than its id.
        _qr({"Account": [{"Id": "99", "Name": "Old Loan", "AccountType": "Long Term Liability"},
                         {"Id": "5", "AcctNum": "4000", "Name": "Sales", "AccountType": "Income"}]}),
    ])
    app.handle(Request("POST", "/t/acme/connect/qbo/migrate", _h("owner@acme.com")))

    state = app._store.load("acme")  # noqa: SLF001 - what both workers read
    assert state.qbo_id_codes == ["12", "57", "99"]
    assert state.fiscal_year_start_month == 7


def _refresh(monkeypatch, *, month: "str | None", mirror_ok: bool = True,
             ledger: bool = True, saved: "int | None" = None) -> "int | None":
    """One Refresh of a mirrored tenant whose QuickBooks company says `month`
    (None: CompanyInfo doesn't say). Returns the fiscal month stored after it."""
    import rgnr8_web.qbo_mirror as mirror_mod
    from rgnr8_web.qbo_mirror import MirrorResult
    from test_qbo_connect import _connected_app, _h, _qr

    monkeypatch.setattr(mirror_mod, "refresh_mirror", lambda *a, **k: MirrorResult(
        mirror_ok, error="" if mirror_ok else "could not read the QuickBooks trial balance"))
    info: dict[str, object] = {"CompanyName": "Sandbox Co"}
    if month is not None:
        info["FiscalYearStartMonth"] = month
    routes = {**DEFAULT_ROUTES, "GET /t/acme/settings": (200, {"settings": {
        "book_of_record": "quickbooks", "mirror_realm_id": "R42"}})}
    app = _connected_app(ledger=LedgerClient(FakeTransport(routes)) if ledger else None,
                         queries=[_qr({"CompanyInfo": [info]})])
    if saved is not None:
        app._store.save("acme", TenantState(fiscal_year_start_month=saved))  # noqa: SLF001
    app.handle(Request("POST", "/t/acme/connect/qbo/sync", _h("owner@acme.com")))
    return app._store.load("acme").fiscal_year_start_month  # noqa: SLF001


def test_a_refresh_of_the_mirror_records_the_fiscal_year_from_quickbooks(monkeypatch) -> None:
    assert _refresh(monkeypatch, month="April") == 4


def test_the_fiscal_year_is_left_alone_unless_the_mirror_was_refreshed(monkeypatch) -> None:
    # A refresh that failed, or one with no books to mirror, doesn't take it.
    assert _refresh(monkeypatch, month="April", mirror_ok=False, saved=10) == 10
    assert _refresh(monkeypatch, month="April", ledger=False, saved=10) == 10
    # QuickBooks not saying isn't QuickBooks saying January.
    assert _refresh(monkeypatch, month=None, saved=10) == 10


def test_a_refresh_updates_what_it_saw_and_keeps_what_it_did_not() -> None:
    """QuickBooks' trial balance only lists accounts with a balance, so an account
    that's at zero today keeps what was recorded for it at go-live."""
    app, _ = _app(state=TenantState(qbo_id_codes=["57", "12"]))
    t = app._tenants["acme"]  # noqa: SLF001
    app._record_quickbooks_facts(t, numbered={"57": True, "88": False})  # noqa: SLF001
    assert app._store.load("acme").qbo_id_codes == ["12", "88"]  # noqa: SLF001


def test_the_mirror_reports_which_codes_are_numbers() -> None:
    from test_qbo_mirror import FakeLedger, FakeQbo

    rows = [{"code": "57", "name": "Checking", "account_type": "Bank", "numbered": False,
             "debit_minor": 100, "credit_minor": 0},
            {"code": "3000", "name": "Equity", "account_type": "Equity", "numbered": True,
             "debit_minor": 0, "credit_minor": 100}]
    from rgnr8_web.qbo_mirror import refresh_mirror
    result = refresh_mirror(FakeQbo(rows), FakeLedger({"57": 100, "3000": -100}), "acme",
                            as_of="2026-10-29")
    assert result.ok and result.numbered == {"57": False, "3000": True}


def test_both_workers_read_the_same_record(tmp_path) -> None:
    path = tmp_path / "state.json"
    routes = {**DEFAULT_ROUTES, "GET /t/acme/accounts": (200, ID_ACCOUNTS),
              "GET /t/acme/trial-balance": (200, ID_TB)}
    worker_a, _ = _app(routes, store=JsonFileTenantStore(path))
    worker_b, _ = _app(routes, store=JsonFileTenantStore(path))
    assert "<th>No.</th>" in _main(_req(worker_b, "/t/acme/books").body)
    worker_a._record_quickbooks_facts(  # noqa: SLF001
        worker_a._tenants["acme"], numbered=dict.fromkeys(ID_CODES, False), fiscal_month=7)  # noqa: SLF001
    assert "<th>No.</th>" not in _main(_req(worker_b, "/t/acme/books").body)
    assert TenantState.from_dict(TenantState(qbo_id_codes=["57"], fiscal_year_start_month=7)
                                 .to_dict()).fiscal_year_start_month == 7


# --- Business health: revenue for the fiscal year to date ---------------------------

RATIOS = {"contract": "financial-ratios/1", "as_of": "2026-10-29", "currency": "USD",
          "liquidity": {}, "leverage": {}, "profitability": {},
          "inputs": {"revenue_minor": "9999900", "net_income_minor": "100"}}
FYTD = {"contract": "financial-statements/1", "income_statement": {
    "revenue": [], "expenses": [], "total_revenue": 1234500, "total_expenses": 34500,
    "net_income": 1200000}}


def _health(month: "int | None", as_of: str = "2026-10-29") -> "tuple[str, FakeTransport]":
    routes = {**DEFAULT_ROUTES, "GET /t/acme/ratios": (200, {**RATIOS, "as_of": as_of}),
              "GET /t/acme/statements": (200, FYTD)}
    app, transport = _app(routes, state=TenantState(fiscal_year_start_month=month))
    return _main(_req(app, "/t/acme/health").body), transport


def test_health_shows_revenue_for_the_fiscal_year_to_date() -> None:
    main, transport = _health(7)
    assert "Revenue, fiscal year to date (Jul 1 – Oct 29, 2026)" in main
    assert "Net income, fiscal year to date (Jul 1 – Oct 29, 2026)" in main
    assert "$12,345.00" in main and "$99,999.00" not in main
    assert "it starts in July" in main
    asked = [p for m, p, _b in transport.calls if p.startswith("/t/acme/statements")]
    assert asked == ["/t/acme/statements?" + urlencode({"from": "2026-07-01", "to": "2026-10-29"})]


def test_a_fiscal_year_that_started_last_calendar_year_says_so() -> None:
    main, _ = _health(7, as_of="2026-03-03")
    assert "Revenue, fiscal year to date (Jul 1, 2025 – Mar 3, 2026)" in main


def test_with_no_fiscal_year_recorded_the_year_starts_in_january() -> None:
    main, _ = _health(None)
    assert "Revenue, fiscal year to date (Jan 1 – Oct 29, 2026)" in main


def test_if_the_statements_cannot_be_read_health_says_what_it_shows_instead() -> None:
    routes = {**DEFAULT_ROUTES, "GET /t/acme/ratios": (200, RATIOS)}
    del routes["GET /t/acme/statements"]
    app, _ = _app(routes, state=TenantState(fiscal_year_start_month=7))
    main = _main(_req(app, "/t/acme/health").body)
    assert "Revenue, through 2026-10-29" in main and "fiscal year to date" not in main


def test_fiscal_year_dates() -> None:
    from datetime import date
    assert fiscal_year_start(date(2026, 10, 29), 7) == date(2026, 7, 1)
    assert fiscal_year_start(date(2026, 6, 30), 7) == date(2025, 7, 1)
    assert fiscal_year_start(date(2026, 1, 1), 1) == date(2026, 1, 1)
    assert period_words(date(2026, 7, 1), date(2026, 10, 29)) == "Jul 1 – Oct 29, 2026"


# --- Settings: see and change it ------------------------------------------------------

SETTINGS = {"tenant": "acme", "settings": {"inventory_costing_method": "FIFO"}}


def test_settings_shows_and_saves_the_fiscal_year_start() -> None:
    routes = {**DEFAULT_ROUTES, "GET /t/acme/settings": (200, SETTINGS),
              "POST /t/acme/settings": (200, SETTINGS)}
    app, _ = _app(routes, state=TenantState(fiscal_year_start_month=7))
    page = _req(app, "/t/acme/settings").body
    assert "Fiscal year starts in" in page
    assert '<option value="7" selected>July</option>' in page

    r = _req(app, "/t/acme/settings", method="POST", body="fiscal_year_start_month=10")
    assert r.status == 302 and "Settings+saved" in dict(r.extra_headers)["Location"]
    assert app._store.load("acme").fiscal_year_start_month == 10  # noqa: SLF001

    r = _req(app, "/t/acme/settings", method="POST", body="fiscal_year_start_month=13")
    assert "err=" in dict(r.extra_headers)["Location"]
    assert app._store.load("acme").fiscal_year_start_month == 10  # noqa: SLF001


# --- every save starts from what's stored, so two workers can't erase each other ---

def test_a_worker_saving_its_own_change_keeps_what_the_other_worker_recorded() -> None:
    """Each worker loads a tenant's state once, at startup. A Refresh on worker B
    records the account numbering and fiscal year; worker A then records a
    decision, changes an assumption and erases. Each used to save A's startup
    copy, wiping what B had recorded."""
    import json

    from factory import _inputs as factory_inputs

    store = InMemoryTenantStore()
    workers = []
    for _ in range(2):
        app = WebApp(store=store)
        app.add_tenant("bright", "Bright Agency", factory_inputs("80000.00"),
                       ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="tok-bright")
        workers.append(app)
    a, b = workers
    auth = {"authorization": "Bearer tok-bright"}

    b._record_quickbooks_facts(b._tenants["bright"],  # noqa: SLF001
                               numbered={"57": False}, fiscal_month=7)
    b.handle(Request("POST", "/api/bright/decisions", auth, json.dumps({"label": "Hire later"})))

    r = a.handle(Request("POST", "/api/bright/decisions", auth, json.dumps({"label": "Raise prices"})))
    assert json.loads(r.body)["id"] == 2                 # numbered after B's, not over it
    a.handle(Request("POST", "/api/bright/assumptions", auth,
                     json.dumps({"minimum_cash": "20000.00"})))
    state = store.load("bright")
    assert state.qbo_id_codes == ["57"] and state.fiscal_year_start_month == 7
    assert [d.get("label") for d in state.decisions][:2] == ["Hire later", "Raise prices"]
    assert state.min_cash_override_dto is not None

    a.handle(Request("POST", "/api/bright/erase", auth))
    state = store.load("bright")
    assert state.decisions == [] and state.min_cash_override_dto is None
    assert state.qbo_id_codes == ["57"] and state.fiscal_year_start_month == 7


# --- forms that took a typed account code now pick one -------------------------------

IDS = frozenset(ID_CODES)


def _select(html: str, name: str) -> str:
    m = re.search(rf'<select name="{name}">(.*?)</select>', html)
    assert m, f"no picker for {name}"
    return m.group(1)


def test_asset_forms_pick_the_account_instead_of_asking_for_a_code() -> None:
    from rgnr8_web.asset_screens import render_asset_detail, render_assets

    page = render_assets("acme", {}, can_write=True, accounts=ID_ACCOUNTS, id_codes=IDS)
    paid = _select(page, "paid_from_code")
    # Optional in the ledger: left blank, the purchase isn't posted.
    assert paid.startswith("<option value=\"\" selected>Don't post the purchase</option>")
    assert '<option value="57">Business Checking</option>' in paid and "57 — " not in paid
    assert 'placeholder="1000"' not in page

    detail = render_asset_detail("acme", {"asset": {"id": "TRUCK", "status": "ACTIVE"}},
                                 can_write=True, accounts=ID_ACCOUNTS, id_codes=IDS)
    # The ledger would fall back to 1000, which this chart doesn't have: the bank
    # account is chosen instead, so leaving it alone still means "the bank".
    assert '<option value="57" selected>Business Checking</option>' in _select(detail, "proceeds_to_code")


def test_debt_forms_pick_the_account() -> None:
    from rgnr8_web.debt_screens import render_debt

    page = render_debt("acme", {"loans": []}, can_write=True, accounts=ID_ACCOUNTS, id_codes=IDS)
    assert _select(page, "proceeds_to_code").startswith('<option value="" selected>')
    assert '<option value="57" selected>Business Checking</option>' in _select(page, "paid_from_code")
    assert 'placeholder="1000"' not in page
    # A numbered chart keeps the ledger's own default selected.
    page = render_debt("acme", {"loans": []}, can_write=True, accounts=ACCOUNTS)
    assert '<option value="1000" selected>1000 — Business Checking</option>' in _select(page, "paid_from_code")


def test_inventory_forms_pick_the_accounts() -> None:
    from rgnr8_web.inventory_screens import render_inventory

    accounts = {"accounts": [*ID_ACCOUNTS["accounts"],
                             {"code": "61", "name": "Stock on hand", "type": "ASSET"},
                             {"code": "62", "name": "Materials used", "type": "EXPENSE"}]}
    ids = IDS | {"61", "62"}
    page = render_inventory("acme", {"items": [{"sku": "PLY", "name": "Plywood"}]}, {}, {},
                            accounts, can_edit=True, id_codes=ids)
    stock, cost = _select(page, "inventory_account_code"), _select(page, "cost_account_code")
    assert "Stock on hand" in stock and "Rent" not in stock          # assets only
    assert "Materials used" in cost and "Business Checking" not in cost  # expenses only
    assert '<option value="57" selected>Business Checking</option>' in _select(page, "paid_from_code")
    assert not re.search(r"\b(57|61|62) — ", page)


def test_the_assets_page_reads_the_chart_for_its_form() -> None:
    routes = {**DEFAULT_ROUTES, "GET /t/acme/accounts": (200, ID_ACCOUNTS),
              "GET /t/acme/assets": (200, {"assets": [], "totals": {}})}
    app, _ = _app(routes, state=TenantState(qbo_id_codes=list(ID_CODES)))
    page = _req(app, "/t/acme/assets").body
    assert '<option value="57">Business Checking</option>' in _select(page, "paid_from_code")


def test_with_no_chart_to_offer_the_field_stays_a_text_box() -> None:
    from rgnr8_web.debt_screens import render_debt

    page = render_debt("acme", {"loans": []}, can_write=True)
    assert '<input name="paid_from_code" placeholder="1000">' in page


# --- the other pickers and titles ------------------------------------------------------

def test_revenue_pickers_and_the_reconcile_title_name_the_account() -> None:
    from rgnr8_web.arap_screens import _account_pick
    from rgnr8_web.estimate_screens import render_estimates
    from rgnr8_web.job_screens import render_jobs
    from rgnr8_web.reconcile_screens import render_reconcile

    est = render_estimates("acme", {}, {}, {}, ID_ACCOUNTS, can_edit=True, id_codes=IDS)
    jobs = render_jobs("acme", {}, {}, ID_ACCOUNTS, can_edit=True, id_codes=IDS)
    for page, field in ((est, "account1"), (jobs, "revenue_account_code")):
        picker = _select(page, field)
        assert picker == '<option value="1">Services</option>'   # income accounts, by name

    title = render_reconcile("acme", {"account_code": "57", "account_name": "R&D Checking",
                                      "statement_date": "2026-10-31"},
                             can_post=False, id_codes=IDS)
    assert "Reconcile R&amp;D Checking · as of 2026-10-31" in title
    assert "&amp;amp;" not in title and "57 — " not in title

    # Credits on a cutover document: the app passes each account's label.
    assert '<option value="12">Rent</option>' in _account_pick((("12", "Rent"),))


def test_an_account_with_no_name_still_has_a_link() -> None:
    from rgnr8_web.books_screens import render_chart_of_accounts, render_trial_balance

    chart = render_chart_of_accounts("acme", {"accounts": [{"code": "4000", "name": "",
                                                            "type": "REVENUE"}]})
    assert 'books/accounts/4000">4000</a>' in chart
    tb = render_trial_balance("acme", {"rows": [{"code": "57", "name": "", "type": "ASSET",
                                                 "debit_minor": "1", "credit_minor": "0"}]})
    assert 'books/accounts/57">57</a>' in tb


# --- Settings and the QuickBooks reads: the edges ------------------------------------------

def test_an_unchanged_month_is_not_written_or_audited() -> None:
    routes = {**DEFAULT_ROUTES, "GET /t/acme/settings": (200, SETTINGS),
              "POST /t/acme/settings": (200, SETTINGS)}
    app, transport = _app(routes)
    r = _req(app, "/t/acme/settings", method="POST", body="fiscal_year_start_month=1")
    assert "Nothing+to+change" in dict(r.extra_headers)["Location"]
    assert app._store.load("acme").fiscal_year_start_month is None  # noqa: SLF001
    assert not [c for c in transport.calls if c[0] == "POST"]


def test_a_store_failure_changes_nothing_and_says_so() -> None:
    class BrokenStore(InMemoryTenantStore):
        def save(self, tenant_id: str, state: TenantState) -> None:
            raise OSError("disk full")

    routes = {**DEFAULT_ROUTES, "GET /t/acme/settings": (200, SETTINGS),
              "POST /t/acme/settings": (200, SETTINGS)}
    app, transport = _app(routes, store=BrokenStore())
    r = _req(app, "/t/acme/settings", method="POST",
             body="fiscal_year_start_month=7&inventory_costing_method=FIFO")
    assert r.status == 302 and "err=" in dict(r.extra_headers)["Location"]
    assert not [c for c in transport.calls if c[0] == "POST"]   # the ledger wasn't half-saved


def test_numbering_reads_the_chart_only_after_a_trial_balance_was_read() -> None:
    class Client:
        reads = 0

        def chart_of_accounts(self, *, include_inactive: bool = False) -> list[dict[str, str]]:
            Client.reads += 1
            return [{"id": "57", "code": " 57 ", "number": ""},
                    {"id": "2", "code": "2300", "number": "2300"}]

    assert WebApp._numbering(Client(), {}) == {} and Client.reads == 0  # noqa: SLF001
    # Codes are stripped, as refresh_mirror keys them; the trial balance's word wins.
    assert WebApp._numbering(Client(), {"88": False}) == {  # noqa: SLF001
        "57": False, "2300": True, "88": False}
