"""Client-side user management: invitations + the audit trail."""

import sqlite3
from typing import Any, cast

from rgnr8_web import (
    InMemoryAuditLog,
    InMemoryUserDirectory,
    InvitationError,
    InvitationService,
    InvitationStatus,
    Role,
    SqlAuditLog,
    User,
)

NOW = 1_760_000_000


def _svc(audit: InMemoryAuditLog | None = None) -> tuple[InvitationService, InMemoryUserDirectory, InMemoryAuditLog]:
    directory = InMemoryUserDirectory()
    directory.upsert_user(User("owner@acme.com", "owner@acme.com", "Ada Owner"))
    directory.set_membership("owner@acme.com", "acme", Role.OWNER)
    log = audit or InMemoryAuditLog()
    seq = {"n": 0}

    def token() -> str:
        seq["n"] += 1
        return f"tok-{seq['n']}"

    svc = InvitationService(directory, audit=log, clock=lambda: NOW, token_factory=token, ttl_days=7)
    return svc, directory, log


def test_invite_creates_pending_and_audits() -> None:
    svc, _dir, log = _svc()
    inv = svc.invite("book@acme.com", "acme", Role.BOOKKEEPER, "owner@acme.com")
    assert inv.status is InvitationStatus.PENDING
    assert inv.expires_at == NOW + 7 * 86_400
    assert svc.pending_for("acme")[0].email == "book@acme.com"
    ev = log.events(tenant_id="acme")
    assert ev[0].action == "invite.created" and ev[0].target == "book@acme.com"


def test_accept_creates_user_and_membership() -> None:
    svc, directory, log = _svc()
    inv = svc.invite("book@acme.com", "acme", Role.BOOKKEEPER, "owner@acme.com")
    m = svc.accept(inv.token, name="Ben Books")
    assert m.role is Role.BOOKKEEPER
    assert directory.membership("book@acme.com", "acme") is not None
    assert directory.get_user("book@acme.com").name == "Ben Books"
    assert svc.pending_for("acme") == []                       # no longer pending
    assert any(e.action == "membership.set" for e in log.events(tenant_id="acme"))


def test_cannot_accept_twice() -> None:
    svc, _dir, _log = _svc()
    inv = svc.invite("x@acme.com", "acme", Role.VIEWER, "owner@acme.com")
    svc.accept(inv.token)
    try:
        svc.accept(inv.token)
        assert False, "expected InvitationError"
    except InvitationError:
        pass


def test_expired_invitation_is_rejected() -> None:
    directory = InMemoryUserDirectory()
    clock = {"t": NOW}
    svc = InvitationService(directory, clock=lambda: clock["t"],
                            token_factory=lambda: "tok", ttl_days=1)
    inv = svc.invite("late@acme.com", "acme", Role.VIEWER, "owner@acme.com")
    clock["t"] = NOW + 2 * 86_400   # past the 1-day TTL
    try:
        svc.accept(inv.token)
        assert False, "expected expiry"
    except InvitationError:
        pass


def test_platform_role_is_not_invitable() -> None:
    svc, _dir, _log = _svc()
    try:
        svc.invite("staff@rgnr8.co", "acme", Role.OPERATOR, "owner@acme.com")
        assert False, "expected InvitationError"
    except InvitationError:
        pass


def test_revoke_blocks_acceptance() -> None:
    svc, _dir, _log = _svc()
    inv = svc.invite("x@acme.com", "acme", Role.VIEWER, "owner@acme.com")
    svc.revoke(inv.token, "owner@acme.com")
    try:
        svc.accept(inv.token)
        assert False
    except InvitationError:
        pass


def test_sql_audit_log_roundtrip() -> None:
    conn = sqlite3.connect(":memory:")
    log = SqlAuditLog(cast("Any", conn), placeholder="?")
    log.create_schema()
    log.record("owner@acme.com", "close.sealed", NOW, tenant_id="acme", detail="2026-08")
    log.record("ops@rgnr8.co", "support.impersonate", NOW, tenant_id="acme", account_id="acct_1")
    acme = log.events(tenant_id="acme")
    assert [e.action for e in acme] == ["close.sealed", "support.impersonate"]
    assert log.events(actor="ops@rgnr8.co")[0].action == "support.impersonate"


def test_sql_audit_log_seq_is_db_assigned_pk_and_increases() -> None:
    # `record` must return the real autoincrement PK (via RETURNING), so two
    # back-to-back inserts get distinct, strictly increasing seqs — no racy
    # SELECT COUNT(*), and the returned seq matches the stored row.
    conn = sqlite3.connect(":memory:")
    log = SqlAuditLog(cast("Any", conn), placeholder="?")
    log.create_schema()

    a = log.record("owner@acme.com", "invite.created", NOW, tenant_id="acme", target="x@acme.com")
    b = log.record("owner@acme.com", "invite.created", NOW, tenant_id="acme", target="y@acme.com")
    c = log.record("owner@acme.com", "membership.set", NOW, tenant_id="acme", target="x@acme.com")

    # Distinct, strictly increasing DB-assigned primary keys.
    assert a.seq < b.seq < c.seq
    assert len({a.seq, b.seq, c.seq}) == 3

    # Each returned seq matches the seq actually persisted for that row.
    stored = {e.target + e.action: e.seq for e in log.events(tenant_id="acme")}
    assert stored["x@acme.cominvite.created"] == a.seq
    assert stored["y@acme.cominvite.created"] == b.seq
    assert stored["x@acme.commembership.set"] == c.seq

    # The returned seq is the genuine row id, not a count-derived guess: reading
    # the row straight back by seq yields the same event.
    row = conn.execute("SELECT actor, action FROM audit_event WHERE seq = ?", (b.seq,)).fetchone()
    assert row == ("owner@acme.com", "invite.created")
