"""JWT session auth: signing/verification (incl. the alg-confusion and expiry
guards) and wiring into the web app as the production authenticator."""

from __future__ import annotations

import base64
import json
from datetime import date

from rgnr8_forecast import CashPosition, ForecastConfig, ForecastInputs, Invoice, Money
from rgnr8_web import (
    JwtAuthenticator,
    JwtError,
    Request,
    WebApp,
    sign_jwt,
    verify_jwt,
)

SECRET = "s3cret-signing-key"
NOW = 1_760_000_000  # a fixed epoch for determinism


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


# --- verify_jwt --------------------------------------------------------------

def test_sign_then_verify_roundtrips_claims() -> None:
    tok = sign_jwt({"tenant": "bright", "exp": NOW + 3600}, SECRET)
    claims = verify_jwt(tok, SECRET, now=NOW)
    assert claims["tenant"] == "bright"


def test_expired_token_is_rejected() -> None:
    tok = sign_jwt({"tenant": "bright", "exp": NOW - 1}, SECRET)
    try:
        verify_jwt(tok, SECRET, now=NOW)
    except JwtError as e:
        assert "expired" in str(e)
    else:
        raise AssertionError("expected expiry rejection")


def test_not_before_is_respected() -> None:
    tok = sign_jwt({"tenant": "bright", "nbf": NOW + 100, "exp": NOW + 1000}, SECRET)
    try:
        verify_jwt(tok, SECRET, now=NOW)
    except JwtError:
        pass
    else:
        raise AssertionError("expected nbf rejection")
    # valid once nbf has passed
    assert verify_jwt(tok, SECRET, now=NOW + 200)["tenant"] == "bright"


def test_missing_exp_is_rejected() -> None:
    tok = sign_jwt({"tenant": "bright"}, SECRET)  # no exp → must be rejected
    try:
        verify_jwt(tok, SECRET, now=NOW)
    except JwtError:
        pass
    else:
        raise AssertionError("expected missing-exp rejection")


def test_wrong_secret_is_rejected() -> None:
    tok = sign_jwt({"tenant": "bright", "exp": NOW + 3600}, SECRET)
    try:
        verify_jwt(tok, "not-the-secret", now=NOW)
    except JwtError as e:
        assert "signature" in str(e)
    else:
        raise AssertionError("expected signature rejection")


def test_alg_none_confusion_attack_is_rejected() -> None:
    # a token that claims alg:none with an empty signature must not be accepted
    header = _b64url(json.dumps({"alg": "none", "typ": "JWT"}).encode())
    payload = _b64url(json.dumps({"tenant": "bright"}).encode())
    forged = f"{header}.{payload}."
    try:
        verify_jwt(forged, SECRET, now=NOW)
    except JwtError:
        pass
    else:
        raise AssertionError("alg:none must be rejected")


def test_tampered_payload_is_rejected() -> None:
    tok = sign_jwt({"tenant": "bright", "exp": NOW + 3600}, SECRET)
    header, _payload, sig = tok.split(".")
    evil = _b64url(json.dumps({"tenant": "acme", "exp": NOW + 3600}).encode())
    try:
        verify_jwt(f"{header}.{evil}.{sig}", SECRET, now=NOW)
    except JwtError:
        pass
    else:
        raise AssertionError("tampered payload must fail signature check")


def test_malformed_token_is_rejected() -> None:
    for bad in ("", "a.b", "not-a-token", "a.b.c.d"):
        try:
            verify_jwt(bad, SECRET, now=NOW)
        except JwtError:
            pass
        else:
            raise AssertionError(f"expected rejection for {bad!r}")


# --- web app wiring ----------------------------------------------------------

def _inputs() -> ForecastInputs:
    return ForecastInputs(
        opening=CashPosition(as_of=date(2026, 8, 3), available=Money.from_decimal("80000.00")),
        invoices=(Invoice("INV-1", "acme", date(2026, 8, 1), date(2026, 8, 20), Money.from_decimal("15000.00")),),
    )


def _app_with_jwt() -> WebApp:
    auth = JwtAuthenticator(SECRET, clock=lambda: NOW)
    app = WebApp(authenticator=auth)
    for tid in ("bright", "acme"):
        app.add_tenant(tid, tid.title(), _inputs(), ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token=f"tok-{tid}")
    return app


def _bearer(tok: str) -> dict[str, str]:
    return {"authorization": f"Bearer {tok}"}


def test_web_app_authorizes_via_jwt() -> None:
    app = _app_with_jwt()
    tok = sign_jwt({"tenant": "bright", "exp": NOW + 3600}, SECRET)
    r = app.handle(Request("GET", "/api/bright/today", _bearer(tok)))
    assert r.status == 200


def test_web_app_rejects_missing_and_expired_jwt() -> None:
    app = _app_with_jwt()
    assert app.handle(Request("GET", "/api/bright/today", {})).status == 401
    expired = sign_jwt({"tenant": "bright", "exp": NOW - 5}, SECRET)
    assert app.handle(Request("GET", "/api/bright/today", _bearer(expired))).status == 401


def test_jwt_tenant_isolation_is_enforced() -> None:
    app = _app_with_jwt()
    # a valid token for bright cannot reach acme's data
    tok = sign_jwt({"tenant": "bright", "exp": NOW + 3600}, SECRET)
    assert app.handle(Request("GET", "/api/acme/today", _bearer(tok))).status == 403


def test_jwt_for_unknown_tenant_reaches_nothing() -> None:
    app = _app_with_jwt()
    tok = sign_jwt({"tenant": "ghost", "exp": NOW + 3600}, SECRET)
    # validly signed, but 'ghost' isn't a registered tenant
    assert app.handle(Request("GET", "/api/ghost/today", _bearer(tok))).status == 404


def test_static_token_map_still_works_without_an_authenticator() -> None:
    # backwards-compatible default path
    app = WebApp()
    app.add_tenant("bright", "Bright", _inputs(), ForecastConfig(minimum_cash=Money.from_decimal("10000.00")), token="tok-bright")
    assert app.handle(Request("GET", "/api/bright/today", _bearer("tok-bright"))).status == 200
    assert app.handle(Request("GET", "/api/bright/today", _bearer("wrong"))).status == 401
