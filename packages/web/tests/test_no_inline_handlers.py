"""Every control that promises to do something has to be able to.

The app serves a nonce-only Content-Security-Policy (`script-src 'self'
'nonce-…'`). Under it a browser refuses *inline attribute* handlers outright —
`onclick=`, `onchange=`, `onsubmit=` — because `script-src-attr` falls back to
`script-src` and an attribute cannot carry a nonce. Every such handler in the
product was therefore dead in a real browser, while every test passed: nothing
in the suite enforces a CSP. An outside review found it from the outside —
"a CFO cash question produced no visible response", "a $1,000 expense scenario
produced no result" — and the same cause also killed seal & publish, the bank
feed's categorize dropdown, and every control on the team page, including the
only way to invite a second person.

These tests read the rendered pages the way the browser does:

* no inline event-handler attribute anywhere, on any page;
* the delegated dispatcher is present and carries this request's nonce;
* every `data-action` / `data-change` / `data-submit` names a function the
  page actually defines, so a renamed handler cannot fail silently either.
"""

from __future__ import annotations

import re

from factory import app_with_two_tenants

from rgnr8_web import Request

_HANDLER_ATTR = re.compile(r"<[^>]*\son[a-z]+\s*=", re.I)
_NONCE = re.compile(r"script-src 'self' 'nonce-([^']+)'")
_NAMED = re.compile(r'data-(?:action|change|submit)\s*=\s*["\']([A-Za-z_]\w*)["\']')
_BUILTIN = {"selectAll"}

ROUTES = [
    "/t/acme",
    "/t/acme/briefing",
    "/t/acme/scenarios",
    "/t/acme/close",
    "/t/acme/team",
    "/t/acme/transactions",
    "/t/acme/estimates",
    "/t/acme/receivables",
    "/t/acme/reports",
    "/t/acme/health",
    "/t/acme/connect",
    "/t/acme/settings",
]


def _get(app, path: str):
    return app.handle(Request("GET", path, {"authorization": "Bearer tok-acme"}, ""))


def _pages():
    app = app_with_two_tenants()
    for route in ROUTES:
        res = _get(app, route)
        if res.status == 200 and "<html" in res.body:
            yield route, res


def test_the_routes_under_test_actually_render() -> None:
    # Guard the guard: a test over pages that all 404 would pass vacuously.
    assert len(list(_pages())) >= 8


def test_no_page_carries_an_inline_event_handler() -> None:
    for route, res in _pages():
        hit = _HANDLER_ATTR.search(res.body)
        assert hit is None, f"{route}: inline handler the CSP will block: {hit.group(0)[:120]!r}"


def test_the_dispatcher_runs_under_the_pages_own_nonce() -> None:
    # The CSP header itself is added by the WSGI middleware from the same
    # per-request contextvar, so `csp_value()` read straight after `handle()`
    # is exactly the header this response would carry.
    from rgnr8_web.csp import csp_value

    app = app_with_two_tenants()
    seen = 0
    for route in ROUTES:
        res = _get(app, route)
        if res.status != 200 or "<html" not in res.body:
            continue
        m = _NONCE.search(csp_value())
        assert m, f"{route}: no nonce in the CSP"
        assert f'<script nonce="{m.group(1)}">(function(){{function call(' in res.body, (
            f"{route}: the delegated dispatcher is missing or carries the wrong nonce")
        seen += 1
    assert seen >= 8


def test_destructive_forms_ask_before_they_post() -> None:
    """Dispose, clear owner data and remove webhook used `onsubmit="return
    confirm(…)"`. The CSP blocked the guard and not the form, so each one
    posted immediately without asking."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "rgnr8_web"
    for name in ("asset_screens.py", "settings_screens.py", "integration_screens.py"):
        text = (src / name).read_text(encoding="utf-8")
        assert "data-confirm=" in text, name
        assert "return confirm(" not in text, name


def test_every_named_handler_is_defined_on_its_page() -> None:
    for route, res in _pages():
        for name in set(_NAMED.findall(res.body)) - _BUILTIN:
            assert re.search(rf"\bfunction\s+{name}\s*\(", res.body), (
                f"{route}: a control calls {name}(), which the page never defines")


def test_the_empty_cash_page_links_somewhere_real() -> None:
    """"Connect your bank" pointed at `#connect`, an anchor on no page."""
    from rgnr8_forecast import ForecastConfig
    from factory import _inputs, usd

    from rgnr8_web import WebApp

    app = WebApp()
    app.add_tenant("acme", "Acme Co", _inputs("0.00").__class__(opening=_inputs("0.00").opening),
                   ForecastConfig(minimum_cash=usd("10000.00")), token="tok-acme")
    body = _get(app, "/t/acme").body
    assert 'href="#connect"' not in body
    assert 'href="/t/acme/connect"' in body
