"""The action queue as the owner actually receives it: through a request.

`test_attention.py` covers the ranking policy in isolation. This file covers the
part that isolation can't: that the queue is wired to the cash page at all, that
it survives a source failing, and that it never takes the page down with it.
That distinction matters here — the component set shipped with every field grid
in the app inert precisely because the tests only ever asserted on markup that
was never asked to mean anything.
"""

from __future__ import annotations

import re
from datetime import date
from types import SimpleNamespace

import pytest
from factory import app_with_two_tenants

from rgnr8_forecast import (
    CashPosition,
    ForecastConfig,
    ForecastInputs,
    Invoice,
    Money,
)
from rgnr8_web import Request, WebApp
from rgnr8_web.shell import _SHELL_CSS


def _get(app, path: str, token: str = "tok-acme"):
    return app.handle(Request("GET", path, {"authorization": f"Bearer {token}"}, ""))


def test_the_cash_page_carries_the_queue() -> None:
    res = _get(app_with_two_tenants(), "/t/acme")
    assert res.status == 200
    assert "What needs you" in res.body


def test_an_unwired_bank_feed_says_so_rather_than_implying_all_clear() -> None:
    # The stock composition has no ledger service, so nothing is reviewing the
    # feed. The owner must be told that, not shown an empty queue.
    body = _get(app_with_two_tenants(), "/t/acme").body
    assert "No bank feed is wired" in body
    assert "No accounting source is connected" in body
    assert "Nothing needs you right now" not in body


def test_the_queue_offers_a_way_to_act_on_each_item() -> None:
    body = _get(app_with_two_tenants(), "/t/acme").body
    assert 'class="queue"' in body
    # Every row carries a button, and every link stays inside this tenant.
    assert 'href="/t/acme/connect"' in body
    assert "/t/bright" not in body


class _Raises:
    def feed_inbox(self, *a, **k):
        raise RuntimeError("ledger service is unreachable")


class _Errors:
    def feed_inbox(self, *a, **k):
        return SimpleNamespace(ok=False, body={}, error=lambda: "502 from ledger")


class _Nonsense:
    def feed_inbox(self, *a, **k):
        return SimpleNamespace(ok=True, body={"pending": "lots"}, error=lambda: "")


@pytest.mark.parametrize("client", [_Raises(), _Errors(), _Nonsense()])
def test_a_feed_that_cannot_answer_counts_as_unknown_not_zero(client) -> None:
    # Whether the client raises, returns an error, or returns a payload with a
    # nonsense count, the answer is None — and None renders as "nothing is
    # being reviewed", never as a reassuring 0.
    app = app_with_two_tenants()
    app._ledger = client  # type: ignore[attr-defined]
    tenant = app._tenants["acme"]  # type: ignore[attr-defined]
    assert app._review_count(tenant) is None  # type: ignore[attr-defined]


def test_an_overdue_invoice_reaches_the_queue_through_the_real_adapter() -> None:
    """Exercises `AppServer._attention`, not just the policy.

    Every other test here runs against a tenant with nothing overdue, and
    `test_attention.py` hands `Signals` its fields directly — so the adapter
    that reads them off a real `ChaseItem` had no coverage at all, and shipped
    reading `invoice.customer_name`, which does not exist. mypy caught it; a
    test should have.
    """
    app = WebApp()
    app.add_tenant(
        "late", "Late Co",
        ForecastInputs(
            opening=CashPosition(as_of=date(2026, 8, 3), available=Money.from_decimal("50000.00")),
            invoices=(
                Invoice("INV-9", "riverside-llc", date(2026, 5, 1), date(2026, 6, 1),
                        Money.from_decimal("18000.00")),
            ),
        ),
        ForecastConfig(minimum_cash=Money.from_decimal("1000.00")),
        token="tok-late",
    )
    body = _get(app, "/t/late", token="tok-late").body
    assert "1 invoice overdue" in body
    assert "riverside-llc is 63 days late" in body
    assert "$18,000.00 overdue" in body


def test_the_queue_never_raises_out_of_the_page() -> None:
    # The queue is decoration on a page whose real job is the cash figure. Even
    # with every optional source missing, the page must render.
    res = _get(app_with_two_tenants(), "/t/acme")
    assert res.status == 200 and "Cash today" in res.body


def test_the_old_standalone_do_this_banner_is_gone_when_a_queue_is_drawn() -> None:
    # Two competing instructions on one page can disagree; the briefing's
    # primary action is folded into the queue instead.
    body = _get(app_with_two_tenants(), "/t/acme").body
    assert "Do this — " not in body


def test_every_class_inside_the_chart_is_styled_by_the_page_that_embeds_it() -> None:
    """The 13-week chart rendered as a solid black rectangle in the app.

    `_cash_chart` is borrowed from the briefing package, and its rules stayed
    behind in that package's own private <style>. Inside the shell every
    element fell back to SVG's default `fill:black; stroke:none`, so the
    below-floor band painted over the whole plot and the cash line — filled
    instead of stroked — disappeared into it.

    `test_components.py` could not catch this: it reads `class="..."` literals
    out of `rgnr8_web/*.py`, and these classes are minted inside an SVG string
    built by another package. So the check is repeated here against the
    rendered page, which is the only place the two halves meet.
    """
    body = _get(app_with_two_tenants(), "/t/acme").body
    svg = body[body.index("<svg", body.index("Next 13 weeks")):]
    svg = svg[: svg.index("</svg>")]
    used = set(re.findall(r'class="([^"]*)"', svg))
    assert used, "the chart should carry classes; if it stopped, delete this test"
    for name in sorted({t for attr in used for t in attr.split()}):
        # The rule has to be scoped to an SVG. `.grid` and `.danger` also exist
        # as app classes (a field grid, a destructive button), and an unscoped
        # check would accept those and pass while the chart still rendered
        # black — which is exactly the trap this test exists to avoid.
        assert re.search(rf"svg \.{re.escape(name)}\b", _SHELL_CSS), (
            f"the chart paints an element with class={name!r} and the page has no "
            "svg-scoped rule for it, so it falls back to SVG's default black"
        )


def test_the_cash_line_is_stroked_rather_than_filled() -> None:
    # The specific shape of the black-box bug: a polyline with no stroke and a
    # default fill is not a line, it is a solid polygon.
    series = re.search(r"svg \.series\{([^}]*)\}", _SHELL_CSS)
    assert series, "the chart's line needs a rule in the stylesheet the app serves"
    assert "fill:none" in series.group(1)
    assert "stroke:var(--rg-sage" in series.group(1)


def test_the_cash_figure_still_leads_the_page() -> None:
    # The queue sits under the hero, never above it: an owner opens this screen
    # to see a number first.
    body = _get(app_with_two_tenants(), "/t/acme").body
    assert body.index("Cash today") < body.index("What needs you")
