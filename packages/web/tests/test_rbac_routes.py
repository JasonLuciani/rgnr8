"""Role-gated web routes end to end (JWT with a subject + a user directory)."""

import json
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

SECRET = "rbac-web-secret"
NOW = 1_760_000_000


def _inputs() -> ForecastInputs:
    return ForecastInputs(opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("50000.00")))


def _app() -> WebApp:
    users = InMemoryUserDirectory()
    users.upsert_user(User("u-owner", "owner@acme.com", "Owner"))
    users.upsert_user(User("u-view", "view@acme.com", "Viewer"))
    users.set_membership("u-owner", "acme", Role.OWNER)
    users.set_membership("u-view", "acme", Role.VIEWER)
    app = WebApp(authenticator=JwtAuthenticator(SECRET, clock=lambda: NOW), users=users)
    app.add_tenant("acme", "Acme Co", _inputs(), ForecastConfig(minimum_cash=Money.from_decimal("10000.00")),
                   token="unused")
    return app


def _tok(sub: str, tenant: str = "acme") -> str:
    return sign_jwt({"sub": sub, "tenant": tenant, "exp": NOW + 3600}, SECRET)


def _get(app: WebApp, path: str, sub: str, method: str = "GET", body: str = "") -> object:
    return app.handle(Request(method, path, {"authorization": f"Bearer {_tok(sub)}"}, body))


def test_viewer_can_read_cash_but_not_write_assumptions() -> None:
    app = _app()
    assert _get(app, "/api/acme/today", "u-view").status == 200
    r = _get(app, "/api/acme/assumptions", "u-view", "POST", '{"minimum_cash":"5000.00"}')
    assert r.status == 403
    assert json.loads(r.body)["need"] == "edit_assumptions"


def test_owner_can_write_and_manage_users_viewer_cannot() -> None:
    app = _app()
    assert _get(app, "/api/acme/assumptions", "u-owner", "POST", '{"minimum_cash":"5000.00"}').status == 200
    assert _get(app, "/api/acme/users", "u-owner").status == 200
    assert _get(app, "/api/acme/users", "u-view").status == 403  # viewer lacks manage_users


def test_me_reports_role_and_permissions() -> None:
    app = _app()
    r = _get(app, "/api/acme/me", "u-owner")
    assert r.status == 200
    body = json.loads(r.body)
    assert body["role"] == "owner" and body["rbac"] is True
    assert "manage_users" in body["permissions"] and "publish_close" in body["permissions"]

    v = json.loads(_get(app, "/api/acme/me", "u-view").body)
    assert v["role"] == "viewer"
    assert "edit_assumptions" not in v["permissions"]


def test_owner_invites_a_member_and_sets_role() -> None:
    app = _app()
    # add a bookkeeper by email
    r = _get(app, "/api/acme/users", "u-owner", "POST",
             '{"email":"book@acme.com","name":"Book","role":"bookkeeper"}')
    assert r.status == 200 and json.loads(r.body)["role"] == "bookkeeper"
    # the new member now shows up and can view cash but not manage users
    members = json.loads(_get(app, "/api/acme/users", "u-owner").body)["members"]
    assert any(m["email"] == "book@acme.com" and m["role"] == "bookkeeper" for m in members)
    assert _get(app, "/api/acme/today", "book@acme.com").status == 200
    assert _get(app, "/api/acme/users", "book@acme.com").status == 403


def test_unknown_and_platform_roles_are_rejected() -> None:
    app = _app()
    assert _get(app, "/api/acme/users", "u-owner", "POST",
                '{"email":"x@acme.com","role":"wizard"}').status == 400
    assert _get(app, "/api/acme/users", "u-owner", "POST",
                '{"email":"x@acme.com","role":"operator"}').status == 400  # platform role not assignable


def test_a_stranger_with_a_valid_token_but_no_membership_is_forbidden() -> None:
    app = _app()
    # validly-signed token, subject has no membership in acme
    r = _get(app, "/api/acme/today", "u-nobody")
    assert r.status == 403
    assert _get(app, "/api/acme/me", "u-nobody").status == 403