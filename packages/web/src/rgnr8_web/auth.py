"""Authentication for the owner surface.

The dev/default path is a static bearer-token→tenant map. Production wants real,
verifiable **sessions**: a signed JWT the client presents, which the server
verifies without a lookup table. This module provides both behind one
`Authenticator` seam (`tenant_for(headers) -> tenant_id | None`), plus a small,
dependency-free HS256 JWT implementation (`sign_jwt` / `verify_jwt`).

Security notes baked in: the verifier pins the algorithm to HS256 and **rejects
`alg: none` and any algorithm mismatch** (the classic JWT confusion attack),
compares signatures in constant time, and checks `exp`/`nbf` against an
**injected clock** (so verification is deterministic and testable — no hidden
system time). A tenant is taken from a configurable claim; the web app still
requires that tenant to be registered, so a validly-signed token for an unknown
tenant reaches nothing.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
from collections.abc import Callable, Mapping
from hashlib import sha256
from typing import Protocol

ALG = "HS256"


class JwtError(Exception):
    """Any failure to produce a trustworthy set of claims from a token."""


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _sign(signing_input: bytes, secret: str) -> str:
    sig = hmac.new(secret.encode("utf-8"), signing_input, sha256).digest()
    return _b64url_encode(sig)


def sign_jwt(claims: Mapping[str, object], secret: str) -> str:
    """Mint an HS256 JWT for the given claims. Callers set `exp`/`nbf`/`iat`
    themselves (epoch seconds) so token lifetime stays an explicit decision."""
    header = {"alg": ALG, "typ": "JWT"}
    header_seg = _b64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    payload_seg = _b64url_encode(json.dumps(dict(claims), separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_seg}.{payload_seg}".encode("ascii")
    return f"{header_seg}.{payload_seg}.{_sign(signing_input, secret)}"


def verify_jwt(token: str, secret: str, *, now: int, leeway: int = 0) -> dict[str, object]:
    """Verify an HS256 JWT and return its claims, or raise `JwtError`.

    Checks: exactly three segments; header `alg == HS256` (rejects `none` and
    mismatches); constant-time signature match; `exp`/`nbf` against `now`
    (± `leeway` seconds). `now` is injected — never read from the clock here."""
    parts = token.split(".")
    if len(parts) != 3:
        raise JwtError("malformed token")
    header_seg, payload_seg, sig_seg = parts

    try:
        header = json.loads(_b64url_decode(header_seg))
    except (ValueError, json.JSONDecodeError) as exc:
        raise JwtError("unreadable header") from exc
    if not isinstance(header, dict) or header.get("alg") != ALG:
        raise JwtError(f"unexpected alg (want {ALG})")

    expected = _sign(f"{header_seg}.{payload_seg}".encode("ascii"), secret)
    if not hmac.compare_digest(expected, sig_seg):
        raise JwtError("bad signature")

    try:
        claims = json.loads(_b64url_decode(payload_seg))
    except (ValueError, json.JSONDecodeError) as exc:
        raise JwtError("unreadable payload") from exc
    if not isinstance(claims, dict):
        raise JwtError("payload is not an object")

    exp = claims.get("exp")
    # exp is REQUIRED: a token without a numeric expiry would never expire.
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise JwtError("token missing exp")
    if now > exp + leeway:
        raise JwtError("token expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now + leeway < nbf:
        raise JwtError("token not yet valid")

    return dict(claims)


# --- authenticator seam ------------------------------------------------------


def _bearer(headers: Mapping[str, str]) -> str | None:
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    return auth[7:].strip()


class Authenticator(Protocol):
    def tenant_for(self, headers: Mapping[str, str]) -> str | None:
        """The tenant a request is authenticated as, or None to reject (401)."""
        ...


class StaticTokenAuthenticator:
    """The dev/default map: opaque bearer token → tenant id."""

    def __init__(self, tokens: Mapping[str, str]) -> None:
        self._tokens = tokens

    def tenant_for(self, headers: Mapping[str, str]) -> str | None:
        tok = _bearer(headers)
        return self._tokens.get(tok) if tok is not None else None

    def principal_for(self, headers: Mapping[str, str]) -> tuple[str, str] | None:
        """(tenant, subject). A static token has no user, so the subject is the
        tenant itself — RBAC only kicks in with real user tokens."""
        t = self.tenant_for(headers)
        return (t, t) if t is not None else None


class JwtAuthenticator:
    """Verify a signed session JWT and read the tenant from a claim. `clock` is
    injectable (epoch seconds) so tests are deterministic; defaults to the
    system clock."""

    def __init__(
        self,
        secret: str,
        *,
        tenant_claim: str = "tenant",
        subject_claim: str = "sub",
        leeway: int = 0,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._secret = secret
        self._claim = tenant_claim
        self._subject_claim = subject_claim
        self._leeway = leeway
        self._clock = clock if clock is not None else (lambda: int(time.time()))

    def _claims(self, headers: Mapping[str, str]) -> dict[str, object] | None:
        tok = _bearer(headers)
        if tok is None:
            return None
        try:
            return verify_jwt(tok, self._secret, now=self._clock(), leeway=self._leeway)
        except JwtError:
            return None

    def tenant_for(self, headers: Mapping[str, str]) -> str | None:
        claims = self._claims(headers)
        if claims is None:
            return None
        tenant = claims.get(self._claim)
        return tenant if isinstance(tenant, str) else None

    def principal_for(self, headers: Mapping[str, str]) -> tuple[str, str] | None:
        """(tenant, subject). Subject comes from the `sub` claim (the user); it
        falls back to the tenant when the token carries no subject."""
        claims = self._claims(headers)
        if claims is None:
            return None
        tenant = claims.get(self._claim)
        if not isinstance(tenant, str):
            return None
        subject = claims.get(self._subject_claim)
        return (tenant, subject if isinstance(subject, str) else tenant)
