"""Read and verify the published financial package on the Python side.

The financial package is sealed and persisted by the TypeScript accounting stack
(`@rgnr8/close`): a closed period's statements + trial balance + QBO
reconciliation, fingerprinted with SHA-256 over a canonical serialization of the
*financial content only*. This module lets the Python web surface read those
rows straight from the same `financial_package` SQL table and **independently
re-verify** the fingerprint — a genuine cross-language integrity check, the same
way `forecast-inputs/1` crosses the TS↔Python boundary.

The canonicalization matches the TS `canonicalize` exactly (recursively
key-sorted, compact JSON, non-ASCII left raw), so an identical fingerprint is
reproduced from the stored bytes. If they ever diverge, the package is reported
as failing verification rather than served as trustworthy.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from rgnr8_forecast.brand import RG_BASE_CSS, RG_TOKENS_CSS, brand_bar

FINANCIAL_PACKAGE_VERSION = "financial-package/1"

# The fields the fingerprint covers — must match the TS content subset exactly.
_CONTENT_KEYS = (
    "version",
    "periodKey",
    "currency",
    "trialBalance",
    "incomeStatement",
    "balanceSheet",
    "qboReconciliation",
)


def canonicalize(content: dict[str, object]) -> str:
    """Deterministic JSON with recursively-sorted keys and compact separators —
    byte-identical to the TS `canonicalize`."""
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint_content(content: dict[str, object]) -> str:
    return hashlib.sha256(canonicalize(content).encode("utf-8")).hexdigest()


def content_of(pkg: dict[str, object]) -> dict[str, object]:
    """The fingerprinted subset of a full package (drops the envelope metadata)."""
    return {k: pkg[k] for k in _CONTENT_KEYS if k in pkg}


def verify_package(pkg: dict[str, object]) -> bool:
    """Recompute the fingerprint from the package's content and compare it to the
    stored one — False if any sealed number was altered."""
    stored = pkg.get("fingerprint")
    if not isinstance(stored, str):
        return False
    return fingerprint_content(content_of(pkg)) == stored


class PackageIntegrityError(Exception):
    """Raised when a stored package fails fingerprint verification on read."""


class DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchone(self) -> tuple[object, ...] | None: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class DbApiConnection(Protocol):
    def cursor(self) -> DbApiCursor: ...


class FinancialPackageReader:
    """Read published packages from the `financial_package` table the TS
    `SqlFinancialPackageStore` writes. Verifies the fingerprint on every read."""

    def __init__(
        self,
        connection: DbApiConnection,
        *,
        table: str = "financial_package",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._table = table
        self._ph = placeholder

    def list_periods(self, tenant_id: str) -> list[str]:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT period_key FROM {self._table} WHERE tenant_id = {self._ph}",
                (tenant_id,),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        return sorted(str(r[0]) for r in rows)

    def get(self, tenant_id: str, period_key: str) -> dict[str, object] | None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT package_json FROM {self._table} "
                f"WHERE tenant_id = {self._ph} AND period_key = {self._ph}",
                (tenant_id, period_key),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        if row is None:
            return None
        pkg: dict[str, object] = json.loads(str(row[0]))
        if not verify_package(pkg):
            raise PackageIntegrityError(f"{tenant_id} {period_key} failed fingerprint verification")
        return pkg


# --- HTML render -------------------------------------------------------------


def _minor_to_decimal(minor: str, scale: int = 2) -> str:
    neg = minor.startswith("-")
    digits = minor[1:] if neg else minor
    digits = digits.rjust(scale + 1, "0")
    whole, frac = digits[:-scale], digits[-scale:]
    return f"{'-' if neg else ''}{whole}.{frac}"


def render_package_html(pkg: dict[str, object]) -> str:
    tb = pkg.get("trialBalance", {})
    inc = pkg.get("incomeStatement", {})
    bs = pkg.get("balanceSheet", {})
    rows = tb.get("rows", []) if isinstance(tb, dict) else []
    verified = verify_package(pkg)

    def esc(s: object) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def dec(d: object) -> str:
        return _minor_to_decimal(str(d)) if d is not None else ""

    tb_rows = "\n".join(
        f"<tr><td>{esc(r.get('code'))}</td><td>{esc(r.get('name'))}</td>"
        f"<td class='num'>{dec(r.get('debitMinor'))}</td>"
        f"<td class='num'>{dec(r.get('creditMinor'))}</td></tr>"
        for r in rows
        if isinstance(r, dict)
    )
    inc_d = inc if isinstance(inc, dict) else {}
    bs_d = bs if isinstance(bs, dict) else {}
    badge = (
        "<span class='badge good'>✓ verified</span>"
        if verified
        else "<span class='badge bad'>✗ integrity check failed</span>"
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Financial package — {esc(pkg.get('periodKey'))}</title>
<style>
{RG_TOKENS_CSS}
{RG_BASE_CSS}
  .wrap{{max-width:840px;margin:0 auto;padding:24px 20px 56px}}
  h1{{font-size:22px;margin:0 0 2px;font-weight:800;letter-spacing:-.01em;display:flex;align-items:center;gap:10px;flex-wrap:wrap}}
  h2{{font-size:11px;margin:24px 0 8px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--rg-muted)}}
  .sub{{color:var(--rg-muted);margin:0 0 12px;font-size:13px}}
  .badge{{display:inline-flex;align-items:center;border-radius:var(--rg-pill);padding:4px 12px;font-weight:700;font-size:12px;letter-spacing:.03em}}
  .good{{background:color-mix(in srgb, var(--rg-pos) 14%, var(--rg-paper));color:color-mix(in srgb, var(--rg-pos) 78%, black);border:1px solid color-mix(in srgb, var(--rg-pos) 32%, transparent)}}
  .bad{{background:color-mix(in srgb, var(--rg-risk) 14%, var(--rg-paper));color:color-mix(in srgb, var(--rg-risk) 78%, black);border:1px solid color-mix(in srgb, var(--rg-risk) 32%, transparent)}}
  table{{border-collapse:separate;border-spacing:0;width:100%;background:var(--rg-paper);
    border:1px solid var(--rg-line);border-radius:var(--rg-radius);overflow:hidden;box-shadow:var(--rg-shadow)}}
  th,td{{text-align:left;padding:9px 13px;border-bottom:1px solid var(--rg-line);font-size:13.5px}}
  thead th{{background:var(--rg-surface);font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-muted)}}
  tbody tr:last-child td{{border-bottom:none}}
  .num{{text-align:right;font-variant-numeric:tabular-nums;font-weight:700}}
  .fp{{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px;color:var(--rg-muted);word-break:break-all;margin-top:14px}}
</style></head>
<body>
{brand_bar("Financial package")}
<div class="wrap">
  <h1>{esc(pkg.get('periodKey'))} {badge}</h1>
  <p class="sub">Published record · sealed {esc(pkg.get('closedAt'))} by {esc(pkg.get('closedBy'))} · {esc(pkg.get('currency'))}</p>
  <h2>Income statement</h2>
  <table><tbody>
    <tr><td>Revenue</td><td class="num">{dec(inc_d.get('revenueMinor'))}</td></tr>
    <tr><td>Expenses</td><td class="num">{dec(inc_d.get('expensesMinor'))}</td></tr>
    <tr><td><strong>Net income</strong></td><td class="num"><strong>{dec(inc_d.get('netIncomeMinor'))}</strong></td></tr>
  </tbody></table>
  <h2>Balance sheet</h2>
  <table><tbody>
    <tr><td>Total assets</td><td class="num">{dec(bs_d.get('totalAssetsMinor'))}</td></tr>
    <tr><td>Total liabilities + equity</td><td class="num">{dec(bs_d.get('totalLiabilitiesAndEquityMinor'))}</td></tr>
  </tbody></table>
  <h2>Trial balance</h2>
  <table><thead><tr><th>Code</th><th>Account</th><th class="num">Debit</th><th class="num">Credit</th></tr></thead>
  <tbody>
{tb_rows}
  </tbody></table>
  <p class="fp">fingerprint (sha256): {esc(pkg.get('fingerprint'))}</p>
</div>
</body></html>"""
