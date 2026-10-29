"""Receipts: upload a photo or PDF, or paste the text, and record what it says.

A receipt becomes one of two things, and the difference is when the money
moves. An **expense** is already paid (card, debit or check) and goes straight
into the books. A **bill** is paid later: it waits in Bills until it is paid.
The draft offers both, worded that way, because picking the wrong one either
leaves a debt on the books that was never owed or loses one that is.

An uploaded file is kept the moment it arrives, before anything is read from
it, and is filed onto whatever gets created. Reading a photo needs a receipt
reader (`WebApp.set_ocr`); with none wired the file is still kept, and the
owner types the vendor, date and total. The text path needs no external
service. A low-confidence or total-less read is flagged for review rather than
recorded blind: a captured receipt is a claim, not an entry.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _banner, _card, _esc, _seq, money
from .parallel_run import form_label
from .provenance_labels import Provenance
from .provenance_labels import legend as prov_legend

# A captured receipt is IMPORTED (a claim read from OCR/text); recording it
# posts it to the ledger, at which point it is POSTED.
_CAPTURE_PROV = (Provenance.IMPORTED, Provenance.POSTED)

_SAMPLE = (
    "HOME DEPOT #4512\nDate: 03/14/2026\n"
    "3/4in Plywood   2 @ 48.00   96.00\nTax   8.68\nTOTAL   117.18"
)

_RECEIPT_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".pdf")


def is_receipt_file(filename: str, content_type: str) -> bool:
    """A photo or a PDF. Browsers label some photos (HEIC above all) as
    application/octet-stream, so the file's extension counts too."""
    ct = content_type.split(";", 1)[0].strip().lower()
    if ct.startswith("image/") or ct == "application/pdf":
        return True
    return ct in ("", "application/octet-stream") and filename.lower().endswith(
        _RECEIPT_EXTENSIONS)


def render_capture(
    tenant: str,
    *,
    draft: Mapping[str, object] | None = None,
    raw_text: str = "",
    vendors: list[object] | None = None,
    receipt_id: str = "",
    receipt_name: str = "",
    unread: bool = False,
    kept: list[Mapping[str, object]] | None = None,
    done: str = "",
    error: str = "",
) -> str:
    banner = ""
    if done:
        banner += _banner("good", done)
    if error:
        banner += _banner("warn", error)

    scan_form = (
        '<p class="muted">Upload a photo or PDF of a receipt, or paste its text. We\'ll '
        "keep the file, pull out the vendor, date and total, and you choose what it is: "
        "an expense (already paid) or a bill (to pay later).</p>"
        f'<form method="post" enctype="multipart/form-data" action="/t/{_esc(tenant)}/capture" '
        'class="grid">'
        '<label class="wide">Photo or PDF of the receipt'
        '<input type="file" name="file" accept="image/*,application/pdf"></label>'
        f'<label class="wide">Or paste the receipt text'
        f'<textarea name="text" rows="6" placeholder="{_esc(_SAMPLE)}">{_esc(raw_text)}</textarea></label>'
        '<div class="wide"><button type="submit">Read receipt</button></div>'
        "</form>"
    )
    sections = [banner, prov_legend(_CAPTURE_PROV), _card("Capture a receipt", scan_form)]

    if draft is not None:
        sections.append(_render_draft(tenant, draft, vendors or [], receipt_id=receipt_id,
                                      receipt_name=receipt_name, unread=unread))
    if kept:
        sections.append(_render_kept(tenant, kept))
    return "".join(sections)


def _render_draft(
    tenant: str, draft: Mapping[str, object], vendors: list[object], *,
    receipt_id: str, receipt_name: str, unread: bool,
) -> str:
    confidence = draft.get("confidence", 0)
    if unread:
        review = _banner(
            "warn",
            f"We kept {receipt_name or 'the file'}, but this server can't read photos or "
            "PDFs yet. Type the vendor, date and total from the receipt below.",
        )
    elif draft.get("needs_review"):
        review = _banner(
            "warn",
            f"Please check this before saving — {draft.get('review_reason') or 'low confidence'}.",
        )
    else:
        review = _banner("good", f"Read with high confidence ({confidence}/1000).")

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

    kept = ""
    if receipt_id:
        kept = (
            f'<input type="hidden" name="receipt" value="{_esc(receipt_id)}">'
            f'<input type="hidden" name="receipt_name" value="{_esc(receipt_name)}">'
            '<p class="note wide">Kept: <a href="/t/'
            f'{_esc(tenant)}/files/{_esc(receipt_id)}/download">{_esc(receipt_name or "receipt")}</a>. '
            "It is attached to the expense or bill you create.</p>"
        )
    read = ""
    if not unread:
        read = (f'<div class="muted" style="margin-bottom:8px">Total read: '
                f'<strong>{money(draft.get("total_minor"))}</strong> &nbsp;·&nbsp; tax shown on '
                f'the receipt: {money(draft.get("tax_minor"))}</div>')
    form = (
        review + read
        + f'<form method="post" action="/t/{_esc(tenant)}/capture/bill" class="grid"'
        f'{form_label("Receipt: expense or bill", "+ New → Expense, or + New → Bill")}>'
        + vendor_field
        + f'<label>Date<input name="date" type="date" value="{_esc(draft.get("date"))}" required></label>'
        f'<label>Amount ($)<input name="amount" value="{_esc(_to_dollars(draft.get("total_minor")) if not unread else "")}" required></label>'
        f'<label>Expense account code<input name="expense_account_code" '
        f'value="{_esc(draft.get("expense_account_code") or "6400")}"></label>'
        '<label class="wide">Memo<input name="memo" placeholder="From captured receipt"></label>'
        + kept
        + '<p class="note wide">Already paid by card, debit or check? Record it as an '
        "expense: it goes straight into the books. Paying it later? Create a bill: it waits "
        "in Bills until you pay it.</p>"
        '<div class="wide">'
        f'<button class="btn" type="submit" formmethod="get" '
        f'formaction="/t/{_esc(tenant)}/expenses">Record as expense (already paid)</button> '
        '<button class="btn ghost" type="submit">Create bill (pay later)</button></div>'
        "</form>"
    )
    # Read off a receipt, not yet posted: the card says so in its header.
    return _card("Drafted from the receipt", form, provenance=Provenance.IMPORTED)


def _render_kept(tenant: str, kept: list[Mapping[str, object]]) -> str:
    """Receipts kept here and not yet recorded: none is lost because the draft
    was closed, or because nothing could be recorded during the parallel run."""
    rows = []
    for a in kept:
        att = _esc(a.get("id"))
        subject = _esc(a.get("subject_id"))
        rows.append(
            f'<tr><td><a href="/t/{_esc(tenant)}/files/{att}/download">'
            f"{_esc(a.get('filename'))}</a></td>"
            f"<td class='muted'>{_esc(str(a.get('uploaded_at'))[:10])}</td>"
            f'<td><a class="btn-link" href="/t/{_esc(tenant)}/capture?receipt={att}">Record it</a> '
            f'<a class="btn-link" href="/t/{_esc(tenant)}/files/receipt/{subject}">Remove</a></td></tr>'
        )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>File</th><th>Kept</th><th></th>'
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )
    return _card("Kept, not recorded yet", table)


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


__all__ = ["is_receipt_file", "render_capture", "render_capture_unavailable", "_seq"]
