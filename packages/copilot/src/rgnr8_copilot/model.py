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
class FigureRef:
    """One dollar figure in an answer, tied back to what backs it — the
    source-traceable "working record" that lets an owner question any number,
    see its source, and correct it on the spot.

    `display` is the figure exactly as it reads in the answer ("$12,500.00").
    `sources` are the citations that produced it (most-correctable first).
    `correct_href`/`correct_kind` point the "correct this" affordance at the most
    specific correctable source — a posted entry or an account register when one
    backs the figure, else the report it was read from. `stated` is True when the
    figure came from the user or an earlier verified turn rather than a tool this
    turn (so the UI can label it "as stated" instead of inventing a source)."""

    minor: int
    display: str
    sources: tuple[Citation, ...] = ()
    correct_href: str = ""
    correct_kind: str = ""   # "entry" | "register" | "report" | ""
    stated: bool = False


@dataclass(frozen=True)
class AskAnswer:
    text: str
    citations: tuple[Citation, ...]
    trace: tuple[TraceStep, ...]
    verified: bool          # did every money figure in `text` trace to a tool result?
    refused: bool = False   # true when the assistant declined / couldn't answer
    # Per-figure provenance for every dollar amount in `text`, in order of
    # appearance — the working record surfaced under the answer.
    working_record: tuple[FigureRef, ...] = ()


__all__ = ["Citation", "ToolResult", "AskContext", "TraceStep", "FigureRef", "AskAnswer"]
