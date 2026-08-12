"""Real end-user credentials — password login for the public track.

Where the dev surface logs a person in from an email+role picker (no secret) and
the SSO surface trusts an IdP, the *public* track needs RGNR8 to be the identity
provider: a person signs up with an email + password, proves the email with a
single-use verification token, then logs in. This module is that primitive.

Security baked in, mirroring the rest of the package:
  * passwords are hashed with **scrypt** (`hashlib.scrypt`) using a per-credential
    random salt and sensible cost params — never stored plaintext, never a fast
    hash; the salt + params + digest are self-describing in one string so a
    stored hash verifies without any external config.
  * verification compares digests in **constant time** (`hmac.compare_digest`),
    and `login` runs a hash even for an unknown email so "no such user" and "bad
    password" are indistinguishable (no user enumeration by timing or by status).
  * clocks, token minting, and salt generation are **injected** (`clock`,
    `token_factory`, `salt_factory`) with real defaults, so every flow is
    deterministic under test — the same seam as `invitations.py` / `apikeys.py`.

`AuthService` orchestrates signup → verify → login and the password-reset flow on
top of a `CredentialStore` + `VerificationTokenStore`, emitting audit events and
firing an injected `on_signup` seam so provisioning/billing can hook in without
`web` ever importing `billing`.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .audit import AuditSink


# --- password hashing --------------------------------------------------------


class PasswordHasher:
    """scrypt password hashing with a self-describing encoding.

    `hash(password)` returns ``scrypt$<n>$<r>$<p>$<salt_b64>$<digest_b64>`` — the
    cost params and the random salt travel with the digest, so `verify` needs no
    external state and old hashes keep verifying if defaults change. `salt_factory`
    is injectable (bytes) for deterministic tests; it defaults to `secrets`."""

    def __init__(
        self,
        *,
        n: int = 1 << 14,
        r: int = 8,
        p: int = 1,
        dklen: int = 32,
        salt_bytes: int = 16,
        maxmem: int = 132 * 1024 * 1024,
        salt_factory: Callable[[], bytes] | None = None,
    ) -> None:
        self._n = n
        self._r = r
        self._p = p
        self._dklen = dklen
        self._maxmem = maxmem
        self._salt = salt_factory if salt_factory is not None else (lambda: secrets.token_bytes(salt_bytes))

    def _derive(self, password: str, salt: bytes, *, n: int, r: int, p: int, dklen: int) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=dklen, maxmem=self._maxmem
        )

    def hash(self, password: str, *, salt: bytes | None = None) -> str:
        s = salt if salt is not None else self._salt()
        dk = self._derive(password, s, n=self._n, r=self._r, p=self._p, dklen=self._dklen)
        return "$".join(
            ["scrypt", str(self._n), str(self._r), str(self._p),
             base64.b64encode(s).decode("ascii"), base64.b64encode(dk).decode("ascii")]
        )

    def verify(self, password: str, encoded: str) -> bool:
        """Constant-time verify of `password` against an encoded scrypt hash. A
        malformed encoding returns False rather than raising."""
        try:
            scheme, n_s, r_s, p_s, salt_b64, dk_b64 = encoded.split("$")
            if scheme != "scrypt":
                return False
            n, r, p = int(n_s), int(r_s), int(p_s)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(dk_b64)
        except (ValueError, TypeError):
            return False
        actual = self._derive(password, salt, n=n, r=r, p=p, dklen=len(expected))
        return hmac.compare_digest(actual, expected)


# --- credential record + store -----------------------------------------------


@dataclass(frozen=True, slots=True)
class Credential:
    user_id: str        # the subject (matches the UserDirectory id — an email here)
    email: str
    password_hash: str
    verified: bool = False
    created_at: int = 0


class CredentialStore(Protocol):
    def save(self, cred: Credential) -> None: ...
    def get_by_user_id(self, user_id: str) -> Credential | None: ...
    def get_by_email(self, email: str) -> Credential | None: ...


class InMemoryCredentialStore:
    def __init__(self) -> None:
        self._by_id: dict[str, Credential] = {}
        self._by_email: dict[str, str] = {}   # lower(email) -> user_id

    def save(self, cred: Credential) -> None:
        self._by_id[cred.user_id] = cred
        self._by_email[cred.email.lower()] = cred.user_id

    def get_by_user_id(self, user_id: str) -> Credential | None:
        return self._by_id.get(user_id)

    def get_by_email(self, email: str) -> Credential | None:
        uid = self._by_email.get(email.lower())
        return self._by_id.get(uid) if uid is not None else None


# --- verification / reset tokens ---------------------------------------------


class TokenPurpose(str, Enum):
    VERIFY_EMAIL = "verify_email"
    PASSWORD_RESET = "password_reset"


@dataclass(frozen=True, slots=True)
class VerificationToken:
    token: str
    user_id: str
    email: str
    purpose: TokenPurpose
    created_at: int = 0
    expires_at: int = 0
    consumed: bool = False

    def is_expired(self, now: int) -> bool:
        return self.expires_at != 0 and now >= self.expires_at


class VerificationTokenStore(Protocol):
    def save(self, token: VerificationToken) -> None: ...
    def get(self, token: str) -> VerificationToken | None: ...


class InMemoryVerificationTokenStore:
    def __init__(self) -> None:
        self._by_token: dict[str, VerificationToken] = {}

    def save(self, token: VerificationToken) -> None:
        self._by_token[token.token] = token

    def get(self, token: str) -> VerificationToken | None:
        return self._by_token.get(token)


# --- SQL stores (any DB-API 2.0 connection) ----------------------------------


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlCredentialStore:
    """End-user credentials over any DB-API 2.0 connection (only the scrypt hash
    is stored). Mirrors `SqlApiKeyStore` exactly."""

    def __init__(self, connection: _DbApiConnection, *, table: str = "rgnr8_credential",
                 placeholder: str = "?") -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(user_id TEXT PRIMARY KEY, email TEXT NOT NULL, password_hash TEXT NOT NULL, "
                "verified INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL DEFAULT 0)")
        finally:
            cur.close()
        self._conn.commit()

    def save(self, cred: Credential) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (user_id, email, password_hash, verified, created_at) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}) "
                "ON CONFLICT (user_id) DO UPDATE SET email=excluded.email, "
                "password_hash=excluded.password_hash, verified=excluded.verified",
                (cred.user_id, cred.email, cred.password_hash,
                 1 if cred.verified else 0, cred.created_at))
        finally:
            cur.close()
        self._conn.commit()

    def _rows(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def _to_cred(self, r: tuple[object, ...]) -> Credential:
        return Credential(str(r[0]), str(r[1]), str(r[2]), bool(int(str(r[3]))), int(str(r[4])))

    def get_by_user_id(self, user_id: str) -> Credential | None:
        rows = self._rows(
            f"SELECT user_id, email, password_hash, verified, created_at "
            f"FROM {self._t} WHERE user_id={self._ph}", (user_id,))
        return self._to_cred(rows[0]) if rows else None

    def get_by_email(self, email: str) -> Credential | None:
        rows = self._rows(
            f"SELECT user_id, email, password_hash, verified, created_at "
            f"FROM {self._t} WHERE lower(email)={self._ph}", (email.lower(),))
        return self._to_cred(rows[0]) if rows else None


class SqlVerificationTokenStore:
    """Single-use email-verification / password-reset tokens over any DB-API 2.0
    connection. Mirrors `SqlInvitationStore` (token + expiry + a consumed flag)."""

    def __init__(self, connection: _DbApiConnection, *, table: str = "rgnr8_verification_token",
                 placeholder: str = "?") -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(token TEXT PRIMARY KEY, user_id TEXT NOT NULL, email TEXT NOT NULL, "
                "purpose TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT 0, "
                "expires_at INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0)")
        finally:
            cur.close()
        self._conn.commit()

    def save(self, token: VerificationToken) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (token, user_id, email, purpose, created_at, expires_at, consumed) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}) "
                "ON CONFLICT (token) DO UPDATE SET consumed=excluded.consumed",
                (token.token, token.user_id, token.email, token.purpose.value,
                 token.created_at, token.expires_at, 1 if token.consumed else 0))
        finally:
            cur.close()
        self._conn.commit()

    def get(self, token: str) -> VerificationToken | None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT token, user_id, email, purpose, created_at, expires_at, consumed "
                f"FROM {self._t} WHERE token={self._ph}", (token,))
            rows = cur.fetchall()
        finally:
            cur.close()
        if not rows:
            return None
        r = rows[0]
        return VerificationToken(str(r[0]), str(r[1]), str(r[2]), TokenPurpose(str(r[3])),
                                 int(str(r[4])), int(str(r[5])), bool(int(str(r[6]))))


# --- the service -------------------------------------------------------------


class AuthError(Exception):
    """A signup couldn't be created (weak password, duplicate email, bad email)."""


_HOUR = 3_600
# A fixed salt used only to derive a throwaway hash for the unknown-email branch of
# login, so we spend the same scrypt work whether or not the email exists — without
# perturbing the injected salt_factory sequence tests rely on.
_DUMMY_SALT = b"rgnr8-dummy-salt"


class AuthService:
    """Signup / verify / login + password-reset over a credential + token store.

    Clocks, token minting and salt generation are injected for deterministic tests.
    `on_signup(email)` fires after a credential is created so provisioning/billing
    can react without `web` importing them. Audit events (`user.signup`,
    `user.verified`, `password.reset`) are recorded when an `AuditSink` is given."""

    def __init__(
        self,
        credentials: CredentialStore | None = None,
        tokens: VerificationTokenStore | None = None,
        audit: AuditSink | None = None,
        *,
        hasher: PasswordHasher | None = None,
        clock: Callable[[], int] | None = None,
        token_factory: Callable[[], str] | None = None,
        salt_factory: Callable[[], bytes] | None = None,
        on_signup: Callable[[str], None] | None = None,
        min_password_length: int = 8,
        verify_ttl_hours: int = 24,
        reset_ttl_hours: int = 1,
    ) -> None:
        self._creds: CredentialStore = credentials if credentials is not None else InMemoryCredentialStore()
        self._tokens: VerificationTokenStore = tokens if tokens is not None else InMemoryVerificationTokenStore()
        self._audit = audit
        self._hasher = hasher if hasher is not None else PasswordHasher(salt_factory=salt_factory)
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._token = token_factory if token_factory is not None else (lambda: secrets.token_urlsafe(24))
        self._on_signup = on_signup
        self._min_len = min_password_length
        self._verify_ttl = verify_ttl_hours * _HOUR
        self._reset_ttl = reset_ttl_hours * _HOUR
        # Precomputed once so the unknown-email login branch costs the same as a
        # real verify (constant-time against user enumeration).
        self._dummy_hash = self._hasher.hash("x" * max(self._min_len, 1), salt=_DUMMY_SALT)

    def _issue_token(self, cred: Credential, purpose: TokenPurpose, ttl: int) -> VerificationToken:
        now = self._clock()
        tok = VerificationToken(
            token=self._token(), user_id=cred.user_id, email=cred.email, purpose=purpose,
            created_at=now, expires_at=now + ttl,
        )
        self._tokens.save(tok)
        return tok

    def signup(self, email: str, password: str) -> tuple[Credential, str]:
        """Create an UNVERIFIED credential + a verification token. Rejects a bad or
        duplicate email and a weak/short password. Returns (credential, token)."""
        email = email.strip()
        if "@" not in email:
            raise AuthError("a valid email is required")
        if len(password) < self._min_len:
            raise AuthError(f"password must be at least {self._min_len} characters")
        if self._creds.get_by_email(email) is not None:
            raise AuthError("that email is already registered")
        now = self._clock()
        cred = Credential(
            user_id=email, email=email, password_hash=self._hasher.hash(password),
            verified=False, created_at=now,
        )
        self._creds.save(cred)
        tok = self._issue_token(cred, TokenPurpose.VERIFY_EMAIL, self._verify_ttl)
        if self._on_signup is not None:
            self._on_signup(email)
        if self._audit is not None:
            self._audit.record(cred.user_id, "user.signup", now, target=email)
        return cred, tok.token

    def verify_email(self, token: str) -> bool:
        """Mark the credential verified. Single-use: the token is consumed and a
        second call returns False."""
        rec = self._tokens.get(token)
        now = self._clock()
        if rec is None or rec.purpose is not TokenPurpose.VERIFY_EMAIL or rec.consumed:
            return False
        if rec.is_expired(now):
            return False
        cred = self._creds.get_by_user_id(rec.user_id)
        if cred is None:
            return False
        self._creds.save(dataclasses.replace(cred, verified=True))
        self._tokens.save(dataclasses.replace(rec, consumed=True))
        if self._audit is not None:
            self._audit.record(cred.user_id, "user.verified", now, target=cred.email)
        return True

    def login(self, email: str, password: str) -> str | None:
        """Return the subject (user id) on success, else None. Succeeds ONLY when
        the credential exists, the password matches, and the email is verified.
        Constant-time and non-committal: every failure returns None, and an unknown
        email still spends a scrypt verify so it is indistinguishable from a bad
        password."""
        cred = self._creds.get_by_email(email)
        if cred is None:
            self._hasher.verify(password, self._dummy_hash)  # equalize timing
            return None
        if not self._hasher.verify(password, cred.password_hash):
            return None
        if not cred.verified:
            return None
        return cred.user_id

    def request_password_reset(self, email: str) -> str | None:
        """Mint a single-use reset token, or None if no such email. Callers must
        respond identically either way so the endpoint does not reveal membership."""
        cred = self._creds.get_by_email(email)
        if cred is None:
            return None
        tok = self._issue_token(cred, TokenPurpose.PASSWORD_RESET, self._reset_ttl)
        return tok.token

    def reset_password(self, token: str, new_password: str) -> bool:
        """Set a new password from a reset token. Single-use + expiring; rejects a
        weak password. Also marks the credential verified (a reset proves the
        person controls the mailbox)."""
        rec = self._tokens.get(token)
        now = self._clock()
        if rec is None or rec.purpose is not TokenPurpose.PASSWORD_RESET or rec.consumed:
            return False
        if rec.is_expired(now):
            return False
        if len(new_password) < self._min_len:
            return False
        cred = self._creds.get_by_user_id(rec.user_id)
        if cred is None:
            return False
        self._creds.save(dataclasses.replace(
            cred, password_hash=self._hasher.hash(new_password), verified=True))
        self._tokens.save(dataclasses.replace(rec, consumed=True))
        if self._audit is not None:
            self._audit.record(cred.user_id, "password.reset", now, target=cred.email)
        return True
