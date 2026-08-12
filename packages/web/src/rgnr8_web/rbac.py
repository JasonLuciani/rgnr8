"""Users, roles, and permissions — authorization inside a business.

The token proves *who a request is* (a tenant + a subject/user). This module
decides *what they may do*. A **business (tenant)** has **members**, each with a
**role**; a role grants a fixed set of **permissions**; and RGNR8 staff hold a
**platform role** that spans tenants (for onboarding + the fleet). Identity can
come from any IdP; authorization (memberships + roles) lives here, in RGNR8.

Design: roles → permissions is a small, explicit table (no per-user grants to
reason about), the `AccessPolicy` answers a single `can(user, tenant, perm)`
question, and the `UserDirectory` seam has an in-memory impl for tests and a SQL
impl for production — same pattern as every other store.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Permission(str, Enum):
    # owner-facing (read)
    VIEW_CASH = "view_cash"
    VIEW_BRIEFING = "view_briefing"
    ASK_CFO = "ask_cfo"
    VIEW_PACKAGE = "view_package"
    VIEW_TRANSACTIONS = "view_transactions"  # the bank register / feed (read)
    CATEGORIZE_TXNS = "categorize_transactions"  # categorize / match / accept
    # owner-facing (write)
    EDIT_ASSUMPTIONS = "edit_assumptions"
    RECORD_DECISION = "record_decision"
    # accounting operations
    MANAGE_CLOSE = "manage_close"       # advance close-calendar tasks
    PUBLISH_CLOSE = "publish_close"     # seal the immutable financial package
    MANAGE_CONNECTORS = "manage_connectors"  # connect/reconnect bank/payroll/QBO
    # administration
    MANAGE_SUBSCRIPTIONS = "manage_subscriptions"  # briefing recipients
    MANAGE_USERS = "manage_users"       # invite / change roles within the business
    # platform (RGNR8 staff)
    VIEW_FLEET = "view_fleet"
    ONBOARD_TENANT = "onboard_tenant"


class Role(str, Enum):
    # tenant roles
    OWNER = "owner"
    CONTROLLER = "controller"
    BOOKKEEPER = "bookkeeper"
    ACCOUNTANT = "accountant"   # external co-delivery (Tier 3)
    VIEWER = "viewer"
    # platform roles (RGNR8 staff)
    OPERATOR = "operator"
    SUPPORT = "support"

    @property
    def is_platform(self) -> bool:
        return self in (Role.OPERATOR, Role.SUPPORT)


# everyone who can see the business can see cash, briefing, package, and the
# bank feed (read-only) — QBO-like transparency.
_VIEW = frozenset(
    {Permission.VIEW_CASH, Permission.VIEW_BRIEFING, Permission.ASK_CFO,
     Permission.VIEW_PACKAGE, Permission.VIEW_TRANSACTIONS}
)

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.OWNER: _VIEW
    | {
        Permission.EDIT_ASSUMPTIONS,
        Permission.RECORD_DECISION,
        Permission.CATEGORIZE_TXNS,
        Permission.MANAGE_CLOSE,
        Permission.PUBLISH_CLOSE,
        Permission.MANAGE_CONNECTORS,
        Permission.MANAGE_SUBSCRIPTIONS,
        Permission.MANAGE_USERS,
    },
    Role.CONTROLLER: _VIEW
    | {
        Permission.EDIT_ASSUMPTIONS,
        Permission.RECORD_DECISION,
        Permission.CATEGORIZE_TXNS,
        Permission.MANAGE_CLOSE,
        Permission.PUBLISH_CLOSE,
        Permission.MANAGE_SUBSCRIPTIONS,
    },
    # the bookkeeper lives in the bank feed: categorize + reconcile, run the
    # close, but can't seal the package or manage users.
    Role.BOOKKEEPER: _VIEW
    | {Permission.RECORD_DECISION, Permission.CATEGORIZE_TXNS, Permission.MANAGE_CLOSE,
       Permission.MANAGE_CONNECTORS},
    # external accountant co-delivering the close: categorize + run + publish the
    # close, but not manage the business's users or connectors.
    Role.ACCOUNTANT: _VIEW
    | {Permission.RECORD_DECISION, Permission.CATEGORIZE_TXNS, Permission.MANAGE_CLOSE,
       Permission.PUBLISH_CLOSE},
    Role.VIEWER: _VIEW,
    # platform staff — cross-tenant, no owner-write by default (support can view).
    Role.OPERATOR: _VIEW | {Permission.VIEW_FLEET, Permission.ONBOARD_TENANT, Permission.MANAGE_CONNECTORS},
    Role.SUPPORT: _VIEW | {Permission.VIEW_FLEET},
}


def role_permissions(role: Role) -> frozenset[Permission]:
    return ROLE_PERMISSIONS.get(role, frozenset())


@dataclass(frozen=True, slots=True)
class User:
    id: str
    email: str
    name: str = ""


@dataclass(frozen=True, slots=True)
class Membership:
    user_id: str
    tenant_id: str
    role: Role


# --- the directory seam ------------------------------------------------------


class UserDirectory(Protocol):
    def get_user(self, user_id: str) -> User | None: ...
    def find_by_email(self, email: str) -> User | None: ...
    def upsert_user(self, user: User) -> None: ...
    def set_membership(self, user_id: str, tenant_id: str, role: Role) -> None: ...
    def remove_membership(self, user_id: str, tenant_id: str) -> None: ...
    def membership(self, user_id: str, tenant_id: str) -> Membership | None: ...
    def members(self, tenant_id: str) -> list[Membership]: ...
    def set_platform_role(self, user_id: str, role: Role | None) -> None: ...
    def platform_role(self, user_id: str) -> Role | None: ...


class InMemoryUserDirectory:
    def __init__(self) -> None:
        self._users: dict[str, User] = {}
        self._by_email: dict[str, str] = {}
        self._memberships: dict[tuple[str, str], Membership] = {}
        self._platform: dict[str, Role] = {}

    def get_user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    def find_by_email(self, email: str) -> User | None:
        uid = self._by_email.get(email.lower())
        return self._users.get(uid) if uid is not None else None

    def upsert_user(self, user: User) -> None:
        self._users[user.id] = user
        self._by_email[user.email.lower()] = user.id

    def set_membership(self, user_id: str, tenant_id: str, role: Role) -> None:
        self._memberships[(user_id, tenant_id)] = Membership(user_id, tenant_id, role)

    def remove_membership(self, user_id: str, tenant_id: str) -> None:
        self._memberships.pop((user_id, tenant_id), None)

    def membership(self, user_id: str, tenant_id: str) -> Membership | None:
        return self._memberships.get((user_id, tenant_id))

    def members(self, tenant_id: str) -> list[Membership]:
        out = [m for m in self._memberships.values() if m.tenant_id == tenant_id]
        out.sort(key=lambda m: m.user_id)
        return out

    def set_platform_role(self, user_id: str, role: Role | None) -> None:
        if role is None:
            self._platform.pop(user_id, None)
        else:
            self._platform[user_id] = role

    def platform_role(self, user_id: str) -> Role | None:
        return self._platform.get(user_id)


# --- the policy --------------------------------------------------------------


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlUserDirectory:
    """Persist users + memberships over any DB-API 2.0 connection. `placeholder`
    is the driver's marker (`?` sqlite, `%s` psycopg)."""

    def __init__(
        self,
        connection: _DbApiConnection,
        *,
        user_table: str = "rgnr8_user",
        membership_table: str = "rgnr8_membership",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._ut = user_table
        self._mt = membership_table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._ut} "
                "(id TEXT PRIMARY KEY, email TEXT NOT NULL, name TEXT NOT NULL DEFAULT '', "
                "platform_role TEXT)"
            )
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._mt} "
                "(user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, role TEXT NOT NULL, "
                "PRIMARY KEY (user_id, tenant_id))"
            )
        finally:
            cur.close()
        self._conn.commit()

    def _one(self, sql: str, params: tuple[object, ...]) -> tuple[object, ...] | None:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            rows = cur.fetchall()
        finally:
            cur.close()
        return rows[0] if rows else None

    def get_user(self, user_id: str) -> User | None:
        r = self._one(f"SELECT id, email, name FROM {self._ut} WHERE id={self._ph}", (user_id,))
        return User(str(r[0]), str(r[1]), str(r[2])) if r else None

    def find_by_email(self, email: str) -> User | None:
        r = self._one(
            f"SELECT id, email, name FROM {self._ut} WHERE lower(email)={self._ph}", (email.lower(),)
        )
        return User(str(r[0]), str(r[1]), str(r[2])) if r else None

    def upsert_user(self, user: User) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._ut} (id, email, name) VALUES ({p}, {p}, {p}) "
                "ON CONFLICT (id) DO UPDATE SET email=excluded.email, name=excluded.name",
                (user.id, user.email, user.name),
            )
        finally:
            cur.close()
        self._conn.commit()

    def set_membership(self, user_id: str, tenant_id: str, role: Role) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._mt} (user_id, tenant_id, role) VALUES ({p}, {p}, {p}) "
                "ON CONFLICT (user_id, tenant_id) DO UPDATE SET role=excluded.role",
                (user_id, tenant_id, role.value),
            )
        finally:
            cur.close()
        self._conn.commit()

    def remove_membership(self, user_id: str, tenant_id: str) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(f"DELETE FROM {self._mt} WHERE user_id={p} AND tenant_id={p}", (user_id, tenant_id))
        finally:
            cur.close()
        self._conn.commit()

    def membership(self, user_id: str, tenant_id: str) -> Membership | None:
        r = self._one(
            f"SELECT role FROM {self._mt} WHERE user_id={self._ph} AND tenant_id={self._ph}",
            (user_id, tenant_id),
        )
        return Membership(user_id, tenant_id, Role(str(r[0]))) if r else None

    def members(self, tenant_id: str) -> list[Membership]:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT user_id, role FROM {self._mt} WHERE tenant_id={self._ph} ORDER BY user_id",
                (tenant_id,),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        return [Membership(str(r[0]), tenant_id, Role(str(r[1]))) for r in rows]

    def set_platform_role(self, user_id: str, role: Role | None) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(f"UPDATE {self._ut} SET platform_role={p} WHERE id={p}",
                        (role.value if role is not None else None, user_id))
        finally:
            cur.close()
        self._conn.commit()

    def platform_role(self, user_id: str) -> Role | None:
        r = self._one(f"SELECT platform_role FROM {self._ut} WHERE id={self._ph}", (user_id,))
        return Role(str(r[0])) if r and r[0] is not None else None


class AccessPolicy:
    """Answers 'may this user do this in this business?'. A user's effective
    permissions in a tenant are their tenant-role permissions unioned with their
    platform-role permissions (so an operator sees the fleet + can view any
    tenant they support), and nothing else."""

    def __init__(self, directory: UserDirectory) -> None:
        self._dir = directory

    def permissions(self, user_id: str, tenant_id: str) -> frozenset[Permission]:
        perms: set[Permission] = set()
        m = self._dir.membership(user_id, tenant_id)
        if m is not None:
            perms |= role_permissions(m.role)
        pr = self._dir.platform_role(user_id)
        if pr is not None:
            perms |= role_permissions(pr)
        return frozenset(perms)

    def can(self, user_id: str, tenant_id: str, permission: Permission) -> bool:
        return permission in self.permissions(user_id, tenant_id)

    def role_in(self, user_id: str, tenant_id: str) -> Role | None:
        m = self._dir.membership(user_id, tenant_id)
        return m.role if m is not None else None
