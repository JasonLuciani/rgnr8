"""The receipt behind the number.

A journal entry says $249 went to Software & Subscriptions on the 21st. It does
not say what was bought — and in an audit, a deduction dispute, or a
conversation with an accountant eleven months later, that is the only thing
anyone wants to know. The entry is the claim; the receipt is the evidence.

So this is a small screen with one job: show what evidences a given thing, and
let the owner add more. It is reachable from every row that could have a
receipt, rather than being a filing cabinet somewhere else in the product.
"""

from __future__ import annotations

from collections.abc import Mapping

from .books_screens import _card, _esc, _seq


def _banner(kind: str, text: str) -> str:
    return f'<div class="banner {kind}">{_esc(text)}</div>'


_SUBJECT_LABEL = {
    "entry": "journal entry",
    "feed": "bank transaction",
    "invoice": "invoice",
    "bill": "bill",
    "payroll": "payroll run",
}


def _size(bytes_: object) -> str:
    try:
        n = int(str(bytes_))
    except (TypeError, ValueError):
        return ""
    if n < 1024:
        return f"{n} bytes"
    if n < 1_048_576:
        return f"{n / 1024:.0f} KB"
    return f"{n / 1_048_576:.1f} MB"


def attachment_link(tenant: str, kind: str, subject_id: str, count: int = 0) -> str:
    """A paperclip for a list row — filled when there's evidence, hollow when not."""
    label = f"📎 {count}" if count else "📎"
    title = (
        f"{count} attachment(s)" if count else "No receipt attached yet"
    )
    style = "" if count else "opacity:.45"
    return (
        f'<a class="btn-link" style="{style}" title="{_esc(title)}" '
        f'href="/t/{_esc(tenant)}/files/{_esc(kind)}/{_esc(subject_id)}">{label}</a>'
    )


def render_attachments(
    tenant: str,
    kind: str,
    subject_id: str,
    data: Mapping[str, object],
    *,
    can_post: bool,
    message: str = "",
    error: str = "",
) -> str:
    """Everything attached to one thing, and the form to attach more."""
    rows = []
    for a in _seq(data.get("attachments")):
        if not isinstance(a, Mapping):
            continue
        att_id = _esc(a.get("id"))
        remove = ""
        if can_post:
            remove = (
                f'<form method="post" action="/t/{_esc(tenant)}/files/{_esc(kind)}/'
                f'{_esc(subject_id)}/{att_id}/delete" style="margin:0">'
                '<button class="btn ghost" style="padding:4px 10px;margin:0" '
                'type="submit">Remove</button></form>'
            )
        rows.append(
            f"<tr><td><a href=\"/t/{_esc(tenant)}/files/{att_id}/download\">"
            f"{_esc(a.get('filename'))}</a><br>"
            f'<span class="muted" style="font-size:12px">{_esc(a.get("note"))}</span></td>'
            f"<td class='muted'>{_esc(a.get('content_type'))}</td>"
            f"<td class='muted'>{_esc(_size(a.get('bytes')))}</td>"
            f"<td class='muted'>{_esc(str(a.get('uploaded_at'))[:10])}</td>"
            f"<td>{remove}</td></tr>"
        )
    empty = (
        '<tr><td colspan="5" class="muted" style="text-align:center;padding:24px">'
        "Nothing attached yet.</td></tr>"
    )
    table = (
        '<div class="table-scroll"><table><thead><tr><th>File</th><th>Type</th>'
        "<th>Size</th><th>Added</th><th></th></tr></thead><tbody>"
        + ("".join(rows) or empty) + "</tbody></table></div>"
    )

    head = ""
    if message:
        head += _banner("good", message)
    if error:
        head += _banner("warn", error)

    label = _SUBJECT_LABEL.get(kind, kind)
    back = _back_link(tenant, kind, subject_id)
    out = head + _card(f"Attached to {label} {_esc(subject_id)}", table, back)
    if can_post:
        out += _card(
            "Attach a receipt",
            f'<form method="post" enctype="multipart/form-data" '
            f'action="/t/{_esc(tenant)}/files/{_esc(kind)}/{_esc(subject_id)}">'
            '<div class="grid2">'
            '<div><label>File</label><input type="file" name="file" multiple></div>'
            '<div><label>Note (optional)</label>'
            '<input name="note" placeholder="What is this?"></div>'
            "</div>"
            '<p class="note">Up to 8 MB per file. Stored beside the books, so a '
            "restore brings the evidence back with the entries rather than "
            "leaving the ledger pointing at files that are gone.</p>"
            '<button class="btn" type="submit">Attach</button></form>',
        )
    return out


def _back_link(tenant: str, kind: str, subject_id: str) -> str:
    """Back to whatever this is evidence for, not to a generic index."""
    targets = {
        "feed": f"/t/{tenant}/inbox",
        "invoice": f"/t/{tenant}/invoices",
        "bill": f"/t/{tenant}/bills",
        "payroll": f"/t/{tenant}/payroll/{subject_id}",
        "entry": f"/t/{tenant}/books",
    }
    href = targets.get(kind, f"/t/{tenant}/books")
    return f'<a class="btn-link" href="{_esc(href)}">Back</a>'


def render_attachments_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Attachments unavailable",
        _banner("warn", "The ledger service isn't reachable right now.")
        + '<p class="muted">Your files are safe — this screen just can\'t list them '
        "at the moment.</p>" + extra,
    )
