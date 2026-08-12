"""Roles, permissions, memberships, and the access policy."""

import sqlite3

from rgnr8_web import (
    AccessPolicy,
    InMemoryUserDirectory,
    Permission,
    Role,
    SqlUserDirectory,
    User,
    role_permissions,
)


def _dir() -> InMemoryUserDirectory:
    d = InMemoryUserDirectory()
    d.upsert_user(User("u-owner", "owner@acme.com", "Ada Owner"))
    d.upsert_user(User("u-book", "book@acme.com", "Ben Bookkeeper"))
    d.upsert_user(User("u-cpa", "cpa@ext.com", "Cara CPA"))
    d.upsert_user(User("u-op", "ops@rgnr8.app", "Ops Staff"))
    d.set_membership("u-owner", "acme", Role.OWNER)
    d.set_membership("u-book", "acme", Role.BOOKKEEPER)
    d.set_membership("u-cpa", "acme", Role.ACCOUNTANT)
    d.set_platform_role("u-op", Role.OPERATOR)
    return d


def test_role_permission_boundaries() -> None:
    p = AccessPolicy(_dir())
    # owner can manage users + publish close
    assert p.can("u-owner", "acme", Permission.MANAGE_USERS)
    assert p.can("u-owner", "acme", Permission.PUBLISH_CLOSE)
    # bookkeeper can run the close but NOT publish it or manage users
    assert p.can("u-book", "acme", Permission.MANAGE_CLOSE)
    assert not p.can("u-book", "acme", Permission.PUBLISH_CLOSE)
    assert not p.can("u-book", "acme", Permission.MANAGE_USERS)
    # external accountant can publish the close but not manage users/connectors
    assert p.can("u-cpa", "acme", Permission.PUBLISH_CLOSE)
    assert not p.can("u-cpa", "acme", Permission.MANAGE_USERS)
    assert not p.can("u-cpa", "acme", Permission.MANAGE_CONNECTORS)


def test_everyone_can_view_cash() -> None:
    p = AccessPolicy(_dir())
    for uid in ("u-owner", "u-book", "u-cpa"):
        assert p.can(uid, "acme", Permission.VIEW_CASH)


def test_platform_operator_spans_tenants_but_a_member_does_not() -> None:
    p = AccessPolicy(_dir())
    # operator sees the fleet and can view a tenant they have no membership in
    assert p.can("u-op", "acme", Permission.VIEW_FLEET)
    assert p.can("u-op", "any-other-tenant", Permission.VIEW_CASH)
    # a tenant member has NO access to a different tenant
    assert not p.can("u-owner", "other", Permission.VIEW_CASH)
    assert p.can("u-owner", "acme", Permission.VIEW_CASH)


def test_no_membership_means_no_permissions() -> None:
    p = AccessPolicy(_dir())
    assert p.permissions("nobody", "acme") == frozenset()
    assert not p.can("nobody", "acme", Permission.VIEW_CASH)


def test_viewer_is_read_only() -> None:
    d = _dir()
    d.upsert_user(User("u-view", "view@acme.com"))
    d.set_membership("u-view", "acme", Role.VIEWER)
    p = AccessPolicy(d)
    assert p.can("u-view", "acme", Permission.VIEW_BRIEFING)
    for perm in (Permission.EDIT_ASSUMPTIONS, Permission.RECORD_DECISION,
                 Permission.MANAGE_CLOSE, Permission.MANAGE_USERS):
        assert not p.can("u-view", "acme", perm)


def test_role_change_takes_effect() -> None:
    d = _dir()
    p = AccessPolicy(d)
    assert not p.can("u-book", "acme", Permission.PUBLISH_CLOSE)
    d.set_membership("u-book", "acme", Role.CONTROLLER)  # promote
    assert p.can("u-book", "acme", Permission.PUBLISH_CLOSE)


def test_sql_directory_persists_users_and_memberships() -> None:
    conn = sqlite3.connect(":memory:")
    d = SqlUserDirectory(conn)
    d.create_schema()
    d.upsert_user(User("u1", "a@b.com", "A"))
    d.set_membership("u1", "acme", Role.CONTROLLER)
    d.set_platform_role("u1", Role.SUPPORT)

    # reopen over the same connection
    d2 = SqlUserDirectory(conn)
    assert d2.find_by_email("A@B.com").id == "u1"  # type: ignore[union-attr]
    assert d2.membership("u1", "acme").role is Role.CONTROLLER  # type: ignore[union-attr]
    assert d2.platform_role("u1") is Role.SUPPORT
    assert [m.user_id for m in d2.members("acme")] == ["u1"]
    p = AccessPolicy(d2)
    assert p.can("u1", "acme", Permission.PUBLISH_CLOSE)


def test_role_permissions_table_is_total() -> None:
    for role in Role:
        assert isinstance(role_permissions(role), frozenset)
