"""One page frame across every screen (issues 19-24).

A contract over the whole nav, not a test per screen: a new screen that writes
its own title, puts a banner above it, or strings its sub-pages together with
" · " fails here without anyone having to remember to add a test for it.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterator
from html.parser import HTMLParser

import pytest
from test_books import DEFAULT_ROUTES, FakeTransport, _inputs
from test_books_e2e import _app as e2e_app
from test_books_e2e import _req as e2e_req
from test_books_e2e import _seed as seed
from test_books_e2e import ledger_service  # noqa: F401 - the real ledger service, as a fixture

from rgnr8_copilot import FakeLLM
from rgnr8_forecast import ForecastConfig, Money
from rgnr8_web import LedgerClient, Request, WebApp
from rgnr8_web.components import card, metric
from rgnr8_web.modules import OPTIONAL_MODULES
from rgnr8_web.page_frame import TABS, frame
from rgnr8_web.provenance_labels import Provenance, badge, legend
from rgnr8_web.shell import _NAV_GROUPS, nav_label

NAV = [(item.suffix, item.label) for group in _NAV_GROUPS for item in group.items]


def _app() -> WebApp:
    app = WebApp(ask_llm=FakeLLM())  # Ask is only a screen when it is switched on
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
    # Every card is laid out the same way (issues 22 and 24).
    _check_cards(main, where=suffix or "home")


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


# --- one card layout everywhere (issues 22 and 24) ----------------------------
#
# A card is [title + provenance label] / content / [actions, bottom-left]. Two
# rules are checked on the rendered pages, so a new card that drifts fails here:
#
# * issue 22: no card puts its action in its header, or pushes it to the right
#   (float, margin-left:auto, a space-between row). Actions inside a table row
#   act on that row, not the card, and stay where the row puts them.
# * issue 24: a provenance label marks a whole card, in its header; it is never
#   on one figure inside the card. (The legend at the foot explains the labels
#   and is the one other place they appear.) On a screen whose figures come
#   out of the books, every card that shows an amount carries one.

_VOID = {"area", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
_RIGHT = ("float:right", "margin-left:auto", "justify-content:space-between",
          "justify-content:flex-end", "text-align:right")
_MONEY = re.compile(r"\$\s?[\d,]+\.\d\d")


class _El:
    def __init__(self, tag: str, attrs: dict[str, str], parent: "_El | None") -> None:
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.kids: list[_El] = []
        self.text: list[str] = []

    @property
    def cls(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def walk(self) -> Iterator["_El"]:
        yield self
        for k in self.kids:
            yield from k.walk()

    def ancestors(self, stop: "_El | None" = None) -> Iterator["_El"]:
        a = self.parent
        while a is not None and a is not stop:
            yield a
            a = a.parent

    def words(self) -> str:
        """The text a reader sees, not counting what is typed into a form."""
        if self.tag in ("script", "style", "select", "textarea", "option"):
            return ""
        return " ".join(self.text + [k.words() for k in self.kids])


class _Tree(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = self.cur = _El("root", {}, None)
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, "str | None"]]) -> None:
        el = _El(tag, {k: v or "" for k, v in attrs}, self.cur)
        self.cur.kids.append(el)
        if tag not in _VOID:
            self.cur = el

    def handle_endtag(self, tag: str) -> None:
        el: "_El | None" = self.cur
        while el is not None and el.tag != tag:
            el = el.parent
        if el is not None and el.parent is not None:
            self.cur = el.parent

    def handle_data(self, data: str) -> None:
        self.cur.text.append(data)


def _is_card(el: _El) -> bool:
    return "card" in el.cls


def _is_action(el: _El) -> bool:
    return (el.tag == "button" or (el.tag == "input" and el.attrs.get("type") == "submit")
            or (el.tag == "a" and ("btn" in el.cls or "btn-link" in el.cls)))


def _is_badge(el: _El) -> bool:
    return "rg-prov" in el.cls


def _header(c: _El) -> "_El | None":
    """A card's header: the shared `.card-head`, or a hand-written first child
    that is, or holds, the card's title."""
    first = c.kids[0] if c.kids else None
    if first is None:
        return None
    if "card-head" in first.cls or first.tag in ("h2", "h3"):
        return first
    return first if any(k.tag in ("h2", "h3") for k in first.kids) else None


def _check_cards(html: str, *, where: str, ledger_backed: bool = False) -> None:
    root = _Tree(html).root
    for el in root.walk():
        # A label sits in its card's header, and nowhere else but the legend.
        if _is_badge(el) and not any("rg-prov-legend" in a.cls for a in el.ancestors()):
            owner = next((a for a in el.ancestors() if _is_card(a)), None)
            assert owner is not None and el.parent is _header(owner), (
                f"{where}: a provenance label on one figure rather than on its card's "
                f"header: {el.words().strip()!r} inside <{el.parent.tag if el.parent else '?'}>")
    cards = [el for el in root.walk()
             if _is_card(el) and not any(_is_card(a) for a in el.ancestors())]
    for c in cards:
        head = _header(c)
        name = " ".join((head if head is not None else c).words().split()[:8])
        if head is not None:
            acts = [a.words().strip() for a in head.walk() if _is_action(a)]
            assert not acts, f"{where}: card {name!r} has {acts} in its header"
            assert sum(1 for a in head.walk() if _is_badge(a)) <= 1, f"{where}: {name!r}"
        for a in c.walk():
            if not _is_action(a) or any(p.tag == "table" for p in a.ancestors(stop=c)):
                continue
            pushed = [p.tag for p in a.ancestors(stop=c)
                      if any(r in (p.attrs.get("style") or "").replace(" ", "") for r in _RIGHT)]
            assert not pushed, (f"{where}: card {name!r} pushes {a.words().strip()!r} "
                                "to the right; a card's actions sit bottom-left")
        # A card labelled as recorded fact can't also carry a verdict about
        # the future: a status chip outside a table (where one describes a
        # record, "Open", "Paid") is the outlook's call, and belongs on a card
        # labelled Forecast.
        level = next((b.cls[1] for b in (head.kids if head is not None else []) if _is_badge(b)),
                     "")
        if level in ("rg-prov-posted", "rg-prov-reconciled", "rg-prov-sealed"):
            verdicts = [v.words().strip() for v in c.walk() if "chip-status" in v.cls
                        and not any(p.tag == "table" for p in v.ancestors(stop=c))]
            assert not verdicts, (f"{where}: card {name!r} is labelled {level[8:]} but carries "
                                  f"the outlook's verdict {verdicts}")
        if ledger_backed and _MONEY.search(c.words()):
            assert head is not None and any(_is_badge(b) for b in head.kids), (
                f"{where}: card {name!r} shows figures from the books but its header "
                "doesn't say where they came from")


def test_a_card_built_by_the_shared_helper_has_its_label_on_top_and_actions_last() -> None:
    html = card("Invoices", "<table><tr><td>$1.00</td></tr></table>",
                actions='<a class="btn" href="/new">New invoice</a>',
                provenance=Provenance.POSTED)
    assert (html.index("card-head") < html.index("Posted") < html.index("<table>")
            < html.index("New invoice"))
    _check_cards(html, where="helper", ledger_backed=True)


def test_the_contract_catches_an_action_in_a_card_header_or_pushed_right() -> None:
    in_head = ('<section class="card"><div style="display:flex;justify-content:space-between">'
               '<h2>Jobs</h2><a class="btn" href="/jobs/new">New job</a></div><p>x</p></section>')
    with pytest.raises(AssertionError, match="in its header"):
        _check_cards(in_head, where="t")
    pushed = ('<div class="card"><div class="row" style="justify-content:space-between">'
              "<strong>Seal</strong><button>Seal</button></div></div>")
    with pytest.raises(AssertionError, match="to the right"):
        _check_cards(pushed, where="t")


def test_the_contract_catches_a_label_on_a_figure_and_a_card_without_one() -> None:
    on_figure = ('<section class="card"><div class="card-head"><h2>Cash</h2></div>'
                 f'<div class="metric"><div class="k">Cash today {badge(Provenance.POSTED)}</div>'
                 '<div class="v">$1.00</div></div></section>')
    with pytest.raises(AssertionError, match="one figure"):
        _check_cards(on_figure, where="t")
    unlabelled = '<section class="card"><div class="card-head"><h2>TB</h2></div><p>$5.00</p></section>'
    with pytest.raises(AssertionError, match="doesn't say where"):
        _check_cards(unlabelled, where="t", ledger_backed=True)


def test_a_figure_cannot_carry_a_label_of_its_own() -> None:
    assert "provenance" not in inspect.signature(metric).parameters


# --- the same contract on real books, with real figures on the cards ----------

# Screens (and tabs) whose figures come out of the ledger: each card on them
# that shows an amount says, in its header, where the amount came from.
_LEDGER_SCREENS = (
    "invoices", "bills", "payroll", "debt", "assets", "jobs", "estimates", "pipeline",
    "health", "inbox", "books", "books/trial-balance", "books/statements", "books/gl",
    "books/budget", "books/reconcile", "books/dimensions", "books/recurring", "books/split",
    "books/wip", "books/1099", "books/accounts", "books/accounts/1000", "inventory",
    "receivables/aging", "payables/aging", "inbox/rules",
)
# No "ask": this app has no AI provider, so Ask is a 404 here; the stub sweep
# above covers it switched on.
_OTHER_SCREENS = ("", "briefing", "receivables", "capture", "close", "packages",
                  "reports", "scenarios", "connect", "team", "settings", "integrations",
                  "audit", "costs", "transactions")


def test_every_card_follows_the_layout_with_real_books(ledger_service: str) -> None:
    app = e2e_app(ledger_service, ["cardco"])
    seed(ledger_service, "cardco", "PROFESSIONAL_SERVICES")

    def go(path: str, method: str = "GET", body: str = "") -> str:
        r = e2e_req(app, "cardco", f"/t/cardco/{path}".rstrip("/"), method, body)
        if method == "POST":
            assert r.status == 302 and "err=" not in str(dict(r.extra_headers)), (path, r.body)
            return ""
        if r.status == 302:
            r = e2e_req(app, "cardco", dict(r.extra_headers)["Location"])
        assert r.status == 200, (path, r.status)
        body = str(r.body)
        return body[body.index('<main class="shell">'):]

    go("settings/modules", "POST", "&".join(f"module={m}" for m in OPTIONAL_MODULES))
    go("books/entries", "POST",
       "date=2026-08-05&memo=Cash+sale&code1=1000&debit1=5000&code2=4000&credit2=5000")
    go("customers", "POST", "name=Northwind+Ltd&terms_days=30")
    go("invoices", "POST", "id=INV-1&party_id=northwind-ltd&date=2026-08-01"
                           "&desc1=Consulting&amount1=3500&code1=4100")
    go("vendors", "POST", "name=Copyshop&terms_days=30")
    go("bills", "POST", "id=BILL-1&party_id=copyshop&date=2026-08-02"
                        "&desc1=Brochures&amount1=360&code1=6400")
    go("payroll/employees", "POST", "name=Ada+Reyes")
    go("payroll", "POST", "id=PR-1&date=2026-08-15&employer_taxes=100.00"
                          "&employee1=ada-reyes&gross1=2000.00&taxes1=300.00")

    for path in _LEDGER_SCREENS + _OTHER_SCREENS:
        _check_cards(go(path), where=path or "home", ledger_backed=path in _LEDGER_SCREENS)


# --- cards the screen sweep above can't reach with its data -------------------


def _close(sealed: bool = False) -> str:
    from rgnr8_web.screens import CloseBoard, CloseTask, render_close_body
    board = CloseBoard("2026-08", (CloseTask("feeds", "Feeds", "done"),), sealed=sealed)
    return render_close_body(board, "acme", can_manage=True, can_publish=True)


def _connect() -> str:
    from rgnr8_web.screens import render_connect_page
    return render_connect_page(
        "acme", configured=True, status="connected", realm_id="R42",
        last_sync={"company": "Sandbox & Co", "cash": "$2,001.50", "bank_accounts": "2",
                   "ar_total": "$150.00", "invoice_count": "1", "ap_total": "$400.00",
                   "bill_count": "1"})


def _open_items() -> str:
    from rgnr8_web.arap_screens import render_open_items
    rows = ({"number": "1001", "party": "Amy", "date": "2026-07-01", "due_date": "2026-08-01",
             "open_minor": "15000"},)
    return render_open_items("invoices", rows, as_of="2026-08-31", total_minor=15000,
                             books_minor=15000)


def _drafted_bill() -> str:
    from rgnr8_web.capture_screens import render_capture
    return render_capture("acme", draft={"vendor": "Copyshop", "total_minor": "3600",
                                         "tax_minor": "0", "date": "2026-08-02",
                                         "confidence": 950})


@pytest.mark.parametrize("render", [_close, _connect, _open_items, _drafted_bill],
                         ids=["close", "connect", "quickbooks-open-items", "drafted-bill"])
def test_these_cards_follow_the_layout_too(render: "Callable[[], str]") -> None:
    _check_cards(render(), where=render.__name__)


def test_the_seal_button_comes_after_what_it_seals() -> None:
    html = _close()
    seal = html.index("Seal &amp; publish package")
    assert html.index("Seal the period") < html.index("immutable financial package") < seal
    assert '<div class="card-acts"><button id="sealBtn"' in html


def test_quickbooks_figures_are_labelled_once_per_card_not_once_per_row() -> None:
    html = _open_items()
    assert html.count("rg-prov-imported") == 1
    assert re.search(r'<div class="card-head"><h2>Open in QuickBooks \(1\)</h2>'
                     r'<span class="rg-prov rg-prov-imported"', html)
    last_refresh = _connect()
    assert re.search(r"<h2>Last refresh — Sandbox &amp; Co</h2><span class=\"rg-prov "
                     r"rg-prov-imported\"", last_refresh)


def test_the_home_cash_card_is_labelled_in_its_header_and_the_projection_is_a_forecast() -> None:
    main = _page(_app(), "/t/acme")
    _check_cards(main, where="home")
    hero = main[main.index('<h2>Cash today</h2>'):]
    # The books are wired (a fake ledger), so cash today says where it came from
    # in the card's header, and the 13-week card says it is a projection.
    assert hero.index("rg-prov") < hero.index('class="metric hero"')
    chart = main[main.index("<h2>Next 13 weeks — projected cash</h2>"):]
    assert chart.index("rg-prov-forecast") < chart.index("cash-chart-summary")


def _cards_by_title(main: str) -> dict[str, _El]:
    root = _Tree(main).root
    out = {}
    for el in root.walk():
        head = _header(el) if _is_card(el) else None
        if head is not None:
            out[" ".join(next(k for k in head.walk() if k.tag == "h2").words().split())] = el
    return out


@pytest.mark.parametrize("books", [True, False], ids=["books-wired", "no-books"])
def test_cash_today_holds_only_the_cash_figure_and_the_verdict_leads_the_outlook(
        books: bool) -> None:
    # The Posted card used to carry the status chip and the floor verdict too,
    # which are forecasts; and the low point was stated twice.
    app = _app()
    if not books:
        app = WebApp()
        app.add_tenant("acme", "Acme Co", _inputs(),
                       ForecastConfig(minimum_cash=Money.from_decimal("1000.00")), token="tok")
    main = _page(app, "/t/acme")
    _check_cards(main, where="home")
    cards = _cards_by_title(main)
    hero, outlook = cards["Cash today"], cards["Next 13 weeks — projected cash"]
    assert hero.words().split() == ["Cash", "today"] + (["Posted"] if books else []) + ["$3,000.00"]
    assert not any("chip-status" in el.cls for el in hero.walk())
    # Without books there is nothing to tell apart, so no label on either card.
    assert ("rg-prov" in main) is books
    # The 13-week card sits directly under it, and opens with the status and
    # the one sentence that states the low point and the floor.
    order = list(cards)
    assert order.index("Next 13 weeks — projected cash") == order.index("Cash today") + 1
    lead = next(el for el in outlook.walk() if "cash-verdict" in el.cls)
    assert "chip-status" in lead.kids[0].cls
    assert lead.kids[1].attrs.get("id") == "cash-chart-summary"
    assert lead.kids[1].words().startswith("Cash starts at $3,000.00 today")
    # (This tenant has no costs, so the sentence says so rather than claiming
    # the floor holds; test_monday_answer covers an outlook with costs.)
    assert main.count("lowest point") <= 1 and "Low point is" not in main
    assert "Minimum-cash floor" not in main


def test_every_label_on_a_page_is_explained_at_its_foot() -> None:
    body = card("Trial balance", "<p>$5.00</p>", provenance=Provenance.POSTED)
    out = frame(body, title="Ledger", tenant="acme", section="books", here="books/trial-balance")
    foot = out[out.index('<footer class="page-foot">'):]
    assert "rg-prov-legend" in foot and "Posted" in foot
    # A legend the screen drew is kept, and gains any level it left out.
    body = legend((Provenance.SEALED,)) + body
    out = frame(body, title="Ledger", tenant="acme", section="books", here="books/trial-balance")
    foot = out[out.index('<footer class="page-foot">'):]
    assert "Sealed" in foot and "Posted" in foot and out.count("rg-prov-legend") == 1
