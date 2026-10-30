"""The QBO recorder: pagination, the read-only guard, the manifest, and where it writes.
Everything runs against a fake HTTP seam. No network, no real credentials, and the
only database is a throwaway sqlite file in tmp_path."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlsplit

import pytest
from rgnr8_qbo import HttpResponse, QboApiClient, RetryPolicy
from rgnr8_qbo.connection import QboConnection, QboStatus, SqlConnectionStore
from rgnr8_qbo.oauth import TOKEN_ENDPOINT
from rgnr8_qbo.recorder import (
    DEFAULT_OUT,
    ENTITIES,
    OAuthRefreshHttp,
    ReadOnlyHttp,
    ReadOnlyViolation,
    RecorderError,
    _months,
    main,
    record,
)

API_BASE = "https://sandbox-quickbooks.api.intuit.com"
REALM = "9130000001"
TOKEN = "tok-SECRET-do-not-write-0123456789"
NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
ACCOUNT_URL = f"{API_BASE}/v3/company/{REALM}/query?query=x"


class FakeQbo:
    """Answers the Accounting API from in-memory rows and records every call."""

    def __init__(self, rows: Mapping[str, list[dict[str, Any]]] | None = None) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = dict(rows or {})
        self.calls: list[tuple[str, str]] = []

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.calls.append(("GET", url))
        parts = urlsplit(url)
        if parts.path.endswith("/query"):
            sql = parse_qs(parts.query)["query"][0]
            entity = re.search(r"from (\w+)", sql)
            assert entity is not None
            start_match = re.search(r"STARTPOSITION (\d+)", sql)
            size_match = re.search(r"MAXRESULTS (\d+)", sql)
            start = int(start_match.group(1)) if start_match else 1
            size = int(size_match.group(1)) if size_match else 1000
            page = self.rows.get(entity.group(1), [])[start - 1:start - 1 + size]
            body = {"QueryResponse": {entity.group(1): page, "startPosition": start}}
            return HttpResponse(200, json.dumps(body))
        if "/reports/" in parts.path:
            return HttpResponse(200, json.dumps({"Header": {}, "Rows": {"Row": [
                {"ColData": [{"value": "Cash"}, {"value": "1.00"}]}]}}))
        if "/companyinfo/" in parts.path:
            return HttpResponse(200, json.dumps({"CompanyInfo": {"CompanyName": "Fixture Co"}}))
        if parts.path.endswith("/preferences"):
            return HttpResponse(200, json.dumps({"Preferences": {}}))
        if parts.path.endswith("/cdc"):
            return HttpResponse(200, json.dumps({"CDCResponse": [{"QueryResponse": [{}]}]}))
        return HttpResponse(404, "{}")

    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
        self.calls.append(("POST", url))
        return HttpResponse(200, "{}")


def make_api(fake: FakeQbo) -> QboApiClient:
    api = QboApiClient(
        http=fake,
        api_base=API_BASE,
        realm_id=REALM,
        access_token=TOKEN,
        retry=RetryPolicy(sleep=lambda _s: None),
    )
    api.http = ReadOnlyHttp(fake, api_base=API_BASE, realm_id=REALM)
    return api


def accounts(n: int) -> list[dict[str, Any]]:
    return [{"Id": str(i + 1), "Name": f"Account {i + 1}", "AccountType": "Expense"} for i in range(n)]


def all_text(root: Path) -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in root.rglob("*") if p.is_file())


def test_paginates_2500_accounts_into_three_pages(tmp_path: Path) -> None:
    fake = FakeQbo({"Account": accounts(2500)})
    out = tmp_path / "out"

    manifest = record(make_api(fake), out, since=date(2026, 1, 1), now=NOW)

    pages = sorted(p.name for p in (out / "raw" / "Account").iterdir())
    assert pages == ["0001.json", "0002.json", "0003.json"]
    account_queries = [u for m, u in fake.calls if "from Account" in parse_qs(urlsplit(u).query).get("query", [""])[0]]
    assert len(account_queries) == 3
    record_for_accounts = next(r for r in manifest["records"] if r["name"] == "Account")
    assert record_for_accounts["count"] == 2500
    assert record_for_accounts["pages"] == 3


def test_every_entity_type_is_recorded(tmp_path: Path) -> None:
    out = tmp_path / "out"
    record(make_api(FakeQbo()), out, since=date(2026, 1, 1), now=NOW)

    for entity in ENTITIES:
        assert (out / "raw" / entity / "0001.json").is_file(), entity
    assert {"Account", "Customer", "Vendor", "Employee", "Item", "Class", "Department", "Term",
            "PaymentMethod", "TaxCode", "TaxRate", "Invoice", "Payment", "SalesReceipt", "CreditMemo",
            "RefundReceipt", "Deposit", "Transfer", "Bill", "BillPayment", "VendorCredit", "Purchase",
            "JournalEntry", "Estimate", "PurchaseOrder", "TimeActivity", "Attachable"} == set(ENTITIES)
    assert (out / "raw" / "CompanyInfo" / "0001.json").is_file()
    assert (out / "raw" / "Preferences" / "0001.json").is_file()
    assert (out / "raw" / "CDC" / "0001.json").is_file()


def test_cdc_asks_for_the_last_thirty_days(tmp_path: Path) -> None:
    fake = FakeQbo()
    manifest = record(make_api(fake), tmp_path / "out", since=date(2026, 1, 1), now=NOW)

    cdc = next(r for r in manifest["records"] if r["name"] == "CDC")
    assert cdc["changed_since"] == "2025-12-16T12:00:00+00:00"
    assert "changedSince=2025-12-16T12%3A00%3A00%2B00%3A00" in cdc["requests"][0]


def test_monthly_reports_cover_every_month_since_the_start(tmp_path: Path) -> None:
    fake = FakeQbo()
    manifest = record(make_api(fake), tmp_path / "out", since=date(2025, 11, 15), now=NOW)

    trial = [r["name"] for r in manifest["records"] if r["name"].startswith("TrialBalance")]
    assert trial == ["TrialBalance 2025-11", "TrialBalance 2025-12", "TrialBalance 2026-01"]
    assert (tmp_path / "out" / "raw" / "reports" / "ProfitAndLoss" / "2026-01.json").is_file()


def test_months_clip_the_first_and_last_month() -> None:
    assert _months(date(2025, 11, 15), date(2026, 1, 15)) == [
        (date(2025, 11, 15), date(2025, 11, 30)),
        (date(2025, 12, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 1, 15)),
    ]
    assert _months(date(2026, 3, 1), date(2026, 1, 15)) == []


def test_refuses_post() -> None:
    fake = FakeQbo()
    guarded = ReadOnlyHttp(fake, api_base=API_BASE, realm_id=REALM)

    with pytest.raises(ReadOnlyViolation, match="POST"):
        guarded.post(ACCOUNT_URL, "", {})
    assert fake.calls == []


@pytest.mark.parametrize("url", [
    f"{API_BASE}/v3/company/{REALM}/invoice",                       # entity list, not a query
    f"{API_BASE}/v3/company/{REALM}/batch",                         # batch writes
    f"{API_BASE}/v3/company/{REALM}/attachable/1/downloadable",     # attachment content
    f"{API_BASE}/v3/company/{REALM}/query/../batch",                # traversal
    f"{API_BASE}/v3/company/{REALM}/reports/",                      # no report name
    f"{API_BASE}/v3/company/OTHERREALM/query?query=x",              # another company
    f"{API_BASE}/oauth2/v1/tokens/revoke",                          # not the accounting API
    f"https://evil.example.com/v3/company/{REALM}/query?query=x",   # another host
    f"http://sandbox-quickbooks.api.intuit.com/v3/company/{REALM}/query?query=x",  # not https
    f"{API_BASE}/v3/company/{REALM}/Invoice/12/void",               # extra path segment
])
def test_refuses_paths_off_the_allowlist(url: str) -> None:
    fake = FakeQbo()
    guarded = ReadOnlyHttp(fake, api_base=API_BASE, realm_id=REALM)

    with pytest.raises(ReadOnlyViolation):
        guarded.get(url, {})
    assert fake.calls == []


@pytest.mark.parametrize("path", [
    "/query?query=select+*+from+Account",
    "/cdc?entities=Account",
    "/preferences",
    "/companyinfo/" + REALM,
    "/companyinfo",
    "/reports/TrialBalance?start_date=2026-01-01",
    "/Invoice/123",
])
def test_allows_the_read_paths(path: str) -> None:
    fake = FakeQbo()
    guarded = ReadOnlyHttp(fake, api_base=API_BASE, realm_id=REALM)

    guarded.get(f"{API_BASE}/v3/company/{REALM}{path}", {})
    assert len(fake.calls) == 1


def test_oauth_seam_posts_only_to_the_token_endpoint() -> None:
    fake = FakeQbo()
    seam = OAuthRefreshHttp(fake)

    seam.post(TOKEN_ENDPOINT, "grant_type=refresh_token", {})
    with pytest.raises(ReadOnlyViolation):
        seam.post(f"{API_BASE}/v3/company/{REALM}/batch", "", {})
    with pytest.raises(ReadOnlyViolation):
        seam.get(ACCOUNT_URL, {})
    assert fake.calls == [("POST", TOKEN_ENDPOINT)]


def test_record_refuses_a_client_without_the_guard(tmp_path: Path) -> None:
    fake = FakeQbo()
    api = QboApiClient(http=fake, api_base=API_BASE, realm_id=REALM, access_token=TOKEN)

    with pytest.raises(RecorderError, match="read-only guard"):
        record(api, tmp_path / "out", since=date(2026, 1, 1), now=NOW)
    assert fake.calls == []


def test_manifest_and_raw_files_hold_no_token_and_no_realm(tmp_path: Path) -> None:
    out = tmp_path / "out"
    record(make_api(FakeQbo({"Account": accounts(3)})), out, since=date(2026, 1, 1), now=NOW)

    text = all_text(out)
    assert TOKEN not in text
    assert "Bearer" not in text
    assert REALM not in (out / "manifest.json").read_text(encoding="utf-8")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["minorversion"] == "75"
    assert manifest["recorded_at"] == "2026-01-15T12:00:00+00:00"
    assert all("{realm}" in r for rec in manifest["records"] for r in rec["requests"])


def test_writes_nothing_outside_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    out = tmp_path / "out"
    monkeypatch.chdir(cwd)

    def outside() -> list[str]:
        return sorted(str(p) for p in tmp_path.rglob("*") if out not in [p, *p.parents])

    before = outside()
    record(make_api(FakeQbo({"Account": accounts(2)})), out, since=date(2026, 1, 1), now=NOW)
    assert outside() == before


def test_a_rerun_replaces_the_previous_raw_output(tmp_path: Path) -> None:
    out = tmp_path / "out"
    record(make_api(FakeQbo({"Account": accounts(2500)})), out, since=date(2026, 1, 1), now=NOW)
    record(make_api(FakeQbo({"Account": accounts(1)})), out, since=date(2026, 1, 1), now=NOW)

    assert sorted(p.name for p in (out / "raw" / "Account").iterdir()) == ["0001.json"]


def _seed_connection(db: Path) -> None:
    """A throwaway sqlite file standing in for the app's database, with one tenant
    whose access token has expired and whose refresh token is still good."""
    conn = sqlite3.connect(db)
    store = SqlConnectionStore(conn)
    store.create_schema()
    store.save(QboConnection(
        tenant_id="tenant-1",
        realm_id=REALM,
        access_token="old-access",
        refresh_token="old-refresh",
        access_expires_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        refresh_expires_at=datetime(2999, 1, 1, tzinfo=timezone.utc),
        status=QboStatus.CONNECTED,
    ))
    conn.close()


class RefreshingFake(FakeQbo):
    """Answers the token refresh, then serves data with the refreshed token."""

    def __init__(self, rows: Mapping[str, list[dict[str, Any]]]) -> None:
        super().__init__(rows)
        self.bearers: set[str] = set()

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.bearers.add(headers.get("Authorization", ""))
        return super().get(url, headers)

    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
        self.calls.append(("POST", url))
        if url == TOKEN_ENDPOINT:
            return HttpResponse(200, json.dumps({
                "access_token": "fresh-access", "refresh_token": "fresh-refresh",
                "expires_in": 3600, "x_refresh_token_expires_in": 8640000}))
        return HttpResponse(500, "{}")


def test_run_recording_builds_the_connection_like_the_app(tmp_path: Path) -> None:
    from rgnr8_qbo.recorder import run_recording

    db = tmp_path / "fake.db"
    _seed_connection(db)
    fake = RefreshingFake({"Account": accounts(2)})
    out = tmp_path / "out"
    env = {
        "RGNR8_DATABASE_URL": f"sqlite://{db}",
        "RGNR8_QBO_CLIENT_ID": "client-id-not-real",
        "RGNR8_QBO_CLIENT_SECRET": "client-secret-not-real",
        "RGNR8_QBO_ENVIRONMENT": "sandbox",
    }

    manifest = run_recording("tenant-1", out, since=date(2026, 1, 1), env=env, http=fake, now=NOW)

    assert manifest["records"]
    # the expired token was refreshed through the token endpoint, and only that POST happened
    posts = [u for m, u in fake.calls if m == "POST"]
    assert posts == [TOKEN_ENDPOINT]
    # every data GET used the refreshed bearer
    assert fake.bearers == {"Bearer fresh-access"}
    assert "fresh-refresh" not in all_text(out)


def test_run_recording_needs_its_environment(tmp_path: Path) -> None:
    from rgnr8_qbo.recorder import run_recording

    with pytest.raises(RecorderError, match="DATABASE_URL"):
        run_recording("t", tmp_path / "o", since=date(2026, 1, 1), env={})
    with pytest.raises(RecorderError, match="CLIENT_ID"):
        run_recording("t", tmp_path / "o", since=date(2026, 1, 1), env={"DATABASE_URL": "sqlite://"})


def test_cli_rejects_a_bad_since_and_a_missing_database(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--tenant", "t", "--out", str(tmp_path / "o"), "--since", "not-a-date"], env={}) == 2
    assert main(["--tenant", "t", "--out", str(tmp_path / "o")], env={}) == 1
    assert "DATABASE_URL" in capsys.readouterr().err


def test_default_out_is_qbo_recordings_at_the_repo_root() -> None:
    assert DEFAULT_OUT.name == "qbo-recordings"
    assert (DEFAULT_OUT.parent / "packages" / "qbo").is_dir()


def test_gitignore_covers_raw_recordings() -> None:
    root = Path(__file__).resolve().parents[3]
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        pytest.skip("not running inside the repository")
    lines = {line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines()}
    assert "qbo-recordings/" in lines
    assert "**/raw/" in lines
