"""The one network seam every provider adapter shares.

Production binds a real `requests`/`urllib` client; tests and the shape-correct
adapters bind `RecordingHttpClient`, which returns canned responses and records
exactly what would have been sent. No adapter in this package opens a socket
itself — they build requests and hand them to this client, so the whole surface
is exercisable offline and a real client turns everything live at once.
"""

from __future__ import annotations

from typing import Protocol


class HttpClient(Protocol):
    def get_json(
        self, url: str, headers: dict[str, str]
    ) -> dict[str, object]: ...

    def post_json(
        self, url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]: ...


class RecordingHttpClient:
    """A test/dev client: returns queued responses, records every call. Stands in
    for a real HTTP client so an adapter's request-building is fully testable."""

    def __init__(self, responses: list[dict[str, object]] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[dict[str, object]] = []

    def _next(self) -> dict[str, object]:
        return self._responses.pop(0) if self._responses else {}

    def get_json(self, url: str, headers: dict[str, str]) -> dict[str, object]:
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        return self._next()

    def post_json(
        self, url: str, body: dict[str, object], headers: dict[str, str]
    ) -> dict[str, object]:
        self.calls.append({"method": "POST", "url": url, "body": body, "headers": headers})
        return self._next()


__all__ = ["HttpClient", "RecordingHttpClient"]
