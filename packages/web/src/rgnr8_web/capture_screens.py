"""Receipt capture — paste a receipt, get a drafted bill.

The novel part QuickBooks and Xero charge for: read a receipt and pre-fill a
bill. The text path works with no external service (a typed receipt, an emailed
invoice, or any OCR output); a real vision provider would feed the same extractor
from a photo. A low-confidence or total-less read is flagged for review rather
than posted blind — a captured receipt is a claim, not an entry.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _banner, _card, _esc, _seq, money
from .provenance_labels import Provenance
from .provenance_labels import badge as prov_badge
from .provenance_labels import legend as prov_legend

# A captured receipt is IMPORTED (a claim read from OCR/text); creating the bill
# posts it to the ledger, at which point it is POSTED.
_CAPTURE_PROV = (Provenance.IMPORTED, Provenance.POSTED)

_SAMPLE = (
    "HOME DEPOT #4512\nDate: 03/14/2026\n"
    "3/4in Plywood   2 @ 48.00   96.00\nTax   8.68\nTOTAL   117.18"
)


def render_capture(
    tenant: str,
    *,
    draft: Mapping[str, object] | None = None,
    raw_text: str = "",
    vendors: list[object] | None = None,
    done: str = "",
    error: str = "",
) -> str:
    banner = ""
    if done:
        banner += _banner("good", done)
    if error:
        banner += _banner("warn", error)

    scan_form = (
        '<p class="muted">Paste the text of a receipt or invoice. We\'ll pull out the '
        "vendor, date and total and draft a bill for you to review.</p>"
        f'<form method="post" action="/t/{_esc(tenant)}/capture" class="grid">'
        f'<label style="grid-column:1/-1">Receipt text'
        f'<textarea name="text" rows="8" placeholder="{_esc(_SAMPLE)}">{_esc(raw_text)}</textarea></label>'
        '<div style="grid-column:1/-1"><button type="submit">Scan receipt</button></div>'
        "</form>"
    )
    sections = [banner, prov_legend(_CAPTURE_PROV), _card("Capture a receipt", scan_form)]

    if draft is not None:
        sections.append(_render_draft(tenant, draft, vendors or []))
    return "".join(sections)


def _render_draft(tenant: str, draft: Mapping[str, object], vendors: list[object]) -> str:
    needs_review = bool(draft.get("needs_review"))
    confidence = draft.get("confidence", 0)
    review = ""
    if needs_review:
        review = _banner(
            "warn",
            f"Please check this before saving — {_esc(draft.get('review_reason') or 'low confidence')}.",
        )
    else:
        review = _banner("good", f"Read with high confidence ({_esc(confidence)}/1000).")

    vendor_opts = "".join(
        f'<option value="{_esc(v.get("id"))}">{_esc(v.get("name"))}</option>'
        for v in vendors if isinstance(v, Mapping)
    )
    vendor_field = (
        '<label>Vendor (new)<input name="vendor" value="' + _esc(draft.get("vendor")) + '"></label>'
    )
    if vendor_opts:
        vendor_field = (
            '<label>Existing vendor<select name="vendor_id"><option value="">— new vendor below —</option>'
            + vendor_opts + "</select></label>" + vendor_field
        )

    total = money(draft.get("total_minor"))
    tax = money(draft.get("tax_minor"))
    amount_dollars = _to_dollars(draft.get("total_minor"))

    form = (
        review
        + f'<div class="muted" style="margin-bottom:8px">Extracted total: <strong>{total}</strong> '
        f'{prov_badge(Provenance.IMPORTED)}'
        f' &nbsp;·&nbsp; tax shown on receipt: {tax}</div>'
        f'<form method="post" action="/t/{_esc(tenant)}/capture/bill" class="grid">'
        + vendor_field
        + f'<label>Date<input name="date" type="date" value="{_esc(draft.get("date"))}" required></label>'
        f'<label>Amount ($)<input name="amount" value="{_esc(amount_dollars)}" required></label>'
        f'<label>Expense account code<input name="expense_account_code" '
        f'value="{_esc(draft.get("expense_account_code") or "6400")}"></label>'
        '<label style="grid-column:1/-1">Memo<input name="memo" placeholder="From captured receipt"></label>'
        '<div style="grid-column:1/-1"><button type="submit">Create bill</button></div>'
        "</form>"
    )
    return _card("Drafted bill", form)


def _to_dollars(minor: object) -> str:
    try:
        n = int(str(minor))
    except (TypeError, ValueError):
        return ""
    sign = "-" if n < 0 else ""
    whole, frac = divmod(abs(n), 100)
    return f"{sign}{whole}.{frac:02d}"


def render_capture_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card("Capture unavailable", _banner("warn", "The ledger service isn't reachable right now.") + extra)


__all__ = ["render_capture", "render_capture_unavailable", "_seq"]
