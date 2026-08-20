"""The tool registry and the v1 read tools.

Each tool is a thin wrapper over an existing ledger read endpoint: it builds the
path, calls the tenant-scoped reader, shapes a `ToolResult` (money stays integer
minor units), and attaches citations. The registry filters the catalog to the
caller's RBAC scopes, so a tool the caller can't use is never even offered to the
model. Money math is done by the ledger engines, never here or by the model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable

from .model import AskContext, Citation, ToolResult

RunFn = Callable[[AskContext, dict[str, object]], ToolResult]


class ToolError(Exception):
    """A bad argument or a tool that can't run — surfaced to the model, not raised."""


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params_schema: dict[str, object]
    permission: str | None   # RBAC scope required; None = any authenticated member
    determinism: str         # "exact" | "projection"
    run: RunFn


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool]) -> None:
        self._by_name: dict[str, Tool] = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._by_name.get(name)

    def permitted(self, name: str, permissions: frozenset[str]) -> bool:
        t = self._by_name.get(name)
        return t is not None and (t.permission is None or t.permission in permissions)

    def catalog(self, permissions: frozenset[str]) -> list[dict[str, object]]:
        """The tool schemas the model may see, filtered to the caller's scopes."""
        return [
            {"name": t.name, "description": t.description, "input_schema": t.params_schema}
            for t in self._by_name.values()
            if t.permission is None or t.permission in permissions
        ]


# --- helpers -----------------------------------------------------------------

def _fmt(minor: int, symbol: str = "$") -> str:
    neg = minor < 0
    whole, frac = divmod(abs(minor), 100)
    return f"{'-' if neg else ''}{symbol}{whole:,}.{frac:02d}"


def _int(v: object) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def _rows(body: Mapping[str, object], key: str) -> list[Mapping[str, object]]:
    v = body.get(key)
    return [r for r in v if isinstance(r, Mapping)] if isinstance(v, list) else []


def collect_minor_figures(obj: object) -> set[int]:
    """Every authoritative money figure a tool returned — found by RGNR8's naming
    convention (any key ending in `_minor`). This is the allow-list the numeric
    backstop checks an answer's dollar figures against."""
    found: set[int] = set()

    def walk(o: object) -> None:
        if isinstance(o, Mapping):
            for k, v in o.items():
                if isinstance(k, str) and k.endswith("_minor") and not isinstance(v, (dict, list)):
                    found.add(abs(_int(v)))
                else:
                    walk(v)
        elif isinstance(o, list):
            for item in o:
                walk(item)

    walk(obj)
    return found


# --- the read tools ----------------------------------------------------------

def _cash_position(ctx: AskContext, args: dict[str, object]) -> ToolResult:
    body = ctx.ledger.read("/trial-balance", {})
    raw_codes = ctx.hints.get("cash_accounts")
    cash_codes = {str(c) for c in raw_codes} if isinstance(raw_codes, list) else {"1000", "1010"}
    total = 0
    accounts: list[dict[str, object]] = []
    for r in _rows(body, "rows"):
        code = str(r.get("code"))
        if code not in cash_codes:
            continue
        bal = _int(r.get("debit_minor")) - _int(r.get("credit_minor"))
        total += bal
        accounts.append({"code": code, "name": r.get("name"), "balance_minor": str(bal)})
    return ToolResult(
        data={"total_cash_minor": str(total), "accounts": accounts},
        citations=(Citation("Cash on hand", "report", "/trial-balance"),),
        summary_hint=f"Cash on hand: {_fmt(total)}",
    )


def _ratios(ctx: AskContext, args: dict[str, object]) -> ToolResult:
    body = ctx.ledger.read("/ratios", _asof(args))
    return ToolResult(
        data=body,
        citations=(Citation("Financial ratios", "report", "/ratios"),),
        summary_hint="Liquidity, leverage and profitability, computed from the books.",
    )


def _pnl(ctx: AskContext, args: dict[str, object]) -> ToolResult:
    body = ctx.ledger.read("/statements", _range(args))
    inc = body.get("income_statement")
    data: dict[str, object] = {"income_statement": inc} if isinstance(inc, Mapping) else dict(body)
    return ToolResult(
        data=data,
        citations=(Citation("Income statement", "report", "/statements"),),
        summary_hint="Profit & loss for the period.",
    )


def _balance_sheet(ctx: AskContext, args: dict[str, object]) -> ToolResult:
    body = ctx.ledger.read("/statements", _range(args))
    bs = body.get("balance_sheet")
    data: dict[str, object] = {"balance_sheet": bs} if isinstance(bs, Mapping) else dict(body)
    return ToolResult(
        data=data,
        citations=(Citation("Balance sheet", "report", "/statements"),),
        summary_hint="Assets, liabilities and equity as of the date.",
    )


def _debt_summary(ctx: AskContext, args: dict[str, object]) -> ToolResult:
    body = ctx.ledger.read("/debt", {})
    return ToolResult(
        data=body,
        citations=(Citation("Debt dashboard", "report", "/debt"),),
        summary_hint="Total debt, blended rate and monthly debt service.",
    )


def _aging(ctx: AskContext, args: dict[str, object]) -> ToolResult:
    side = str(args.get("side", "ar")).lower()
    if side not in ("ar", "ap"):
        raise ToolError("side must be 'ar' (who owes you) or 'ap' (what you owe)")
    body = ctx.ledger.read("/aging", {"side": side})
    return ToolResult(
        data=body,
        citations=(Citation(f"{side.upper()} aging", "report", "/aging"),),
        summary_hint=f"{side.upper()} aging buckets.",
    )


def _account_register(ctx: AskContext, args: dict[str, object]) -> ToolResult:
    code = str(args.get("code", "")).strip()
    if not code:
        raise ToolError("account code is required")
    body = ctx.ledger.read(f"/accounts/{code}/register", _range(args))
    return ToolResult(
        data=body,
        citations=(Citation(f"Register for account {code}", "register", f"/accounts/{code}/register"),),
        summary_hint=f"Entries posted to account {code}.",
    )


def _asof(args: dict[str, object]) -> dict[str, str]:
    a = str(args.get("as_of", "")).strip()
    return {"as_of": a} if a else {}


def _range(args: dict[str, object]) -> dict[str, str]:
    out: dict[str, str] = {}
    frm = str(args.get("from", "")).strip()
    to = str(args.get("to", "")).strip() or str(args.get("as_of", "")).strip()
    if frm:
        out["from"] = frm
    if to:
        out["to"] = to
    return out


_RANGE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "from": {"type": "string", "description": "start date YYYY-MM-DD"},
        "to": {"type": "string", "description": "end date YYYY-MM-DD"},
    },
}
_ASOF_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {"as_of": {"type": "string", "description": "date YYYY-MM-DD"}},
}
_EMPTY_SCHEMA: dict[str, object] = {"type": "object", "properties": {}}


def default_registry() -> ToolRegistry:
    """The v1 read-tool catalog. Segment tools (job costing, WIP, consolidation)
    layer on in Phase 2; these are the cross-segment core."""
    return ToolRegistry([
        Tool("cash_position", "How much cash is on hand right now (bank accounts).",
             _EMPTY_SCHEMA, "ledger:read", "exact", _cash_position),
        Tool("ratios", "Financial health: liquidity, leverage (incl. DSCR), profitability, "
             "with a plain-language read. Use for 'am I healthy', 'what's my margin', 'DSCR'.",
             _ASOF_SCHEMA, "reports:read", "exact", _ratios),
        Tool("pnl", "Profit & loss (income statement) for a date range. Use for 'how did we do', "
             "'revenue', 'net income', 'margin'.", _RANGE_SCHEMA, "reports:read", "exact", _pnl),
        Tool("balance_sheet", "Balance sheet as of a date (assets, liabilities, equity).",
             _ASOF_SCHEMA, "reports:read", "exact", _balance_sheet),
        Tool("debt_summary", "Loans and lines of credit: total owed, rate, monthly service, "
             "next payments. Use for 'what do I owe', 'my loans'.",
             _EMPTY_SCHEMA, "reports:read", "exact", _debt_summary),
        Tool("aging", "Who owes you (side='ar') or what you owe (side='ap'), by age bucket.",
             {"type": "object", "properties": {"side": {"type": "string", "enum": ["ar", "ap"]}}},
             "ledger:read", "exact", _aging),
        Tool("account_register", "Every entry posted to one account over a date range. Use to "
             "'show the entries' behind a number.",
             {"type": "object", "properties": {
                 "code": {"type": "string", "description": "account code, e.g. 1000"},
                 "from": {"type": "string"}, "to": {"type": "string"}},
              "required": ["code"]},
             "ledger:read", "exact", _account_register),
    ])


__all__ = [
    "Tool", "ToolRegistry", "ToolError", "RunFn",
    "default_registry", "collect_minor_figures",
]
