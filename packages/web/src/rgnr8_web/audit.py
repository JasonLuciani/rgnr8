"""An append-only audit log for authorization-relevant actions.

Every action that changes who-can-do-what, or that RGNR8 staff take on a client's
behalf, should leave a record: invites, role changes, close seals, and — most
importantly — support impersonation ("acting as"). The log is append-only (no
edits, no deletes), stamped with the actor, and queryable by tenant/account/actor
for a client's own admin view and for our internal review. In-memory for tests +
a DB-API SQL sink for production, same seam as the other stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

# Evidence classes that a retention sweep must NEVER delete: close/publication
# records, data-erasure records, membership/role changes, support impersonation,
# account settings + integration config, and the retention actions themselves.
# These are the accounting- and security-control trail; a controller must not be
# able to shrink the horizon and quietly purge them (that would defeat the whole
# "append-only" guarantee). Everything else (routine user/transaction actions)
# remains subject to the tenant's configured retention.
PROTECTED_AUDIT_PREFIXES: tuple[str, ...] = (
    "close.", "data.", "membership.", "platform_role.", "support.",
    "retention.", "settings.", "integration.",
)


def is_protected_action(action: str) -> bool:
    """True if this audit action is control/close evidence exempt from retention."""
    return any(action.startswith(pfx) for pfx in PROTECTED_AUDIT_PREFIXES)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    seq: int
    actor: str          # user/operator id that took the action
    action: str         # dotted verb: "invite.created", "membership.set", "close.sealed", "support.impersonate", "account.provisioned"
    at: int             # epoch seconds
    tenant_id: str = ""
    account_id: str = ""
    target: str = ""    # the affected subject (e.g. the invited email, the tenant onboarded)
    detail: str = ""


class AuditSink(Protocol):
    def record(self, actor: str, action: str, at: int, *, tenant_id: str = "",
               account_id: str = "", target: str = "", detail: str = "") -> AuditEvent: ...
    def events(self, *, tenant_id: str | None = None, account_id: str | None = None,
               actor: str | None = None) -> list[AuditEvent]: ...
    def purge_older_than(self, tenant_id: str, before_at: int) -> int: ...


class InMemoryAuditLog:
    def __init__(self) -> None:
        self._events: list[AuditEvent] = []

    def record(self, actor: str, action: str, at: int, *, tenant_id: str = "",
               account_id: str = "", target: str = "", detail: str = "") -> AuditEvent:
        ev = AuditEvent(len(self._events) + 1, actor, action, at, tenant_id=tenant_id,
                        account_id=account_id, target=target, detail=detail)
        self._events.append(ev)
        return ev

    def events(self, *, tenant_id: str | None = None, account_id: str | None = None,
               actor: str | None = None) -> list[AuditEvent]:
        out = self._events
        if tenant_id is not None:
            out = [e for e in out if e.tenant_id == tenant_id]
        if account_id is not None:
            out = [e for e in out if e.account_id == account_id]
        if actor is not None:
            out = [e for e in out if e.actor == actor]
        return list(out)

    def purge_older_than(self, tenant_id: str, before_at: int) -> int:
        """Retention enforcement: drop this tenant's audit rows stamped before
        `before_at` (epoch seconds). Distinct from editing — this is a policy-
        driven, whole-row deletion of aged records, not a change to any of them.
        Control/close evidence (see `PROTECTED_AUDIT_PREFIXES`) is never purged.
        Returns the number removed."""
        keep = [
            e for e in self._events
            if not (e.tenant_id == tenant_id and e.at < before_at and not is_protected_action(e.action))
        ]
        removed = len(self._events) - len(keep)
        self._events = keep
        return removed


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlAuditLog:
    """Append-only audit over any DB-API 2.0 connection. `seq` is assigned by the
    DB (autoincrement); reads come back ordered."""

    def __init__(self, connection: _DbApiConnection, *, table: str = "audit_event",
                 placeholder: str = "?") -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(seq INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL, action TEXT NOT NULL, "
                "at INTEGER NOT NULL, tenant_id TEXT NOT NULL DEFAULT '', account_id TEXT NOT NULL DEFAULT '', "
                "target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '')"
            )
        finally:
            cur.close()
        self._conn.commit()

    def record(self, actor: str, action: str, at: int, *, tenant_id: str = "",
               account_id: str = "", target: str = "", detail: str = "") -> AuditEvent:
        # `seq` is the DB-assigned autoincrement primary key. We read it back with
        # a `RETURNING seq` clause (supported by both Postgres and modern sqlite)
        # so the returned seq is the *actual* inserted row id — atomic under
        # concurrency and O(1), not a racy `SELECT COUNT(*)` scan.
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (actor, action, at, tenant_id, account_id, target, detail) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p}) RETURNING seq",
                (actor, action, at, tenant_id, account_id, target, detail),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        self._conn.commit()
        seq = int(str(rows[0][0])) if rows else 0
        return AuditEvent(seq, actor, action, at, tenant_id=tenant_id,
                          account_id=account_id, target=target, detail=detail)

    def events(self, *, tenant_id: str | None = None, account_id: str | None = None,
               actor: str | None = None) -> list[AuditEvent]:
        clauses, params = [], []
        for col, val in (("tenant_id", tenant_id), ("account_id", account_id), ("actor", actor)):
            if val is not None:
                clauses.append(f"{col}={self._ph}")
                params.append(val)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT seq, actor, action, at, tenant_id, account_id, target, detail "
                f"FROM {self._t}{where} ORDER BY seq", tuple(params))
            rows = cur.fetchall()
        finally:
            cur.close()
        return [AuditEvent(int(str(r[0])), str(r[1]), str(r[2]), int(str(r[3])),
                           tenant_id=str(r[4]), account_id=str(r[5]), target=str(r[6]),
                           detail=str(r[7])) for r in rows]

    def purge_older_than(self, tenant_id: str, before_at: int) -> int:
        """Retention enforcement: delete this tenant's audit rows stamped before
        `before_at`. A policy-driven removal of aged records (not an edit).
        Returns the number of rows removed."""
        p = self._ph
        # Exclude protected evidence: `AND action NOT LIKE 'close.%' AND ...`.
        not_like = " ".join(f"AND action NOT LIKE {p}" for _ in PROTECTED_AUDIT_PREFIXES)
        params: tuple[object, ...] = (
            tenant_id, before_at, *(f"{pfx}%" for pfx in PROTECTED_AUDIT_PREFIXES),
        )
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"DELETE FROM {self._t} WHERE tenant_id={p} AND at<{p} {not_like}",
                params,
            )
            removed = getattr(cur, "rowcount", -1)
        finally:
            cur.close()
        self._conn.commit()
        return int(removed) if isinstance(removed, int) and removed >= 0 else 0
