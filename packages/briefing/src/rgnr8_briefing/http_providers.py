"""Real, HTTP-backed delivery transports.

`providers.py` defines the `EmailTransport`/`PushTransport` seams and the
`ProviderDeliverer` that routes an envelope across them; `FakeEmailTransport`/
`FakePushTransport` are the in-memory test doubles. This module adds the
*production-shaped* transports: they format a real provider payload (SendGrid for
email, a generic FCM/Expo-style body for push) and post it over an injected
`HttpClient` — exactly the seam pattern the connectors package uses for `fetch`.
That keeps payload-building, auth headers, and error handling fully unit-testable
with a fake, so going live is just supplying a real HTTP client + credentials.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from .providers import TransportError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""


class HttpClient(Protocol):
    def post_json(
        self, url: str, headers: dict[str, str], payload: dict[str, object]
    ) -> HttpResponse:
        """POST a JSON payload; return the response. The only thing to implement
        for production (wrap urllib/requests). Raise TransportError on a
        transport-level failure (connection refused, timeout)."""
        ...


def _require_2xx(resp: HttpResponse, what: str) -> None:
    if not (200 <= resp.status < 300):
        raise TransportError(f"{what} failed: HTTP {resp.status} {resp.body[:200]}")


class HttpEmailTransport:
    """Send email via a SendGrid-shaped JSON API over an injected HTTP client.

    The default `api_url` and payload match SendGrid v3 `mail/send`, but the
    shape is standard enough that other providers slot in with a different URL +
    key. The provider message id comes from the `X-Message-Id` response header
    (SendGrid returns 202 + that header); absent one, a synthetic `accepted:<n>`
    id is returned so callers always get a handle.
    """

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str,
        from_email: str,
        api_url: str = "https://api.sendgrid.com/v3/mail/send",
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._from = from_email
        self._url = api_url
        self._counter = 0

    def send_email(
        self, recipient: str, subject: str, text_body: str, html_body: str
    ) -> str:
        payload: dict[str, object] = {
            "personalizations": [{"to": [{"email": recipient}]}],
            "from": {"email": self._from},
            "subject": subject,
            "content": [
                {"type": "text/plain", "value": text_body},
                {"type": "text/html", "value": html_body},
            ],
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = self._http.post_json(self._url, headers, payload)
        _require_2xx(resp, "email send")
        self._counter += 1
        mid = resp.headers.get("X-Message-Id") or resp.headers.get("x-message-id")
        return mid if mid else f"accepted:{self._counter}"


class HttpPushTransport:
    """Send a push notification via a generic JSON API (FCM/Expo-style) over an
    injected HTTP client. Returns the provider id from the response body's `id`
    field, else a synthetic id."""

    def __init__(
        self,
        http: HttpClient,
        *,
        api_key: str,
        api_url: str,
    ) -> None:
        self._http = http
        self._api_key = api_key
        self._url = api_url
        self._counter = 0

    def send_push(self, recipient: str, title: str, body: str) -> str:
        payload: dict[str, object] = {"to": recipient, "title": title, "body": body}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        resp = self._http.post_json(self._url, headers, payload)
        _require_2xx(resp, "push send")
        self._counter += 1
        try:
            parsed = json.loads(resp.body) if resp.body else {}
            pid = parsed.get("id") if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pid = None
        return str(pid) if pid else f"accepted:{self._counter}"


# --- production HTTP client (stdlib urllib; not exercised in unit tests) ------


class UrllibHttpClient:
    """A real `HttpClient` over stdlib `urllib` — no third-party dependency.
    Network-touching, so it's the production path, not unit-tested here."""

    def __init__(self, *, timeout: float = 10.0) -> None:
        self._timeout = timeout

    def post_json(
        self, url: str, headers: dict[str, str], payload: dict[str, object]
    ) -> HttpResponse:  # pragma: no cover - network path
        import urllib.error
        import urllib.request

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                hdrs = {k: v for k, v in resp.headers.items()}
                return HttpResponse(status=resp.status, headers=hdrs, body=raw)
        except urllib.error.HTTPError as exc:  # 4xx/5xx still return a response
            raw = exc.read().decode("utf-8", "replace") if exc.fp else ""
            return HttpResponse(status=exc.code, headers=dict(exc.headers or {}), body=raw)
        except urllib.error.URLError as exc:
            raise TransportError(f"connection failed: {exc.reason}") from exc


# --- fake for tests ----------------------------------------------------------


class FakeHttpClient:
    """Records POSTs; returns a scripted response (default 202 with a message id).
    Set `status`/`headers`/`body` to script the reply, or `raise_error` to
    simulate a transport failure."""

    def __init__(
        self,
        *,
        status: int = 202,
        headers: dict[str, str] | None = None,
        body: str = "",
        raise_error: bool = False,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._status = status
        self._headers = headers if headers is not None else {"X-Message-Id": "msg-abc123"}
        self._body = body
        self._raise = raise_error

    def post_json(
        self, url: str, headers: dict[str, str], payload: dict[str, object]
    ) -> HttpResponse:
        self.calls.append({"url": url, "headers": headers, "payload": payload})
        if self._raise:
            raise TransportError("simulated connection failure")
        return HttpResponse(status=self._status, headers=dict(self._headers), body=self._body)
