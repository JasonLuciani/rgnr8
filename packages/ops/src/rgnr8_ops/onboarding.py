"""Onboarding metadata the operator captures per client: which chart-of-accounts
template to seed, and the cutover to RGNR8 as system of record.

The accounting engine that actually *builds* the chart and posts the opening
balances lives in the TypeScript core (`@rgnr8/ledger-kernel`
`buildChartForCategory` / `executeCutover`). This module is the Python
control-plane counterpart: it records the operator's choices so the TS core can
act on them, and tracks go-live status for the console. The category slugs here
are the cross-language contract — they match `ledger-kernel`'s `BusinessCategory`
enum values exactly, so a slug recorded here maps to a TS template one-to-one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from rgnr8_runtime.subscriptions import DbApiConnection

# Slugs + human labels — MUST match ledger-kernel's BusinessCategory enum values.
BUSINESS_CATEGORIES: tuple[tuple[str, str], ...] = (
    ("SERVICE_GENERAL", "General Service Business"),
    ("PROFESSIONAL_SERVICES", "Professional Services"),
    ("CONTRACTOR_TRADES", "Contractor & Trades"),
    ("RETAIL", "Retail"),
    ("ECOMMERCE", "E-commerce"),
    ("RESTAURANT", "Restaurant & Food Service"),
    ("REAL_ESTATE", "Real Estate"),
    ("HEALTHCARE_PRACTICE", "Healthcare Practice"),
    ("NONPROFIT", "Nonprofit"),
)

_CATEGORY_SLUGS = frozenset(slug for slug, _ in BUSINESS_CATEGORIES)

# Source systems we cut over from.
SOURCE_SYSTEMS = frozenset({"quickbooks", "xero", "other"})

# The cross-language go-live contract consumed by ledger-kernel `goLiveFromDto`.
GO_LIVE_CONTRACT = "go-live/1"

# QBO/Xero account classifications → our AccountSubtype slugs (which match the TS
# `AccountSubtype` enum values). Keys are lower-cased so provider casing doesn't
# matter. This is how a source trial balance becomes placeable source accounts.
_ACCOUNT_TYPE_TO_SUBTYPE: dict[str, str] = {
    # assets
    "bank": "BANK",
    "accounts receivable": "ACCOUNTS_RECEIVABLE",
    "accounts_receivable": "ACCOUNTS_RECEIVABLE",
    "undeposited funds": "UNDEPOSITED_FUNDS",
    "other current asset": "OTHER_CURRENT_ASSET",
    "other current assets": "OTHER_CURRENT_ASSET",
    "inventory": "INVENTORY",
    "fixed asset": "FIXED_ASSET",
    "fixed assets": "FIXED_ASSET",
    "other asset": "OTHER_ASSET",
    "other assets": "OTHER_ASSET",
    # liabilities
    "accounts payable": "ACCOUNTS_PAYABLE",
    "accounts_payable": "ACCOUNTS_PAYABLE",
    "credit card": "CREDIT_CARD",
    "other current liability": "OTHER_CURRENT_LIABILITY",
    "other current liabilities": "OTHER_CURRENT_LIABILITY",
    "long term liability": "LONG_TERM_LIABILITY",
    "long-term liability": "LONG_TERM_LIABILITY",
    # equity
    "equity": "EQUITY",
    # income / expense
    "income": "INCOME",
    "revenue": "INCOME",
    "other income": "OTHER_INCOME",
    "cost of goods sold": "COST_OF_GOODS_SOLD",
    "expense": "EXPENSE",
    "expenses": "EXPENSE",
    "other expense": "OTHER_EXPENSE",
    "other expenses": "OTHER_EXPENSE",
}


def subtype_for_account_type(account_type: str) -> str | None:
    """Map a QBO/Xero account classification to our AccountSubtype slug, or None."""
    return _ACCOUNT_TYPE_TO_SUBTYPE.get(account_type.strip().lower())


def qbo_trial_balance_to_source_accounts(
    rows: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Normalize a QBO/Xero trial balance into go-live ``source_accounts``. Each
    row needs a ``code`` (account number), ``name``, an ``account_type`` (QBO
    classification), and ``debit_minor``/``credit_minor`` integer columns. The
    signed, debit-positive balance is ``debit − credit``; the account type maps
    to a subtype so the account can be placed. Rows whose type can't be mapped
    raise ``OnboardingError`` so nothing silently lands unclassified."""
    out: list[dict[str, object]] = []
    for r in rows:
        code = str(r.get("code", "")).strip()
        name = str(r.get("name", "")).strip()
        account_type = str(r.get("account_type", "")).strip()
        if not code or not name:
            raise OnboardingError("each trial-balance row needs a code and a name")
        subtype = subtype_for_account_type(account_type)
        if subtype is None:
            raise OnboardingError(f"account {code}: unmappable account type {account_type!r}")
        # Bind the loop's `code` as a default so the closure captures this row's
        # value, not whatever `code` is at call time (it is called in-iteration,
        # so this is a correctness clarification rather than a live bug).
        def _as_int(v: object, field: str, code: str = code) -> int:
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise OnboardingError(f"account {code} has non-integer {field}")
            try:
                return int(v)
            except ValueError:
                raise OnboardingError(f"account {code} has non-integer {field}") from None

        debit = _as_int(r.get("debit_minor", 0), "debit_minor")
        credit = _as_int(r.get("credit_minor", 0), "credit_minor")
        out.append({
            "code": code,
            "name": name,
            "balance_minor": debit - credit,
            "subtype": subtype,
        })
    return out


def build_go_live_request(
    tenant_id: str,
    source_system: str,
    cutover_date: str,
    source_accounts: list[dict[str, object]],
    *,
    opening_balance_equity_code: str = "3010",
    currency: str = "USD",
    coa_category: str | None = None,
) -> dict[str, object]:
    """Assemble a ``go-live/1`` request the TS accounting core executes end to end
    (seed the COA template, bring over the source's chart + as-of trial balance,
    post the opening-balance journal, lock the period). ``source_accounts`` is the
    source system's trial balance: each item needs ``code``, ``name``, and a
    signed debit-positive ``balance_minor`` (integer minor units), plus a
    ``subtype`` or ``type`` so a brought-over account can be placed. Validated so a
    malformed request never reaches the ledger."""
    if source_system not in SOURCE_SYSTEMS:
        raise OnboardingError(f"unknown source system {source_system!r}")
    if coa_category is not None and not is_valid_category(coa_category):
        raise OnboardingError(f"unknown COA category {coa_category!r}")
    if not source_accounts:
        raise OnboardingError("go-live needs at least one source account (the trial balance)")

    accounts: list[dict[str, object]] = []
    for a in source_accounts:
        code = str(a.get("code", "")).strip()
        name = str(a.get("name", "")).strip()
        if not code or not name:
            raise OnboardingError("each source account needs a code and a name")
        raw_balance = a.get("balance_minor")
        if not isinstance(raw_balance, (int, str)) or isinstance(raw_balance, bool):
            raise OnboardingError(f"account {code} has a non-integer balance_minor")
        try:
            balance_minor = int(raw_balance)
        except ValueError:
            raise OnboardingError(f"account {code} has a non-integer balance_minor")
        if not a.get("subtype") and not a.get("type"):
            raise OnboardingError(f"account {code} needs a subtype or type to be placed")
        entry: dict[str, object] = {"code": code, "name": name, "balance_minor": str(balance_minor)}
        if a.get("subtype"):
            entry["subtype"] = str(a["subtype"])
        if a.get("type"):
            entry["type"] = str(a["type"])
        accounts.append(entry)

    request: dict[str, object] = {
        "contract": GO_LIVE_CONTRACT,
        "tenant_id": tenant_id,
        "source_system": source_system,
        "cutover_date": cutover_date,
        "currency": currency,
        "opening_balance_equity_code": opening_balance_equity_code,
        "source_accounts": accounts,
    }
    if coa_category is not None:
        request["coa_category"] = coa_category
    return request


def is_valid_category(slug: str) -> bool:
    return slug in _CATEGORY_SLUGS


def category_catalog() -> list[dict[str, str]]:
    return [{"slug": slug, "label": label} for slug, label in BUSINESS_CATEGORIES]


class OnboardingError(Exception):
    """An invalid onboarding choice (unknown category / source system)."""


@dataclass(frozen=True, slots=True)
class CutoverRecord:
    """A client's go-live: the moment RGNR8 became its system of record."""

    tenant_id: str
    source_system: str
    cutover_date: str  # ISO date
    marked_by: str
    marked_at: int  # epoch seconds (operator clock)
    opening_entry_id: str = ""  # set once the TS core posts the opening balances


class OnboardingRegistry:
    """Per-tenant onboarding state: chosen COA template + cutover status. A tiny
    in-memory store the operator surface reads/writes; production persists it."""

    def __init__(self) -> None:
        self._coa_category: dict[str, str] = {}
        self._cutover: dict[str, CutoverRecord] = {}
        self._go_live_request: dict[str, dict[str, object]] = {}

    # --- COA template choice --------------------------------------------------
    def set_coa_category(self, tenant_id: str, category: str) -> None:
        if not is_valid_category(category):
            raise OnboardingError(f"unknown COA category {category!r}")
        self._coa_category[tenant_id] = category

    def coa_category(self, tenant_id: str) -> str | None:
        return self._coa_category.get(tenant_id)

    # --- cutover / go-live ----------------------------------------------------
    def mark_cutover(
        self, tenant_id: str, source_system: str, cutover_date: str,
        *, marked_by: str, marked_at: int, opening_entry_id: str = "",
    ) -> CutoverRecord:
        if source_system not in SOURCE_SYSTEMS:
            raise OnboardingError(f"unknown source system {source_system!r}")
        rec = CutoverRecord(tenant_id, source_system, cutover_date, marked_by,
                            marked_at, opening_entry_id)
        self._cutover[tenant_id] = rec
        return rec

    def cutover(self, tenant_id: str) -> CutoverRecord | None:
        return self._cutover.get(tenant_id)

    def is_live(self, tenant_id: str) -> bool:
        """True once RGNR8 is the system of record for this tenant."""
        return tenant_id in self._cutover

    # --- the go-live/1 handoff to the TS core ---------------------------------
    def set_go_live_request(self, tenant_id: str, request: dict[str, object]) -> None:
        self._go_live_request[tenant_id] = request

    def go_live_request(self, tenant_id: str) -> dict[str, object] | None:
        """The go-live/1 request queued for the TS accounting core, if any."""
        return self._go_live_request.get(tenant_id)


class SqlOnboardingRegistry(OnboardingRegistry):
    """Durable onboarding state over any DB-API 2.0 connection (sqlite in tests,
    psycopg/Postgres in production). Same surface as `OnboardingRegistry`, but the
    COA choice, cutover record, and queued go-live request survive a process
    restart — go-live tracking must not vanish when the operator pod bounces."""

    def __init__(self, connection: DbApiConnection, *, placeholder: str = "?") -> None:
        # Deliberately do NOT call super().__init__(): all state lives in the DB,
        # and every public method is overridden below.
        self._conn = connection
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS rgnr8_onboarding ("
                "tenant_id TEXT PRIMARY KEY, coa_category TEXT, go_live_json TEXT)"
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS rgnr8_cutover ("
                "tenant_id TEXT PRIMARY KEY, source_system TEXT NOT NULL, "
                "cutover_date TEXT NOT NULL, marked_by TEXT NOT NULL, "
                "marked_at INTEGER NOT NULL, opening_entry_id TEXT NOT NULL DEFAULT '')"
            )
        finally:
            cur.close()
        self._conn.commit()

    def _upsert_onboarding(self, tenant_id: str, *, coa: str | None, go_live: str | None) -> None:
        # Merge onto the existing row so setting one column never clears the other.
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT coa_category, go_live_json FROM rgnr8_onboarding "
                        f"WHERE tenant_id={p}", (tenant_id,))
            _rows = cur.fetchall()
            row = _rows[0] if _rows else None
            cur_coa = row[0] if row else None
            cur_gl = row[1] if row else None
            new_coa = coa if coa is not None else cur_coa
            new_gl = go_live if go_live is not None else cur_gl
            cur.execute(
                f"INSERT INTO rgnr8_onboarding (tenant_id, coa_category, go_live_json) "
                f"VALUES ({p}, {p}, {p}) ON CONFLICT (tenant_id) DO UPDATE SET "
                "coa_category=excluded.coa_category, go_live_json=excluded.go_live_json",
                (tenant_id, new_coa, new_gl),
            )
        finally:
            cur.close()
        self._conn.commit()

    def set_coa_category(self, tenant_id: str, category: str) -> None:
        if not is_valid_category(category):
            raise OnboardingError(f"unknown COA category {category!r}")
        self._upsert_onboarding(tenant_id, coa=category, go_live=None)

    def coa_category(self, tenant_id: str) -> str | None:
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT coa_category FROM rgnr8_onboarding WHERE tenant_id={self._ph}",
                        (tenant_id,))
            _rows = cur.fetchall()
            row = _rows[0] if _rows else None
        finally:
            cur.close()
        return str(row[0]) if row and row[0] is not None else None

    def mark_cutover(
        self, tenant_id: str, source_system: str, cutover_date: str,
        *, marked_by: str, marked_at: int, opening_entry_id: str = "",
    ) -> CutoverRecord:
        if source_system not in SOURCE_SYSTEMS:
            raise OnboardingError(f"unknown source system {source_system!r}")
        rec = CutoverRecord(tenant_id, source_system, cutover_date, marked_by,
                            marked_at, opening_entry_id)
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO rgnr8_cutover (tenant_id, source_system, cutover_date, "
                f"marked_by, marked_at, opening_entry_id) VALUES ({p}, {p}, {p}, {p}, {p}, {p}) "
                "ON CONFLICT (tenant_id) DO UPDATE SET source_system=excluded.source_system, "
                "cutover_date=excluded.cutover_date, marked_by=excluded.marked_by, "
                "marked_at=excluded.marked_at, opening_entry_id=excluded.opening_entry_id",
                (tenant_id, source_system, cutover_date, marked_by, marked_at, opening_entry_id),
            )
        finally:
            cur.close()
        self._conn.commit()
        return rec

    def cutover(self, tenant_id: str) -> CutoverRecord | None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT tenant_id, source_system, cutover_date, marked_by, marked_at, "
                f"opening_entry_id FROM rgnr8_cutover WHERE tenant_id={self._ph}", (tenant_id,))
            _rows = cur.fetchall()
            row = _rows[0] if _rows else None
        finally:
            cur.close()
        if row is None:
            return None
        return CutoverRecord(str(row[0]), str(row[1]), str(row[2]), str(row[3]),
                             int(str(row[4])), str(row[5]))

    def is_live(self, tenant_id: str) -> bool:
        return self.cutover(tenant_id) is not None

    def set_go_live_request(self, tenant_id: str, request: dict[str, object]) -> None:
        self._upsert_onboarding(tenant_id, coa=None, go_live=json.dumps(request))

    def go_live_request(self, tenant_id: str) -> dict[str, object] | None:
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT go_live_json FROM rgnr8_onboarding WHERE tenant_id={self._ph}",
                        (tenant_id,))
            _rows = cur.fetchall()
            row = _rows[0] if _rows else None
        finally:
            cur.close()
        if not row or row[0] is None:
            return None
        parsed = json.loads(str(row[0]))
        return parsed if isinstance(parsed, dict) else None
