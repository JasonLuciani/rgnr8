"""Parallel run: while QuickBooks is the book of record, nothing is entered here.

The ledger refuses every posting during a parallel run (see the ledger
service's bookOfRecord.ts). This is the web app's side of the same rule, so an
owner never fills in a form only to be refused:

- `is_posting_path` names the web routes that would post to the ledger, so
  the app answers them itself with the reason and where to go instead;
- `posting_forms_to_notice` swaps every form that submits to one of those
  routes for that same reason, on whatever screen it appears.

Pure: the app decides whether a tenant is in a parallel run, from the ledger's
own settings (the one place that state lives).
"""

from __future__ import annotations

import re
from html import escape, unescape

# Word for word what the ledger answers with its 409, so the reason reads the
# same whichever layer gave it.
PARALLEL_RUN_MESSAGE = (
    "QuickBooks is the book of record for this account during the parallel run. "
    "Add this in QuickBooks, then press Refresh to bring it across."
)

# The one source the QuickBooks mirror writes under. Callers may never claim it.
QUICKBOOKS_SOURCE = "quickbooks"


def is_posting_path(parts: list[str], method: str) -> bool:
    """True for a request that would post or reverse a journal entry.

    `parts` is the route split on "/" (`/t/acme/invoices` → `["t", "acme",
    "invoices"]`). Connections (where Refresh lives) and settings are never
    posting routes; creating customers, items, jobs or budgets isn't either.
    """
    if method != "POST" or len(parts) < 3:
        return False
    if parts[0] == "api":
        return len(parts) == 3 and parts[2] in ("entries", "ingest")
    if parts[0] != "t":
        return False
    rest = parts[2:]
    head, n = rest[0], len(rest)
    action = rest[2] if n >= 3 else ""
    if head in ("invoices", "bills"):
        return n == 1 or (n == 3 and action in ("payments", "credits", "refunds"))
    if head == "expenses":  # an expense is a journal entry, paid on the spot
        return n == 1
    if head == "books":
        return rest[1:] in (["entries"], ["wip"], ["recurring", "run"])
    if head == "payroll":  # drafting a run (POST /payroll) posts nothing; posting it does
        return rest[1:] == ["remit"] or (n == 3 and action in ("post", "void"))
    if head == "debt":
        return n == 2 and rest[1] in ("loans", "payments", "draws")
    if head == "assets":
        return n == 1 or (n == 3 and action in ("depreciate", "usage", "dispose"))
    if head == "capture":
        return rest[1:] == ["bill"]
    if head == "inventory":
        return n == 2 and rest[1] in ("receipts", "issues", "counts")
    if head == "work-orders":
        return n == 3 and action == "entries"
    if head in ("sales-orders", "estimates"):
        return n == 3 and action == "invoice"
    if head == "purchase-orders":
        return n == 3 and action in ("receipts", "bill")
    if head == "jobs":
        return n >= 3 and action in ("bill", "deposits", "retainage")
    if head == "inbox":
        return rest[1:] == ["bulk-accept"] or (
            n == 3 and action in ("accept", "match", "exclude", "undo"))
    return False


def notice_html(tenant: str, form_name: str = "", qbo_path: str = "") -> str:
    """The reason, with the way to Refresh one click away.

    A form that says what it is (`form_name`, and where QuickBooks keeps the same
    thing, `qbo_path`) gets a notice naming it, so a page of locked forms still
    reads as a list of things you will do here, not one warning repeated."""
    refresh = f'<a href="/t/{escape(tenant)}/connect">press Refresh</a>'
    if form_name:
        where = f" ({escape(qbo_path)})" if qbo_path else ""
        return (
            '<div class="banner warn" role="note" data-parallel-run="1">'
            f"<strong>{escape(form_name)}</strong>: this form opens here after the"
            " cutover, when RGNR8 becomes the book of record. Until then, add it in"
            f" QuickBooks{where}, then {refresh} to bring it across.</div>"
        )
    return (
        '<div class="banner warn" role="note" data-parallel-run="1">'
        "QuickBooks is the book of record during the parallel run. Add this in"
        f" QuickBooks, then {refresh} to bring it across.</div>"
    )


def form_label(name: str, qbo_path: str = "") -> str:
    """The attributes a posting form carries to name itself in the notice."""
    out = f' data-form-name="{escape(name)}"'
    return out + (f' data-qbo-path="{escape(qbo_path)}"' if qbo_path else "")


_FORM = re.compile(r"<form\b([^>]*)>.*?</form>", re.DOTALL | re.IGNORECASE)


def _attr(tag: str, name: str) -> str:
    """One attribute's value off a form's opening tag, unescaped."""
    m = re.search(rf'(?<![\w-]){name}="([^"]*)"', tag)
    return unescape(m.group(1)) if m else ""


def _posts(action: str) -> bool:
    return is_posting_path([p for p in action.split("?", 1)[0].split("/") if p], "POST")


def has_posting_form(html: str) -> bool:
    """Whether the page holds any form that would post: only then is it worth
    asking the ledger whose books these are."""
    return any(_posts(_attr(m.group(1), "action")) for m in _FORM.finditer(html))


def posting_forms_to_notice(html: str, tenant: str) -> str:
    """Swap every form that would post to the ledger for the parallel-run notice.

    Our own templates never nest forms, so a form runs to its first `</form>`.
    Forms that don't post (a customer, a budget, a rule) are left as they are.
    """
    def swap(m: re.Match[str]) -> str:
        tag = m.group(1)
        if not _posts(_attr(tag, "action")):
            return m.group(0)
        return notice_html(tenant, _attr(tag, "data-form-name"), _attr(tag, "data-qbo-path"))

    return _FORM.sub(swap, html)
