"""The cutover page and the bank-account choice on payments (issues 39-41)."""

from __future__ import annotations

import json
from typing import Any

from test_books import DEFAULT_ROUTES, _app, _req

from rgnr8_web.arap_screens import render_documents
from rgnr8_web.cutover_screens import render_cutover

DOC = {"id": "INV-1", "party_id": "c1", "date": "2026-11-01", "due_date": "2026-12-01",
       "total_minor": "5000", "open_minor": "5000", "status": "OPEN"}


def test_a_payment_names_its_bank_account_only_when_there_is_a_choice() -> None:
    docs = {"documents": [DOC], "open_total_minor": "5000"}
    one = render_documents("acme", "invoices", docs, {}, today="2026-11-05", can_post=True,
                           banks=(("10", "Operating Checking"),))
    assert 'name="bank_code"' not in one
    two = render_documents("acme", "invoices", docs, {}, today="2026-11-05", can_post=True,
                           banks=(("10", "Operating Checking"), ("11", "Savings")))
    assert 'name="bank_code"' in two and '<option value="11">Savings</option>' in two


def test_the_chosen_bank_account_reaches_the_ledger() -> None:
    routes: dict[str, tuple[int, dict[str, Any]]] = dict(DEFAULT_ROUTES)
    routes["POST /t/acme/invoices/INV-1/payments"] = (201, {"tenant": "acme"})
    app, transport, _ = _app(routes)
    _req(app, "/t/acme/invoices/INV-1/payments", method="POST", body="amount=50.00&bank_code=11")
    sent = [json.loads(b) for (m, p, b) in transport.calls if m == "POST" and p.endswith("/payments")]
    assert sent and sent[0]["bank_code"] == "11"


def _plan(**kw: Any) -> dict[str, Any]:
    base = {"cutover_date": "2026-10-31", "open_items_as_of": "2026-11-02", "blockers": [],
            "ready": True, "report_hash": "a" * 64, "documents": [],
            "tie_out": [{"control": "Accounts Receivable", "books_minor": "100",
                         "open_items_minor": "100", "difference_minor": "0"}]}
    base.update(kw)
    return base


def test_a_blocked_report_shows_why_and_offers_no_sign_off() -> None:
    page = render_cutover("acme", status={}, plan=_plan(blockers=["2026-10-15 is not the last day of a month"]),
                          cutover_date="2026-10-15")
    assert "can't go ahead yet" in page and "not the last day of a month" in page
    assert "Cut over to RGNR8" not in page


def test_a_difference_has_to_be_accepted_on_the_form() -> None:
    clean = render_cutover("acme", status={}, plan=_plan(), cutover_date="2026-10-31")
    assert "Every check passes." in clean and 'name="accept_differences"' not in clean
    off = _plan(tie_out=[{"control": "Accounts Receivable", "books_minor": "37320",
                          "open_items_minor": "38995", "difference_minor": "1675"}])
    page = render_cutover("acme", status={}, plan=off, cutover_date="2026-10-31")
    assert 'name="accept_differences" required' in page


def test_after_cutover_the_page_shows_the_signer_and_whether_it_can_be_undone() -> None:
    record = {"state": "live", "cutover_date": "2026-10-31", "signed_by": "J. Accountant",
              "report_hash": "b" * 64}
    open_ = render_cutover("acme", status={"record": record, "backout_blocker": ""}, plan=None,
                           cutover_date="")
    assert "J. Accountant" in open_ and "Back out the cutover" in open_
    closed = render_cutover("acme", status={"record": record, "backout_blocker": "RGNR8 has recorded "
                                            "its first entries"}, plan=None, cutover_date="")
    assert "can no longer be backed out" in closed and "Back out the cutover" not in closed


def test_connections_and_routes_are_never_refused_as_postings() -> None:
    from rgnr8_web.parallel_run import is_posting_path

    for path in ("/t/acme/connect/cutover", "/t/acme/connect/cutover/backout"):
        assert not is_posting_path([p for p in path.split("/") if p], "POST"), path


def test_a_payment_has_no_bank_until_one_is_chosen() -> None:
    docs = {"documents": [{**DOC, "id": "INV/7 #2"}], "open_total_minor": "5000"}
    page = render_documents("acme", "invoices", docs, {}, today="2026-11-05", can_post=True,
                            banks=(("10", "Operating Checking"), ("11", "Savings")))
    assert 'name="bank_code" aria-label="Bank account" required' in page
    assert '<option value="">Bank…</option><option value="10">' in page  # blank comes first
    # The id is one path segment, whatever QuickBooks numbered it.
    assert 'action="/t/acme/invoices/INV%2F7%20%232/payments"' in page
