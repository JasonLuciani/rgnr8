"""Team invitations — the client-side path to add a user to a business.

The owner (or anyone with `manage_users`) invites an email to a tenant with a
role; RGNR8 mints a single-use token and holds a pending invitation until the
person accepts, at which point their user + membership are created in the
`UserDirectory`. This is how the accountant/bookkeeper actually get in — and it's
the same directory the token-based auth already reads, so nothing else changes.
Every create/accept/revoke is written to the audit log.
"""

from __future__ import annotations

import dataclasses
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .audit import AuditSink
from .rbac import Membership, Role, User, UserDirectory


class InvitationStatus(str, Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class Invitation:
    token: str
    email: str
    tenant_id: str
    role: Role
    invited_by: str
    status: InvitationStatus = InvitationStatus.PENDING
    created_at: int = 0
    expires_at: int = 0

    def is_expired(self, now: int) -> bool:
        return self.expires_at != 0 and now >= self.expires_at


class InvitationStore(Protocol):
    def save(self, inv: Invitation) -> None: ...
    def get(self, token: str) -> Invitation | None: ...
    def list_for_tenant(self, tenant_id: str) -> list[Invitation]: ...


class InMemoryInvitationStore:
    def __init__(self) -> None:
        self._by_token: dict[str, Invitation] = {}

    def save(self, inv: Invitation) -> None:
        self._by_token[inv.token] = inv

    def get(self, token: str) -> Invitation | None:
        return self._by_token.get(token)

    def list_for_tenant(self, tenant_id: str) -> list[Invitation]:
        return [i for i in self._by_token.values() if i.tenant_id == tenant_id]


class InvitationError(Exception):
    """An invitation couldn't be created or redeemed."""


_DAY = 86_400


class InvitationService:
    def __init__(
        self,
        directory: UserDirectory,
        store: InvitationStore | None = None,
        audit: AuditSink | None = None,
        *,
        clock: Callable[[], int] | None = None,
        token_factory: Callable[[], str] | None = None,
        ttl_days: int = 7,
    ) -> None:
        self._dir = directory
        self._store: InvitationStore = store if store is not None else InMemoryInvitationStore()
        self._audit = audit
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._token = token_factory if token_factory is not None else (lambda: secrets.token_urlsafe(24))
        self._ttl_days = ttl_days

    def invite(self, email: str, tenant_id: str, role: Role, invited_by: str) -> Invitation:
        if "@" not in email:
            raise InvitationError("a valid email is required")
        if role.is_platform:
            raise InvitationError("platform roles are not invitable per-business")
        now = self._clock()
        inv = Invitation(
            token=self._token(), email=email, tenant_id=tenant_id, role=role,
            invited_by=invited_by, created_at=now, expires_at=now + self._ttl_days * _DAY,
        )
        self._store.save(inv)
        if self._audit is not None:
            self._audit.record(invited_by, "invite.created", now, tenant_id=tenant_id,
                               target=email, detail=role.value)
        return inv

    def accept(self, token: str, *, name: str = "") -> Membership:
        now = self._clock()
        inv = self._store.get(token)
        if inv is None:
            raise InvitationError("unknown invitation")
        if inv.status is not InvitationStatus.PENDING:
            raise InvitationError(f"invitation is {inv.status.value}")
        if inv.is_expired(now):
            self._store.save(dataclasses.replace(inv, status=InvitationStatus.EXPIRED))
            raise InvitationError("invitation has expired")
        user = self._dir.find_by_email(inv.email)
        if user is None:
            user = User(id=inv.email, email=inv.email, name=name)
            self._dir.upsert_user(user)
        self._dir.set_membership(user.id, inv.tenant_id, inv.role)
        self._store.save(dataclasses.replace(inv, status=InvitationStatus.ACCEPTED))
        if self._audit is not None:
            self._audit.record(user.id, "membership.set", now, tenant_id=inv.tenant_id,
                               target=user.id, detail=inv.role.value)
        return Membership(user.id, inv.tenant_id, inv.role)

    def revoke(self, token: str, revoked_by: str) -> Invitation:
        inv = self._store.get(token)
        if inv is None:
            raise InvitationError("unknown invitation")
        updated = dataclasses.replace(inv, status=InvitationStatus.REVOKED)
        self._store.save(updated)
        if self._audit is not None:
            self._audit.record(revoked_by, "invite.revoked", self._clock(),
                               tenant_id=inv.tenant_id, target=inv.email)
        return updated

    def pending_for(self, tenant_id: str) -> list[Invitation]:
        return [i for i in self._store.list_for_tenant(tenant_id)
                if i.status is InvitationStatus.PENDING]


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlInvitationStore:
    """Invitations over any DB-API 2.0 connection."""

    def __init__(self, connection: _DbApiConnection, *, table: str = "rgnr8_invitation",
                 placeholder: str = "?") -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(token TEXT PRIMARY KEY, email TEXT NOT NULL, tenant_id TEXT NOT NULL, "
                "role TEXT NOT NULL, invited_by TEXT NOT NULL, status TEXT NOT NULL, "
                "created_at INTEGER NOT NULL DEFAULT 0, expires_at INTEGER NOT NULL DEFAULT 0)")
        finally:
            cur.close()
        self._conn.commit()

    def save(self, inv: Invitation) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (token, email, tenant_id, role, invited_by, status, created_at, expires_at) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}) "
                "ON CONFLICT (token) DO UPDATE SET status=excluded.status",
                (inv.token, inv.email, inv.tenant_id, inv.role.value, inv.invited_by,
                 inv.status.value, inv.created_at, inv.expires_at))
        finally:
            cur.close()
        self._conn.commit()

    def _to_inv(self, r: tuple[object, ...]) -> Invitation:
        return Invitation(str(r[0]), str(r[1]), str(r[2]), Role(str(r[3])), str(r[4]),
                          InvitationStatus(str(r[5])), int(str(r[6])), int(str(r[7])))

    def get(self, token: str) -> Invitation | None:
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT token, email, tenant_id, role, invited_by, status, created_at, "
                        f"expires_at FROM {self._t} WHERE token={self._ph}", (token,))
            rows = cur.fetchall()
        finally:
            cur.close()
        return self._to_inv(rows[0]) if rows else None

    def list_for_tenant(self, tenant_id: str) -> list[Invitation]:
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT token, email, tenant_id, role, invited_by, status, created_at, "
                        f"expires_at FROM {self._t} WHERE tenant_id={self._ph}", (tenant_id,))
            rows = cur.fetchall()
        finally:
            cur.close()
        return [self._to_inv(r) for r in rows]
