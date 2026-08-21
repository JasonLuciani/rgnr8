"""v1 scope: production surfaces render clean; provisional ones are badged in the
nav and banner their body so an owner never mistakes them for a system of record."""

from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
    sign_jwt,
)
from rgnr8_web.scope import (
    GRADUATION_CHECKLIST,
    PROVISIONAL_NOTE,
    V1_PRODUCTION,
    is_provisional,
)
from rgnr8_web.shell import render_shell
from rgnr8_web.inbox_screens import render_inbox
from rgnr8_web.provenance_labels import Provenance, label
from rgnr8_web.rbac import Permission

SECRET = "scope-secret"
NOW = 1_760_000_000


def _app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("owner@acme.com", "owner@acme.com", "Owner"))
    users.set_membership("owner@acme.com", "acme", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW)
    app.add_tenant("acme", "Acme Co",
                   ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                                       available=Money.from_decimal("90000.00"))),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return app


def _h(sub: str) -> dict[str, str]:
    return {"authorization": f"Bearer {sign_jwt({'sub': sub, 'tenant': 'acme', 'exp': NOW + 3600}, SECRET)}"}


def test_scope_line_covers_cash_close_and_handoff() -> None:
    # The v1 production surface is exactly cash clarity + guided close + handoff
    # (+ admin infra). Spot-check the anchors and the provisional exclusions.
    for prod in ("", "close", "packages", "books", "reports", "receivables", "connect"):
        assert not is_provisional(prod), prod
    for prov in ("jobs", "estimates", "invoices", "bills", "payroll", "debt", "assets", "pipeline"):
        assert is_provisional(prov), prov
    assert "" in V1_PRODUCTION  # cash home


def test_bank_feed_is_graduated_to_v1() -> None:
    # C-1: the bank feed / review inbox graduated out of provisional.
    assert not is_provisional("transactions")
    assert not is_provisional("inbox")
    # C-0: the graduation gate template exists.
    assert len(GRADUATION_CHECKLIST) >= 4


def test_review_inbox_carries_provenance_labels() -> None:
    html = render_inbox("acme", {"items": []}, {}, can_post=True)
    # The review queue is IMPORTED (from the bank, not yet in the books), and the
    # legend explains the imported → posted → reconciled ladder.
    assert "What do the labels mean?" in html
    assert label(Provenance.IMPORTED) in html


def test_production_screen_has_no_provisional_banner() -> None:
    r = _app().handle(Request("GET", "/t/acme", _h("owner@acme.com")))
    assert r.status == 200
    assert PROVISIONAL_NOTE not in r.body
    assert 'class="rg-nav"' in r.body


def test_provisional_screen_is_bannered_and_nav_badged() -> None:
    r = _app().handle(Request("GET", "/t/acme/jobs", _h("owner@acme.com")))
    assert r.status == 200
    # The body is topped with the provisional disclosure...
    assert PROVISIONAL_NOTE in r.body
    assert "Provisional" in r.body
    # ...and the nav marks provisional items with a beta dot.
    assert 'class="rg-beta"' in r.body


def test_hide_provisional_drops_them_from_the_nav() -> None:
    perms = frozenset(Permission)  # every permission, so every nav item is eligible
    shown = render_shell(tenant="acme", display_name="Acme", role=Role.OWNER,
                         permissions=perms, active="cash", body_html="<p>x</p>")
    hidden = render_shell(tenant="acme", display_name="Acme", role=Role.OWNER,
                          permissions=perms, active="cash", body_html="<p>x</p>",
                          hide_provisional=True)
    assert ">Jobs" in shown and ">Jobs" not in hidden      # a provisional item
    assert ">Cash" in shown and ">Cash" in hidden          # a production item stays
    assert 'class="rg-beta"' not in hidden                 # nothing left to badge
