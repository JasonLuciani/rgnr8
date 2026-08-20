"""A durable outbox for outbound webhooks — delivery that survives a restart.

``webhooks_out.WebhookDispatcher`` signs and POSTs events, but it retries only
within one in-process run and remembers what it delivered only in memory. If the
process dies mid-delivery, or an endpoint is down for an hour, those events are
lost — and "a silent drop in an integration is a customer nobody called back".

This module makes delivery durable. Every event is written to an **outbox**: one
row per subscribed endpoint, ``PENDING``. A worker (driven by the scheduler, or
a route) delivers everything due, and on failure schedules a retry with
exponential backoff, giving up to a ``DEAD`` letter after a bounded number of
attempts. Because the row itself is the record, a crash resumes exactly where it
left off, the same event is never delivered twice (a unique ``(endpoint, event)``
key), and a dead letter can be **replayed** by resetting it to pending.

The signing, SSRF guard, and HTTP seam are reused verbatim from ``webhooks_out``
so partners verify one signature scheme and tests need no network.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import Callable, Protocol, Sequence

from .webhooks_out import (
    HttpClient,
    PlatformEvent,
    Resolver,
    WebhookEndpoint,
    WebhookEndpointStore,
    sign,
    validate_target,
)

# delivery lifecycle
PENDING = "pending"
DELIVERED = "delivered"
DEAD = "dead"

_DEFAULT_MAX_ATTEMPTS = 6
_BACKOFF_BASE_SECONDS = 60
_BACKOFF_CAP_SECONDS = 3600


def backoff_seconds(attempts: int, *, base: int = _BACKOFF_BASE_SECONDS,
                    cap: int = _BACKOFF_CAP_SECONDS) -> int:
    """Exponential backoff after ``attempts`` failures, capped. 1→base, 2→2·base…"""
    if attempts <= 0:
        return base
    return int(min(base * (2 ** (attempts - 1)), cap))


@dataclass(frozen=True, slots=True)
class OutboxDelivery:
    id: str                 # stable: f"{endpoint_id}:{event_id}" — the dedupe key
    tenant_id: str
    endpoint_id: str
    event_id: str
    event_type: str
    body: str               # the exact signed envelope bytes
    status: str = PENDING
    attempts: int = 0
    next_attempt_at: int = 0
    last_error: str = ""
    last_status: int = 0
    created_at: int = 0


class WebhookOutbox(Protocol):
    def enqueue(self, delivery: OutboxDelivery) -> bool: ...
    def due(self, now: int, *, limit: int = 100) -> list[OutboxDelivery]: ...
    def update(self, delivery: OutboxDelivery) -> None: ...
    def get(self, delivery_id: str) -> OutboxDelivery | None: ...
    def for_tenant(self, tenant_id: str, *, status: str | None = None) -> list[OutboxDelivery]: ...


class InMemoryWebhookOutbox:
    def __init__(self) -> None:
        self._rows: dict[str, OutboxDelivery] = {}

    def enqueue(self, delivery: OutboxDelivery) -> bool:
        # Idempotent: an already-queued (endpoint, event) is never duplicated.
        if delivery.id in self._rows:
            return False
        self._rows[delivery.id] = delivery
        return True

    def due(self, now: int, *, limit: int = 100) -> list[OutboxDelivery]:
        out = [d for d in self._rows.values()
               if d.status == PENDING and d.next_attempt_at <= now]
        out.sort(key=lambda d: (d.next_attempt_at, d.created_at, d.id))
        return out[:limit]

    def update(self, delivery: OutboxDelivery) -> None:
        self._rows[delivery.id] = delivery

    def get(self, delivery_id: str) -> OutboxDelivery | None:
        return self._rows.get(delivery_id)

    def for_tenant(self, tenant_id: str, *, status: str | None = None) -> list[OutboxDelivery]:
        out = [d for d in self._rows.values()
               if d.tenant_id == tenant_id and (status is None or d.status == status)]
        out.sort(key=lambda d: (d.created_at, d.id))
        return out


class _DbApiCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _DbApiConnection(Protocol):
    def cursor(self) -> _DbApiCursor: ...
    def commit(self) -> None: ...


class SqlWebhookOutbox:
    """The outbox over any DB-API 2.0 connection. ``placeholder`` is ``?``
    (sqlite) or ``%s`` (psycopg)."""

    def __init__(self, connection: _DbApiConnection, *, table: str = "webhook_outbox",
                 placeholder: str = "?") -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, endpoint_id TEXT NOT NULL, "
                "event_id TEXT NOT NULL, event_type TEXT NOT NULL, body TEXT NOT NULL, "
                "status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
                "next_attempt_at INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', "
                "last_status INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL DEFAULT 0)"
            )
        finally:
            cur.close()
        self._conn.commit()

    def _row(self, r: tuple[object, ...]) -> OutboxDelivery:
        return OutboxDelivery(
            id=str(r[0]), tenant_id=str(r[1]), endpoint_id=str(r[2]), event_id=str(r[3]),
            event_type=str(r[4]), body=str(r[5]), status=str(r[6]), attempts=int(str(r[7])),
            next_attempt_at=int(str(r[8])), last_error=str(r[9]), last_status=int(str(r[10])),
            created_at=int(str(r[11])),
        )

    _COLS = ("id, tenant_id, endpoint_id, event_id, event_type, body, status, attempts, "
             "next_attempt_at, last_error, last_status, created_at")

    def enqueue(self, delivery: OutboxDelivery) -> bool:
        p = self._ph
        cur = self._conn.cursor()
        try:
            # ON CONFLICT DO NOTHING makes re-enqueue idempotent on the (endpoint,
            # event) primary key — the crash-safe equivalent of the in-memory dedupe.
            cur.execute(
                f"INSERT INTO {self._t} ({self._COLS}) VALUES "
                f"({p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}, {p}) "
                "ON CONFLICT (id) DO NOTHING",
                (delivery.id, delivery.tenant_id, delivery.endpoint_id, delivery.event_id,
                 delivery.event_type, delivery.body, delivery.status, delivery.attempts,
                 delivery.next_attempt_at, delivery.last_error, delivery.last_status,
                 delivery.created_at),
            )
            inserted = getattr(cur, "rowcount", 1)
        finally:
            cur.close()
        self._conn.commit()
        return bool(inserted) if isinstance(inserted, int) else True

    def due(self, now: int, *, limit: int = 100) -> list[OutboxDelivery]:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT {self._COLS} FROM {self._t} WHERE status='pending' AND next_attempt_at<={p} "
                f"ORDER BY next_attempt_at, created_at, id LIMIT {p}",
                (now, limit),
            )
            rows = cur.fetchall()
        finally:
            cur.close()
        return [self._row(r) for r in rows]

    def update(self, delivery: OutboxDelivery) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"UPDATE {self._t} SET status={p}, attempts={p}, next_attempt_at={p}, "
                f"last_error={p}, last_status={p} WHERE id={p}",
                (delivery.status, delivery.attempts, delivery.next_attempt_at,
                 delivery.last_error, delivery.last_status, delivery.id),
            )
        finally:
            cur.close()
        self._conn.commit()

    def get(self, delivery_id: str) -> OutboxDelivery | None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(f"SELECT {self._COLS} FROM {self._t} WHERE id={p}", (delivery_id,))
            rows = cur.fetchall()
        finally:
            cur.close()
        return self._row(rows[0]) if rows else None

    def for_tenant(self, tenant_id: str, *, status: str | None = None) -> list[OutboxDelivery]:
        p = self._ph
        clause = f" AND status={p}" if status is not None else ""
        params: tuple[object, ...] = (tenant_id, status) if status is not None else (tenant_id,)
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT {self._COLS} FROM {self._t} WHERE tenant_id={p}{clause} "
                "ORDER BY created_at, id", params)
            rows = cur.fetchall()
        finally:
            cur.close()
        return [self._row(r) for r in rows]


@dataclass(frozen=True, slots=True)
class DeliverySummary:
    considered: int
    delivered: int
    retried: int
    dead: int


class DurableWebhookDispatcher:
    """Enqueue events to the outbox, then deliver what's due — durably.

    ``enqueue`` fans an event out to one PENDING row per subscribed endpoint and
    returns immediately (no network on the request path). ``deliver_due`` is the
    worker the scheduler runs: it POSTs each due delivery and, on failure,
    reschedules it with exponential backoff until it either succeeds or exhausts
    its attempts and becomes a dead letter. ``replay`` re-arms a dead (or
    delivered) letter for another attempt.
    """

    def __init__(
        self,
        outbox: WebhookOutbox,
        endpoints: WebhookEndpointStore,
        http: HttpClient,
        *,
        clock: Callable[[], int] | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        allow_hosts: "Sequence[str] | None" = None,
        resolver: "Resolver | None" = None,
    ) -> None:
        self._outbox = outbox
        self._endpoints = endpoints
        self._http = http
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._max = max_attempts
        self._allow_hosts = tuple(allow_hosts) if allow_hosts is not None else None
        self._resolver = resolver

    def enqueue(self, event: PlatformEvent) -> int:
        """Persist one PENDING delivery per subscribed endpoint. Returns how many
        were newly queued (idempotent: a re-enqueued event adds nothing)."""
        body = event.envelope()
        now = self._clock()
        queued = 0
        for ep in self._endpoints.for_tenant(event.tenant_id):
            if not ep.wants(event.type):
                continue
            delivery = OutboxDelivery(
                id=f"{ep.id}:{event.id}", tenant_id=event.tenant_id, endpoint_id=ep.id,
                event_id=event.id, event_type=event.type, body=body,
                status=PENDING, attempts=0, next_attempt_at=now, created_at=now,
            )
            if self._outbox.enqueue(delivery):
                queued += 1
        return queued

    def deliver_due(self, *, limit: int = 100) -> DeliverySummary:
        now = self._clock()
        due = self._outbox.due(now, limit=limit)
        delivered = retried = dead = 0
        endpoints_by_id: dict[str, WebhookEndpoint] = {}
        for d in due:
            ep = endpoints_by_id.get(d.endpoint_id)
            if ep is None:
                ep = next((e for e in self._endpoints.for_tenant(d.tenant_id)
                           if e.id == d.endpoint_id), None)
                if ep is not None:
                    endpoints_by_id[d.endpoint_id] = ep
            outcome = self._attempt(d, ep)
            self._outbox.update(outcome)
            if outcome.status == DELIVERED:
                delivered += 1
            elif outcome.status == DEAD:
                dead += 1
            else:
                retried += 1
        return DeliverySummary(len(due), delivered, retried, dead)

    def replay(self, delivery_id: str) -> bool:
        """Re-arm a dead (or already-delivered) letter for another attempt now."""
        d = self._outbox.get(delivery_id)
        if d is None:
            return False
        self._outbox.update(replace(d, status=PENDING, next_attempt_at=self._clock(),
                                    last_error="", last_status=0))
        return True

    def _attempt(self, d: OutboxDelivery, ep: WebhookEndpoint | None) -> OutboxDelivery:
        attempts = d.attempts + 1
        if ep is None:
            return self._fail(d, attempts, 0, "endpoint no longer registered")
        blocked = validate_target(ep.url, allow_hosts=self._allow_hosts, resolver=self._resolver)
        if blocked is not None:
            return self._fail(d, attempts, 0, blocked)
        headers = {"content-type": "application/json",
                   "X-RGNR8-Signature": sign(d.body, ep.secret),
                   "X-RGNR8-Event-Id": d.event_id}
        try:
            resp = self._http.post_json(ep.url, d.body, headers)
            if 200 <= resp.status < 300:
                return replace(d, status=DELIVERED, attempts=attempts,
                               last_status=resp.status, last_error="")
            return self._fail(d, attempts, resp.status, f"HTTP {resp.status}")
        except Exception as exc:  # transport failure → backoff + retry
            return self._fail(d, attempts, 0, f"{type(exc).__name__}: {exc}")

    def _fail(self, d: OutboxDelivery, attempts: int, status: int, error: str) -> OutboxDelivery:
        if attempts >= self._max:
            # exhausted — a dead letter, kept for inspection and replay, never dropped
            return replace(d, status=DEAD, attempts=attempts, last_status=status,
                           last_error=error, next_attempt_at=self._clock())
        return replace(d, status=PENDING, attempts=attempts, last_status=status,
                       last_error=error,
                       next_attempt_at=self._clock() + backoff_seconds(attempts))
