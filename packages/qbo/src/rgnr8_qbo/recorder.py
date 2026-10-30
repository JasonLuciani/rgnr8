"""Record a connected QuickBooks company's data as raw JSON, read-only.

The recorder builds the same connection the app builds (the connection store
encrypted by ``RGNR8_SECRET_KEY``, the connect service, the API client) and then
issues only ``GET`` requests against the Accounting API. It writes every response
body exactly as Intuit sent it, one file per page, plus a manifest:

    <out>/raw/<Entity>/0001.json           one query page per file (STARTPOSITION/MAXRESULTS)
    <out>/raw/CompanyInfo/0001.json
    <out>/raw/Preferences/0001.json
    <out>/raw/CDC/0001.json                change data capture, last 30 days
    <out>/raw/reports/<Report>/<YYYY-MM>.json
    <out>/manifest.json                    counts, request URLs (realm redacted), timestamp

**The raw output contains a real company's customers, vendors and amounts. It must
never be committed.** ``qbo-recordings/`` and ``**/raw/`` are git-ignored; the
scrubber (:mod:`rgnr8_qbo.scrub`) turns it into fixtures that are safe to review and
commit.

The read-only guard
-------------------
Every Accounting API request goes through :class:`ReadOnlyHttp`, which refuses any
method other than GET and any path outside the allowlist below. The token refresh
(which the app also performs, and which is needed to keep the tokens alive) goes
through :class:`OAuthRefreshHttp`, which can POST to Intuit's token endpoint and
nothing else.

Allowed paths, relative to ``/v3/company/{realm}/``::

    query                 the query endpoint (``select * from <Entity> ...``)
    reports/<Name>        TrialBalance, ProfitAndLoss, ...
    cdc                   change data capture
    companyinfo[/<id>]    the company info record
    preferences           company preferences
    <Entity>/<id>         one entity by id (GET only)

The realm in the path must be the connected company's realm.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlencode, urlsplit

from .client import QboApiClient
from .connection import SqlConnectionStore
from .oauth import TOKEN_ENDPOINT, HttpClient, HttpResponse, QboEnvironment, QboOAuthConfig, UrllibHttpClient
from .secrets_cipher import cipher_from_env
from .service import QboConnectService

# Every entity the recorder reads, in the order it reads them. Department is the
# API entity behind QuickBooks' "Location" tracking.
ENTITIES: tuple[str, ...] = (
    "Account", "Customer", "Vendor", "Employee", "Item", "Class", "Department",
    "Term", "PaymentMethod", "TaxCode", "TaxRate",
    "Invoice", "Payment", "SalesReceipt", "CreditMemo", "RefundReceipt", "Deposit",
    "Transfer", "Bill", "BillPayment", "VendorCredit", "Purchase", "JournalEntry",
    "Estimate", "PurchaseOrder", "TimeActivity", "Attachable",
)
REPORTS: tuple[str, ...] = ("TrialBalance", "ProfitAndLoss")
PAGE_SIZE = 1000
MAX_PAGES = 200
CDC_DAYS = 30
DEFAULT_SINCE = date(2025, 1, 1)
# repo root / qbo-recordings, from packages/qbo/src/rgnr8_qbo/recorder.py
DEFAULT_OUT = Path(__file__).resolve().parents[4] / "qbo-recordings"

_ACCOUNTING_PATH = re.compile(
    r"/v3/company/([^/]+)/"
    r"(query|cdc|preferences|companyinfo(?:/[0-9]+)?|reports/[A-Za-z]+|[A-Za-z]+/[0-9]+)"
)


class RecorderError(Exception):
    """The recording could not be made. The message is safe to print."""


class ReadOnlyViolation(RecorderError):
    """A request the read-only guard refused: a non-GET method, or a path off the allowlist."""


def check_read_only(method: str, url: str, *, api_base: str, realm_id: str) -> None:
    """Raise :class:`ReadOnlyViolation` unless ``method url`` is a GET of an allowed
    Accounting API path for ``realm_id`` on ``api_base``'s host."""
    if method != "GET":
        raise ReadOnlyViolation(f"the recorder refuses {method} requests; only GET is allowed")
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.netloc != urlsplit(api_base).netloc:
        raise ReadOnlyViolation("the recorder refuses requests to any host but the Accounting API")
    match = _ACCOUNTING_PATH.fullmatch(parts.path)
    if match is None:
        raise ReadOnlyViolation(f"the recorder refuses the path {parts.path!r}: not on the read-only allowlist")
    if match.group(1) != realm_id:
        raise ReadOnlyViolation("the recorder refuses a request for a company other than the one it is recording")


class ReadOnlyHttp:
    """The Accounting API's HTTP seam with the read-only guard in front of it."""

    def __init__(self, inner: HttpClient, *, api_base: str, realm_id: str) -> None:
        self._inner = inner
        self._api_base = api_base
        self._realm_id = realm_id

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        check_read_only("GET", url, api_base=self._api_base, realm_id=self._realm_id)
        return self._inner.get(url, headers)

    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
        check_read_only("POST", url, api_base=self._api_base, realm_id=self._realm_id)
        raise AssertionError("unreachable: check_read_only refuses every POST")  # pragma: no cover


class OAuthRefreshHttp:
    """The OAuth seam the connect service uses. It may POST to Intuit's token
    endpoint (a refresh rotates the tokens, exactly as in the app) and make no
    other request."""

    def __init__(self, inner: HttpClient) -> None:
        self._inner = inner

    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
        if url != TOKEN_ENDPOINT:
            raise ReadOnlyViolation("the recorder's OAuth layer only posts to the token endpoint")
        return self._inner.post(url, body, headers)

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        raise ReadOnlyViolation("the recorder's OAuth layer makes no GET requests")


def _url(api: QboApiClient, path: str, params: Mapping[str, str]) -> str:
    query = dict(params)
    query["minorversion"] = api.minor_version
    return f"{api.api_base}/v3/company/{api.realm_id}{path}?{urlencode(query)}"


def _redact(api: QboApiClient, url: str) -> str:
    return url.replace(api.realm_id, "{realm}")


def _fetch(api: QboApiClient, url: str, label: str) -> str:
    """One guarded GET. Returns the body as received; raises on a non-2xx answer or
    a body that is not JSON. The error names the record, never the response body."""
    resp = api.get_url(url)
    if not 200 <= resp.status < 300:
        raise RecorderError(f"{label}: QuickBooks answered HTTP {resp.status}")
    try:
        json.loads(resp.body)
    except json.JSONDecodeError as exc:
        raise RecorderError(f"{label}: QuickBooks answered with something that is not JSON") from exc
    return resp.body


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _query_rows(body: str, entity: str) -> int:
    data = json.loads(body)
    qr = data.get("QueryResponse") if isinstance(data, dict) else None
    rows = qr.get(entity) if isinstance(qr, dict) else None
    return len(rows) if isinstance(rows, list) else 0


def _count_column_rows(node: object) -> int:
    if isinstance(node, dict):
        own = 1 if isinstance(node.get("ColData"), list) else 0
        return own + sum(_count_column_rows(v) for v in node.values())
    if isinstance(node, list):
        return sum(_count_column_rows(v) for v in node)
    return 0


def _cdc_rows(body: str) -> int:
    data = json.loads(body)
    total = 0
    responses = data.get("CDCResponse") if isinstance(data, dict) else None
    for response in responses if isinstance(responses, list) else []:
        query_responses = response.get("QueryResponse") if isinstance(response, dict) else None
        for qr in query_responses if isinstance(query_responses, list) else []:
            if isinstance(qr, dict):
                total += sum(len(v) for v in qr.values() if isinstance(v, list))
    return total


def _months(since: date, today: date) -> list[tuple[date, date]]:
    """(first, last) day of each month from ``since`` through ``today``, the last
    month ending today."""
    out: list[tuple[date, date]] = []
    year, month = since.year, since.month
    while (year, month) <= (today.year, today.month):
        first = max(date(year, month, 1), since)
        next_year = year + (1 if month == 12 else 0)
        next_month = 1 if month == 12 else month + 1
        last = date(next_year, next_month, 1) - timedelta(days=1)
        out.append((first, min(last, today)))
        year, month = next_year, next_month
    return out


def _record_entity(api: QboApiClient, raw: Path, entity: str) -> dict[str, Any]:
    count = 0
    requests: list[str] = []
    start = 1
    for page in range(1, MAX_PAGES + 1):
        sql = f"select * from {entity} STARTPOSITION {start} MAXRESULTS {PAGE_SIZE}"
        url = _url(api, "/query", {"query": sql})
        body = _fetch(api, url, entity)
        _write_text(raw / entity / f"{page:04d}.json", body)
        rows = _query_rows(body, entity)
        count += rows
        requests.append(_redact(api, url))
        if rows < PAGE_SIZE:
            return {"name": entity, "kind": "entity", "pages": page, "count": count, "requests": requests}
        start += PAGE_SIZE
    raise RecorderError(f"{entity}: more than {PAGE_SIZE * MAX_PAGES} rows; refusing to guess where the list ends")


def _record_single(api: QboApiClient, raw: Path, name: str, path: str) -> dict[str, Any]:
    url = _url(api, path, {})
    body = _fetch(api, url, name)
    _write_text(raw / name / "0001.json", body)
    return {"name": name, "kind": "single", "pages": 1, "count": 1, "requests": [_redact(api, url)]}


def _record_cdc(api: QboApiClient, raw: Path, now: datetime) -> dict[str, Any]:
    changed_since = (now - timedelta(days=CDC_DAYS)).replace(microsecond=0).isoformat()
    url = _url(api, "/cdc", {"entities": ",".join(ENTITIES), "changedSince": changed_since})
    body = _fetch(api, url, "CDC")
    _write_text(raw / "CDC" / "0001.json", body)
    return {"name": "CDC", "kind": "cdc", "pages": 1, "count": _cdc_rows(body),
            "changed_since": changed_since, "requests": [_redact(api, url)]}


def _record_report(api: QboApiClient, raw: Path, name: str, first: date, last: date) -> dict[str, Any]:
    params = {"start_date": first.isoformat(), "end_date": last.isoformat(), "accounting_method": "Accrual"}
    url = _url(api, f"/reports/{name}", params)
    label = f"{name} {first:%Y-%m}"
    body = _fetch(api, url, label)
    _write_text(raw / "reports" / name / f"{first:%Y-%m}.json", body)
    return {"name": label, "kind": "report", "pages": 1,
            "count": _count_column_rows(json.loads(body)), "requests": [_redact(api, url)]}


def record(api: QboApiClient, out: Path, *, since: date, now: datetime) -> dict[str, Any]:
    """Record one connected company into ``out`` and return the manifest written
    there. ``api`` must be behind :class:`ReadOnlyHttp`; anything else is refused.
    A previous ``out/raw`` is replaced, so a run never mixes two recordings."""
    if not isinstance(api.http, ReadOnlyHttp):
        raise RecorderError("refusing to record: the API client is not behind the read-only guard")
    if not api.realm_id:
        raise RecorderError("refusing to record: the connection has no company (realm) id")
    raw = out / "raw"
    if raw.exists():
        shutil.rmtree(raw)
    out.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = [_record_entity(api, raw, entity) for entity in ENTITIES]
    records.append(_record_single(api, raw, "CompanyInfo", f"/companyinfo/{api.realm_id}"))
    records.append(_record_single(api, raw, "Preferences", "/preferences"))
    records.append(_record_cdc(api, raw, now))
    for name in REPORTS:
        for first, last in _months(since, now.date()):
            records.append(_record_report(api, raw, name, first, last))

    manifest: dict[str, Any] = {
        "recorded_at": now.isoformat(timespec="seconds"),
        "minorversion": api.minor_version,
        "since": since.isoformat(),
        "api_host": urlsplit(api.api_base).netloc,
        "records": records,
    }
    _write_text(out / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return manifest


def _open_db(database_url: str) -> tuple[Any, str]:
    """A DB-API connection and its placeholder, for the URL schemes the app uses."""
    low = database_url.lower()
    if low.startswith(("postgres://", "postgresql://")):
        import psycopg  # lazily: sqlite-only setups do not need the driver

        return psycopg.connect(database_url), "%s"
    if low.startswith("sqlite://"):
        return sqlite3.connect(database_url[len("sqlite://"):] or ":memory:"), "?"
    raise RecorderError("DATABASE_URL must be a postgres:// or sqlite:// URL")


def run_recording(
    tenant_id: str,
    out: Path,
    *,
    since: date,
    env: Mapping[str, str],
    http: HttpClient | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the tenant's connection from ``env`` the way the app does, then record.

    Reads ``RGNR8_DATABASE_URL`` (where the connection rows live, as the web
    service names it; ``DATABASE_URL`` is the fallback), ``RGNR8_SECRET_KEY``
    (decrypts them), and ``RGNR8_QBO_CLIENT_ID`` / ``RGNR8_QBO_CLIENT_SECRET`` /
    ``RGNR8_QBO_ENVIRONMENT`` (needed to refresh an expired access token). ``http``
    is the network seam; the default is the real one, and tests pass a fake."""
    database_url = (env.get("RGNR8_DATABASE_URL", "").strip()
                    or env.get("DATABASE_URL", "").strip())
    if not database_url:
        raise RecorderError("RGNR8_DATABASE_URL (or DATABASE_URL) is not set")
    client_id = env.get("RGNR8_QBO_CLIENT_ID", "").strip()
    client_secret = env.get("RGNR8_QBO_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise RecorderError("RGNR8_QBO_CLIENT_ID and RGNR8_QBO_CLIENT_SECRET must be set")
    try:
        environment = QboEnvironment((env.get("RGNR8_QBO_ENVIRONMENT") or "sandbox").strip().lower())
    except ValueError as exc:
        raise RecorderError("RGNR8_QBO_ENVIRONMENT must be 'sandbox' or 'production'") from exc

    real = http if http is not None else UrllibHttpClient()
    conn, placeholder = _open_db(database_url)
    try:
        store = SqlConnectionStore(conn, placeholder=placeholder, cipher=cipher_from_env(env))
        config = QboOAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=env.get("RGNR8_QBO_REDIRECT_URI", ""),
            environment=environment,
        )
        # The state secret only signs OAuth callbacks, which the recorder never handles.
        service = QboConnectService(config, OAuthRefreshHttp(real), store,
                                    state_secret=secrets.token_hex(16))
        api = service.api_client(tenant_id)
        if api is None:
            raise RecorderError(f"tenant {tenant_id} has no usable QuickBooks connection "
                                "(not connected, or it needs a reconnect)")
        api.http = ReadOnlyHttp(real, api_base=api.api_base, realm_id=api.realm_id)
        return record(api, out, since=since, now=now or datetime.now(timezone.utc))
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m rgnr8_qbo.recorder",
        description="Record a connected QuickBooks company as raw, read-only JSON. "
                    "The output holds real company data: never commit it.")
    parser.add_argument("--tenant", required=True, help="the tenant id whose QuickBooks connection to read")
    parser.add_argument("--out", default=str(DEFAULT_OUT),
                        help="output directory (default: qbo-recordings/ at the repo root)")
    parser.add_argument("--since", default=DEFAULT_SINCE.isoformat(),
                        help="first month (YYYY-MM-DD) of the monthly reports (default: 2025-01-01)")
    return parser


def main(argv: Sequence[str] | None = None, *, env: Mapping[str, str] | None = None,
         http: HttpClient | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        since = date.fromisoformat(args.since)
    except ValueError:
        print("recorder: --since must be a date, YYYY-MM-DD", file=sys.stderr)
        return 2
    out = Path(args.out)
    try:
        manifest = run_recording(args.tenant, out, since=since,
                                 env=os.environ if env is None else env, http=http)
    except RecorderError as exc:
        print(f"recorder: {exc}", file=sys.stderr)
        return 1
    entities = [r for r in manifest["records"] if r["kind"] == "entity"]
    print(f"recorded {len(entities)} entity types into {out} "
          f"({sum(r['count'] for r in entities)} rows). Raw output is not for committing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
