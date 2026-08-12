"""RS256 / JWKS verification against a real-IdP-style key (dependency-free).

Uses a fixed 2048-bit RSA test key. A tiny pure-Python RS256 signer stands in
for the IdP; production never signs — it only verifies against the JWKS.
"""

import base64
import json

from rgnr8_web import (
    JwksAuthenticator,
    StaticJwksProvider,
    jwk_from_public_numbers,
    verify_rs256,
)

# --- fixed RSA test key (generated once via openssl; PRIVATE key is test-only) ---
N = 22429810602240851761439102585508743092009209457070556108060627950688841750930046771742219640619140783108739048105781252256669276407699406490253979290395326276774610460543044894035271473024892901211296579041294878937411549274344307214284382760195229677579676179096497883736201682923726931102077853844526650806347960001137140133037091786331165320994198055753565070390155943881548818364374738888476434149567415151968712643783379375237976920796100333191744723274517429929253469596247438293206808757421847814986155235385495862187400920211701920727599959879765586468197638859305578046695759580134710312041746100449631332287
E = 65537
D = 1697713894325583184500643428527642707293768154047309055556933410888383653592451546625986549999103527085629492471866699754630330128330453116482366667226238857377366299792846395118207488013488277407550493314224686008957230116962707111043497271494704316884111016470210381193423996340130726791459132917218890875740423169757093156967543354568104806012864312694295225532789224211625235248669080845265426610958346363659021850560136105718135667315333271329746797968395049769623286119905009991381642779174289392016677207711940384039420322167461495545435096020882956410539219901949200238924190727647631051884891980930196287033
KID = "test-1"
JWKS = [jwk_from_public_numbers(N, E, KID)]


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def sign_rs256(claims: dict, *, kid: str = KID) -> str:
    """Test-only RS256 signer using the private exponent (pure int mod-exp)."""
    from hashlib import sha256

    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    hseg = _b64u(json.dumps(header, separators=(",", ":")).encode())
    pseg = _b64u(json.dumps(claims, separators=(",", ":")).encode())
    digest = sha256(f"{hseg}.{pseg}".encode("ascii")).digest()
    t = _SHA256_DIGESTINFO + digest
    k = (N.bit_length() + 7) // 8
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(t)) + b"\x00" + t
    m = int.from_bytes(em, "big")
    s = pow(m, D, N)
    sig = _b64u(s.to_bytes(k, "big"))
    return f"{hseg}.{pseg}.{sig}"


NOW = 1_760_000_000
ISS = "https://issuer.example.com/"
AUD = "rgnr8-api"


def _claims(**over):
    base = {"iss": ISS, "aud": AUD, "tenant": "acme", "exp": NOW + 3600, "nbf": NOW - 10}
    base.update(over)
    return base


def test_verify_rs256_accepts_a_valid_token() -> None:
    tok = sign_rs256(_claims())
    claims = verify_rs256(tok, JWKS, issuer=ISS, audience=AUD, now=NOW)
    assert claims["tenant"] == "acme"


def test_rejects_expired_and_wrong_issuer_audience() -> None:
    import pytest

    with pytest.raises(Exception):
        verify_rs256(sign_rs256(_claims(exp=NOW - 1)), JWKS, issuer=ISS, audience=AUD, now=NOW)
    with pytest.raises(Exception):
        verify_rs256(sign_rs256(_claims(iss="https://evil/")), JWKS, issuer=ISS, audience=AUD, now=NOW)
    with pytest.raises(Exception):
        verify_rs256(sign_rs256(_claims(aud="other")), JWKS, issuer=ISS, audience=AUD, now=NOW)


def test_rejects_tampered_payload_and_alg_confusion() -> None:
    import pytest

    tok = sign_rs256(_claims())
    h, p, s = tok.split(".")
    forged = _b64u(json.dumps({**_claims(), "tenant": "evil"}, separators=(",", ":")).encode())
    with pytest.raises(Exception):
        verify_rs256(f"{h}.{forged}.{s}", JWKS, issuer=ISS, audience=AUD, now=NOW)

    # alg:none confusion
    none_h = _b64u(json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":")).encode())
    with pytest.raises(Exception):
        verify_rs256(f"{none_h}.{p}.", JWKS, issuer=ISS, audience=AUD, now=NOW)


def test_jwks_authenticator_reads_tenant() -> None:
    auth = JwksAuthenticator(StaticJwksProvider(JWKS), issuer=ISS, audience=AUD,
                             clock=lambda: NOW)
    tok = sign_rs256(_claims())
    assert auth.tenant_for({"authorization": f"Bearer {tok}"}) == "acme"
    # a bad token yields None (rejected), not an exception
    assert auth.tenant_for({"authorization": "Bearer not.a.jwt"}) is None
    assert auth.tenant_for({}) is None


def test_authenticator_refreshes_keys_on_unknown_kid() -> None:
    # provider starts empty; refresh() installs the real key → rotation handling
    class Rotating:
        def __init__(self) -> None:
            self.refreshed = 0
            self._keys: list = []

        def keys(self):
            return self._keys

        def refresh(self):
            self.refreshed += 1
            self._keys = JWKS

    prov = Rotating()
    auth = JwksAuthenticator(prov, issuer=ISS, audience=AUD, clock=lambda: NOW)
    tok = sign_rs256(_claims())
    assert auth.tenant_for({"authorization": f"Bearer {tok}"}) == "acme"
    assert prov.refreshed == 1  # refreshed exactly once to pick up the key
