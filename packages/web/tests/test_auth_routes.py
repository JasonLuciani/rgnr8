"""End-to-end public-track auth over the HTTP surface: POST /signup, /verify,
/login (password), /password/reset-request, /password/reset, and the owner-gated
data export — plus proof the dev/static login path is untouched when no credential
store is configured."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Money
from rgnr8_web import (
    AuthService,
    InMemoryAuditLog,
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

SECRET = "auth-routes-secret"
NOW = 1_760_000_000
HOUR = 3_600


def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 31), available=Money.from_decimal("50000.00"))
    )


def _make_app(clock_ref: dict[str, int], audit: InMemoryAuditLog | None = None) -> WebApp:
    seq = {"n": 0}

    def token() -> str:
        seq["n"] += 1
        return f"tok-{seq['n']}"

    svc = AuthService(
        InMemoryCredentialStore(),
        InMemoryVerificationTokenStore(),
        audit,
        clock=lambda: clock_ref["t"],
        token_factory=token,
        salt_factory=lambda: b"0123456789abcdef",
        min_password_length=8,
        verify_ttl_hours=24,
        reset_ttl_hours=1,
    )
    users = InMemoryUserDirectory()
    # Production shape: a user directory (so signup is invitation-only), an
    # invitation service to mint the tokens, and an emailer so the verify/reset
    # round trip stays real rather than being auto-verified away.
    outbox: list[tuple[str, str, str]] = []
    app = WebApp(
        users=users,
        session_secret=SECRET,
        session_clock=lambda: clock_ref["t"],
        auth_service=svc,
        invitations=InvitationService(users, InMemoryInvitationStore(), audit,
                                      clock=lambda: clock_ref["t"]),
        emailer=lambda to, purpose, tok: outbox.append((to, purpose, tok)),
        audit=audit,
    )
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    app._outbox = outbox  # type: ignore[attr-defined]
    return app


def _post(app: WebApp, path: str, payload: dict[str, Any]) -> Any:
    return app.handle(Request("POST", path, {"content-type": "application/json"}, json.dumps(payload)))


def _cookie_from(resp: Any) -> str:
    return str(resp.headers.get("Set-Cookie", "")).split(";")[0]


def _mailed(app: WebApp, purpose: str) -> str:
    """The most recent token the app emailed for `purpose`."""
    return [tok for (_to, p, tok) in app._outbox if p == purpose][-1]  # type: ignore[attr-defined]


def _invite(app: WebApp, email: str, *, tenant: str = "acme", role: Role = Role.VIEWER) -> str:
    """Mint an invitation the way an owner would, and return its token."""
    assert app._invitations is not None
    return app._invitations.invite(email, tenant, role, "owner@rgnr8.co").token


def _signup(app: WebApp, email: str, password: str, *, tenant: str = "acme",
            role: Role = Role.VIEWER) -> Any:
    """Create an account the only way production allows: redeem an invitation.
    Returns the /signup response; the verification token lands in the outbox."""
    return _post(app, "/signup", {"token": _invite(app, email, tenant=tenant, role=role),
                                  "password": password})


def _signup_verified(app: WebApp, email: str, password: str, *, tenant: str = "acme",
                     role: Role = Role.VIEWER) -> Any:
    r = _signup(app, email, password, tenant=tenant, role=role)
    _post(app, "/verify", {"token": _mailed(app, "verify_email")})
    return r


# --- signup / verify ---------------------------------------------------------


def test_signup_redeems_an_invitation_into_an_account_and_a_membership() -> None:
    app = _make_app({"t": NOW})
    r = _signup(app, "ada@acme.com", "hunter2222", role=Role.BOOKKEEPER)
    assert r.status == 201
    body = json.loads(r.body)
    assert body["email"] == "ada@acme.com" and body["verified"] is False
    # the invitation, not the request, decided which business and which role
    assert body["tenant"] == "acme" and body["role"] == "bookkeeper"
    assert app._users is not None
    m = app._users.membership("ada@acme.com", "acme")
    assert m is not None and m.role is Role.BOOKKEEPER
    # with an emailer wired the token is sent, never returned in the response
    assert "verify_token" not in body
    assert _mailed(app, "verify_email") == "tok-1"


def test_signup_without_an_invitation_is_refused() -> None:
    # The audit's finding: open signup let anyone mint an account against the
    # production app. With a user directory present there is no un-invited path.
    app = _make_app({"t": NOW})
    r = _post(app, "/signup", {"email": "stranger@evil.com", "password": "hunter2222"})
    assert r.status == 403
    assert "invitation" in json.loads(r.body)["error"]
    # and no credential was created — the address can't then log in or reset
    assert _post(app, "/login", {"email": "stranger@evil.com", "password": "hunter2222"}).status == 401
    assert "reset_token" not in json.loads(
        _post(app, "/password/reset-request", {"email": "stranger@evil.com"}).body)


def test_signup_with_a_dead_invitation_is_refused_and_leaves_no_account() -> None:
    app = _make_app({"t": NOW})
    assert app._invitations is not None
    token = _invite(app, "ada@acme.com")
    app._invitations.revoke(token, "owner@rgnr8.co")
    r = _post(app, "/signup", {"token": token, "password": "hunter2222"})
    assert r.status == 400
    # the credential is never minted, so a revoked invite leaves nothing behind
    assert _post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"}).status == 401
    assert app._users is not None and app._users.membership("ada@acme.com", "acme") is None
    # an unknown token is refused the same way
    assert _post(app, "/signup", {"token": "not-a-token", "password": "hunter2222"}).status == 400


def test_an_invitation_is_single_use() -> None:
    app = _make_app({"t": NOW})
    token = _invite(app, "ada@acme.com")
    assert _post(app, "/signup", {"token": token, "password": "hunter2222"}).status == 201
    again = _post(app, "/signup", {"token": token, "password": "another11"})
    assert again.status == 400


def test_signup_ignores_an_email_supplied_by_the_caller() -> None:
    # The credential and the membership must be the same person: posting someone
    # else's address alongside a valid token must not fork them apart.
    app = _make_app({"t": NOW})
    token = _invite(app, "ada@acme.com", role=Role.VIEWER)
    r = _post(app, "/signup", {"token": token, "email": "attacker@evil.com",
                               "password": "hunter2222"})
    assert r.status == 201
    assert json.loads(r.body)["email"] == "ada@acme.com"
    assert app._users is not None
    assert app._users.membership("attacker@evil.com", "acme") is None


def test_signup_rejects_weak_and_duplicate() -> None:
    app = _make_app({"t": NOW})
    assert _post(app, "/signup", {"token": _invite(app, "ada@acme.com"),
                                  "password": "x"}).status == 400
    assert _signup(app, "ada@acme.com", "hunter2222").status == 201
    dup = _signup(app, "ada@acme.com", "another11")
    assert dup.status == 400


def test_verify_endpoint_is_single_use() -> None:
    app = _make_app({"t": NOW})
    _signup(app, "ada@acme.com", "hunter2222")
    token = _mailed(app, "verify_email")
    assert _post(app, "/verify", {"token": token}).status == 200
    assert _post(app, "/verify", {"token": token}).status == 400        # consumed


# --- login -------------------------------------------------------------------


def test_login_blocked_until_verified_then_sets_cookie() -> None:
    app = _make_app({"t": NOW})
    _signup(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    token = _mailed(app, "verify_email")
    # not verified yet → 401, no cookie
    pre = _post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"})
    assert pre.status == 401 and "Set-Cookie" not in pre.headers
    # verify, then login succeeds with the same HttpOnly session cookie shape
    assert _post(app, "/verify", {"token": token}).status == 200
    ok = _post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"})
    assert ok.status == 302 and ok.headers.get("Location") == "/app"
    assert "rgnr8_session=" in ok.headers.get("Set-Cookie", "")
    assert "HttpOnly" in ok.headers.get("Set-Cookie", "")


def test_login_wrong_password_is_401_indistinguishable() -> None:
    app = _make_app({"t": NOW})
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    bad = _post(app, "/login", {"email": "ada@acme.com", "password": "WRONG"})
    unknown = _post(app, "/login", {"email": "ghost@acme.com", "password": "hunter2222"})
    assert bad.status == 401 and unknown.status == 401
    assert bad.body == unknown.body                                    # same message
    assert "Set-Cookie" not in bad.headers and "Set-Cookie" not in unknown.headers


def test_verified_login_can_reach_the_app() -> None:
    app = _make_app({"t": NOW})
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    cookie = _cookie_from(_post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"}))
    home = app.handle(Request("GET", "/app", {"cookie": cookie}))
    assert home.status == 200                                          # authenticated session works


def test_an_account_with_no_membership_reaches_no_business() -> None:
    # The other half of closing the tenant fallback: even a real, verified
    # credential must land nowhere until an invitation grants it a membership.
    # Previously the sole tenant was handed to any authenticated caller.
    app = _make_app({"t": NOW})
    assert app._auth_service is not None
    _cred, tok = app._auth_service.signup("stranger@evil.com", "hunter2222")
    app._auth_service.verify_email(tok)
    login = _post(app, "/login", {"email": "stranger@evil.com", "password": "hunter2222"})
    assert login.status == 403 and "Set-Cookie" not in login.headers
    assert "isn&#x27;t a member of any business" in login.body
    assert app._users is not None
    assert app._users.membership("stranger@evil.com", "acme") is None   # never auto-seated


def test_login_cannot_seat_itself_by_naming_a_tenant() -> None:
    # Naming a business in the login body used to resolve it and mint a VIEWER
    # membership on the spot — an invitation gate you could walk around.
    app = _make_app({"t": NOW})
    assert app._auth_service is not None
    _cred, tok = app._auth_service.signup("stranger@evil.com", "hunter2222")
    app._auth_service.verify_email(tok)
    r = _post(app, "/login", {"email": "stranger@evil.com", "password": "hunter2222",
                              "tenant": "acme"})
    assert r.status == 403 and "Set-Cookie" not in r.headers
    assert app._users is not None
    assert app._users.membership("stranger@evil.com", "acme") is None


def test_browser_session_cookie_works_with_an_hs256_authenticator() -> None:
    # Production shape: an hs256 JwtAuthenticator (verifying with the JWT secret)
    # AND a self-issued session cookie (signed with a DIFFERENT session secret).
    # The session cookie must still authenticate the browser — the authenticator
    # can't verify it, so _principal falls back to the session secret. Without
    # that fallback, login would loop straight back to /login.
    from rgnr8_web.auth import JwtAuthenticator

    clock = {"t": NOW}

    def token() -> str:
        return "tok-x"

    svc = AuthService(
        InMemoryCredentialStore(), InMemoryVerificationTokenStore(), None,
        clock=lambda: clock["t"], token_factory=token,
        salt_factory=lambda: b"0123456789abcdef", min_password_length=8,
    )
    users = InMemoryUserDirectory()
    app = WebApp(
        users=users,
        authenticator=JwtAuthenticator("JWT-secret-not-the-session-one"),  # different key
        session_secret="SESSION-secret",
        session_clock=lambda: clock["t"],
        auth_service=svc,
        invitations=InvitationService(users, InMemoryInvitationStore(),
                                      clock=lambda: clock["t"]),
    )
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    assert app._invitations is not None
    inv = app._invitations.invite("ada@acme.com", "acme", Role.OWNER, "owner@rgnr8.co")
    tok = json.loads(_post(app, "/signup", {"token": inv.token,
                                            "password": "hunter2222"}).body)["verify_token"]
    _post(app, "/verify", {"token": tok})
    login = _post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"})
    assert login.status == 302 and login.headers.get("Location") == "/app"   # single tenant → straight in
    home = app.handle(Request("GET", "/app", {"cookie": _cookie_from(login)}))
    assert home.status == 200                                          # the session cookie authenticates


# --- the "choose your view" chooser -----------------------------------------


def test_staff_login_lands_on_the_view_chooser() -> None:
    # A staffer with no business of their own logs in once and lands on the
    # chooser showing the RGNR8 Fin OS (operator console) tile — not auto-seated
    # into some tenant, and not bounced to a dead end.
    app = _make_app({"t": NOW})
    # Staff have no business to be invited into, so their credential is created
    # directly (as the platform onboarding path does) rather than via /signup.
    assert app._auth_service is not None
    _cred, token = app._auth_service.signup("boss@rgnr8.co", "hunter2222")
    app._auth_service.verify_email(token)
    assert app._users is not None
    app._users.set_platform_role("boss@rgnr8.co", Role.OPERATOR)
    ok = _post(app, "/login", {"email": "boss@rgnr8.co", "password": "hunter2222"})
    assert ok.status == 302 and ok.headers.get("Location") == "/choose"
    page = app.handle(Request("GET", "/choose", {"cookie": _cookie_from(ok)}))
    assert page.status == 200
    assert "Choose your view" in page.body
    assert "RGNR8 Fin OS" in page.body            # the staff console tile
    assert "/operator" in page.body               # links to the console, same origin


def test_multi_business_chooser_and_authorized_enter() -> None:
    app = _make_app({"t": NOW})
    app.add_tenant("beta", "Beta LLC", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    app.add_tenant("gamma", "Gamma Inc", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    assert app._users is not None
    app._users.set_membership("ada@acme.com", "beta", Role.OWNER)   # member of two, not gamma

    ok = _post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"})
    assert ok.status == 302 and ok.headers.get("Location") == "/choose"
    cookie = _cookie_from(ok)
    page = app.handle(Request("GET", "/choose", {"cookie": cookie}))
    assert page.status == 200 and "Acme Co" in page.body and "Beta LLC" in page.body
    assert "Gamma Inc" not in page.body                              # not a member → not offered

    # entering a business the user belongs to scopes the session and reaches /app
    enter = app.handle(Request("GET", "/choose/enter?tenant=beta", {"cookie": cookie}))
    assert enter.status == 302 and enter.headers.get("Location") == "/app"
    assert app.handle(Request("GET", "/app", {"cookie": _cookie_from(enter)})).status == 200

    # entering a business the user does NOT belong to is refused → back to chooser
    bad = app.handle(Request("GET", "/choose/enter?tenant=gamma", {"cookie": cookie}))
    assert bad.status == 302 and bad.headers.get("Location") == "/choose"


def test_choose_requires_a_session() -> None:
    app = _make_app({"t": NOW})
    assert app.handle(Request("GET", "/choose")).headers.get("Location") == "/login"
    assert app.handle(Request("GET", "/choose/enter?tenant=acme")).headers.get("Location") == "/login"


# --- password reset ----------------------------------------------------------


def test_password_reset_flow_over_http() -> None:
    app = _make_app({"t": NOW})
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    req = _post(app, "/password/reset-request", {"email": "ada@acme.com"})
    assert req.status == 202
    assert "reset_token" not in json.loads(req.body)   # emailed, never in the response
    reset_token = _mailed(app, "password_reset")
    done = _post(app, "/password/reset", {"token": reset_token, "password": "brand-new-pass"})
    assert done.status == 200
    # old password no longer works; new one does
    assert _post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"}).status == 401
    assert _post(app, "/login", {"email": "ada@acme.com", "password": "brand-new-pass"}).status == 302


def test_reset_request_for_unknown_email_still_202_without_token() -> None:
    app = _make_app({"t": NOW})
    r = _post(app, "/password/reset-request", {"email": "ghost@acme.com"})
    assert r.status == 202
    assert "reset_token" not in json.loads(r.body)                     # no membership leak


def test_reset_with_expired_token_fails() -> None:
    clock = {"t": NOW}
    app = _make_app(clock)
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    _post(app, "/password/reset-request", {"email": "ada@acme.com"})
    reset_token = _mailed(app, "password_reset")
    clock["t"] = NOW + 2 * HOUR                                        # past the 1h TTL
    assert _post(app, "/password/reset", {"token": reset_token, "password": "brand-new-pass"}).status == 400


# --- where emailed links land ------------------------------------------------
# A mail client can only issue a GET. Both of these routes were POST-only, so
# every link the emailer sends would have 404'd on arrival.


def _get(app: WebApp, path: str) -> Any:
    return app.handle(Request("GET", path))


def test_emailed_verify_link_confirms_the_address() -> None:
    app = _make_app({"t": NOW})
    _signup(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    token = _mailed(app, "verify_email")
    # blocked before clicking
    assert _post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"}).status == 401
    landed = _get(app, f"/verify?token={token}")
    assert landed.status == 200 and "Email confirmed" in landed.body
    assert _post(app, "/login", {"email": "ada@acme.com", "password": "hunter2222"}).status == 302


def test_a_spent_verify_link_lands_somewhere_useful() -> None:
    app = _make_app({"t": NOW})
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    again = _get(app, f"/verify?token={_mailed(app, 'verify_email')}")
    assert again.status == 400
    assert "no longer valid" in again.body
    assert "current-password" in again.body          # the real sign-in form, not a dead end
    assert "Sign in as (demo)" not in again.body


def test_emailed_reset_link_renders_a_form_without_spending_the_token() -> None:
    app = _make_app({"t": NOW})
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    _post(app, "/password/reset-request", {"email": "ada@acme.com"})
    token = _mailed(app, "password_reset")

    page = _get(app, f"/password/reset?token={token}")
    assert page.status == 200
    assert f'value="{token}"' in page.body            # carried in a hidden field
    assert "new-password" in page.body
    # a link-prefetcher visiting twice must not burn the one reset
    assert _get(app, f"/password/reset?token={token}").status == 200

    done = app.handle(Request("POST", "/password/reset", {},
                              f"token={token}&password=brand-new-pass"))
    assert done.status == 200 and "Password updated" in done.body
    assert _post(app, "/login", {"email": "ada@acme.com", "password": "brand-new-pass"}).status == 302


def test_a_rejected_new_password_keeps_the_form_and_the_token() -> None:
    app = _make_app({"t": NOW})
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    _post(app, "/password/reset-request", {"email": "ada@acme.com"})
    token = _mailed(app, "password_reset")
    weak = app.handle(Request("POST", "/password/reset", {}, f"token={token}&password=short"))
    assert weak.status == 400 and f'value="{token}"' in weak.body
    # the token survived a rejected attempt, so retrying just works
    ok = app.handle(Request("POST", "/password/reset", {},
                            f"token={token}&password=long-enough-now"))
    assert ok.status == 200


def test_forgot_password_page_says_the_same_thing_either_way() -> None:
    # The endpoint behind it already answers 202 for unknown addresses; the page
    # must not undo that by confirming which accounts exist.
    app = _make_app({"t": NOW})
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    assert _get(app, "/password/forgot").status == 200

    known = app.handle(Request("POST", "/password/forgot", {}, "email=ada@acme.com"))
    ghost = app.handle(Request("POST", "/password/forgot", {}, "email=ghost@acme.com"))
    assert known.status == 302 and ghost.status == 302
    assert known.headers.get("Location") == ghost.headers.get("Location")
    assert _get(app, "/password/forgot?sent=1").body == _get(app, "/password/forgot?sent=1").body
    # ...but only the real one actually got mail
    assert [to for (to, p, _t) in app._outbox if p == "password_reset"] == ["ada@acme.com"]


def test_forgot_link_is_offered_only_when_mail_can_be_delivered() -> None:
    app = _make_app({"t": NOW})                       # emailer wired
    assert "/password/forgot" in _get(app, "/login").body
    app._emailer = None                               # beta posture: no outbound mail
    assert "/password/forgot" not in _get(app, "/login").body


# --- audit -------------------------------------------------------------------


def test_audit_events_recorded_for_signup_verify_reset() -> None:
    audit = InMemoryAuditLog()
    app = _make_app({"t": NOW}, audit=audit)
    _signup_verified(app, "ada@acme.com", "hunter2222", role=Role.OWNER)
    _post(app, "/password/reset-request", {"email": "ada@acme.com"})
    _post(app, "/password/reset", {"token": _mailed(app, "password_reset"),
                                   "password": "brand-new-pass"})
    actions = [e.action for e in audit.events()]
    # the invitation and the membership it grants are auditable too, so the
    # whole "how did this person get in" chain is on the record
    assert actions == ["invite.created", "user.signup", "membership.set",
                       "user.verified", "password.reset"]


# --- data export (owner-gated) ----------------------------------------------


def _login_cookie(app: WebApp, email: str, password: str) -> str:
    return _cookie_from(_post(app, "/login", {"email": email, "password": password}))


def test_export_is_owner_gated_and_returns_bundle() -> None:
    audit = InMemoryAuditLog()
    app = _make_app({"t": NOW}, audit=audit)
    # invite two people at different roles; the invitation is what seats them
    _signup_verified(app, "owner@acme.com", "hunter2222", role=Role.OWNER)
    _signup_verified(app, "view@acme.com", "hunter2222", role=Role.VIEWER)
    directory = app._users
    assert directory is not None
    # a tenant-scoped audit event should appear in the export (auth events are global)
    audit.record("owner@acme.com", "close.sealed", NOW, tenant_id="acme", detail="2026-08")

    owner_cookie = _login_cookie(app, "owner@acme.com", "hunter2222")
    view_cookie = _login_cookie(app, "view@acme.com", "hunter2222")

    ok = app.handle(Request("GET", "/api/acme/export", {"cookie": owner_cookie}))
    assert ok.status == 200
    bundle = json.loads(ok.body)
    assert bundle["tenant"] == "acme"
    assert "forecast_inputs" in bundle and "decisions" in bundle
    assert "transactions" in bundle and "close_board" in bundle
    assert any(e["action"] == "close.sealed" for e in bundle["audit_events"])

    # viewer (default role) lacks manage_users → forbidden
    denied = app.handle(Request("GET", "/api/acme/export", {"cookie": view_cookie}))
    assert denied.status == 403


# --- back-compat: dev/static login unaffected when no credential store -------


def test_dev_login_unchanged_without_credential_store() -> None:
    # No auth_service/credentials configured → the dev email+role login still works
    # with no password required (the existing behavior).
    users = InMemoryUserDirectory()
    users.upsert_user(User("owner@acme.com", "owner@acme.com", "Ada"))
    users.set_membership("owner@acme.com", "acme", Role.OWNER)
    app = WebApp(users=users, session_secret=SECRET, session_clock=lambda: NOW)
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    r = app.handle(Request("POST", "/login", {}, "email=owner@acme.com&role=owner"))
    assert r.status == 302 and "rgnr8_session=" in r.headers.get("Set-Cookie", "")
    # and the public-track endpoints are inert (501) without a service
    assert app.handle(Request("POST", "/signup", {}, '{"email":"x@acme.com","password":"hunter2222"}')).status == 501


# --- SSO mode: the chooser must not try to re-mint a self-issued cookie ------


class _IdpAuthenticator:
    """Stands in for a real IdP-backed authenticator: resolves a bearer/cookie to
    (tenant, subject) without any self-issued session secret being configured."""

    def __init__(self, subject: str, tenant: str) -> None:
        self._principal = (tenant, subject)

    def tenant_for(self, headers: Any) -> str | None:
        return self._principal[0] if headers.get("authorization") else None

    def principal_for(self, headers: Any) -> tuple[str, str] | None:
        return self._principal if headers.get("authorization") else None


def _sso_app() -> WebApp:
    """jwks/SSO posture: an authenticator is configured and `session_secret` is
    deliberately unset (see Settings.from_env — in jwks mode the IdP mints tokens)."""
    users = InMemoryUserDirectory()
    users.upsert_user(User(id="ada@acme.com", email="ada@acme.com"))
    users.set_membership("ada@acme.com", "acme", Role.OWNER)
    app = WebApp(
        users=users,
        session_secret=None,                       # <- no self-issued secret
        authenticator=_IdpAuthenticator("ada@acme.com", "acme"),
        session_clock=lambda: NOW,
    )
    app.add_tenant("acme", "Acme Co", _inputs(),
                   ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="unused")
    return app


def test_choose_enter_does_not_crash_without_a_self_issued_secret() -> None:
    """`/choose/enter` re-mints the self-issued session cookie. In SSO mode there
    is no secret to sign with, but `_session_subject` still resolves a subject
    through the IdP — so the tenant check passed and `sign_jwt(claims, None)` blew
    up on `None.encode()`, turning a normal click into a 500. mypy caught it as
    `Argument 2 to "sign_jwt" has incompatible type "str | None"`."""
    app = _sso_app()
    resp = app.handle(Request("GET", "/choose/enter?tenant=acme", {"cookie": "rgnr8_session=idp-issued-token"}))
    assert resp.status == 400, resp.status
    assert "identity provider" in resp.body


def test_choose_page_still_renders_under_sso() -> None:
    """The guard is scoped to re-minting: the chooser itself must still list the
    businesses, or SSO users would lose the screen entirely."""
    app = _sso_app()
    page = app.handle(Request("GET", "/choose", {"cookie": "rgnr8_session=idp-issued-token"}))
    assert page.status == 200 and "Acme Co" in page.body
