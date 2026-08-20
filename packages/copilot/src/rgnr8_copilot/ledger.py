"""The read seam every tool calls.

`LedgerReader.read(path, params)` mirrors the ledger service's HTTP GET surface
(`/ratios`, `/statements`, `/debt`, `/accounts/<code>/register`, …), returning the
parsed JSON body as a dict. In production the web app binds an adapter over its
RLS-scoped `LedgerClient` (so tenant isolation is enforced at the database);
tests bind `FakeLedgerReader`, which returns canned bodies keyed by path — so the
whole tool/orchestrator stack runs offline and deterministically.

No tool ever takes a tenant argument: the reader is already scoped to one tenant,
which is what keeps a question from ever reaching another tenant's books.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol


class LedgerReadError(Exception):
    pass


class LedgerReader(Protocol):
    def read(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        """GET a ledger read endpoint (tenant already scoped). `path` starts with
        '/', e.g. '/ratios' or '/accounts/1000/register'. Returns the JSON body."""
        ...


class FakeLedgerReader:
    """Returns canned bodies for exact paths (params ignored unless registered).

    Register with `on('/ratios', {...})`. A path with no registered body raises,
    surfacing a missing fixture rather than silently returning empty."""

    def __init__(self, bodies: dict[str, dict[str, object]] | None = None) -> None:
        self._bodies: dict[str, dict[str, object]] = dict(bodies or {})
        self.calls: list[tuple[str, dict[str, str]]] = []

    def on(self, path: str, body: dict[str, object]) -> FakeLedgerReader:
        self._bodies[path] = body
        return self

    def read(self, path: str, params: Mapping[str, str]) -> dict[str, object]:
        self.calls.append((path, dict(params)))
        if path not in self._bodies:
            raise LedgerReadError(f"no fixture for {path}")
        return self._bodies[path]


__all__ = ["LedgerReader", "FakeLedgerReader", "LedgerReadError"]
