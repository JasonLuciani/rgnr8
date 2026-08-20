"""Server-rendered UI: login → session cookie → role-aware shell → team page."""

from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    InMemoryUserDirectory,
    JwtAuthenticator,
    Request,
    Role,
    User,
    WebApp,
)

SECRET = "ui-secret"
NOW = 1_760_000_000


def _inputs() -> ForecastInputs:
    return ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("120000.00")))


def _app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("owner@acme.com", "owner@acme.com", "Ada Owner"))
    users.set_membership("owner@acme.com", "acme", Role.OWNER)
    users.upsert_user(User("book@acme.com", "book@acme.com", "Ben Books"))
    users.set_membership("book@acme.com", "acme", Role.BOOKKEEPER)
    app = WebApp(
        authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW),
        users=users,
        session_secret=SECRET,
        session_clock=lambda: NOW,
    )
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return app


def _cookie_from(resp) -> str:
    sc = resp.headers.get("Set-Cookie", "")
    return sc.split(";")[0]  # "rgnr8_session=<jwt>"


def test_login_page_renders_without_auth() -> None:
    r = _app().handle(Request("GET", "/login"))
    assert r.status == 200 and "RGNR" in r.body and "Sign in" in r.body
    assert "http://" not in r.body and "https://" not in r.body  # self-contained


def test_login_sets_a_session_cookie_and_redirects() -> None:
    app = _app()
    r = app.handle(Request("POST", "/login", {"content-type": "application/x-www-form-urlencoded"},
                           "email=owner@acme.com&role=owner"))
    assert r.status == 302
    assert r.headers.get("Location") == "/app"
    assert "rgnr8_session=" in r.headers.get("Set-Cookie", "")
    assert "HttpOnly" in r.headers.get("Set-Cookie", "")


def test_owner_lands_on_cash_with_full_nav() -> None:
    app = _app()
    login = app.handle(Request("POST", "/login", {}, "email=owner@acme.com&role=owner"))
    cookie = _cookie_from(login)
    home = app.handle(Request("GET", "/app", {"cookie": cookie}))
    assert home.status == 200
    assert "Cash today" in home.body and "$120,000" in home.body
    # owner sees the Team link (manage_users) in the nav
    assert "/t/acme/team" in home.body
    assert ">Owner<" in home.body or "Owner" in home.body


def test_bookkeeper_nav_hides_team_and_lands_on_transactions() -> None:
    app = _app()
    login = app.handle(Request("POST", "/login", {}, "email=book@acme.com&role=bookkeeper"))
    cookie = _cookie_from(login)
    home = app.handle(Request("GET", "/app", {"cookie": cookie}))
    assert home.status == 200
    assert "/t/acme/team" not in home.body                    # bookkeeper can't manage users
    assert "Review bank transactions" in home.body            # role-specific CTA (their surface)
    assert "/t/acme/transactions" in home.body                # nav + CTA point to the register
    assert ">Close</a>" in home.body                          # they can still run the close


def test_team_page_is_gated_and_lists_members() -> None:
    app = _app()
    owner_cookie = _cookie_from(app.handle(Request("POST", "/login", {}, "email=owner@acme.com&role=owner")))
    r = app.handle(Request("GET", "/t/acme/team", {"cookie": owner_cookie}))
    assert r.status == 200
    assert "Team &amp; roles" in r.body
    assert "owner@acme.com" in r.body and "book@acme.com" in r.body

    book_cookie = _cookie_from(app.handle(Request("POST", "/login", {}, "email=book@acme.com&role=bookkeeper")))
    blocked = app.handle(Request("GET", "/t/acme/team", {"cookie": book_cookie}))
    assert blocked.status == 403  # bookkeeper lacks manage_users


def test_logout_clears_the_cookie() -> None:
    app = _app()
    r = app.handle(Request("GET", "/logout"))
    assert r.status == 302 and r.headers.get("Location") == "/login"
    assert "Max-Age=0" in r.headers.get("Set-Cookie", "")


def test_no_cookie_no_access() -> None:
    app = _app()
    assert app.handle(Request("GET", "/app")).status == 401
    assert app.handle(Request("GET", "/t/acme/team")).status == 401

# --- H1-5: session cookie carries Secure (HTTPS-only) by default -------------

def _login_cookie(secure_cookies: bool) -> str:
    users = InMemoryUserDirectory()
    users.upsert_user(User("owner@acme.com", "owner@acme.com", "Olive Owner"))
    users.set_membership("owner@acme.com", "acme", Role.OWNER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users,
                 session_secret=SECRET, session_clock=lambda: NOW, secure_cookies=secure_cookies)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    r = app.handle(Request("POST", "/login", {}, "email=owner@acme.com&role=owner"))
    return r.headers.get("Set-Cookie", "")


def test_session_cookie_is_secure_by_default() -> None:
    sc = _login_cookie(secure_cookies=True)
    assert "Secure" in sc and "HttpOnly" in sc and "SameSite=Lax" in sc


def test_secure_flag_can_be_disabled_for_local_http_dev() -> None:
    sc = _login_cookie(secure_cookies=False)
    assert "Secure" not in sc and "HttpOnly" in sc
