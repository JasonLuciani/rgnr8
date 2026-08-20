"""The Ask RGNR8 orchestrator.

Runs the tool-use loop (plan → run tool → observe → finalize) and enforces the
one rule that makes "answers you can trust" real: **the numeric-integrity
backstop.** Every dollar figure in the final answer must trace to a figure a tool
returned (or a figure the user themselves stated). If it doesn't, the assistant
is asked once to re-answer from tool results only; if it still can't, it declines
rather than ship an unverified number. This is what an overlay on someone else's
ledger structurally cannot promise.
"""

from __future__ import annotations

import json
import re

from .llm import LLMProvider, Msg
from .model import AskAnswer, AskContext, Citation, TraceStep
from .tools import ToolRegistry, ToolError, collect_minor_figures

SYSTEM_PROMPT = (
    "You are Ask RGNR8, a financial assistant for a business. Answer ONLY using the "
    "tools provided; they compute from the business's own books. NEVER compute money "
    "yourself — always call a tool. Every dollar figure in your answer must come from "
    "a tool result. If a question can't be answered by the tools, say so plainly. If a "
    "question is ambiguous (which period? which account?), ask ONE clarifying question. "
    "Amounts from tools are integer minor units (cents); format them as currency. Be brief."
)

_CORRECTION = (
    "One or more dollar amounts in that answer do not match any figure returned by the "
    "tools. Re-answer using ONLY figures from tool results — call another tool if you "
    "need a number you don't have yet."
)

_FALLBACK = (
    "I couldn't verify one or more figures against your books, so I won't guess. "
    "Ask about a specific account, report, or period and I'll pull the exact numbers."
)

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")


def extract_money_minor(text: str) -> set[int]:
    """Every `$`-prefixed amount in `text`, as integer minor units. Only currency
    tokens are checked — counts, years, and percentages are deliberately ignored."""
    out: set[int] = set()
    for m in _MONEY.finditer(text):
        raw = m.group(1).replace(",", "")
        whole, _dot, frac = raw.partition(".")
        frac = (frac + "00")[:2]
        try:
            out.add(int(whole) * 100 + int(frac))
        except ValueError:
            continue
    return out


def _verified(value: int, allowed: set[int]) -> bool:
    # ±1 minor unit tolerance for display rounding
    return any(abs(value - a) <= 1 for a in allowed)


class AskOrchestrator:
    def __init__(
        self, llm: LLMProvider, registry: ToolRegistry, *, max_steps: int = 6
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._max_steps = max_steps

    def answer(self, ctx: AskContext, question: str) -> AskAnswer:
        catalog = self._registry.catalog(ctx.permissions)
        messages: list[Msg] = [Msg("user", question)]
        # A dollar figure is "allowed" in the answer if a tool returned it, or the
        # user stated it in the question (fair to echo their own number back).
        allowed: set[int] = set(extract_money_minor(question))
        citations: list[Citation] = []
        trace: list[TraceStep] = []
        retried = False

        for _step in range(self._max_steps + 1):
            turn = self._llm.plan(SYSTEM_PROMPT, messages, catalog)

            if turn.is_final:
                final = turn.final_text or ""
                unverified = [m for m in extract_money_minor(final) if not _verified(m, allowed)]
                if unverified and not retried:
                    retried = True
                    messages.append(Msg("assistant", final))
                    messages.append(Msg("user", _CORRECTION))
                    continue
                if unverified:
                    return AskAnswer(_FALLBACK, tuple(citations), tuple(trace),
                                     verified=False, refused=True)
                return AskAnswer(final, tuple(citations), tuple(trace), verified=True)

            for call in turn.tool_calls:
                if not self._registry.permitted(call.name, ctx.permissions):
                    msg = f"tool '{call.name}' is not available to you"
                    messages.append(Msg("tool", json.dumps({"tool": call.name, "error": msg})))
                    trace.append(TraceStep(call.name, call.args, False, msg))
                    continue
                tool = self._registry.get(call.name)
                if tool is None:  # unreachable given permitted(), but keeps types honest
                    continue
                try:
                    result = tool.run(ctx, call.args)
                except ToolError as exc:
                    messages.append(Msg("tool", json.dumps({"tool": call.name, "error": str(exc)})))
                    trace.append(TraceStep(call.name, call.args, False, str(exc)))
                    continue
                allowed |= collect_minor_figures(result.data)
                citations.extend(result.citations)
                messages.append(Msg("tool", json.dumps({
                    "tool": call.name, "data": result.data, "summary": result.summary_hint,
                })))
                trace.append(TraceStep(call.name, call.args, True, result.summary_hint))

        return AskAnswer(_FALLBACK, tuple(citations), tuple(trace), verified=False, refused=True)


__all__ = ["AskOrchestrator", "extract_money_minor", "SYSTEM_PROMPT"]
