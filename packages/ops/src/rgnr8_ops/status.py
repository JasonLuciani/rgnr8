"""Per-client operational status the operator dashboard shows alongside cash.

Cash is only half of "how is this client doing." The other half is whether the
picture can be *trusted*: are the connectors still feeding fresh data (books
current), and has the month-end close actually been done? Both of those live in
the TypeScript half — `@rgnr8/connectors buildHealthReport` and `@rgnr8/close
closeCalendarStatus` — so this module mirrors their headline numbers as small,
injected Python status objects. In production the operator serializes the TS
reports into these when building the fleet view; here they're plain data, which
keeps `build_ops_report` deterministic and lets the dashboard show
*books-current* + *close-progress* next to each client's cash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ConnectorHealth:
    """Mirror of `@rgnr8/connectors` ConnectionHealthReport headline counts."""

    total: int
    needs_attention: int  # auth dead → the owner must reconnect
    stale: int  # healthy-looking but the data is old (or never synced)
    last_sync: str | None = None  # ISO of the most recent sync across connections

    @staticmethod
    def from_dict(d: dict[str, object]) -> "ConnectorHealth":
        """Parse the TS `opsConnectorStatus(...)` JSON fragment."""
        ls = d.get("last_sync")
        return ConnectorHealth(
            total=int(str(d.get("total", 0))),
            needs_attention=int(str(d.get("needs_attention", 0))),
            stale=int(str(d.get("stale", 0))),
            last_sync=str(ls) if ls is not None else None,
        )

    @property
    def healthy(self) -> int:
        return max(0, self.total - self.needs_attention - self.stale)

    @property
    def books_current(self) -> bool:
        """True only when every connection is connected and fresh."""
        return self.total > 0 and self.needs_attention == 0 and self.stale == 0

    def summary(self) -> str:
        if self.total == 0:
            return "no connectors"
        if self.books_current:
            return f"{self.total} ok"
        parts: list[str] = []
        if self.needs_attention:
            parts.append(f"{self.needs_attention} reconnect")
        if self.stale:
            parts.append(f"{self.stale} stale")
        return ", ".join(parts) if parts else f"{self.total} ok"


@dataclass(frozen=True, slots=True)
class CloseProgress:
    """Mirror of `@rgnr8/close` CloseCalendarStatus headline numbers."""

    period: str  # e.g. "2026-08"
    total: int
    done: int
    overdue: int
    blocked: int
    next_task: str | None = None  # label of the next actionable task
    next_due: str | None = None  # ISO date of that task

    @staticmethod
    def from_dict(d: dict[str, object]) -> "CloseProgress":
        """Parse the TS `opsCloseStatus(...)` JSON fragment."""
        nt, nd = d.get("next_task"), d.get("next_due")
        return CloseProgress(
            period=str(d.get("period", "")),
            total=int(str(d.get("total", 0))),
            done=int(str(d.get("done", 0))),
            overdue=int(str(d.get("overdue", 0))),
            blocked=int(str(d.get("blocked", 0))),
            next_task=str(nt) if nt is not None else None,
            next_due=str(nd) if nd is not None else None,
        )

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.done == self.total

    def summary(self) -> str:
        if self.total == 0:
            return "no close scheduled"
        if self.complete:
            return f"{self.period} closed"
        tail = f" · {self.overdue} overdue" if self.overdue else ""
        return f"{self.period} {self.done}/{self.total}{tail}"


@dataclass(frozen=True, slots=True)
class TenantOpsStatus:
    """The operational side-panel for one client: books freshness + close state."""

    connectors: ConnectorHealth | None = None
    close: CloseProgress | None = None

    @staticmethod
    def from_json(payload: dict[str, object] | str) -> "TenantOpsStatus":
        """Assemble from the cross-language ``ops-status/1`` shape
        ``{"connectors": {...}|null, "close": {...}|null}`` — the JSON emitted by
        the TS `opsConnectorStatus` / `opsCloseStatus` serializers. Either side
        may be absent/null (renders as ``—`` on the dashboard)."""
        d = json.loads(payload) if isinstance(payload, str) else payload
        c = d.get("connectors")
        k = d.get("close")
        return TenantOpsStatus(
            connectors=ConnectorHealth.from_dict(dict(c)) if isinstance(c, dict) else None,
            close=CloseProgress.from_dict(dict(k)) if isinstance(k, dict) else None,
        )

    def to_dict(self) -> dict[str, object]:
        """The ``ops-status/1`` shape (round-trips with `from_json`)."""
        conn: dict[str, object] | None = None
        if self.connectors is not None:
            c = self.connectors
            conn = {"total": c.total, "needs_attention": c.needs_attention,
                    "stale": c.stale, "last_sync": c.last_sync}
        close: dict[str, object] | None = None
        if self.close is not None:
            k = self.close
            close = {"period": k.period, "total": k.total, "done": k.done,
                     "overdue": k.overdue, "blocked": k.blocked,
                     "next_task": k.next_task, "next_due": k.next_due}
        return {"connectors": conn, "close": close}

    def to_json(self) -> str:
        return json.dumps(self.to_dict())
