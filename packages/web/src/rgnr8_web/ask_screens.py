"""Ask RGNR8 — the chat surface.

Server-rendered and multi-turn: the owner asks, the copilot answers with numbers
computed from the books, and the thread accumulates so follow-ups ("and last
quarter?") carry context. Every answer carries its citations and a trust footer.
The differentiator, made visible: every answer says "computed from your books"
and lists what it read — because it can.
"""

from __future__ import annotations

from collections.abc import Sequence

from rgnr8_copilot import AskAnswer

from .books_screens import _banner, _card, _esc

_CHIPS = [
    "What's my cash and runway?",
    "Am I healthy? (margins, DSCR)",
    "Who owes me money?",
    "What do I owe on my loans?",
    "Which job made money?",
    "What's my inventory worth — does it tie?",
]

# One exchange in the thread: the owner's question and the copilot's answer.
AskTurn = tuple[str, AskAnswer]


def render_ask_unavailable(detail: str = "") -> str:
    extra = f'<p class="muted">{_esc(detail)}</p>' if detail else ""
    return _card(
        "Ask RGNR8",
        _banner("warn", "The assistant isn't configured yet.")
        + '<p class="muted">Ask RGNR8 answers plain-English questions from your own '
        "books — every figure computed by the ledger and cited to its entries. It "
        "needs an AI provider connected to go live.</p>" + extra,
    )


def render_ask(
    tenant: str,
    *,
    turns: Sequence[AskTurn] = (),
    question: str = "",
    error: str = "",
) -> str:
    base = f"/t/{_esc(tenant)}/ask"

    chips = "".join(
        f'<a class="chip" href="{base}?q={_esc(_url(c))}">{_esc(c)}</a>' for c in _CHIPS
    )
    placeholder = (
        "e.g. and how does that compare to last quarter?" if turns
        else "e.g. which job made money last quarter?"
    )
    clear = (
        f'<form method="post" action="{base}/clear" style="margin:0">'
        '<button type="submit" class="btn ghost" style="font-size:12px;padding:6px 12px">Clear conversation</button>'
        "</form>"
        if turns else ""
    )
    form = (
        f'<form method="post" action="{base}" class="grid">'
        '<label style="grid-column:1/-1">Ask about your money'
        f'<textarea name="q" rows="2" placeholder="{placeholder}">{_esc(question)}</textarea></label>'
        '<div style="grid-column:1/-1;display:flex;gap:8px;align-items:center;flex-wrap:wrap">'
        '<button type="submit">Ask</button>'
        f'<span class="muted" style="font-size:12px">or try:</span>{chips}'
        f'<span style="flex:1"></span>{clear}</div>'
        "</form>"
    )
    sections = [_card("Ask RGNR8", form)]
    if error:
        sections.append(_banner("warn", error))
    for q, answer in turns:
        sections.append(_render_answer(q, answer))
    return "".join(sections)


def _render_answer(question: str, answer: AskAnswer) -> str:
    q = f'<p class="muted" style="margin:0 0 8px">You asked: <em>{_esc(question)}</em></p>' if question else ""
    body_text = _esc(answer.text).replace("\n", "<br>")
    body = f'<div style="font:15px/1.5 var(--rg-sans)">{body_text}</div>'

    cites = ""
    if answer.citations:
        items = "".join(
            f'<li>{_esc(c.label)} <span class="muted" style="font-size:12px">({_esc(c.source_type)})</span></li>'
            for c in answer.citations
        )
        cites = f'<div style="margin-top:10px"><div class="muted" style="font-size:12px">Sources</div><ul>{items}</ul></div>'

    work = _render_trace(answer)

    n_tools = sum(1 for s in answer.trace if s.ok)
    if answer.refused:
        footer_txt = "Couldn't verify against your books — no figure shown."
    else:
        footer_txt = (
            f"Computed from your books · {n_tools} "
            f"{'source' if n_tools == 1 else 'sources'} read · every figure ties out"
        )
    footer = f'<div class="muted" style="margin-top:12px;font-size:12px;border-top:1px solid var(--rg-line);padding-top:8px">{_esc(footer_txt)}</div>'

    return _card("Answer", q + body + cites + work + footer)


def _render_trace(answer: AskAnswer) -> str:
    """A collapsible "how I worked this out" — the exact tools the copilot ran, in
    order, each marked ok/failed with its one-line summary. This is the audit trail
    that an overlay on someone else's ledger can't honestly show."""
    if not answer.trace:
        return ""
    rows = "".join(
        f'<li>{"✓" if s.ok else "✕"} <code>{_esc(s.tool)}</code>'
        f'{(" — " + _esc(s.summary)) if s.summary else ""}</li>'
        for s in answer.trace
    )
    return (
        '<details style="margin-top:10px"><summary class="muted" style="font-size:12px;cursor:pointer">'
        "How I worked this out</summary>"
        f'<ul style="font-size:13px;margin:8px 0 0">{rows}</ul></details>'
    )


def _url(s: str) -> str:
    return s.replace(" ", "+").replace("?", "%3F").replace("&", "%26")


__all__ = ["render_ask", "render_ask_unavailable", "AskTurn"]
