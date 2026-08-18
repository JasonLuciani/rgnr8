"""RGNR8 QuickBooks Online connect — the OAuth 2.0 acquisition flow + connection.

The TS ``@rgnr8/connectors`` package already syncs QBO *given* an access token +
company realm. This package is the missing half: how a tenant actually **grants**
that access (the OAuth authorization-code dance), how the resulting tokens are
**persisted** per tenant, and how they're **refreshed** before each sync — all
behind an injected HTTP + clock seam so it's deterministic and testable with a
fake and no real credentials.

* :mod:`.oauth` — the low-level OAuth client (authorize URL, code exchange, token
  refresh, revoke) against Intuit's endpoints, over an :class:`HttpClient` seam.
* :mod:`.connection` — :class:`QboConnection` + a per-tenant store (Protocol +
  in-memory + SQL).
* :mod:`.service` — :class:`QboConnectService`, the four-method surface the web
  layer calls (begin / complete / ensure_fresh / disconnect), with signed CSRF
  ``state``.
"""

from __future__ import annotations

from .oauth import (
    ACCOUNTING_SCOPE,
    AUTHORIZE_ENDPOINT,
    HttpClient,
    HttpResponse,
    QboEnvironment,
    QboOAuthConfig,
    QboOAuthError,
    QboTokens,
    REVOKE_ENDPOINT,
    TOKEN_ENDPOINT,
    UrllibHttpClient,
    authorize_url,
    exchange_code,
    refresh_tokens,
    revoke,
)
from .connection import (
    ConnectionStore,
    InMemoryConnectionStore,
    QboConnection,
    QboStatus,
    SqlConnectionStore,
    connection_from_dict,
    connection_to_dict,
)
from .client import (
    MINOR_VERSION,
    QboAccount,
    QboApiClient,
    QboApiError,
    QboBill,
    QboBankTxn,
    QboCompany,
    QboInvoice,
)
from .service import (
    DEFAULT_STATE_TTL,
    QboConnectService,
    StateError,
    StateSigner,
)

__version__ = "0.1.0"

__all__ = [
    # oauth
    "QboOAuthConfig",
    "QboEnvironment",
    "QboTokens",
    "QboOAuthError",
    "HttpClient",
    "HttpResponse",
    "UrllibHttpClient",
    "authorize_url",
    "exchange_code",
    "refresh_tokens",
    "revoke",
    "ACCOUNTING_SCOPE",
    "AUTHORIZE_ENDPOINT",
    "TOKEN_ENDPOINT",
    "REVOKE_ENDPOINT",
    # connection
    "QboConnection",
    "QboStatus",
    "ConnectionStore",
    "InMemoryConnectionStore",
    "SqlConnectionStore",
    "connection_to_dict",
    "connection_from_dict",
    # service
    "QboConnectService",
    "StateSigner",
    "StateError",
    "DEFAULT_STATE_TTL",
    # api client
    "QboApiClient",
    "QboApiError",
    "QboAccount",
    "QboInvoice",
    "QboBill",
    "QboBankTxn",
    "QboCompany",
    "MINOR_VERSION",
]
