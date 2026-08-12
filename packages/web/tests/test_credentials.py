"""Real end-user password auth: the scrypt hasher, the AuthService signup →
verify → login + reset flows, and the SQL credential/token stores."""

from __future__ import annotations

import sqlite3
from typing import Any, cast

from rgnr8_web import (
    AuthError,
    AuthService,
    Credential,
    InMemoryAuditLog,
    InMemoryCredentialStore,
    InMemoryVerificationTokenStore,
    PasswordHasher,
    SqlCredentialStore,
    SqlVerificationTokenStore,
    TokenPurpose,
    VerificationToken,
)

NOW = 1_760_000_000
HOUR = 3_600


def _fixed_salt() -> bytes:
    return b"0123456789abcdef"


def _svc(
    audit: InMemoryAuditLog | None = None,
    on_signup: "list[str] | None" = None,
    clock_ref: "dict[str, int] | None" = None,
) -> AuthService:
    clock = clock_ref if clock_ref is not None else {"t": NOW}
    seq = {"n": 0}

    def token() -> str:
        seq["n"] += 1
        return f"tok-{seq['n']}"

    hook = (lambda email: on_signup.append(email)) if on_signup is not None else None
    return AuthService(
        InMemoryCredentialStore(),
        InMemoryVerificationTokenStore(),
        audit,
        clock=lambda: clock["t"],
        token_factory=token,
        salt_factory=_fixed_salt,
        on_signup=hook,
        min_password_length=8,
        verify_ttl_hours=24,
        reset_ttl_hours=1,
    )


# --- the hasher --------------------------------------------------------------


def test_scrypt_hash_is_not_plaintext_and_verifies() -> None:
    h = PasswordHasher(salt_factory=_fixed_salt)
    enc = h.hash("correct horse battery")
    assert "correct horse battery" not in enc
    assert enc.startswith("scrypt$")
    assert h.verify("correct horse battery", enc) is True
    assert h.verify("wrong password", enc) is False


def test_tampered_hash_fails() -> None:
    h = PasswordHasher(salt_factory=_fixed_salt)
    enc = h.hash("s3cret-password")
    tampered = enc[:-4] + ("AAAA" if not enc.endswith("AAAA") else "BBBB")
    assert h.verify("s3cret-password", tampered) is False
    # a structurally broken encoding is False, not an exception
    assert h.verify("s3cret-password", "not-a-real-hash") is False


def test_distinct_salts_give_distinct_hashes() -> None:
    salts = iter([b"salt-aaaa-aaaa-aa", b"salt-bbbb-bbbb-bb"])
    h = PasswordHasher(salt_factory=lambda: next(salts))
    a = h.hash("same-password")
    b = h.hash("same-password")
    assert a != b
    assert h.verify("same-password", a) and h.verify("same-password", b)


# --- signup / verify / login -------------------------------------------------


def test_signup_creates_unverified_credential_and_token() -> None:
    audit = InMemoryAuditLog()
    hook: list[str] = []
    svc = _svc(audit=audit, on_signup=hook)
    cred, token = svc.signup("ada@acme.com", "hunter2222")
    assert cred.verified is False
    assert cred.user_id == "ada@acme.com" and cred.email == "ada@acme.com"
    assert token == "tok-1"
    assert hook == ["ada@acme.com"]                    # provisioning seam fired
    ev = audit.events()
    assert ev[0].action == "user.signup" and ev[0].target == "ada@acme.com"


def test_login_fails_until_verified() -> None:
    svc = _svc()
    svc.signup("ada@acme.com", "hunter2222")
    assert svc.login("ada@acme.com", "hunter2222") is None   # not verified yet


def test_correct_password_after_verify_logs_in() -> None:
    svc = _svc()
    _cred, token = svc.signup("ada@acme.com", "hunter2222")
    assert svc.verify_email(token) is True
    assert svc.login("ada@acme.com", "hunter2222") == "ada@acme.com"


def test_wrong_password_is_indistinguishable_from_unknown_email() -> None:
    svc = _svc()
    _cred, token = svc.signup("ada@acme.com", "hunter2222")
    svc.verify_email(token)
    assert svc.login("ada@acme.com", "nope-wrong") is None    # bad password
    assert svc.login("ghost@acme.com", "hunter2222") is None  # unknown email
    # both return None with the same type — no enumeration signal


def test_verification_token_is_single_use() -> None:
    audit = InMemoryAuditLog()
    svc = _svc(audit=audit)
    _cred, token = svc.signup("ada@acme.com", "hunter2222")
    assert svc.verify_email(token) is True
    assert svc.verify_email(token) is False                   # consumed
    assert [e.action for e in audit.events()] == ["user.signup", "user.verified"]


def test_verification_token_expires() -> None:
    clock = {"t": NOW}
    svc = _svc(clock_ref=clock)
    _cred, token = svc.signup("ada@acme.com", "hunter2222")
    clock["t"] = NOW + 25 * HOUR                              # past the 24h TTL
    assert svc.verify_email(token) is False
    assert svc.login("ada@acme.com", "hunter2222") is None    # still unverified


def test_weak_password_rejected() -> None:
    svc = _svc()
    try:
        svc.signup("ada@acme.com", "short")
        assert False, "expected AuthError"
    except AuthError:
        pass


def test_duplicate_email_rejected() -> None:
    svc = _svc()
    svc.signup("ada@acme.com", "hunter2222")
    try:
        svc.signup("ADA@acme.com", "another-pass")           # case-insensitive dup
        assert False, "expected AuthError"
    except AuthError:
        pass


def test_bad_email_rejected() -> None:
    svc = _svc()
    try:
        svc.signup("not-an-email", "hunter2222")
        assert False, "expected AuthError"
    except AuthError:
        pass


# --- password reset ----------------------------------------------------------


def test_password_reset_happy_path() -> None:
    audit = InMemoryAuditLog()
    svc = _svc(audit=audit)
    _cred, token = svc.signup("ada@acme.com", "hunter2222")
    svc.verify_email(token)
    reset = svc.request_password_reset("ada@acme.com")
    assert reset is not None
    assert svc.reset_password(reset, "brand-new-pass") is True
    assert svc.login("ada@acme.com", "hunter2222") is None     # old password dead
    assert svc.login("ada@acme.com", "brand-new-pass") == "ada@acme.com"
    assert any(e.action == "password.reset" for e in audit.events())


def test_reset_request_for_unknown_email_returns_none() -> None:
    svc = _svc()
    assert svc.request_password_reset("ghost@acme.com") is None


def test_reset_token_is_single_use_and_expiring() -> None:
    clock = {"t": NOW}
    svc = _svc(clock_ref=clock)
    _cred, token = svc.signup("ada@acme.com", "hunter2222")
    svc.verify_email(token)
    reset = svc.request_password_reset("ada@acme.com")
    assert reset is not None
    assert svc.reset_password(reset, "brand-new-pass") is True
    assert svc.reset_password(reset, "yet-another-pass") is False   # consumed

    reset2 = svc.request_password_reset("ada@acme.com")
    assert reset2 is not None
    clock["t"] = NOW + 2 * HOUR                                     # past the 1h TTL
    assert svc.reset_password(reset2, "too-late-pass") is False


def test_reset_with_invalid_token_fails() -> None:
    svc = _svc()
    assert svc.reset_password("nope", "brand-new-pass") is False


def test_verify_token_cannot_be_used_for_reset() -> None:
    # a verify-email token must not double as a reset token (purpose is checked)
    svc = _svc()
    _cred, token = svc.signup("ada@acme.com", "hunter2222")
    assert svc.reset_password(token, "brand-new-pass") is False


def test_reset_rejects_weak_password() -> None:
    svc = _svc()
    _cred, token = svc.signup("ada@acme.com", "hunter2222")
    svc.verify_email(token)
    reset = svc.request_password_reset("ada@acme.com")
    assert reset is not None
    assert svc.reset_password(reset, "short") is False


# --- SQL stores --------------------------------------------------------------


def test_sql_credential_store_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlCredentialStore(cast("Any", conn), placeholder="?")
    store.create_schema()
    h = PasswordHasher(salt_factory=_fixed_salt)
    cred = Credential("ada@acme.com", "ada@acme.com", h.hash("hunter2222"),
                      verified=False, created_at=NOW)
    store.save(cred)

    got = store.get_by_email("ADA@acme.com")                # case-insensitive
    assert got is not None and got.user_id == "ada@acme.com"
    assert got.verified is False
    assert h.verify("hunter2222", got.password_hash) is True

    store.save(Credential(cred.user_id, cred.email, cred.password_hash, verified=True,
                          created_at=cred.created_at))
    reread = store.get_by_user_id("ada@acme.com")
    assert reread is not None and reread.verified is True


def test_sql_verification_token_store_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    store = SqlVerificationTokenStore(cast("Any", conn), placeholder="?")
    store.create_schema()
    tok = VerificationToken("tok-abc", "ada@acme.com", "ada@acme.com",
                            TokenPurpose.VERIFY_EMAIL, created_at=NOW, expires_at=NOW + HOUR)
    store.save(tok)
    got = store.get("tok-abc")
    assert got is not None and got.purpose is TokenPurpose.VERIFY_EMAIL
    assert got.consumed is False and got.expires_at == NOW + HOUR

    store.save(VerificationToken(tok.token, tok.user_id, tok.email, tok.purpose,
                                 tok.created_at, tok.expires_at, consumed=True))
    reread = store.get("tok-abc")
    assert reread is not None and reread.consumed is True


def test_auth_service_over_sql_stores_end_to_end() -> None:
    conn = sqlite3.connect(":memory:")
    creds = SqlCredentialStore(cast("Any", conn), placeholder="?")
    creds.create_schema()
    toks = SqlVerificationTokenStore(cast("Any", conn), placeholder="?")
    toks.create_schema()
    seq = {"n": 0}

    def token() -> str:
        seq["n"] += 1
        return f"sql-tok-{seq['n']}"

    svc = AuthService(creds, toks, clock=lambda: NOW, token_factory=token,
                      salt_factory=_fixed_salt, min_password_length=8)
    _cred, vt = svc.signup("ada@acme.com", "hunter2222")
    assert svc.login("ada@acme.com", "hunter2222") is None
    assert svc.verify_email(vt) is True
    assert svc.login("ada@acme.com", "hunter2222") == "ada@acme.com"
