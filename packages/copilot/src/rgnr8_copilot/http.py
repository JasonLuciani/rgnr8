"""A concrete `HttpClient` over the standard library.

`HttpLLM` talks to a provider through the `HttpClient` Protocol (a single
`post_json`). Everything else in the package is tested with a fake client and no
network. This is the one place a real socket is opened — kept tiny and dependency
free (urllib, stdlib) so "wiring a real client turns it live" is literally true:
bind `UrllibHttpClient()` and an API key to `HttpLLM` and the copilot is live.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpError(Exception):
    """A non-2xx response or a transport failure, with the status when we have one."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class UrllibHttpClient:
    """POSTs a JSON body and parses a JSON response. `timeout` bounds the call so a
    hung provider can't wedge a request thread."""

    def __init__(self, *, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def post_json(
        self, url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("content-type", "application/json")
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise HttpError(f"HTTP {exc.code}: {detail}", status=exc.code) from exc
        except urllib.error.URLError as exc:
            raise HttpError(f"request failed: {exc.reason}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HttpError("provider returned a non-JSON response") from exc
        if not isinstance(parsed, dict):
            raise HttpError("provider returned a non-object JSON response")
        return parsed


__all__ = ["UrllibHttpClient", "HttpError"]
