"""The connect service — the orchestration the web layer calls.

This ties the OAuth client (:mod:`.oauth`) to connection persistence
(:mod:`.connection`) and adds the CSRF ``state`` handling, so the web surface
only has to call four methods:

* :meth:`QboConnectService.begin` — mint a signed ``state`` and return the Intuit
  authorize URL to redirect the owner to.
* :meth:`QboConnectService.complete` — on the callback, verify ``state``, exchange
  the ``code`` for tokens, stamp absolute expiries from the injected clock, and
  persist the :class:`~rgnr8_qbo.connection.QboConnection`.
* :meth:`QboConnectService.ensure_fresh` — before a sync, refresh the access token
  if it's near expiry (rotating the refresh token); mark the connection expired if
  the refresh token itself has lapsed.
* :meth:`QboConnectService.disconnect` — revoke at Intuit (best-effort) and drop
  the stored connection.

``state`` is an HMAC-signed ``tenant_id|issued_at`` value — unguessable, tenant-
bound, and time-boxed — so no server-side state table is needed and verification
is deterministic under the injected clock. Everything here takes injected HTTP +
clock, so the whole service is unit-testable with a fake and **no real
credentials**.
"""

from __future__ import annotations

import base64
import hmac
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Callable

from .client import QboApiClient
from .connection import ConnectionStore, QboConnection, QboStatus
from .oauth import (
    HttpClient,
    QboOAuthConfig,
    QboTokens,
    authorize_url,
    exchange_code,
    refresh_tokens,
    revoke,
)

Clock = Callable[[], datetime]

# How long a mint-a-connection ``state`` stays valid (the owner has this long to
# finish the Intuit consent screen).
DEFAULT_STATE_TTL = timedelta(minutes=15)


class StateError(Exception):
    """A callback ``state`` was missing, malformed, tampered, or expired."""


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


class StateSigner:
    """Signs/verifies the OAuth ``state`` as ``b64(tenant|issued)|b64(hmac)``.

    Tenant-bound + time-boxed + tamper-evident, so the callback can recover the
    tenant it belongs to and reject anything it didn't issue — without a
    server-side nonce table."""

    def __init__(self, secret: str, *, clock: Clock) -> None:
        self._key = secret.encode("utf-8")
        self._clock = clock

    def _mac(self, payload: str) -> str:
        return _b64u(hmac.new(self._key, payload.encode("utf-8"), sha256).digest())

    def issue(self, tenant_id: str) -> str:
        issued = int(self._clock().timestamp())
        payload = _b64u(f"{tenant_id}|{issued}".encode("utf-8"))
        return f"{payload}.{self._mac(payload)}"

    def verify(self, state: str, *, ttl: timedelta = DEFAULT_STATE_TTL) -> str:
        try:
            payload, mac = state.split(".", 1)
        except ValueError as exc:
            raise StateError("malformed state") from exc
        if not hmac.compare_digest(mac, self._mac(payload)):
            raise StateError("state signature mismatch")
        try:
            tenant_id, issued_s = _b64u_decode(payload).decode("utf-8").rsplit("|", 1)
            issued = int(issued_s)
        except (ValueError, UnicodeDecodeError) as exc:
            raise StateError("undecodable state") from exc
        now = int(self._clock().timestamp())
        if now - issued > int(ttl.total_seconds()):
            raise StateError("state expired")
        if now + 60 < issued:  # issued in the future beyond small clock skew
            raise StateError("state issued in the future")
        return tenant_id


class QboConnectService:
    """Orchestrates connect / callback / refresh / disconnect for one Intuit app.

    ``config`` is the app's OAuth settings, ``http`` the network seam, ``store``
    the connection persistence, ``state_secret`` the HMAC key for ``state``, and
    ``clock`` the injected timezone-aware UTC clock used for every expiry stamp."""

    def __init__(
        self,
        config: QboOAuthConfig,
        http: HttpClient,
        store: ConnectionStore,
        *,
        state_secret: str,
        clock: Clock | None = None,
    ) -> None:
        self._config = config
        self._http = http
        self._store = store
        self._clock = clock if clock is not None else (lambda: datetime.now(timezone.utc))
        self._signer = StateSigner(state_secret, clock=self._clock)

    @property
    def config(self) -> QboOAuthConfig:
        return self._config

    def begin(self, tenant_id: str) -> str:
        """The Intuit authorize URL to redirect the owner to (carries a signed,
        tenant-bound ``state``)."""
        return authorize_url(self._config, self._signer.issue(tenant_id))

    def _connection_from_tokens(
        self, tenant_id: str, realm_id: str, tokens: QboTokens, *, connected_at: datetime | None
    ) -> QboConnection:
        now = self._clock()
        return QboConnection(
            tenant_id=tenant_id,
            realm_id=realm_id,
            access_token=tokens.access_token,
            refresh_token=tokens.refresh_token,
            access_expires_at=now + timedelta(seconds=tokens.expires_in),
            refresh_expires_at=now + timedelta(seconds=tokens.refresh_token_expires_in),
            status=QboStatus.CONNECTED,
            connected_at=connected_at,
        )

    def complete(self, state: str, code: str, realm_id: str) -> QboConnection:
        """Finish the callback: verify ``state``, exchange ``code``, persist. The
        tenant is recovered from the signed ``state`` (never trusted from an
        unsigned query param)."""
        tenant_id = self._signer.verify(state)
        if not realm_id:
            raise StateError("callback missing realmId")
        tokens = exchange_code(self._config, self._http, code)
        conn = self._connection_from_tokens(
            tenant_id, realm_id, tokens, connected_at=self._clock()
        )
        self._store.save(conn)
        return conn

    def status(self, tenant_id: str) -> QboConnection | None:
        """The stored connection for a tenant, or ``None`` if never connected."""
        return self._store.get(tenant_id)

    def ensure_fresh(self, tenant_id: str, *, skew_seconds: int = 120) -> QboConnection | None:
        """Return a connection whose access token is valid, refreshing if needed.

        ``None`` if the tenant has no connection. If the access token is still
        valid it's returned as-is. If it's near/after expiry but the refresh token
        is still valid, we refresh (rotating the refresh token) and persist. If the
        refresh token has itself lapsed, the connection is marked EXPIRED and
        persisted (the owner must reconnect)."""
        conn = self._store.get(tenant_id)
        if conn is None:
            return None
        now = self._clock()
        if conn.status is QboStatus.CONNECTED and conn.access_valid(now, skew_seconds=skew_seconds):
            return conn
        if not conn.refresh_valid(now):
            expired = QboConnection(
                tenant_id=conn.tenant_id,
                realm_id=conn.realm_id,
                access_token=conn.access_token,
                refresh_token=conn.refresh_token,
                access_expires_at=conn.access_expires_at,
                refresh_expires_at=conn.refresh_expires_at,
                status=QboStatus.EXPIRED,
                connected_at=conn.connected_at,
            )
            self._store.save(expired)
            return expired
        tokens = refresh_tokens(self._config, self._http, conn.refresh_token)
        refreshed = self._connection_from_tokens(
            tenant_id, conn.realm_id, tokens, connected_at=conn.connected_at
        )
        self._store.save(refreshed)
        return refreshed

    def api_client(self, tenant_id: str) -> QboApiClient | None:
        """A ready-to-use Accounting API client for a connected tenant, with a
        freshly-refreshed access token. ``None`` if the tenant isn't connected (or
        its refresh token has lapsed → reconnect needed)."""
        conn = self.ensure_fresh(tenant_id)
        if conn is None or conn.status is not QboStatus.CONNECTED:
            return None
        return QboApiClient(
            http=self._http,
            api_base=self._config.api_base,
            realm_id=conn.realm_id,
            access_token=conn.access_token,
        )

    def disconnect(self, tenant_id: str) -> bool:
        """Revoke at Intuit (best-effort) and drop the stored connection. Returns
        whether a connection existed to disconnect."""
        conn = self._store.get(tenant_id)
        if conn is None:
            return False
        try:
            revoke(self._config, self._http, conn.refresh_token)
        except Exception:  # best-effort: still drop our copy even if revoke fails
            pass
        self._store.delete(tenant_id)
        return True
