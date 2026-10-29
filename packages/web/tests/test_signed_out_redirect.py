"""A browser that reaches an app page without a live session goes to sign-in and
comes back to the page it asked for.

Before this, opening any /t/<business>/... page signed out (or after the 8-hour
session ran out) answered with the API's raw JSON 401,
`{"error": "missing or invalid bearer token"}`, which reads as "the app is
broken". The JSON API under /api/ keeps its 401; so does a page route asked for
by a script that wants JSON.

The return address (`next`) is only ever an in-app /t/ path. Anything else
(another host, a scheme, a protocol-relative `//`, a backslash) is dropped and
sign-in lands on /app as before, so the redirect can't be turned into an open
redirect."""

from __future__ import annotations

import json
from datetime import date
from typing import Any
from urllib.parse import quote

import pytest

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    AuthService,
    InMemoryCredentialStore,
    InMemoryInvitationStore,
    InMemoryUserDirectory,
    InMemoryVerificationTokenStore,
    InvitationService,
    Request,
    Role,
    WebApp,
)

SECRET = "signed-out-secret"
NOW = 1_760_000_000
HOUR = 3_600
PASSWORD = "hunter2222"


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("50000.00"))
    )


def _config() -> ForecastConfig:
    return ForecastConfig(minimum_cash=Money.from_decimal("10000.00"))


def _make_app(clock: dict[str, int]) -> WebApp:
    seq = {"n": 0}

    def token() -> str:
        seq["n"] += 1
        return f"tok-{seq['n']}"

    svc = AuthService(
        InMemoryCredentialStore(), InMemoryVerificationTokenStore(), None,
        clock=lambda: clock["t"], token_factory=token,
        salt_factory=lambda: b"0123456789abcdef", min_password_length=8,
    )
    users = InMemoryUserDirectory()
    app = WebApp(
        users=users,
        session_secret=SECRET,
        session_clock=lambda: clock["t"],
        auth_service=svc,
        invitations=InvitationService(users, InMemoryInvitationStore(), clock=lambda: clock["t"]),
    )
    app.add_tenant("acme", "Acme Co", _inputs(), _config(), token="unused")
    return app


def _post(app: WebApp, path: str, payload: dict[str, Any]) -> Any:
    return app.handle(Request("POST", path, {"content-type": "application/json"},
                              json.dumps(payload)))


def _owner(app: WebApp, email: str = "ada@acme.com", tenant: str = "acme") -> None:
    assert app._invitations is not None
    inv = app._invitations.invite(email, tenant, Role.OWNER, "owner@rgnr8.co")
    tok = json.loads(_post(app, "/signup", {"token": inv.token, "password": PASSWORD}).body)
    _post(app, "/verify", {"token": tok["verify_token"]})


def _login_form(app: WebApp, email: str, nxt: str) -> Any:
    body = f"email={quote(email)}&password={PASSWORD}&next={quote(nxt, safe='')}"
    return app.handle(Request("POST", "/login",
                              {"content-type": "application/x-www-form-urlencoded"}, body))


def _cookie(resp: Any) -> str:
    return str(resp.headers.get("Set-Cookie", "")).split(";")[0]


def _login_location(path: str) -> str:
    return "/login?next=" + quote(path, safe="")


# --- signed-out page requests go to sign-in ----------------------------------

def test_a_signed_out_page_request_goes_to_sign_in_with_a_way_back() -> None:
    app = _make_app({"t": NOW})
    r = app.handle(Request("GET", "/t/acme/connect"))
    assert r.status == 302
    assert r.headers["Location"] == _login_location("/t/acme/connect")
    assert "bearer token" not in str(r.body)


def test_the_way_back_keeps_the_query_string() -> None:
    app = _make_app({"t": NOW})
    r = app.handle(Request("GET", "/t/acme/reports/runway?period=2026-09"))
    assert r.headers["Location"] == _login_location("/t/acme/reports/runway?period=2026-09")


def test_an_expired_session_goes_to_sign_in_too() -> None:
    clock = {"t": NOW}
    app = _make_app(clock)
    _owner(app)
    login = _post(app, "/login", {"email": "ada@acme.com", "password": PASSWORD})
    cookie = _cookie(login)
    assert app.handle(Request("GET", "/t/acme/connect", {"cookie": cookie})).status != 302
    clock["t"] = NOW + 9 * HOUR  # the session cookie lasts 8 hours
    r = app.handle(Request("GET", "/t/acme/connect", {"cookie": cookie}))
    assert r.status == 302 and r.headers["Location"] == _login_location("/t/acme/connect")


def test_a_signed_out_form_post_goes_to_sign_in_without_acting() -> None:
    app = _make_app({"t": NOW})
    r = app.handle(Request("POST", "/t/acme/connect/qbo/sync",
                           {"content-type": "application/x-www-form-urlencoded"}, ""))
    # 303 so the browser follows with a GET; nothing was done, nothing is replayed
    assert r.status == 303 and r.headers["Location"] == "/login"


# --- the API and scripts keep their 401 --------------------------------------

def test_the_json_api_still_answers_401() -> None:
    app = _make_app({"t": NOW})
    r = app.handle(Request("GET", "/api/acme/forecast"))
    assert r.status == 401 and json.loads(r.body)["error"] == "missing or invalid bearer token"


def test_a_page_route_asked_for_as_json_still_answers_401() -> None:
    app = _make_app({"t": NOW})
    r = app.handle(Request("GET", "/t/acme/connect", {"accept": "application/json"}))
    assert r.status == 401


def test_a_json_post_to_a_page_route_still_answers_401() -> None:
    app = _make_app({"t": NOW})
    r = _post(app, "/t/acme/connect/qbo/sync", {})
    assert r.status == 401


# --- sign-in honours the way back --------------------------------------------

def test_the_sign_in_page_says_why_and_carries_the_way_back() -> None:
    app = _make_app({"t": NOW})
    page = app.handle(Request("GET", _login_location("/t/acme/connect")))
    assert page.status == 200
    assert "Your session ended" in page.body
    assert 'name="next" value="/t/acme/connect"' in page.body


def test_a_plain_sign_in_page_has_no_session_notice() -> None:
    app = _make_app({"t": NOW})
    page = app.handle(Request("GET", "/login"))
    assert "Your session ended" not in page.body and 'name="next"' not in page.body


def test_signing_in_returns_to_the_page_asked_for() -> None:
    app = _make_app({"t": NOW})
    _owner(app)
    r = _login_form(app, "ada@acme.com", "/t/acme/reports/runway?period=2026-09")
    assert r.status == 302
    assert r.headers["Location"] == "/t/acme/reports/runway?period=2026-09"
    assert "rgnr8_session=" in r.headers["Set-Cookie"]


def test_signing_in_ignores_a_way_back_into_another_business() -> None:
    app = _make_app({"t": NOW})
    app.add_tenant("beta", "Beta LLC", _inputs(), _config(), token="unused")
    _owner(app)  # a member of acme only
    r = _login_form(app, "ada@acme.com", "/t/beta/connect")
    assert r.headers["Location"] == "/app"


@pytest.mark.parametrize("bad", [
    "//evil.example/t/acme",
    "https://evil.example/t/acme",
    "/\\evil.example",
    "/t/acme/\r\nSet-Cookie: x=1",
    "/login",
    "/api/acme/forecast",
    "t/acme/connect",
    "/t/acme/" + "x" * 600,
])
def test_an_unsafe_way_back_is_dropped(bad: str) -> None:
    app = _make_app({"t": NOW})
    _owner(app)
    r = _login_form(app, "ada@acme.com", bad)
    assert r.status == 302 and r.headers["Location"] == "/app"
    page = app.handle(Request("GET", "/login?next=" + quote(bad, safe="")))
    assert 'name="next"' not in page.body


def test_a_failed_sign_in_keeps_the_way_back() -> None:
    app = _make_app({"t": NOW})
    _owner(app)
    body = "email=ada%40acme.com&password=WRONG&next=%2Ft%2Facme%2Fconnect"
    r = app.handle(Request("POST", "/login",
                           {"content-type": "application/x-www-form-urlencoded"}, body))
    assert r.status == 401
    assert 'name="next" value="/t/acme/connect"' in r.body


# --- the chooser passes it through -------------------------------------------

def test_the_chooser_carries_the_way_back_into_the_right_business() -> None:
    app = _make_app({"t": NOW})
    app.add_tenant("beta", "Beta LLC", _inputs(), _config(), token="unused")
    _owner(app)
    assert app._users is not None
    app._users.set_membership("ada@acme.com", "beta", Role.OWNER)

    r = _login_form(app, "ada@acme.com", "/t/beta/connect")
    assert r.headers["Location"] == "/choose?next=" + quote("/t/beta/connect", safe="")
    cookie = _cookie(r)
    page = app.handle(Request("GET", r.headers["Location"], {"cookie": cookie}))
    assert "/choose/enter?tenant=beta&amp;next=%2Ft%2Fbeta%2Fconnect" in page.body

    into_beta = app.handle(Request(
        "GET", "/choose/enter?tenant=beta&next=%2Ft%2Fbeta%2Fconnect", {"cookie": cookie}))
    assert into_beta.headers["Location"] == "/t/beta/connect"
    # picking the other business drops a way back that points somewhere else
    into_acme = app.handle(Request(
        "GET", "/choose/enter?tenant=acme&next=%2Ft%2Fbeta%2Fconnect", {"cookie": cookie}))
    assert into_acme.headers["Location"] == "/app"


def test_signed_in_but_no_business_picked_goes_to_the_chooser_not_sign_in() -> None:
    app = _make_app({"t": NOW})
    app.add_tenant("beta", "Beta LLC", _inputs(), _config(), token="unused")
    _owner(app)
    assert app._users is not None
    app._users.set_membership("ada@acme.com", "beta", Role.OWNER)
    login = _post(app, "/login", {"email": "ada@acme.com", "password": PASSWORD})
    assert login.headers["Location"] == "/choose"  # the cookie names no business yet
    r = app.handle(Request("GET", "/t/beta/connect", {"cookie": _cookie(login)}))
    assert r.status == 302
    assert r.headers["Location"] == "/choose?next=" + quote("/t/beta/connect", safe="")
