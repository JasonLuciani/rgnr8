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
from .model import AskAnswer, AskContext, Citation, FigureRef, TraceStep
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


def _system_for(ctx: AskContext) -> str:
    """The system prompt, grounded with cheap facts from `ctx.hints` (today's date,
    the business name) so the model can turn "last quarter" into explicit from/to
    dates when it calls a tool. This never lets the model compute money — it only
    helps it choose tool arguments. Deterministic: no wall clock, hints are injected."""
    facts: list[str] = []
    today = ctx.hints.get("today")
    if isinstance(today, str) and today:
        facts.append(f"today is {today}")
    business = ctx.hints.get("business")
    if isinstance(business, str) and business:
        facts.append(f"the business is {business}")
    if not facts:
        return SYSTEM_PROMPT
    return (
        SYSTEM_PROMPT
        + " Context: " + "; ".join(facts) + ". When a question implies a period "
        '("last quarter", "this year", "year to date", "last month"), translate it to '
        "explicit from/to dates relative to today when you call a tool."
    )


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


# How correctable a source is — a posted entry or an account register can be
# opened and corrected on the spot; a report is where the figure was read from.
_SOURCE_RANK = {"entry": 0, "register": 1, "report": 2}


def _ordered_money(text: str) -> list[tuple[int, str]]:
    """Every `$`-amount in `text` as (minor, display), in order of first
    appearance, de-duplicated by value — so the working record lists each figure
    once, in the order the reader meets it."""
    seen: set[int] = set()
    out: list[tuple[int, str]] = []
    for m in _MONEY.finditer(text):
        raw = m.group(1).replace(",", "")
        whole, _dot, frac = raw.partition(".")
        frac = (frac + "00")[:2]
        try:
            minor = int(whole) * 100 + int(frac)
        except ValueError:
            continue
        if minor in seen:
            continue
        seen.add(minor)
        out.append((minor, m.group(0).strip()))
    return out


def _source_for(minor: int, figure_sources: dict[int, tuple[Citation, ...]]) -> tuple[Citation, ...]:
    """The citations backing `minor` (±1 tolerance), most-correctable first."""
    cites: list[Citation] = []
    for value, cs in figure_sources.items():
        if abs(value - minor) <= 1:
            cites.extend(cs)
    # de-dup by (ref, source_type), then rank so an entry/register leads a report
    uniq: dict[tuple[str, str], Citation] = {}
    for c in cites:
        uniq.setdefault((c.ref, c.source_type), c)
    return tuple(sorted(uniq.values(), key=lambda c: _SOURCE_RANK.get(c.source_type, 9)))


def _working_record(
    text: str, figure_sources: dict[int, tuple[Citation, ...]], stated: set[int]
) -> tuple[FigureRef, ...]:
    """Tie every dollar figure in `text` to its source(s) and a correction target."""
    refs: list[FigureRef] = []
    for minor, display in _ordered_money(text):
        sources = _source_for(minor, figure_sources)
        best = sources[0] if sources else None
        is_stated = not sources and any(abs(minor - s) <= 1 for s in stated)
        refs.append(FigureRef(
            minor=minor,
            display=display,
            sources=sources,
            correct_href=best.ref if best else "",
            correct_kind=best.source_type if best else "",
            stated=is_stated,
        ))
    return tuple(refs)


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
    # Per-figure provenance carried across turns, so a follow-up that references a
    # figure verified earlier ("still $50,000") keeps its source in the working
    # record. Stored as a hashable tuple of (minor, citation-tuple).
    figure_sources: tuple[tuple[int, tuple[Citation, ...]], ...] = ()

    @staticmethod
    def empty() -> "Conversation":
        return Conversation()

    def sources_map(self) -> dict[int, tuple[Citation, ...]]:
        return {minor: cites for minor, cites in self.figure_sources}


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
        system = _system_for(ctx)
        # Seed the working messages with the prior CLEAN transcript, then this turn's
        # question. Tool traffic below is appended to `messages` for this turn only.
        messages: list[Msg] = [*conversation.messages, Msg("user", question)]
        # A dollar figure is "allowed" in the answer if a tool returned it this turn,
        # the user stated it, or it was verified in an earlier turn of this thread.
        allowed: set[int] = set(conversation.verified_minor) | extract_money_minor(question)
        # Figures the user stated (this turn or verified earlier) with no tool
        # source — the working record labels these "as stated" rather than faking one.
        stated: set[int] = set(conversation.verified_minor) | extract_money_minor(question)
        citations: list[Citation] = []
        trace: list[TraceStep] = []
        # Per-figure provenance: minor value -> the citations of the tool result(s)
        # that returned it. Seeded from earlier turns so carried figures keep sources.
        figure_sources: dict[int, tuple[Citation, ...]] = conversation.sources_map()
        retried = False

        for _step in range(self._max_steps + 1):
            turn = self._llm.plan(system, messages, catalog)

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
                    return ans, self._commit(conversation, question, _FALLBACK, allowed, figure_sources)
                record = _working_record(final, figure_sources, stated)
                ans = AskAnswer(final, tuple(citations), tuple(trace), verified=True,
                                working_record=record)
                return ans, self._commit(conversation, question, final, allowed, figure_sources)

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
                produced = collect_minor_figures(result.data)
                allowed |= produced
                citations.extend(result.citations)
                if result.citations:
                    for minor in produced:
                        figure_sources[minor] = (*figure_sources.get(minor, ()), *result.citations)
                messages.append(Msg("tool", json.dumps({
                    "tool": call.name, "data": result.data, "summary": result.summary_hint,
                })))
                trace.append(TraceStep(call.name, call.args, True, result.summary_hint))

        ans = AskAnswer(_FALLBACK, tuple(citations), tuple(trace), verified=False, refused=True)
        return ans, self._commit(conversation, question, _FALLBACK, allowed, figure_sources)

    def _commit(
        self, conversation: Conversation, question: str, answer_text: str,
        allowed: set[int], figure_sources: dict[int, tuple[Citation, ...]],
    ) -> Conversation:
        """Fold this turn into the carried state: append the clean Q/A pair, trim to
        the last `max_turns`, and keep the accumulated set of book-tied figures plus
        their per-figure provenance."""
        transcript = (*conversation.messages, Msg("user", question), Msg("assistant", answer_text))
        if self._max_turns > 0:
            transcript = transcript[-2 * self._max_turns:]
        return Conversation(
            messages=transcript,
            verified_minor=frozenset(allowed),
            figure_sources=tuple(sorted(figure_sources.items())),
        )


__all__ = ["AskOrchestrator", "Conversation", "extract_money_minor", "SYSTEM_PROMPT"]
