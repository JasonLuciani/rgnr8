"""The web surface reads and re-verifies the financial package sealed by the TS
close stack. FIXTURE below is a real package emitted by `@rgnr8/close`
(`buildFinancialPackage`) — proving the Python fingerprint verifier reproduces
the TS SHA-256 byte-for-byte (a genuine cross-language integrity check)."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Invoice, Money
from rgnr8_web import (
    FinancialPackageReader,
    PackageIntegrityError,
    Request,
    WebApp,
    fingerprint_content,
    verify_package,
)
from rgnr8_web.financial_package import content_of

BRIGHT = {"authorization": "Bearer tok-bright"}

# A package sealed by the TypeScript `@rgnr8/close` buildFinancialPackage(...).
FIXTURE = json.loads(
    '{"version":"financial-package/1","periodKey":"2026-08","currency":"USD",'
    '"trialBalance":{"rows":[{"code":"1000","name":"Cash","debitMinor":"4600000","creditMinor":"0"},'
    '{"code":"1200","name":"Accounts Receivable","debitMinor":"1200000","creditMinor":"0"},'
    '{"code":"4000","name":"Sales","debitMinor":"0","creditMinor":"1200000"},'
    '{"code":"6000","name":"Rent","debitMinor":"400000","creditMinor":"0"},'
    '{"code":"3000","name":"Owner Equity","debitMinor":"0","creditMinor":"5000000"}],'
    '"totalDebitMinor":"6200000","totalCreditMinor":"6200000","inBalance":true},'
    '"incomeStatement":{"revenueMinor":"1200000","expensesMinor":"400000","netIncomeMinor":"800000"},'
    '"balanceSheet":{"totalAssetsMinor":"5800000","totalLiabilitiesAndEquityMinor":"5800000",'
    '"netIncomeMinor":"800000","balances":true},'
    '"qboReconciliation":{"inAgreement":true,"totalAbsDeltaMinor":"0","mismatchCount":0,'
    '"onlyInRgnr8Count":0,"onlyInQboCount":0},'
    '"closedBy":"controller","closedAt":"2026-09-01T17:00:00Z","packagedAt":"2026-09-01T17:00:05Z",'
    '"algorithm":"sha256","fingerprint":"0446e7fd06d12dee3ee88c13fed5adf6c768d0087e773477a73f4fa472122fc5"}'
)


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=Money.from_decimal("80000.00")),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), Money.from_decimal("15000.00")),),
    )


def _db_with_package(pkg: dict[str, object], tenant: str = "bright") -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE financial_package (tenant_id TEXT, period_key TEXT, fingerprint TEXT, package_json TEXT, "
        "PRIMARY KEY (tenant_id, period_key))"
    )
    conn.execute(
        "INSERT INTO financial_package VALUES (?, ?, ?, ?)",
        (tenant, str(pkg["periodKey"]), str(pkg["fingerprint"]), json.dumps(pkg)),
    )
    conn.commit()
    return conn


def _app_with_packages(conn: sqlite3.Connection) -> WebApp:
    app = WebApp(packages=FinancialPackageReader(conn))  # type: ignore[arg-type]
    app.add_tenant("bright", "Bright", _inputs(), ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="tok-bright")
    return app


# --- cross-language verification --------------------------------------------

def test_python_reproduces_the_ts_fingerprint() -> None:
    assert verify_package(FIXTURE) is True
    assert fingerprint_content(content_of(FIXTURE)) == FIXTURE["fingerprint"]


def test_tampering_breaks_verification() -> None:
    tampered = json.loads(json.dumps(FIXTURE))
    tampered["incomeStatement"]["netIncomeMinor"] = "999999"
    assert verify_package(tampered) is False


# --- reader ------------------------------------------------------------------

def test_reader_lists_and_gets_verified() -> None:
    reader = FinancialPackageReader(_db_with_package(FIXTURE))  # type: ignore[arg-type]
    assert reader.list_periods("bright") == ["2026-08"]
    pkg = reader.get("bright", "2026-08")
    assert pkg is not None and pkg["periodKey"] == "2026-08"
    assert reader.get("bright", "2026-07") is None


def test_reader_rejects_a_db_edited_row() -> None:
    conn = _db_with_package(FIXTURE)
    bad = json.loads(json.dumps(FIXTURE))
    bad["balanceSheet"]["totalAssetsMinor"] = "1"
    conn.execute("UPDATE financial_package SET package_json = ? WHERE tenant_id='bright'", (json.dumps(bad),))
    conn.commit()
    reader = FinancialPackageReader(conn)  # type: ignore[arg-type]
    try:
        reader.get("bright", "2026-08")
    except PackageIntegrityError:
        pass
    else:
        raise AssertionError("expected PackageIntegrityError")


# --- web routes --------------------------------------------------------------

def test_list_periods_route() -> None:
    app = _app_with_packages(_db_with_package(FIXTURE))
    r = app.handle(Request("GET", "/api/bright/packages", BRIGHT))
    assert r.status == 200
    assert json.loads(r.body)["periods"] == ["2026-08"]


def test_get_package_json_route() -> None:
    app = _app_with_packages(_db_with_package(FIXTURE))
    r = app.handle(Request("GET", "/api/bright/packages/2026-08", BRIGHT))
    assert r.status == 200
    body = json.loads(r.body)
    assert body["fingerprint"] == FIXTURE["fingerprint"]
    assert body["incomeStatement"]["netIncomeMinor"] == "800000"


def test_missing_period_is_404() -> None:
    app = _app_with_packages(_db_with_package(FIXTURE))
    assert app.handle(Request("GET", "/api/bright/packages/2026-06", BRIGHT)).status == 404


def test_tampered_package_route_is_409() -> None:
    conn = _db_with_package(FIXTURE)
    bad = json.loads(json.dumps(FIXTURE))
    bad["trialBalance"]["totalDebitMinor"] = "1"
    conn.execute("UPDATE financial_package SET package_json = ? WHERE tenant_id='bright'", (json.dumps(bad),))
    conn.commit()
    app = _app_with_packages(conn)
    assert app.handle(Request("GET", "/api/bright/packages/2026-08", BRIGHT)).status == 409


def test_package_html_route_renders_and_shows_verified() -> None:
    app = _app_with_packages(_db_with_package(FIXTURE))
    r = app.handle(Request("GET", "/t/bright/packages/2026-08", BRIGHT))
    assert r.status == 200
    assert "text/html" in r.content_type
    assert "verified" in r.body
    assert "46000.00" in r.body  # cash 4,600,000 minor rendered as decimal


def test_packages_are_tenant_isolated() -> None:
    app = _app_with_packages(_db_with_package(FIXTURE))
    app.add_tenant("acme", "Acme", _inputs(), ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="tok-acme")
    # bright's token cannot read acme's packages
    assert app.handle(Request("GET", "/api/acme/packages/2026-08", BRIGHT)).status == 403


def test_packages_unconfigured_is_404() -> None:
    app = WebApp()  # no packages reader
    app.add_tenant("bright", "Bright", _inputs(), ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="tok-bright")
    assert app.handle(Request("GET", "/api/bright/packages", BRIGHT)).status == 404
