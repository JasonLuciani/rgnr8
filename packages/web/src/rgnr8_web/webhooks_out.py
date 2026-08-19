"""Outbound webhooks — RGNR8 emits signed events to customer endpoints.

The mirror of the inbound Plaid/Gusto webhooks we verify: when something the owner
cares about happens (`cash.at_risk`, `close.sealed`, `briefing.sent`), we POST a
signed JSON envelope to each endpoint a tenant has registered for that event. The
signature is HMAC-SHA256 over the exact body with the endpoint's secret (the same
scheme partners verify on their side). Delivery goes over an injected `HttpClient`
so it's testable without a network; failures are reported per-endpoint with the
attempt count so the scheduler can retry (at-least-once + idempotency via the
event id).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlsplit

# the canonical outbound event catalog
EVENTS = ("cash.at_risk", "close.sealed", "briefing.sent")

# hostnames that always resolve to the loopback interface — rejected by name so a
# blocked target can't slip through when we don't resolve DNS.
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


def _parse_ip(host: str) -> "ipaddress.IPv4Address | ipaddress.IPv6Address | None":
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def validate_target(url: str, *, allow_hosts: "Sequence[str] | None" = None) -> str | None:
    """SSRF guard for an outbound webhook target. Returns an error string when the
    URL must NOT be requested, or ``None`` when it is a safe public https target.

    Blocks: non-https schemes; loopback/localhost; and any URL whose host is an IP
    literal in a private, loopback, link-local (incl. the 169.254.169.254 cloud
    metadata address), reserved, multicast, or unspecified range (covers 10/8,
    172.16/12, 192.168/16, ::1, and fc00::/7). An optional explicit ``allow_hosts``
    list further restricts delivery to named hosts."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        return f"blocked: scheme {parts.scheme or '(none)'!r} is not https"
    host = parts.hostname
    if not host:
        return "blocked: URL has no host"
    h = host.lower()
    if allow_hosts is not None and h not in {a.lower() for a in allow_hosts}:
        return f"blocked: host {host!r} is not in the allowlist"
    if h in _LOOPBACK_NAMES or h.endswith(".localhost"):
        return f"blocked: loopback host {host!r}"
    ip = _parse_ip(host)
    if ip is not None and (
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
        or ip.is_multicast or ip.is_unspecified
    ):
        return f"blocked: non-public address {host}"
    return None


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: str = ""


class HttpClient(Protocol):
    def post_json(self, url: str, body: str, headers: dict[str, str]) -> HttpResponse: ...


class UrllibHttpClient:
    """A concrete HTTP POST client over stdlib urllib — the production transport
    the delivery worker uses. No new dependency; a transport error raises, which
    the durable dispatcher turns into a backoff-and-retry."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def post_json(self, url: str, body: str, headers: dict[str, str]) -> HttpResponse:
        import urllib.error
        import urllib.request

        req = urllib.request.Request(url, data=body.encode("utf-8"),
                                     headers=dict(headers), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                return HttpResponse(resp.status, resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:  # non-2xx still a real HTTP response
            return HttpResponse(exc.code, exc.read().decode("utf-8", "replace"))
        # URLError / socket timeouts propagate → the dispatcher schedules a retry.


@dataclass(frozen=True, slots=True)
class WebhookEndpoint:
    id: str
    tenant_id: str
    url: str
    secret: str
    events: tuple[str, ...] = EVENTS  # which event types this endpoint wants
    active: bool = True

    def wants(self, event_type: str) -> bool:
        return self.active and event_type in self.events


@dataclass(frozen=True, slots=True)
class PlatformEvent:
    id: str          # stable id → consumers dedupe (idempotency)
    type: str
    tenant_id: str
    at: int
    data: dict[str, object] = field(default_factory=dict)

    def envelope(self) -> str:
        return json.dumps({"id": self.id, "type": self.type, "tenant": self.tenant_id,
                           "at": self.at, "data": self.data}, sort_keys=True)


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    endpoint_id: str
    event_id: str
    ok: bool
    status: int
    attempts: int
    error: str = ""


class WebhookEndpointStore(Protocol):
    def save(self, endpoint: WebhookEndpoint) -> None: ...
    def for_tenant(self, tenant_id: str) -> list[WebhookEndpoint]: ...
    def get(self, endpoint_id: str) -> WebhookEndpoint | None: ...
    def delete(self, endpoint_id: str) -> None: ...


class InMemoryWebhookEndpointStore:
    def __init__(self) -> None:
        self._eps: dict[str, WebhookEndpoint] = {}

    def save(self, endpoint: WebhookEndpoint) -> None:
        self._eps[endpoint.id] = endpoint

    def for_tenant(self, tenant_id: str) -> list[WebhookEndpoint]:
        return sorted((e for e in self._eps.values() if e.tenant_id == tenant_id),
                      key=lambda e: e.id)

    def get(self, endpoint_id: str) -> WebhookEndpoint | None:
        return self._eps.get(endpoint_id)

    def delete(self, endpoint_id: str) -> None:
        self._eps.pop(endpoint_id, None)


class _EpCursor(Protocol):
    def execute(self, sql: str, params: object = ..., /) -> object: ...
    def fetchall(self) -> list[tuple[object, ...]]: ...
    def close(self) -> None: ...


class _EpConnection(Protocol):
    def cursor(self) -> _EpCursor: ...
    def commit(self) -> None: ...


class SqlWebhookEndpointStore:
    """Per-tenant webhook endpoints over any DB-API 2.0 connection. The endpoint
    secret is stored as-is here; wrap the connection's column, or pass a cipher at
    a higher layer, if the deployment wants it encrypted at rest like the QBO
    tokens. ``events`` is stored as a comma-separated list."""

    def __init__(self, connection: _EpConnection, *, table: str = "webhook_endpoint",
                 placeholder: str = "?") -> None:
        self._conn = connection
        self._t = table
        self._ph = placeholder

    def create_schema(self) -> None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {self._t} "
                "(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, url TEXT NOT NULL, "
                "secret TEXT NOT NULL, events TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 1)"
            )
        finally:
            cur.close()
        self._conn.commit()

    def _row(self, r: tuple[object, ...]) -> WebhookEndpoint:
        events = tuple(e for e in str(r[4]).split(",") if e)
        return WebhookEndpoint(str(r[0]), str(r[1]), str(r[2]), str(r[3]),
                               events or EVENTS, bool(int(str(r[5]))))

    def save(self, endpoint: WebhookEndpoint) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"INSERT INTO {self._t} (id, tenant_id, url, secret, events, active) "
                f"VALUES ({p}, {p}, {p}, {p}, {p}, {p}) "
                "ON CONFLICT (id) DO UPDATE SET tenant_id=excluded.tenant_id, url=excluded.url, "
                "secret=excluded.secret, events=excluded.events, active=excluded.active",
                (endpoint.id, endpoint.tenant_id, endpoint.url, endpoint.secret,
                 ",".join(endpoint.events), 1 if endpoint.active else 0),
            )
        finally:
            cur.close()
        self._conn.commit()

    def for_tenant(self, tenant_id: str) -> list[WebhookEndpoint]:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT id, tenant_id, url, secret, events, active FROM {self._t} "
                f"WHERE tenant_id={p} ORDER BY id", (tenant_id,))
            rows = cur.fetchall()
        finally:
            cur.close()
        return [self._row(r) for r in rows]

    def get(self, endpoint_id: str) -> WebhookEndpoint | None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(
                f"SELECT id, tenant_id, url, secret, events, active FROM {self._t} WHERE id={p}",
                (endpoint_id,))
            rows = cur.fetchall()
        finally:
            cur.close()
        return self._row(rows[0]) if rows else None

    def delete(self, endpoint_id: str) -> None:
        p = self._ph
        cur = self._conn.cursor()
        try:
            cur.execute(f"DELETE FROM {self._t} WHERE id={p}", (endpoint_id,))
        finally:
            cur.close()
        self._conn.commit()


def sign(body: str, secret: str) -> str:
    """HMAC-SHA256 over the exact body → ``sha256=<hex>`` (what partners verify)."""
    mac = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256)
    return "sha256=" + mac.hexdigest()


class WebhookDispatcher:
    def __init__(
        self,
        store: WebhookEndpointStore,
        http: HttpClient,
        *,
        clock: Callable[[], int] | None = None,
        max_attempts: int = 3,
        allow_hosts: "Sequence[str] | None" = None,
    ) -> None:
        self._store = store
        self._http = http
        self._clock = clock if clock is not None else (lambda: int(time.time()))
        self._max = max_attempts
        # optional explicit host allowlist — when set, only these hosts are targets
        self._allow_hosts = tuple(allow_hosts) if allow_hosts is not None else None
        # server-side idempotency: (endpoint_id, event_id) pairs already delivered
        # OK, so re-dispatching the same event never double-delivers to an endpoint.
        self._delivered: set[tuple[str, str]] = set()

    def dispatch(self, event: PlatformEvent) -> list[DeliveryResult]:
        """Deliver an event to every subscribed endpoint for its tenant. Retries up
        to `max_attempts` on transport error / non-2xx; each attempt re-sends the
        same signed body so consumers can dedupe on `event.id`. Server-side dedupe
        skips any (endpoint, event) pair already delivered — within this run or a
        prior one — so the same event isn't POSTed to an endpoint twice."""
        body = event.envelope()
        results: list[DeliveryResult] = []
        seen: set[tuple[str, str]] = set()
        for ep in self._store.for_tenant(event.tenant_id):
            if not ep.wants(event.type):
                continue
            key = (ep.id, event.id)
            if key in seen or key in self._delivered:
                continue
            seen.add(key)
            result = self._deliver(ep, event.id, body)
            if result.ok:
                self._delivered.add(key)
            results.append(result)
        return results

    def _deliver(self, ep: WebhookEndpoint, event_id: str, body: str) -> DeliveryResult:
        # SSRF guard: never POST to a non-public / non-https target. A blocked
        # endpoint yields a failed result with a clear error and makes no request.
        blocked = validate_target(ep.url, allow_hosts=self._allow_hosts)
        if blocked is not None:
            return DeliveryResult(ep.id, event_id, False, 0, 0, blocked)
        headers = {"content-type": "application/json",
                   "X-RGNR8-Signature": sign(body, ep.secret),
                   "X-RGNR8-Event-Id": event_id}
        last_status = 0
        last_error = ""
        for attempt in range(1, self._max + 1):
            try:
                resp = self._http.post_json(ep.url, body, headers)
                last_status = resp.status
                if 200 <= resp.status < 300:
                    return DeliveryResult(ep.id, event_id, True, resp.status, attempt)
                last_error = f"HTTP {resp.status}"
            except Exception as exc:  # transport failure → retry
                last_error = f"{type(exc).__name__}: {exc}"
        return DeliveryResult(ep.id, event_id, False, last_status, self._max, last_error)


def cash_at_risk_event(event_id: str, tenant_id: str, at: int, *, weeks_until: int,
                       shortfall: str) -> PlatformEvent:
    return PlatformEvent(event_id, "cash.at_risk", tenant_id, at,
                         {"weeks_until_breach": weeks_until, "shortfall": shortfall})


def close_sealed_event(event_id: str, tenant_id: str, at: int, *, period: str) -> PlatformEvent:
    return PlatformEvent(event_id, "close.sealed", tenant_id, at, {"period": period})


def briefing_sent_event(event_id: str, tenant_id: str, at: int, *, status: str) -> PlatformEvent:
    return PlatformEvent(event_id, "briefing.sent", tenant_id, at, {"status": status})
