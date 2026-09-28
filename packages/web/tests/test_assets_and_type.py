"""The self-hosted typeface, and the static route that serves it.

Two things are being protected here. The first is the promise this app has made
since the beginning — **no external assets** — which a font is the most tempting
thing to break. The second is the static route itself: serving files by name is
the classic way to hand out a filesystem, so the tests below try to walk out of
it and assert that there is nothing to walk.
"""

from __future__ import annotations

import re
from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_forecast.brand import RG_BASE_CSS, RG_TOKENS_CSS
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
)
from rgnr8_web.assets import ASSET_PREFIX, ASSETS, asset_url, font_face_css

SECRET = "assets-secret"
NOW = 1_760_000_000


def _app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("owner@acme.com", "owner@acme.com"))
    users.set_membership("owner@acme.com", "acme", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW)
    app.add_tenant("acme", "Acme Co",
                   ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                                       available=Money.from_decimal("1000.00"))),
                   ForecastConfig(minimum_cash=Money.from_decimal("100.00")), token="unused")
    return app


def _get(app: WebApp, path: str):
    return app.handle(Request("GET", path))


# --- the font is real, and ours ---------------------------------------------


def test_the_shipped_files_are_actually_woff2() -> None:
    # A truncated or HTML-error-page download would still be bytes on disk and
    # would still serve with a 200; the browser would just silently fall back.
    assert ASSETS, "no assets loaded"
    for name, asset in ASSETS.items():
        assert name.endswith(".woff2")
        assert asset.body[:4] == b"wOF2", f"{name} is not a woff2"
        # woff2 declares its own total length at offset 8; a partial download
        # fails this even when the signature survived.
        declared = int.from_bytes(asset.body[8:12], "big")
        assert declared == len(asset.body), f"{name} is truncated"


def test_urls_are_content_addressed() -> None:
    # The hash is what makes a one-year immutable cache safe: different bytes
    # can never appear at the same URL.
    url = asset_url("inter-latin")
    assert url.startswith(f"{ASSET_PREFIX}inter-latin.")
    assert url.endswith(".woff2")
    digest = url.rsplit(".", 2)[1]
    assert len(digest) == 12 and all(c in "0123456789abcdef" for c in digest)


# --- the route ---------------------------------------------------------------


def test_the_font_is_served_with_an_immutable_cache() -> None:
    app = _app()
    r = _get(app, asset_url("inter-latin"))
    assert r.status == 200
    assert r.content_type == "font/woff2"
    assert r.body_bytes()[:4] == b"wOF2"
    cache = r.headers.get("Cache-Control", "")
    assert "immutable" in cache and "max-age=31536000" in cache


def test_the_font_loads_without_a_session() -> None:
    # It has to: the sign-in page is the first thing anyone sees, and it is
    # rendered before there is any session to authenticate.
    assert _get(_app(), asset_url("inter-latin")).status == 200


def test_an_unknown_asset_is_a_plain_404() -> None:
    app = _app()
    for path in (f"{ASSET_PREFIX}nope.woff2", f"{ASSET_PREFIX}", f"{ASSET_PREFIX}inter-latin.woff2"):
        assert _get(app, path).status == 404, path


def test_the_route_cannot_be_walked_out_of() -> None:
    # There is no path joining behind this route — the served set is a dict
    # built at import from a fixed tuple — so none of these can resolve to
    # anything. The test exists so that stays true if the route is ever rewritten.
    app = _app()
    for probe in (
        "../app.py",
        "../../../../etc/passwd",
        "..%2Fapp.py",
        "....//app.py",
        "inter-latin.woff2/../../assets.py",
        "/etc/passwd",
    ):
        r = _get(app, f"{ASSET_PREFIX}{probe}")
        assert r.status == 404, probe
        assert b"wOF2" not in r.body_bytes()


# --- one typeface, and it stays ours ----------------------------------------


def test_pages_reference_the_font_and_still_ship_no_external_assets() -> None:
    app = _app()
    for path in ("/login", "/legal/privacy"):
        body = _get(app, path).body
        assert "@font-face" in body
        assert ASSET_PREFIX in body
        # The whole reason a CDN was off the table.
        assert "http://" not in body and "https://" not in body


def _declarations(css: str) -> str:
    """The CSS with /* comments */ removed — the part a browser acts on.

    Worth the four lines: these files explain themselves at length, and a naive
    substring check flags the comment that says why the serif went away.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def test_the_georgia_mixture_is_gone() -> None:
    # The audit's typography finding: headings in Georgia while the UI sat in
    # system-ui made one product look like two.
    live = _declarations(RG_TOKENS_CSS) + _declarations(RG_BASE_CSS)
    # No *face* named here may be a serif. (`--rg-serif` is a token NAME, kept
    # as an alias below — what matters is that nothing resolves to a serif.)
    for face in ("Georgia", "ui-serif", "Times New Roman", "Cambria", "Charter"):
        assert face not in live, f"{face} is still named in the brand CSS"
    assert "Inter" in RG_TOKENS_CSS
    # `--rg-serif` survives only as an alias for consumers outside this repo's
    # Python tree; it must never resolve to an actual serif again.
    assert "--rg-serif:var(--rg-sans)" in RG_TOKENS_CSS


def test_no_screen_still_asks_for_a_serif_face() -> None:
    from rgnr8_web import asset_screens, debt_screens, health_screens, screens

    for mod in (asset_screens, debt_screens, health_screens, screens):
        src = open(mod.__file__, encoding="utf-8").read()
        assert "--rg-serif" not in src, f"{mod.__name__} still sets type in the serif token"


def test_figures_are_tabular_by_default_and_prose_is_not() -> None:
    # Columns of money only line up with tabular figures; running prose reads
    # better without them.
    assert "font-variant-numeric:var(--rg-nums)" in RG_BASE_CSS
    assert "--rg-nums:tabular-nums" in RG_TOKENS_CSS
    assert "p,li{font-variant-numeric:normal}" in RG_BASE_CSS


def test_the_wordmark_is_a_link_that_does_not_look_like_one() -> None:
    # Making the lockup a link home is useful; inheriting the browser's default
    # underline and #0000EE on a dark forest bar is not. Caught by looking at a
    # rendered page, which is the only way this class of bug ever shows up.
    assert ".rg-lockup{" in RG_BASE_CSS
    lockup = RG_BASE_CSS.split(".rg-lockup{", 1)[1].split("}", 1)[0]
    assert "text-decoration:none" in lockup
    assert "color:inherit" in lockup


def test_the_font_is_not_render_blocking() -> None:
    # A financial screen that renders blank while a font downloads is worse
    # than one that reflows.
    assert "font-display:swap" in font_face_css()
    # Two subsets, so a page with no accented characters never fetches latin-ext.
    assert font_face_css().count("@font-face") == 2
    assert "unicode-range" in font_face_css()
