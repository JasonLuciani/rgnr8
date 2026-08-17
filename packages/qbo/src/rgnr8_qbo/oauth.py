"""The QuickBooks Online OAuth 2.0 client — acquire and refresh tokens.

QBO uses the standard OAuth 2.0 **authorization-code** grant. The flow is:

1. Send the owner to Intuit's **authorize** endpoint (`authorize_url`) with our
   client id, redirect URI, the accounting **scope**, and an unguessable
   ``state`` (CSRF).
2. Intuit redirects back to our ``redirect_uri`` with a ``code`` and the company
   ``realmId``.
3. We **exchange** the code for an access token (~1 h) + a refresh token
   (~100 days, rotates on every use) at the **token** endpoint (`exchange_code`).
4. Before the access token expires we **refresh** it (`refresh_tokens`).
5. On disconnect we **revoke** the refresh token (`revoke`).

Endpoints (stable across sandbox + production; only the *API base* differs):

* authorize — ``https://appcenter.intuit.com/connect/oauth2``
* token     — ``https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer``
* revoke    — ``https://developer.api.intuit.com/v2/oauth2/tokens/revoke``

All network I/O goes through the injected :class:`HttpClient` seam (one generic
``post``), so the whole flow is deterministic and unit-testable with a fake — no
real network and **no real credentials** in tests. The default
:class:`UrllibHttpClient` uses only the standard library.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from enum import Enum
from typing import Mapping, Protocol

AUTHORIZE_ENDPOINT = "https://appcenter.intuit.com/connect/oauth2"
TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE_ENDPOINT = "https://developer.api.intuit.com/v2/oauth2/tokens/revoke"

FORM_CONTENT_TYPE = "application/x-www-form-urlencoded"
JSON_CONTENT_TYPE = "application/json"

# The one scope RGNR8 needs — read/write access to the Accounting API.
ACCOUNTING_SCOPE = "com.intuit.quickbooks.accounting"


class QboEnvironment(str, Enum):
    PRODUCTION = "production"
    SANDBOX = "sandbox"

    @property
    def api_base(self) -> str:
        """The company API host for this environment (the authorize/token hosts
        are shared; only the data API base differs)."""
        if self is QboEnvironment.SANDBOX:
            return "https://sandbox-quickbooks.api.intuit.com"
        return "https://quickbooks.api.intuit.com"


class QboOAuthError(Exception):
    """An OAuth request to Intuit failed (non-2xx, or an unparseable response)."""


@dataclass(frozen=True, slots=True)
class QboOAuthConfig:
    """The Intuit Developer app's OAuth settings. ``client_id``/``client_secret``
    come from the app's Keys tab; ``redirect_uri`` must be registered on the app
    **byte-for-byte** as it appears here."""

    client_id: str
    client_secret: str
    redirect_uri: str
    environment: QboEnvironment = QboEnvironment.PRODUCTION
    scopes: tuple[str, ...] = (ACCOUNTING_SCOPE,)

    @property
    def api_base(self) -> str:
        return self.environment.api_base

    def basic_auth(self) -> str:
        """The HTTP Basic ``Authorization`` header value for the token/revoke
        endpoints (base64 of ``client_id:client_secret``)."""
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


@dataclass(frozen=True, slots=True)
class QboTokens:
    """The token set from a code exchange or a refresh. ``expires_in`` and
    ``refresh_token_expires_in`` are lifetimes in **seconds** from *now*; the
    service layer stamps absolute expiries from its injected clock."""

    access_token: str
    refresh_token: str
    expires_in: int
    refresh_token_expires_in: int
    token_type: str = "bearer"


# --- HTTP seam ---------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: str


class HttpClient(Protocol):
    """A generic POST (for the OAuth token calls) and GET (for the Accounting API
    reads). ``body`` is already encoded (form or JSON); ``headers`` carry the
    content type + auth. A fake implements this for tests; :class:`UrllibHttpClient`
    is the real one."""

    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse: ...

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse: ...


class UrllibHttpClient:
    """The production :class:`HttpClient` over the standard library. ``timeout``
    is in seconds."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def _send(self, req: "urllib.request.Request") -> HttpResponse:
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                return HttpResponse(status=int(resp.status), body=resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # non-2xx still carries a body
            return HttpResponse(status=int(exc.code), body=exc.read().decode("utf-8", "replace"))
        except urllib.error.URLError as exc:  # network-level failure
            raise QboOAuthError(f"network error contacting Intuit: {exc.reason}") from exc

    def post(self, url: str, body: str, headers: Mapping[str, str]) -> HttpResponse:
        req = urllib.request.Request(url, data=body.encode("utf-8"), method="POST")
        for key, value in headers.items():
            req.add_header(key, value)
        return self._send(req)

    def get(self, url: str, headers: Mapping[str, str]) -> HttpResponse:
        req = urllib.request.Request(url, method="GET")
        for key, value in headers.items():
            req.add_header(key, value)
        return self._send(req)


# --- flow --------------------------------------------------------------------
def authorize_url(config: QboOAuthConfig, state: str) -> str:
    """The URL to send the owner to so they can grant access. ``state`` is echoed
    back on the callback and must be verified (CSRF)."""
    query = urllib.parse.urlencode(
        {
            "client_id": config.client_id,
            "response_type": "code",
            "scope": " ".join(config.scopes),
            "redirect_uri": config.redirect_uri,
            "state": state,
        }
    )
    return f"{AUTHORIZE_ENDPOINT}?{query}"


def _post_token(config: QboOAuthConfig, http: HttpClient, form: Mapping[str, str]) -> QboTokens:
    headers = {
        "Authorization": config.basic_auth(),
        "Accept": JSON_CONTENT_TYPE,
        "Content-Type": FORM_CONTENT_TYPE,
    }
    resp = http.post(TOKEN_ENDPOINT, urllib.parse.urlencode(dict(form)), headers)
    if resp.status < 200 or resp.status >= 300:
        raise QboOAuthError(f"token endpoint returned {resp.status}: {resp.body[:300]}")
    try:
        data = json.loads(resp.body)
    except json.JSONDecodeError as exc:
        raise QboOAuthError("token endpoint returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise QboOAuthError("token endpoint returned a non-object payload")
    try:
        return QboTokens(
            access_token=str(data["access_token"]),
            refresh_token=str(data["refresh_token"]),
            expires_in=int(data.get("expires_in", 3600)),
            refresh_token_expires_in=int(data.get("x_refresh_token_expires_in", 8_640_000)),
            token_type=str(data.get("token_type", "bearer")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise QboOAuthError(f"token response missing expected fields: {exc}") from exc


def exchange_code(config: QboOAuthConfig, http: HttpClient, code: str) -> QboTokens:
    """Exchange an authorization ``code`` (from the callback) for tokens."""
    return _post_token(
        config,
        http,
        {"grant_type": "authorization_code", "code": code, "redirect_uri": config.redirect_uri},
    )


def refresh_tokens(config: QboOAuthConfig, http: HttpClient, refresh_token: str) -> QboTokens:
    """Exchange a (still-valid) ``refresh_token`` for a fresh token set. Intuit
    rotates the refresh token on every use, so the *returned* refresh token must
    replace the stored one."""
    return _post_token(
        config, http, {"grant_type": "refresh_token", "refresh_token": refresh_token}
    )


def revoke(config: QboOAuthConfig, http: HttpClient, token: str) -> bool:
    """Revoke a refresh (or access) token on disconnect. Returns whether Intuit
    accepted it; best-effort (a failure still lets us drop our stored copy)."""
    headers = {
        "Authorization": config.basic_auth(),
        "Accept": JSON_CONTENT_TYPE,
        "Content-Type": JSON_CONTENT_TYPE,
    }
    resp = http.post(REVOKE_ENDPOINT, json.dumps({"token": token}), headers)
    return 200 <= resp.status < 300
