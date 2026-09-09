"""Invitations over HTTP: minting, listing and revoking are the only supply of
new accounts, so this file is mostly *negative* authorization — proof that the
people who must not mint one, can't.

The positive path (redeem → credential + membership) lives in
`test_auth_routes.py`; here we care about who is allowed to hold the pen.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

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
    User,
    WebApp,
)

SECRET = "invitation-routes-secret"
NOW = 1_760_000_000


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("50000.00"))
    )


def _make_app() -> WebApp:
    seq = {"n": 0}

    def token() -> str:
        seq["n"] += 1
        return f"tok-{seq['n']}"

    svc = AuthService(
        InMemoryCredentialStore(), InMemoryVerificationTokenStore(), None,
        clock=lambda: NOW, token_factory=token,
        salt_factory=lambda: b"0123456789abcdef", min_password_length=8,
    )
    users = InMemoryUserDirectory()
    app = WebApp(
        users=users,
        session_secret=SECRET,
        session_clock=lambda: NOW,
        auth_service=svc,
        invitations=InvitationService(users, InMemoryInvitationStore(), clock=lambda: NOW),
        require_rbac=True,
    )
    for tid, name in (("acme", "Acme Co"), ("beta", "Beta LLC")):
        app.add_tenant(tid, name, _inputs(),
                       ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return app


def _seat(app: WebApp, email: str, tenant: str, role: Role) -> None:
    assert app._users is not None
    app._users.upsert_user(User(id=email, email=email))
    app._users.set_membership(email, tenant, role)


def _cookie(app: WebApp, subject: str, tenant: str) -> str:
    return app._session_cookie(sub=subject, tenant=tenant).split(";")[0]


def _post(app: WebApp, path: str, cookie: str, payload: dict[str, Any] | None = None) -> Any:
    return app.handle(Request("POST", path,
                              {"content-type": "application/json", "cookie": cookie},
                              json.dumps(payload) if payload is not None else ""))


def _get(app: WebApp, path: str, cookie: str = "") -> Any:
    headers = {"cookie": cookie} if cookie else {}
    return app.handle(Request("GET", path, headers))


# --- who may mint an invitation ---------------------------------------------


def test_owner_can_mint_an_invitation() -> None:
    app = _make_app()
    _seat(app, "owner@acme.com", "acme", Role.OWNER)
    r = _post(app, "/api/acme/invitations", _cookie(app, "owner@acme.com", "acme"),
              {"email": "cpa@firm.com", "role": "accountant"})
    assert r.status == 201
    body = json.loads(r.body)
    assert body["email"] == "cpa@firm.com" and body["role"] == "accountant"
    assert body["token"]
    # minting does NOT seat them — only redeeming does
    assert app._users is not None
    assert app._users.membership("cpa@firm.com", "acme") is None


def test_roles_without_manage_users_cannot_mint_an_invitation() -> None:
    # Every non-owner tenant role. If any of these could invite, a bookkeeper
    # could grant themselves a second account or add an outsider to the books.
    for role in (Role.CONTROLLER, Role.BOOKKEEPER, Role.ACCOUNTANT, Role.VIEWER):
        app = _make_app()
        _seat(app, "member@acme.com", "acme", role)
        r = _post(app, "/api/acme/invitations", _cookie(app, "member@acme.com", "acme"),
                  {"email": "friend@evil.com", "role": "owner"})
        assert r.status == 403, f"{role.value} was able to mint an invitation"
        assert json.loads(r.body)["need"] == "manage_users"
        # ...and nothing was created
        owner_view = _get(app, "/api/acme/invitations", _cookie(app, "member@acme.com", "acme"))
        assert owner_view.status == 403


def test_minting_requires_a_session_at_all() -> None:
    app = _make_app()
    _seat(app, "owner@acme.com", "acme", Role.OWNER)
    r = app.handle(Request("POST", "/api/acme/invitations",
                           {"content-type": "application/json"},
                           json.dumps({"email": "x@y.com", "role": "viewer"})))
    assert r.status == 401


def test_owner_of_one_business_cannot_invite_into_another() -> None:
    app = _make_app()
    _seat(app, "owner@acme.com", "acme", Role.OWNER)
    r = _post(app, "/api/beta/invitations", _cookie(app, "owner@acme.com", "acme"),
              {"email": "mole@evil.com", "role": "owner"})
    assert r.status == 403
    assert app._invitations is not None
    assert app._invitations.pending_for("beta") == []


def test_platform_roles_are_not_invitable() -> None:
    # A tenant owner must not be able to mint themselves RGNR8 staff access.
    app = _make_app()
    _seat(app, "owner@acme.com", "acme", Role.OWNER)
    r = _post(app, "/api/acme/invitations", _cookie(app, "owner@acme.com", "acme"),
              {"email": "owner@acme.com", "role": "operator"})
    assert r.status == 400
    assert app._invitations is not None
    assert app._invitations.pending_for("acme") == []


# --- listing and revoking ----------------------------------------------------


def test_pending_invitations_are_listed_for_their_business_only() -> None:
    app = _make_app()
    _seat(app, "owner@acme.com", "acme", Role.OWNER)
    _seat(app, "owner@beta.com", "beta", Role.OWNER)
    assert app._invitations is not None
    app._invitations.invite("a@x.com", "acme", Role.VIEWER, "owner@acme.com")
    app._invitations.invite("b@x.com", "beta", Role.VIEWER, "owner@beta.com")
    r = _get(app, "/api/acme/invitations", _cookie(app, "owner@acme.com", "acme"))
    assert r.status == 200
    emails = [i["email"] for i in json.loads(r.body)["invitations"]]
    assert emails == ["a@x.com"]


def test_revoke_is_scoped_to_the_inviting_business() -> None:
    # Tokens are globally unique strings; without a per-tenant check an owner
    # who came by another business's token could cancel its invitations.
    app = _make_app()
    _seat(app, "owner@acme.com", "acme", Role.OWNER)
    _seat(app, "owner@beta.com", "beta", Role.OWNER)
    assert app._invitations is not None
    beta_inv = app._invitations.invite("b@x.com", "beta", Role.VIEWER, "owner@beta.com")
    r = _post(app, f"/api/acme/invitations/{beta_inv.token}/revoke",
              _cookie(app, "owner@acme.com", "acme"))
    assert r.status == 404
    assert [i.token for i in app._invitations.pending_for("beta")] == [beta_inv.token]

    # its own owner can revoke it, and the link then stops working
    ok = _post(app, f"/api/beta/invitations/{beta_inv.token}/revoke",
               _cookie(app, "owner@beta.com", "beta"))
    assert ok.status == 200
    assert app._invitations.pending_for("beta") == []
    dead = app.handle(Request("POST", "/signup", {"content-type": "application/json"},
                              json.dumps({"token": beta_inv.token, "password": "hunter2222"})))
    assert dead.status == 400


# --- the signup page itself --------------------------------------------------


def test_signup_page_without_a_token_is_refused() -> None:
    app = _make_app()
    r = _get(app, "/signup")
    assert r.status == 403
    assert "invitation-only" in r.body
    assert "Create account" not in r.body          # no open form is rendered


def test_signup_page_with_a_token_is_bound_to_the_invited_address() -> None:
    app = _make_app()
    assert app._invitations is not None
    inv = app._invitations.invite("cpa@firm.com", "acme", Role.ACCOUNTANT, "owner@acme.com")
    r = _get(app, f"/signup?token={inv.token}")
    assert r.status == 200
    assert "cpa@firm.com" in r.body and "readonly" in r.body
    assert "Acme Co" in r.body                     # says which business
    assert inv.token in r.body                     # carried through the form


def test_signup_page_with_a_dead_token_does_not_consume_it() -> None:
    app = _make_app()
    assert app._invitations is not None
    inv = app._invitations.invite("cpa@firm.com", "acme", Role.VIEWER, "owner@acme.com")
    good = _get(app, f"/signup?token={inv.token}")
    assert good.status == 200
    # a GET must never redeem or expire an invitation
    assert [i.token for i in app._invitations.pending_for("acme")] == [inv.token]
    bad = _get(app, "/signup?token=nope")
    assert bad.status == 400 and "no longer valid" in bad.body


def test_login_page_offers_no_create_account_link() -> None:
    app = _make_app()
    r = _get(app, "/login")
    assert r.status == 200
    assert "/signup" not in r.body                 # invitation-only: no dead end
    assert "Sign in as (demo)" not in r.body       # and never the dev role picker
