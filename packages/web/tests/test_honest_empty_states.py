"""Every all-clear is checked against data; empty states link to the fix (issue 30).

"All caught up" was shown when no bank line had ever arrived, "every open
invoice is current" when there were no invoices, and "They agree" about two
zeros. Each said everything was fine when nobody had looked.
"""

from __future__ import annotations

from datetime import date

from test_books import _app, _req

from rgnr8_ar import ar_report, chase_list, draft_nudge
from rgnr8_forecast import Invoice, Money
from rgnr8_web.inbox_screens import render_inbox
from rgnr8_web.inventory_screens import render_inventory
from rgnr8_web.screens import render_ar_body


def _inbox(**counts: int) -> str:
    view = {"items": [], "pending": 0, "posted": 0, "matched": 0, "excluded": 0, **counts}
    return render_inbox("acme", view, {"accounts": []}, can_post=True)


def test_an_empty_queue_is_not_all_caught_up_until_something_came_in() -> None:
    nothing = _inbox()
    assert "All caught up" not in nothing
    assert "No bank lines have come in yet" in nothing
    assert 'href="/t/acme/connect"' in nothing and 'href="/t/acme/books/reconcile"' in nothing
    assert "All caught up" in _inbox(posted=3)


def _ar(invoices: tuple[Invoice, ...]) -> str:
    as_of = date(2026, 10, 29)
    report = ar_report(invoices, as_of=as_of)
    chase = chase_list(invoices, as_of=as_of)
    return render_ar_body("acme", "Acme Co", report, chase, [draft_nudge(c) for c in chase])


def test_no_invoices_is_not_every_invoice_current() -> None:
    page = _ar(())
    assert "every open invoice is current" not in page
    assert "No open invoices in the outlook" in page and 'href="/t/acme/invoices"' in page
    current = (Invoice("INV-1", "c1", date(2026, 10, 20), date(2026, 11, 19), Money(5000)),)
    assert "every open invoice is current" in _ar(current)


def test_two_zeros_do_not_agree() -> None:
    def page(value: str) -> str:
        valuation = {"items": [], "totals": {"ties_out": True, "difference_minor": "0",
                                             "items_value_minor": value, "ledger_balance_minor": value}}
        return render_inventory("acme", valuation, {}, {}, {"accounts": []}, can_edit=False)

    assert "They agree." not in page("0")
    assert "They agree." in page("125000")


def test_an_incomplete_report_says_so_on_screen() -> None:
    app, _, _ = _app()
    body = _req(app, "/t/acme/reports/budget_vs_actual").body
    assert "This report is incomplete" in body and "no budget provided" in body
