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
from rgnr8_web.arap_screens import render_documents
from rgnr8_web.capture_screens import render_capture
from rgnr8_web.payroll_screens import render_payroll_home
from rgnr8_web.job_screens import render_jobs
from rgnr8_web.estimate_screens import render_estimates, render_pipeline
from rgnr8_web.debt_screens import render_debt
from rgnr8_web.asset_screens import render_assets
from rgnr8_web.provenance_labels import Provenance, label
from rgnr8_web.rbac import Permission

_LEGEND = "What do the labels mean?"

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
    # The v1 production anchors — cash clarity + guided close + handoff + admin.
    for prod in ("", "close", "packages", "books", "reports", "receivables", "connect"):
        assert not is_provisional(prod), prod
    assert "" in V1_PRODUCTION  # cash home


def test_all_core_surfaces_are_graduated() -> None:
    # C-1…C-7: every core accounting surface has graduated out of provisional.
    for suffix in ("transactions", "inbox", "invoices", "bills", "capture",
                   "payroll", "jobs", "estimates", "pipeline", "debt", "assets"):
        assert not is_provisional(suffix), suffix


def test_bank_feed_is_graduated_to_v1() -> None:
    # C-1: the bank feed / review inbox graduated out of provisional.
    assert not is_provisional("transactions")
    assert not is_provisional("inbox")
    # C-0: the graduation gate template exists.
    assert len(GRADUATION_CHECKLIST) >= 4


def test_the_graduation_mechanism_still_guards_a_future_surface() -> None:
    # Everything current has graduated, but the machinery stays live: a surface
    # that isn't in V1_PRODUCTION is still treated as provisional and bannered.
    assert is_provisional("some-future-surface")
    perms = frozenset(Permission)
    bannered = render_shell(tenant="acme", display_name="Acme", role=Role.OWNER,
                            permissions=perms, active="some-future-surface",
                            body_html="<p>x</p>")
    assert PROVISIONAL_NOTE in bannered


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


def test_a_graduated_surface_renders_without_the_provisional_banner() -> None:
    # Jobs graduated (C-6): its screen no longer carries the provisional disclosure,
    # and the nav shows no beta dot now that every core surface is production.
    r = _app().handle(Request("GET", "/t/acme/jobs", _h("owner@acme.com")))
    assert r.status == 200
    assert PROVISIONAL_NOTE not in r.body
    assert 'class="rg-beta"' not in r.body


def test_hide_provisional_is_a_noop_now_that_everything_is_graduated() -> None:
    # With no provisional surface left, hiding provisional items changes nothing —
    # the whole nav (Jobs, Cash, …) shows in both, with no beta badges anywhere.
    perms = frozenset(Permission)
    shown = render_shell(tenant="acme", display_name="Acme", role=Role.OWNER,
                         permissions=perms, active="cash", body_html="<p>x</p>")
    hidden = render_shell(tenant="acme", display_name="Acme", role=Role.OWNER,
                          permissions=perms, active="cash", body_html="<p>x</p>",
                          hide_provisional=True)
    assert ">Jobs" in shown and ">Jobs" in hidden          # now production — stays
    assert ">Cash" in shown and ">Cash" in hidden
    assert 'class="rg-beta"' not in shown                  # nothing provisional to badge


# --- per-surface provenance labels (C-2…C-7 acceptance) ----------------------

def test_ar_invoices_carry_provenance_labels() -> None:
    html = render_documents("acme", "invoices", {"documents": []}, {},
                            today="2026-08-21", can_post=True)
    assert _LEGEND in html
    assert label(Provenance.POSTED) in html


def test_ap_bills_carry_provenance_labels() -> None:
    html = render_documents("acme", "bills", {"documents": []}, {},
                            today="2026-08-21", can_post=True)
    assert _LEGEND in html
    assert label(Provenance.POSTED) in html


def test_capture_carries_provenance_labels() -> None:
    html = render_capture("acme")
    assert _LEGEND in html
    assert label(Provenance.IMPORTED) in html


def test_payroll_carries_provenance_labels() -> None:
    html = render_payroll_home("acme", {"runs": []}, {}, {}, can_post=True)
    assert _LEGEND in html
    assert label(Provenance.POSTED) in html


def test_jobs_carry_provenance_labels() -> None:
    html = render_jobs("acme", {"jobs": []}, {}, {}, can_edit=True)
    assert _LEGEND in html
    assert label(Provenance.POSTED) in html


def test_estimates_carry_provenance_labels() -> None:
    html = render_estimates("acme", {"estimates": []}, {}, {}, {}, can_edit=True)
    assert _LEGEND in html
    assert label(Provenance.FORECAST) in html


def test_pipeline_carries_provenance_labels() -> None:
    html = render_pipeline("acme", {"totals": {}, "stages": []}, {}, can_edit=True)
    assert _LEGEND in html
    assert label(Provenance.FORECAST) in html


def test_debt_carries_provenance_labels() -> None:
    html = render_debt("acme", {"totals": {}, "loans": []}, can_write=True)
    assert _LEGEND in html
    assert label(Provenance.POSTED) in html


def test_assets_carry_provenance_labels() -> None:
    html = render_assets("acme", {"totals": {}, "assets": []}, can_write=True)
    assert _LEGEND in html
    assert label(Provenance.POSTED) in html
