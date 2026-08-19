"""API keys: issued once, hashed at rest, verified in constant time, scoped."""

from __future__ import annotations

import itertools

import pytest
from rgnr8_apikeys import (
    ApiKeyError,
    ApiKeyService,
    AuthError,
    InMemoryApiKeyStore,
    authenticate,
    require,
)


def make_service() -> ApiKeyService:
    # deterministic rng: distinct tokens of the requested length, and a fixed clock
    counter = itertools.count(1)

    def rng(n: int) -> str:
        return f"{next(counter):x}".rjust(n, "0")

    return ApiKeyService(InMemoryApiKeyStore(), rng=rng, clock=lambda: 1000.0)


def test_issue_returns_the_full_key_once_and_stores_only_a_hash() -> None:
    svc = make_service()
    issued = svc.issue("acme", {"ledger:read", "invoices:read"}, "reporting bot")
    assert issued.full_key.startswith("rgnr8_live_")
    # the stored record has no secret, only a hash
    assert issued.record.secret_hash != ""
    assert issued.full_key.split("_")[-1] not in issued.record.secret_hash


def test_a_valid_key_verifies_to_its_record() -> None:
    svc = make_service()
    issued = svc.issue("acme", {"ledger:read"}, "bot")
    record = svc.verify(issued.full_key)
    assert record is not None
    assert record.tenant == "acme"
    assert record.allows("ledger:read")


def test_a_tampered_or_unknown_key_fails() -> None:
    svc = make_service()
    issued = svc.issue("acme", {"ledger:read"}, "bot")
    assert svc.verify(issued.full_key[:-1] + "0") is None  # wrong secret
    assert svc.verify("rgnr8_live_deadbeef_nope") is None  # unknown prefix
    assert svc.verify("not-even-a-key") is None


def test_revocation_disables_the_key() -> None:
    svc = make_service()
    issued = svc.issue("acme", {"ledger:read"}, "bot")
    assert svc.verify(issued.full_key) is not None
    assert svc.revoke(issued.record.prefix) is True
    assert svc.verify(issued.full_key) is None, "a revoked key no longer verifies"


def test_unknown_scope_is_refused_at_issue() -> None:
    svc = make_service()
    with pytest.raises(ApiKeyError):
        svc.issue("acme", {"ledger:read", "launch:missiles"}, "bad")
    with pytest.raises(ApiKeyError):
        svc.issue("acme", set(), "empty")


def test_auth_layer_maps_header_to_tenant_and_enforces_scope() -> None:
    svc = make_service()
    issued = svc.issue("acme", {"invoices:read"}, "bot")
    ctx = authenticate(svc, f"Bearer {issued.full_key}")
    assert ctx.tenant == "acme"
    require(ctx, "invoices:read")  # allowed → no raise
    with pytest.raises(AuthError) as ei:
        require(ctx, "invoices:write")  # not granted
    assert ei.value.status == 403


def test_auth_layer_rejects_missing_or_malformed_headers() -> None:
    svc = make_service()
    for header in (None, "", "Basic abc", "Bearer", "Bearer bogus"):
        with pytest.raises(AuthError) as ei:
            authenticate(svc, header)
        assert ei.value.status == 401


def test_redacted_view_never_leaks_the_secret() -> None:
    svc = make_service()
    issued = svc.issue("acme", {"reports:read"}, "bot")
    view = issued.record.redacted()
    assert "secret_hash" not in view
    assert "salt" not in view
    assert view["tenant"] == "acme"
    assert view["scopes"] == ["reports:read"]
