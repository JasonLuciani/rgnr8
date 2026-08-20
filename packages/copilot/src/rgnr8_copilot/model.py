"""Core types for Ask RGNR8."""

from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import LedgerReader


@dataclass(frozen=True)
class Citation:
    """What backs a figure in an answer — a report, a register range, or an entry."""

    label: str
    source_type: str  # "report" | "register" | "entry"
    ref: str          # a link/path or an entry id


@dataclass(frozen=True)
class ToolResult:
    """A tool's structured output. Money is integer minor units. `citations` back
    the figures; the orchestrator derives the set of authoritative figures from
    the `*_minor` keys in `data` for the numeric-integrity backstop."""

    data: dict[str, object]
    citations: tuple[Citation, ...] = ()
    summary_hint: str = ""


@dataclass(frozen=True)
class AskContext:
    """Everything a tool needs, scoped to one tenant and one caller.

    `ledger` is already tenant-scoped (no tool takes a tenant argument). `permissions`
    is the caller's RBAC scope set — the registry hides tools the caller can't use.
    `hints` carries cheap facts from the FinancialContextPack (e.g. cash accounts)."""

    tenant: str
    permissions: frozenset[str]
    ledger: LedgerReader
    hints: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class TraceStep:
    tool: str
    args: dict[str, object]
    ok: bool
    summary: str


@dataclass(frozen=True)
class AskAnswer:
    text: str
    citations: tuple[Citation, ...]
    trace: tuple[TraceStep, ...]
    verified: bool          # did every money figure in `text` trace to a tool result?
    refused: bool = False   # true when the assistant declined / couldn't answer


__all__ = ["Citation", "ToolResult", "AskContext", "TraceStep", "AskAnswer"]
