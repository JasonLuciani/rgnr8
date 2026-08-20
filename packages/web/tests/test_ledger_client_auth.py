"""The ledger client presents a per-tenant token, not the raw shared secret.

This is the client half of H1-1: the bearer sent to the ledger is HMAC(secret,
tenant) derived from the request path, so a credential can only act on the tenant
it was minted for. Public paths (/health) need no tenant token.
"""

import hashlib
import hmac
from typing import Any

from rgnr8_web import LedgerClient, LedgerResponse

SECRET = "svc-secret"


class CapturingTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, path: str, body: str, headers: Any) -> LedgerResponse:
        self.calls.append((method, path, dict(headers)))
        return LedgerResponse(200, {"rows": []})


def _expected(tenant: str) -> str:
    return hmac.new(SECRET.encode(), tenant.encode(), hashlib.sha256).hexdigest()


def test_tenant_paths_get_a_per_tenant_bearer() -> None:
    t = CapturingTransport()
    c = LedgerClient(t, token=SECRET)
    c.trial_balance("acme")
    c.trial_balance("beta")
    auth_acme = t.calls[0][2]["authorization"]
    auth_beta = t.calls[1][2]["authorization"]
    assert auth_acme == f"Bearer {_expected('acme')}"
    assert auth_beta == f"Bearer {_expected('beta')}"
    # different tenants → different tokens, and neither is the raw secret
    assert auth_acme != auth_beta
    assert SECRET not in auth_acme


def test_public_health_path_uses_no_tenant_token() -> None:
    t = CapturingTransport()
    LedgerClient(t, token=SECRET).health()
    # /health carries the raw secret only (service leaves it public); not a tenant token
    assert t.calls[0][2]["authorization"] == f"Bearer {SECRET}"


def test_no_secret_means_no_auth_header() -> None:
    t = CapturingTransport()
    LedgerClient(t, token="").trial_balance("acme")
    assert "authorization" not in t.calls[0][2]
