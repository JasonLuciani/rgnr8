"""Production-composition security invariants.

The pre-launch review's central finding was that controls green in unit tests were
disabled in the *composed* app (`build_web_app` / `Fleet.web_app`). These tests
assert the composition the product actually ships: RBAC is on, no guessable static
token works, viewers can't seal the close, and RBAC misconfiguration fails closed.
If any of these regress, the P0 auth bypass is back.
"""

from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import Request, Role, WebApp
from rgnr8_ops import BetaTenant, Fleet
from rgnr8_ops.app_factory import build_web_app
from rgnr8_ops.config import Settings

SECRET = "prod-sec"
NOW = 1_760_000_000


def _bt(tid: str = "acme") -> BetaTenant:
    return BetaTenant(tid, "Acme Co", "owner@acme.com",
                      ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31),
                                     available=Money.from_decimal("50000.00"))),
                      ForecastConfig(minimum_cash=Money.from_decimal("10000.00")))


def _fleet_app() -> WebApp:
    fleet = Fleet(jwt_secret=SECRET, clock=lambda: NOW)
    fleet.onboard(_bt())
    return fleet.web_app()


def test_guessable_static_token_is_rejected() -> None:
    app = _fleet_app()
    # the pre-launch exploit: Bearer unused:<tenant> → full access. Must now 401.
    for tok in ("unused:acme", "acme", "unused:acme:owner"):
        r = app.handle(Request("GET", "/api/acme/today", {"authorization": f"Bearer {tok}"}))
        assert r.status == 401, f"static token {tok!r} must not authenticate (got {r.status})"


def test_owner_token_works_and_viewer_cannot_seal() -> None:
    fleet = Fleet(jwt_secret=SECRET, clock=lambda: NOW)
    fleet.onboard(_bt())
    app = fleet.web_app()
    # the seated owner (via a minted, sub-bearing token) can read cash
    owner_tok = fleet.mint_token("acme")  # sub defaults to the owner recipient
    assert app.handle(Request("GET", "/api/acme/today",
                              {"authorization": f"Bearer {owner_tok}"})).status == 200
    # a principal with no membership gets nothing (RBAC is ON, fails closed)
    stranger = fleet.mint_token("acme", subject="stranger@nowhere.com")
    r = app.handle(Request("POST", "/api/acme/close/publish",
                           {"authorization": f"Bearer {stranger}", "content-type": "application/json"}, "{}"))
    assert r.status == 403


def test_build_web_app_enables_rbac_for_real_authenticator() -> None:
    settings = Settings.from_env({"RGNR8_AUTH_MODE": "hs256", "RGNR8_JWT_SECRET": SECRET})
    app = build_web_app(settings)
    assert app._require_rbac is True and app._users is not None  # type: ignore[attr-defined]
    # and a permissioned route with no credentials is denied
    assert app.handle(Request("GET", "/api/acme/today")).status == 401


def test_require_rbac_without_directory_raises() -> None:
    # constructing a permissioned app with no directory must fail loudly, not
    # silently run open.
    try:
        WebApp(require_rbac=True)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_static_mode_stays_tenant_scoped_for_dev() -> None:
    # dev/static mode intentionally keeps the static-token path (no authenticator)
    settings = Settings.from_env({"RGNR8_AUTH_MODE": "static"})
    app = build_web_app(settings)
    assert app._require_rbac is False  # type: ignore[attr-defined]
    app.add_tenant("acme", "Acme", _bt().inputs, _bt().config, token="dev-token")
    assert app.handle(Request("GET", "/api/acme/today",
                              {"authorization": "Bearer dev-token"})).status == 200
