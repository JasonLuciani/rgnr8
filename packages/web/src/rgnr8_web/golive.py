"""Self-contained go-live builder for the owner web app's QBO migration.

This is the web-package counterpart to ``rgnr8_ops.onboarding``: the pure
functions that turn a source system's trial balance into a ``go-live/1``
request the TS accounting core (`@rgnr8/ledger-kernel` `goLiveFromDto`)
executes. It is copied here — rather than imported from ``rgnr8_ops`` — because
``rgnr8_ops`` depends on ``rgnr8_web`` (operator app is layered on the owner
app), so the web package must not import back into ops. The category slugs and
the go-live contract are the cross-language contract and must stay in lockstep
with both ``rgnr8_ops.onboarding`` and ledger-kernel.
"""

from __future__ import annotations


class GoLiveError(Exception):
    """An invalid go-live input (unknown category / source system / bad row)."""


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
    raise ``GoLiveError`` so nothing silently lands unclassified."""
    out: list[dict[str, object]] = []
    for r in rows:
        code = str(r.get("code", "")).strip()
        name = str(r.get("name", "")).strip()
        account_type = str(r.get("account_type", "")).strip()
        if not code or not name:
            raise GoLiveError("each trial-balance row needs a code and a name")
        subtype = subtype_for_account_type(account_type)
        if subtype is None:
            raise GoLiveError(f"account {code}: unmappable account type {account_type!r}")

        def _as_int(v: object, field: str, code: str = code) -> int:
            if isinstance(v, bool) or not isinstance(v, (int, str)):
                raise GoLiveError(f"account {code} has non-integer {field}")
            try:
                return int(v)
            except ValueError:
                raise GoLiveError(f"account {code} has non-integer {field}") from None

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
        raise GoLiveError(f"unknown source system {source_system!r}")
    if coa_category is not None and not is_valid_category(coa_category):
        raise GoLiveError(f"unknown COA category {coa_category!r}")
    if not source_accounts:
        raise GoLiveError("go-live needs at least one source account (the trial balance)")

    accounts: list[dict[str, object]] = []
    for a in source_accounts:
        code = str(a.get("code", "")).strip()
        name = str(a.get("name", "")).strip()
        if not code or not name:
            raise GoLiveError("each source account needs a code and a name")
        raw_balance = a.get("balance_minor")
        if not isinstance(raw_balance, (int, str)) or isinstance(raw_balance, bool):
            raise GoLiveError(f"account {code} has a non-integer balance_minor")
        try:
            balance_minor = int(raw_balance)
        except ValueError:
            raise GoLiveError(f"account {code} has a non-integer balance_minor")
        if not a.get("subtype") and not a.get("type"):
            raise GoLiveError(f"account {code} needs a subtype or type to be placed")
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
