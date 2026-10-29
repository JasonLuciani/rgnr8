"""Modules off by default, and a sidebar that works on a phone (issues 31, 33).

Every business saw every screen (twenty-two nav items for a service business
that uses a handful). Optional modules are now per-business switches, off until
turned on, and switching one off only takes it out of the sidebar. On a phone,
a CSS rule hid the whole nav, so "Menu" opened onto nothing.
"""

from __future__ import annotations

import re

from test_books import DEFAULT_ROUTES, FakeTransport, _inputs

from rgnr8_forecast import ForecastConfig, Money
from rgnr8_web import LedgerClient, Request, WebApp
from rgnr8_web.modules import OPTIONAL_MODULES
from rgnr8_web.shell import _MENU_MEDIA, _SHELL_CSS, MENU_BREAKPOINT_PX
from rgnr8_web.store import InMemoryTenantStore

OWNER = {"authorization": "Bearer tok"}


def _app(store: "InMemoryTenantStore | None" = None) -> WebApp:
    app = WebApp(store=store)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("1000.00")), token="tok")
    routes = dict(DEFAULT_ROUTES)
    routes["GET /t/acme/settings"] = (200, {"settings": {"base_currency": "USD"}})
    app.set_ledger(LedgerClient(FakeTransport(routes), token="svc"))
    return app


def _nav(app: WebApp, path: str = "/t/acme/team") -> list[str]:
    body = app.handle(Request("GET", path, OWNER, "")).body
    nav = body[body.index('<nav class="rg-nav"'):body.index("</nav>", body.index('<nav class="rg-nav"'))]
    return re.findall(r">([^<>]+?)(?:<span[^>]*>[^<]*</span>)?</a>", nav)


def test_optional_modules_are_off_until_a_business_switches_them_on() -> None:
    labels = _nav(_app())
    for label, _ in OPTIONAL_MODULES.values():
        assert label not in labels, label
    assert {"Invoices", "Bills", "Ledger", "Payroll"} <= set(labels)
    assert len(labels) <= 16


def test_settings_switches_a_module_on_and_ignores_unknown_ones() -> None:
    app = _app()
    page = app.handle(Request("GET", "/t/acme/settings", OWNER, "")).body
    assert "Modules" in page and 'name="module" value="debt"' in page
    r = app.handle(Request("POST", "/t/acme/settings/modules",
                           {**OWNER, "content-type": "application/x-www-form-urlencoded"},
                           "module=debt&module=nonsense"))
    assert r.status == 302
    assert app._store.load("acme").modules == ["debt"]
    assert "Loans" in _nav(app) and "Estimates" not in _nav(app)


def test_switching_a_module_off_keeps_its_pages() -> None:
    app = _app()
    assert app.handle(Request("GET", "/t/acme/estimates", OWNER, "")).status == 200


def test_a_switch_made_on_one_worker_shows_on_the_other() -> None:
    store = InMemoryTenantStore()
    a, b = _app(store), _app(store)
    assert "Loans" not in _nav(b)
    a.handle(Request("POST", "/t/acme/settings/modules",
                     {**OWNER, "content-type": "application/x-www-form-urlencoded"}, "module=debt"))
    assert "Loans" in _nav(b)


def test_the_phone_menu_opens_onto_the_nav() -> None:
    # The rule that hid the nav below 640px is gone; touch targets are 44px.
    assert ".rg-nav{display:none}" not in _SHELL_CSS.replace(" ", "")
    assert "min-height:44px" in _SHELL_CSS
    body = _app().handle(Request("GET", "/t/acme/team", OWNER, "")).body
    # Open without script (every link reachable); closed by script on a phone.
    assert '<details class="rg-navwrap" open>' in body
    assert f"matchMedia('{_MENU_MEDIA}')" in body and "removeAttribute('open')" in body


def test_the_sidebar_becomes_the_menu_below_1024px_everywhere_at_once() -> None:
    # It folded only below 860px, which left a tablet (or a narrow laptop
    # window) with a 200px sidebar squeezing the page. The stylesheet's two
    # rules and the script that closes the menu on load use one width, so a
    # window can't be a phone to the CSS and a desktop to the script.
    assert MENU_BREAKPOINT_PX == 1024
    assert _MENU_MEDIA == "(max-width:1023.98px)"
    css = re.sub(r"\s+", "", _SHELL_CSS)
    # The menu layout, and the 44px targets that go with it.
    assert f"@media{_MENU_MEDIA}{{.rg-layout{{display:block}}" in css
    assert f"@media{_MENU_MEDIA},(pointer:coarse){{.rg-nav" in css
    body = _app().handle(Request("GET", "/t/acme/team", OWNER, "")).body
    assert f"matchMedia('{_MENU_MEDIA}')" in body
    assert "860px" not in body and "860px" not in _SHELL_CSS
    # A phone still never scrolls sideways: wide tables scroll inside themselves.
    assert ".shell table{display:block;max-width:100%;overflow-x:auto}" in _SHELL_CSS
