"""Real-IdP authentication: RS256 JWT verification against a JWKS endpoint.

The HS256 path (`auth.py`) is a shared-secret session token — fine for a
self-issued session, but production identity providers (Auth0, Okta, Cognito,
Entra ID) issue **RS256** tokens signed with a private key you never hold; you
verify them against the provider's published **JWKS** (public keys), selecting
the key by the token's `kid` and honoring key rotation. This module implements
that verification **dependency-free** — RSA is integer modular exponentiation
plus a PKCS#1 v1.5 unpad, and SHA-256 is `hashlib` — matching the hand-rolled
HS256 verifier's ethos and its security posture:

- the algorithm is pinned to `RS256`; `alg: none` and an HS256/RS256 confusion
  attempt are rejected before any key work,
- the signature is checked with a constant-time compare of the full PKCS#1 block,
- `exp`/`nbf` are checked against an **injected clock** (deterministic tests),
- `iss` and `aud` are enforced against configured expected values,
- an unknown `kid` triggers exactly one JWKS refresh (key rotation) then fails.
"""

from __future__ import annotations

import base64
import hmac
import json
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from hashlib import sha256
from typing import Protocol

from .auth import JwtError, _bearer, _b64url_decode

# DER prefix of DigestInfo(SHA-256) for PKCS#1 v1.5 EMSA.
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")

Jwk = Mapping[str, object]


def _b64u_uint(seg: str) -> int:
    return int.from_bytes(_b64url_decode(seg), "big")


def _find_jwk(keys: Sequence[Jwk], kid: str) -> Jwk | None:
    for k in keys:
        if str(k.get("kid", "")) == kid:
            return k
    if kid == "" and len(keys) == 1:  # single-key JWKS, token omitted kid
        return keys[0]
    return None


def _check_claims(claims: dict[str, object], *, issuer: str | None, audience: str | None,
                  now: int, leeway: int) -> None:
    exp = claims.get("exp")
    # exp is REQUIRED: an IdP token without a numeric expiry would never expire.
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise JwtError("token missing exp")
    if now > exp + leeway:
        raise JwtError("token expired")
    nbf = claims.get("nbf")
    if isinstance(nbf, (int, float)) and now + leeway < nbf:
        raise JwtError("token not yet valid")
    if issuer is not None and str(claims.get("iss", "")) != issuer:
        raise JwtError("issuer mismatch")
    if audience is not None:
        aud = claims.get("aud")
        ok = aud == audience or (isinstance(aud, list) and audience in aud)
        if not ok:
            raise JwtError("audience mismatch")


def verify_rs256(
    token: str,
    keys: Sequence[Jwk],
    *,
    issuer: str | None,
    audience: str | None,
    now: int,
    leeway: int = 0,
) -> dict[str, object]:
    """Verify an RS256 JWT against a set of JWKS keys; return claims or raise."""
    parts = token.split(".")
    if len(parts) != 3:
        raise JwtError("malformed token")
    hseg, pseg, sseg = parts

    try:
        header = json.loads(_b64url_decode(hseg))
    except (ValueError, json.JSONDecodeError) as exc:
        raise JwtError("unreadable header") from exc
    if not isinstance(header, dict) or header.get("alg") != "RS256":
        raise JwtError("unexpected alg (want RS256)")  # rejects none + HS/RS confusion

    jwk = _find_jwk(keys, str(header.get("kid", "")))
    if jwk is None:
        raise JwtError("no matching JWK for kid")
    if str(jwk.get("kty")) != "RSA":
        raise JwtError("unsupported key type")

    n = _b64u_uint(str(jwk["n"]))
    e = _b64u_uint(str(jwk["e"]))
    sig = _b64u_uint(sseg)
    if sig <= 0 or sig >= n:
        raise JwtError("bad signature")
    k = (n.bit_length() + 7) // 8

    em_int = pow(sig, e, n)
    try:
        em = em_int.to_bytes(k, "big")
    except OverflowError as exc:
        raise JwtError("bad signature") from exc

    digest = sha256(f"{hseg}.{pseg}".encode("ascii")).digest()
    t = _SHA256_DIGESTINFO + digest
    ps_len = k - 3 - len(t)
    if ps_len < 8:  # PKCS#1 requires >= 8 bytes of 0xFF padding
        raise JwtError("bad signature")
    expected = b"\x00\x01" + b"\xff" * ps_len + b"\x00" + t
    if not hmac.compare_digest(em, expected):
        raise JwtError("bad signature")

    try:
        claims = json.loads(_b64url_decode(pseg))
    except (ValueError, json.JSONDecodeError) as exc:
        raise JwtError("unreadable payload") from exc
    if not isinstance(claims, dict):
        raise JwtError("payload is not an object")

    _check_claims(claims, issuer=issuer, audience=audience, now=now, leeway=leeway)
    return dict(claims)


# --- JWKS providers ----------------------------------------------------------


class JwksProvider(Protocol):
    def keys(self) -> Sequence[Jwk]:
        """Current JWKS keys (may serve from cache)."""
        ...

    def refresh(self) -> None:
        """Force a re-fetch (called once on an unknown kid — key rotation)."""
        ...


class StaticJwksProvider:
    """A fixed key set — for tests and air-gapped deploys that pin keys."""

    def __init__(self, keys: Sequence[Jwk]) -> None:
        self._keys = list(keys)

    def keys(self) -> Sequence[Jwk]:
        return self._keys

    def refresh(self) -> None:
        return None


class JwksHttpSource(Protocol):
    def fetch(self, url: str) -> Mapping[str, object]:
        """GET the JWKS document (``{"keys": [...]}``) as parsed JSON."""
        ...


class UrllibJwksSource:
    """Stdlib-only JWKS fetcher (no third-party HTTP dependency)."""

    def __init__(self, *, timeout: float = 5.0) -> None:
        self._timeout = timeout

    def fetch(self, url: str) -> Mapping[str, object]:
        with urllib.request.urlopen(url, timeout=self._timeout) as resp:  # noqa: S310 (trusted IdP URL)
            payload = json.loads(resp.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {"keys": []}


class HttpJwksProvider:
    """Fetch + cache a JWKS with a TTL; `refresh()` forces a re-fetch. The clock
    is injectable so cache expiry is deterministic in tests."""

    def __init__(
        self,
        url: str,
        source: JwksHttpSource,
        *,
        ttl_seconds: int = 3600,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._url = url
        self._source = source
        self._ttl = ttl_seconds
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._cache: list[Jwk] = []
        self._fetched_at: int | None = None

    def keys(self) -> Sequence[Jwk]:
        now = self._clock()
        if self._fetched_at is None or now - self._fetched_at >= self._ttl:
            self.refresh()
        return self._cache

    def refresh(self) -> None:
        doc = self._source.fetch(self._url)
        raw = doc.get("keys")
        self._cache = [k for k in raw if isinstance(k, dict)] if isinstance(raw, list) else []
        self._fetched_at = self._clock()


# --- authenticator seam ------------------------------------------------------


class JwksAuthenticator:
    """Verify a real-IdP RS256 token against a JWKS provider and read the tenant
    from a claim. Enforces issuer + audience; refreshes keys once on an unknown
    `kid` (rotation). `clock` is injectable for deterministic tests."""

    def __init__(
        self,
        provider: JwksProvider,
        *,
        issuer: str | None = None,
        audience: str | None = None,
        tenant_claim: str = "tenant",
        subject_claim: str = "sub",
        leeway: int = 0,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self._provider = provider
        self._issuer = issuer
        self._audience = audience
        self._claim = tenant_claim
        self._subject_claim = subject_claim
        self._leeway = leeway
        self._clock = clock if clock is not None else (lambda: int(time.time()))

    def _verify(self, token: str) -> dict[str, object]:
        try:
            return verify_rs256(
                token, self._provider.keys(), issuer=self._issuer,
                audience=self._audience, now=self._clock(), leeway=self._leeway,
            )
        except JwtError:
            # kid may have rotated since we last fetched — refresh once, retry.
            self._provider.refresh()
            return verify_rs256(
                token, self._provider.keys(), issuer=self._issuer,
                audience=self._audience, now=self._clock(), leeway=self._leeway,
            )

    def tenant_for(self, headers: Mapping[str, str]) -> str | None:
        tok = _bearer(headers)
        if tok is None:
            return None
        try:
            claims = self._verify(tok)
        except JwtError:
            return None
        tenant = claims.get(self._claim)
        return tenant if isinstance(tenant, str) else None

    def principal_for(self, headers: Mapping[str, str]) -> tuple[str, str] | None:
        tok = _bearer(headers)
        if tok is None:
            return None
        try:
            claims = self._verify(tok)
        except JwtError:
            return None
        tenant = claims.get(self._claim)
        if not isinstance(tenant, str):
            return None
        subject = claims.get(self._subject_claim)
        return (tenant, subject if isinstance(subject, str) else tenant)


def jwk_from_public_numbers(n: int, e: int, kid: str) -> dict[str, object]:
    """Build a JWKS entry from raw RSA public numbers — handy for tests and for
    pinning a known key without a live JWKS endpoint."""
    def b64u(i: int) -> str:
        b = i.to_bytes((i.bit_length() + 7) // 8, "big")
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")

    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": b64u(n), "e": b64u(e)}
