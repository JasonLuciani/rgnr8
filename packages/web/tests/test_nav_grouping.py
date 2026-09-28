"""The sidebar: grouped by intent, and gated on what this deployment can serve.

The audit's finding was that twenty-five equally-weighted links, most of them
leading to a screen that could only say "not configured", told a business owner
nothing. Two gates fix that and they are deliberately different:

* **permissions** — about the person. A viewer never sees Team.
* **capabilities** — about the deployment. Nobody sees Invoices when there is no
  ledger service behind it, whatever their role.

Everything here is server-rendered markup, so these are assertions about what a
browser actually receives.
"""

from __future__ import annotations

import re
from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    Permission,
    Request,
    Role,
    User,
    WebApp,
)
from rgnr8_web.shell import NAV_CAPABILITIES, _NAV, _NAV_GROUPS, render_shell

SECRET = "nav-secret"
NOW = 1_760_000_000


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("120000.00")))


def _app(**kw) -> WebApp:
    users = InMemoryUserDirectory()
    for email, role in (("owner@acme.com", Role.OWNER), ("view@acme.com", Role.VIEWER)):
        users.upsert_user(User(email, email))
        users.set_membership(email, "acme", role)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW, **kw)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return app


def _page(app: WebApp, who: str = "owner@acme.com", path: str = "/app") -> str:
    login = app.handle(Request("POST", "/login", {}, f"email={who}&role=owner"))
    cookie = login.headers.get("Set-Cookie", "").split(";")[0]
    r = app.handle(Request("GET", path, {"cookie": cookie}))
    assert r.status == 200, (path, r.status)
    return r.body


def _nav_labels(html: str) -> list[str]:
    """The link text of every sidebar item, in render order."""
    nav = html.split('<nav class="rg-nav"', 1)[1].split("</nav>", 1)[0]
    return [re.sub(r"<[^>]+>", "", m) for m in re.findall(r"<a [^>]*>(.*?)</a>", nav)]


def _group_labels(html: str) -> list[str]:
    nav = html.split('<nav class="rg-nav"', 1)[1].split("</nav>", 1)[0]
    return re.findall(r"<h2>(.*?)</h2>", nav)


# --- structure ---------------------------------------------------------------


def _fully_wired() -> str:
    """The shell as a fully-configured deployment renders it for an owner."""
    return render_shell(tenant="acme", display_name="Acme Co", role=Role.OWNER,
                        permissions=frozenset(Permission), active="cash",
                        body_html="", available=NAV_CAPABILITIES)


def test_the_sidebar_is_grouped_not_a_flat_list() -> None:
    assert _group_labels(_fully_wired()) == [
        "Today", "Customers", "Projects", "Vendors", "Books", "Plan", "Admin"]


def test_groups_collapse_away_when_nothing_in_them_is_available() -> None:
    # A fresh deployment with no ledger has no Projects or Vendors screens at
    # all, so those headings are gone rather than standing over an empty space.
    labels = _group_labels(_page(_app()))
    assert "Projects" not in labels and "Vendors" not in labels
    assert labels == ["Today", "Customers", "Books", "Plan", "Admin"]


def test_every_surface_lives_in_exactly_one_group() -> None:
    # Grouping must not silently drop or duplicate a screen. `_NAV` is still the
    # canonical flat list (the provisional-scope audit reads it), so it is the
    # reference both ways.
    grouped = [item.suffix for g in _NAV_GROUPS for item in g.items]
    assert sorted(grouped) == sorted(s for s, _l, _p in _NAV)
    assert len(grouped) == len(set(grouped))


def test_capability_keys_are_not_typos() -> None:
    # A `needs=` value nothing ever supplies would hide a screen forever, and
    # silently. WebApp is the only supplier, so the two sets must agree.
    assert NAV_CAPABILITIES == {"ledger", "qbo", "packages", "ask", "webhooks", "audit"}
    assert _app()._nav_available() <= NAV_CAPABILITIES


# --- gate 1: the person ------------------------------------------------------


def test_a_viewer_sees_no_admin_group_at_all() -> None:
    # An empty group heading is the same broken promise as a dead link, so a
    # group with nothing visible in it is not rendered.
    body = _page(_app(), who="view@acme.com")
    assert "Team" not in _nav_labels(body)
    assert "Admin" not in _group_labels(body)


# --- gate 2: the deployment --------------------------------------------------


def test_an_unconfigured_module_does_not_advertise_itself() -> None:
    # No ledger, no QBO, no packages, no Ask, no webhooks, no audit log — which
    # is exactly the shape of a fresh deployment before anything is connected.
    body = _page(_app())
    labels = _nav_labels(body)
    for dead in ("Invoices", "Bills", "Jobs", "Estimates", "Books", "Connect", "Ask"):
        assert dead not in labels, f"{dead} was linked with nothing behind it"
    # ...while the screens that work without any of it are still there
    assert "Cash" in labels and "Transactions" in labels and "Team" in labels


def test_wiring_a_capability_brings_its_screens_back() -> None:
    app = _app()
    # The team page is used because it reads no ledger data — the point here is
    # the nav reacting to a capability being wired, not what the screen renders.
    assert "Invoices" not in _nav_labels(_page(app, path="/t/acme/team"))

    class _StubLedger:
        """Enough of a ledger to exist. `_nav_available` asks whether one is
        wired at all, which is the whole contract this test is pinning."""

    app.set_ledger(_StubLedger())          # type: ignore[arg-type]
    body = _page(app, path="/t/acme/team")
    assert {"Invoices", "Bills", "Jobs", "Estimates", "Books"} <= set(_nav_labels(body))
    # and the group headings come back with them
    assert {"Customers", "Vendors", "Projects"} <= set(_group_labels(body))


def test_permission_and_capability_are_independent_gates() -> None:
    # A controller genuinely holds MANAGE_CLOSE; that is not a claim the ledger
    # exists. Passing no `available` at all keeps the old behaviour, which is
    # what every direct caller of render_shell relies on.
    everything = frozenset(Permission)
    ungated = render_shell(tenant="acme", display_name="Acme Co", role=Role.OWNER,
                           permissions=everything, active="cash", body_html="")
    assert "Invoices" in _nav_labels(ungated)          # available=None → no gating
    gated = render_shell(tenant="acme", display_name="Acme Co", role=Role.OWNER,
                         permissions=everything, active="cash", body_html="",
                         available=frozenset())
    assert "Invoices" not in _nav_labels(gated)
    assert "Cash" in _nav_labels(gated)                # ungated items unaffected


# --- the frame ---------------------------------------------------------------


def test_the_active_item_is_marked_for_assistive_tech() -> None:
    body = _page(_app(), path="/t/acme/receivables")
    assert 'aria-current="page">Receivables</a>' in body
    assert body.count('aria-current="page"') == 1


def test_the_mobile_menu_needs_no_script_and_defaults_open() -> None:
    # `open` in the markup means every link is reachable even if the CSS never
    # loads; the media query is what collapses it on a phone. No JS is involved,
    # because this app ships zero external assets (asserted below).
    body = _page(_app())
    assert '<details class="rg-navwrap" open>' in body
    assert "<summary" in body
    assert "http://" not in body and "https://" not in body


def test_the_shell_still_wraps_the_screen_body() -> None:
    body = _page(_app())
    assert '<div class="rg-layout">' in body
    assert '<main class="shell">' in body and "</main></div>" in body
