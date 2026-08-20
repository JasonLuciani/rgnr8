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
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class Conversation:
    """The carried state of a multi-turn thread. `messages` is the CLEAN transcript
    (user/assistant only — tool call/result traffic is turn-local scratch and never
    persisted). `verified_minor` is every dollar figure already tied to the books in
    an earlier turn, so a follow-up may legitimately reference "still $50,000"
    without re-running the tool, while a *new* figure the books don't back is still
    refused. Frozen and self-contained — safe to stash per (tenant, caller)."""

    messages: tuple[Msg, ...] = ()
    verified_minor: frozenset[int] = field(default_factory=frozenset)

    @staticmethod
    def empty() -> "Conversation":
        return Conversation()


class AskOrchestrator:
    def __init__(
        self, llm: LLMProvider, registry: ToolRegistry, *, max_steps: int = 6, max_turns: int = 8
    ) -> None:
        self._llm = llm
        self._registry = registry
        self._max_steps = max_steps
        # cap the carried transcript at this many turns (each = one user+assistant
        # pair) so a long-running thread can't grow the prompt without bound.
        self._max_turns = max_turns

    def answer(self, ctx: AskContext, question: str) -> AskAnswer:
        """Single-turn: ask one question, get one answer. Unchanged public API."""
        return self.converse(ctx, Conversation.empty(), question)[0]

    def converse(
        self, ctx: AskContext, conversation: Conversation, question: str
    ) -> tuple[AskAnswer, Conversation]:
        """Answer `question` in the context of `conversation`, returning the answer
        and the updated conversation to carry into the next turn."""
        catalog = self._registry.catalog(ctx.permissions)
        # Seed the working messages with the prior CLEAN transcript, then this turn's
        # question. Tool traffic below is appended to `messages` for this turn only.
        messages: list[Msg] = [*conversation.messages, Msg("user", question)]
        # A dollar figure is "allowed" in the answer if a tool returned it this turn,
        # the user stated it, or it was verified in an earlier turn of this thread.
        allowed: set[int] = set(conversation.verified_minor) | extract_money_minor(question)
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
                    ans = AskAnswer(_FALLBACK, tuple(citations), tuple(trace),
                                    verified=False, refused=True)
                    return ans, self._commit(conversation, question, _FALLBACK, allowed)
                ans = AskAnswer(final, tuple(citations), tuple(trace), verified=True)
                return ans, self._commit(conversation, question, final, allowed)

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

        ans = AskAnswer(_FALLBACK, tuple(citations), tuple(trace), verified=False, refused=True)
        return ans, self._commit(conversation, question, _FALLBACK, allowed)

    def _commit(
        self, conversation: Conversation, question: str, answer_text: str, allowed: set[int]
    ) -> Conversation:
        """Fold this turn into the carried state: append the clean Q/A pair, trim to
        the last `max_turns`, and keep the accumulated set of book-tied figures."""
        transcript = (*conversation.messages, Msg("user", question), Msg("assistant", answer_text))
        if self._max_turns > 0:
            transcript = transcript[-2 * self._max_turns:]
        return Conversation(messages=transcript, verified_minor=frozenset(allowed))


__all__ = ["AskOrchestrator", "Conversation", "extract_money_minor", "SYSTEM_PROMPT"]
