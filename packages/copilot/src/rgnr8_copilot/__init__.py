"""Ask RGNR8 — the conversational finance layer.

An LLM picks tools; RGNR8's own engines compute every number; every figure is
cited; and a numeric-integrity backstop rejects any answer figure that doesn't
trace to a tool result — so the answers tie out because the product owns the
books. Provider-agnostic (Protocol seam + Fake for tests); read-first, and any
future write action is gated by RBAC + explicit confirmation.
"""

from __future__ import annotations

from .ledger import FakeLedgerReader, LedgerReadError, LedgerReader
from .llm import FakeLLM, HttpLLM, LLMProvider, LLMTurn, Msg, ToolCall
from .model import AskAnswer, AskContext, Citation, ToolResult, TraceStep
from .orchestrator import SYSTEM_PROMPT, AskOrchestrator, extract_money_minor
from .tools import Tool, ToolError, ToolRegistry, collect_minor_figures, default_registry

__all__ = [
    "LedgerReader",
    "FakeLedgerReader",
    "LedgerReadError",
    "LLMProvider",
    "FakeLLM",
    "HttpLLM",
    "LLMTurn",
    "Msg",
    "ToolCall",
    "AskContext",
    "AskAnswer",
    "Citation",
    "ToolResult",
    "TraceStep",
    "Tool",
    "ToolRegistry",
    "ToolError",
    "default_registry",
    "collect_minor_figures",
    "AskOrchestrator",
    "extract_money_minor",
    "SYSTEM_PROMPT",
]
