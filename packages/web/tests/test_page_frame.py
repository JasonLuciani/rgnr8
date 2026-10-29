"""One page frame across every screen (issues 19-24).

A contract over the whole nav, not a test per screen: a new screen that writes
its own title, puts a banner above it, or strings its sub-pages together with
" · " fails here without anyone having to remember to add a test for it.
"""

from __future__ import annotations

import re
import pytest
from test_books import DEFAULT_ROUTES, FakeTransport, _inputs

from rgnr8_forecast import ForecastConfig, Money
from rgnr8_web import LedgerClient, Request, WebApp
from rgnr8_web.page_frame import TABS, frame
from rgnr8_web.shell import _NAV_GROUPS, nav_label

NAV = [(item.suffix, item.label) for group in _NAV_GROUPS for item in group.items]


def _app() -> WebApp:
    app = WebApp()
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("1000.00")), token="tok")
    app.set_ledger(LedgerClient(FakeTransport(dict(DEFAULT_ROUTES)), token="svc"))
    return app


def _page(app: WebApp, path: str) -> str:
    r = app.handle(Request("GET", path, {"authorization": "Bearer tok"}, ""))
    if r.status == 302:  # e.g. Bank transactions -> the review queue
        loc = dict(r.extra_headers)["Location"]
        r = app.handle(Request("GET", loc, {"authorization": "Bearer tok"}, ""))
    assert r.status == 200, (path, r.status)
    body = r.body
    return body[body.index('<main class="shell">'):]


@pytest.mark.parametrize(("suffix", "label"), NAV, ids=[s or "home" for s, _ in NAV])
def test_every_screen_is_framed_the_same_way(suffix: str, label: str) -> None:
    main = _page(_app(), f"/t/acme/{suffix}".rstrip("/"))
    h1s = re.findall(r"<h1\b[^>]*>(.*?)</h1>", main, re.S)
    # One title, and it is what the nav calls the screen (issues 19 and 23).
    assert h1s == [label.replace("&", "&amp;")], h1s
    head = main.index('<div class="page-head">')
    # Nothing but the frame above the title: no banner, no legend (issue 21).
    assert "banner" not in main[:head] and "rg-prov-legend" not in main[:head]
    # The legend, where a screen has one, is at the foot.
    if "rg-prov-legend" in main:
        assert main.index("rg-prov-legend") > main.index('<footer class="page-foot">')
    # Sub-pages are tabs, not a run-on line of links (issue 20). (Download formats
    # in a row, "PDF · Excel · CSV", are choices, not sub-pages.)
    assert ' · <a class="btn-link"' not in main


def test_sub_pages_are_tabs_with_the_current_one_marked() -> None:
    app = _app()
    main = _page(app, "/t/acme/books/statements")
    tabs = re.search(r'<nav class="rg-tabs"[^>]*>(.*?)</nav>', main, re.S)
    assert tabs is not None
    assert [re.sub(r"<[^>]+>", "", a) for a in re.findall(r"<a [^>]*>.*?</a>", tabs.group(1))] == [
        t.label for t in TABS["books"]]
    assert re.search(r'aria-current="page">Statements<', tabs.group(1))
    assert re.findall(r"<h1>(.*?)</h1>", main) == ["Ledger"]


def test_a_query_string_tab_is_marked_by_its_query() -> None:
    main = _page(_app(), "/t/acme/inbox?status=EXCLUDED")
    assert re.search(r'aria-current="page">Excluded<', main)
    assert not re.search(r'aria-current="page">For review<', main)


# --- the frame itself, on markup the renderers write --------------------------


def test_a_page_deeper_than_its_section_keeps_its_own_title_under_the_section() -> None:
    out = frame("<h1>Business Checking</h1><p>rows</p>", title="Ledger", tenant="acme",
                section="books", here="books/accounts/1000/extra/register")
    # "books/accounts/..." sits under the Chart of accounts tab, so it is a tab page;
    # one with no tab of its own keeps its own title, with the section above it.
    deep = frame("<h1>Job J-1</h1>", title="Projects", tenant="acme", section="jobs", here="jobs/J-1")
    assert '<div class="rg-eyebrow">Projects</div><h1>Job J-1</h1>' in deep
    assert "<h1>Ledger</h1>" in out


def test_banners_written_above_the_title_move_below_it() -> None:
    out = frame('<div class="banner warn">Heads up <div>nested</div></div>'
                '<h1>Old title</h1><p class="sub">Sub line</p><p>body</p>',
                title="Invoices", tenant="acme", section="invoices", here="invoices")
    assert out.index("<h1>Invoices</h1>") < out.index("Heads up") < out.index("<p>body</p>")
    assert '<p class="sub">Sub line</p>' in out and "Old title" not in out


def test_a_screen_outside_the_nav_keeps_its_own_title() -> None:
    assert nav_label("costs") == ""
    out = frame("<h1>Running costs</h1>", title="", tenant="acme", section="costs", here="costs")
    assert "<h1>Running costs</h1>" in out


def test_actions_in_a_page_head_stay_top_right() -> None:
    head = ('<div class="page-head"><div class="t"><h1>X</h1></div>'
            '<div class="acts"><a class="btn" href="/new">New invoice</a></div></div><p>body</p>')
    out = frame(head, title="Invoices", tenant="acme", section="invoices", here="invoices")
    assert '<div class="acts"><a class="btn" href="/new">New invoice</a></div></div>' in out



# --- from the independent review ----------------------------------------------


def test_a_screen_without_a_title_keeps_its_banners_inside_their_cards() -> None:
    body = ('<section class="card"><h2>Balance sheet</h2>'
            '<div class="banner warn">Balance sheet does not balance</div><table></table></section>')
    out = frame(body, title="Ledger", tenant="acme", section="books", here="books/statements")
    card = out.index('<section class="card">')
    assert card < out.index("does not balance") < out.index("</section>")


def test_a_title_with_an_ampersand_is_escaped_once() -> None:
    out = frame('<div class="rg-eyebrow">Report</div><h1>AR Aging &amp; Collections</h1>',
                title="Reports", tenant="acme", section="reports", here="reports/ar_aging")
    assert "<h1>AR Aging &amp; Collections</h1>" in out and "&amp;amp;" not in out
    # One eyebrow, the section's: the report's own "Report" went with its title.
    assert re.findall(r'<div class="rg-eyebrow">(.*?)</div>', out) == ["Reports"]


def test_the_report_pages_render_with_one_title_and_one_eyebrow() -> None:
    main = _page(_app(), "/t/acme/reports/ar_aging")
    assert re.findall(r"<h1>(.*?)</h1>", main) == ["AR Aging &amp; Collections"]
    assert re.findall(r'<div class="rg-eyebrow">(.*?)</div>', main) == ["Reports"]
