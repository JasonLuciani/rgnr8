"""Persistence for accounts + usage events — the same store seam as everywhere:
a Protocol, an in-memory impl (tests/local), and a DB-API SQL impl (production).
"""

from __future__ import annotations

from typing import Protocol

from .accounts import Account, AccountStatus
from .plans import Tier
from .usage import UsageEvent, UsageKind


class AccountStore(Protocol):
    def save_account(self, account: Account) -> None: ...
    def get_account(self, account_id: str) -> Account | None: ...
    def find_by_tenant(self, tenant_id: str) -> Account | None: ...
    def list_accounts(self) -> list[Account]: ...
    def append_usage(self, event: UsageEvent) -> None: ...
    def usage_for(self, account_id: str, period: str) -> list[UsageEvent]: ...


class InMemoryAccountStore:
    def __init__(self) -> None:
        self._accounts: dict[str, Account] = {}
        self._usage: list[UsageEvent] = []

    def save_account(self, account: Account) -> None:
        self._accounts[account.id] = account

    def get_account(self, account_id: str) -> Account | None:
        return self._accounts.get(account_id)

    def find_by_tenant(self, tenant_id: str) -> Account | None:
        for a in self._accounts.values():
            if tenant_id in a.tenant_ids:
                return a
        return None

    def list_accounts(self) -> list[Account]:
        return sorted(self._accounts.values(), key=lambda a: a.id)

    def append_usage(self, event: UsageEvent) -> None:
        self._usage.append(event)

    def usage_for(self, account_id: str, period: str) -> list[UsageEvent]:
        return [e for e in self._usage if e.account_id == account_id and e.period == period]


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlAccountStore:
    """Accounts + append-only usage over any DB-API 2.0 connection. `placeholder`
    is `?` (sqlite) or `%s` (psycopg)."""

    def __init__(
        self,
        connection: _DbApiConnection,
        *,
        account_table: str = "billing_account",
        usage_table: str = "billing_usage",
        placeholder: str = "?",
    ) -> None:
        self._conn = connection
        self._at = account_table
        self._ut = usage_table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._at} "
                "(id TEXT PRIMARY KEY, name TEXT NOT NULL, billing_email TEXT NOT NULL, "
                "tier TEXT NOT NULL, status TEXT NOT NULL, tenant_ids TEXT NOT NULL DEFAULT '', "
                "seats_used INTEGER NOT NULL DEFAULT 0, stripe_customer_id TEXT, "
                "stripe_subscription_id TEXT, trial_end INTEGER, created_at INTEGER NOT NULL DEFAULT 0)"
            )
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._ut} "
                "(account_id TEXT NOT NULL, kind TEXT NOT NULL, quantity INTEGER NOT NULL, "
                "period TEXT NOT NULL, tenant_id TEXT NOT NULL DEFAULT '', at INTEGER NOT NULL DEFAULT 0, "
                "note TEXT NOT NULL DEFAULT '')"
            )
        finally:
            cur.close()
        self._conn.commit()

    def save_account(self, account: Account) -> None:
        p = self._ph
        cols = ("id", "name", "billing_email", "tier", "status", "tenant_ids", "seats_used",
                "stripe_customer_id", "stripe_subscription_id", "trial_end", "created_at")
        vals = (account.id, account.name, account.billing_email, account.tier.value,
                account.status.value, ",".join(account.tenant_ids), account.seats_used,
                account.stripe_customer_id, account.stripe_subscription_id,
                account.trial_end, account.created_at)
        setters = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._at} ({', '.join(cols)}) "
                f"VALUES ({', '.join([p] * len(cols))}) "
                f"ON CONFLICT (id) DO UPDATE SET {setters}",
                vals,
            )
        finally:
            cur.close()
        self._conn.commit()

    def _row_to_account(self, r: tuple[object, ...]) -> Account:
        tenant_ids = tuple(t for t in str(r[5]).split(",") if t)
        return Account(
            id=str(r[0]), name=str(r[1]), billing_email=str(r[2]), tier=Tier(str(r[3])),
            status=AccountStatus(str(r[4])), tenant_ids=tenant_ids, seats_used=int(str(r[6])),
            stripe_customer_id=str(r[7]) if r[7] is not None else None,
            stripe_subscription_id=str(r[8]) if r[8] is not None else None,
            trial_end=int(str(r[9])) if r[9] is not None else None,
            created_at=int(str(r[10])),
        )

    def _query(self, sql: str, params: tuple[object, ...]) -> list[tuple[object, ...]]:
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            return cur.fetchall()
        finally:
            cur.close()

    def get_account(self, account_id: str) -> Account | None:
        rows = self._query(f"SELECT * FROM {self._at} WHERE id={self._ph}", (account_id,))
        return self._row_to_account(rows[0]) if rows else None

    def find_by_tenant(self, tenant_id: str) -> Account | None:
        for r in self._query(f"SELECT * FROM {self._at}", ()):
            acct = self._row_to_account(r)
            if tenant_id in acct.tenant_ids:
                return acct
        return None

    def list_accounts(self) -> list[Account]:
        rows = self._query(f"SELECT * FROM {self._at} ORDER BY id", ())
        return [self._row_to_account(r) for r in rows]

    def append_usage(self, event: UsageEvent) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._ut} (account_id, kind, quantity, period, tenant_id, at, note) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}, {p})",
                (event.account_id, event.kind.value, event.quantity, event.period,
                 event.tenant_id, event.at, event.note),
            )
        finally:
            cur.close()
        self._conn.commit()

    def usage_for(self, account_id: str, period: str) -> list[UsageEvent]:
        rows = self._query(
            f"SELECT account_id, kind, quantity, period, tenant_id, at, note "
            f"FROM {self._ut} WHERE account_id={self._ph} AND period={self._ph}",
            (account_id, period),
        )
        return [UsageEvent(str(r[0]), UsageKind(str(r[1])), int(str(r[2])), str(r[3]),
                           str(r[4]), int(str(r[5])), str(r[6])) for r in rows]
