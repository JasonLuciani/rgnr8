"""The component vocabulary — one function per thing a screen can show.

Every renderer in this package used to hand-write its own markup for the same
handful of shapes, which is how the prelaunch review found the same table padded
``10px 13px`` on one screen and ``11px 13px`` on another, two different greens
for "good", and 471 inline ``style=`` attributes across the web surface. Styles
that drift are not a cosmetic problem in a financial product: if the "at risk"
pill looks different on two screens, the owner has to work out which one to
believe.

The fix is two-sided and only works if both sides hold:

* ``RG_BASE_CSS`` (in ``rgnr8_forecast.brand``) is the only place these styles
  are *defined*. It ships to every surface — web, reports, console, prototype —
  so there is exactly one card in the product.
* This module is the only place they are *emitted*. A call site asks for
  ``empty_row(5, "No invoices yet")`` instead of describing a centred grey cell,
  so the next change happens in one place.

Everything here takes plain data and returns an HTML string with every
caller-supplied value escaped. Nothing reaches out to a request, a store or a
clock: these are pure functions, which is what makes them cheap to test and
impossible to get subtly wrong per screen.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from html import escape

__all__ = [
    "Action",
    "TimelineEntry",
    "alloc_editor",
    "empty_row",
    "empty_state",
    "form_section",
    "metric",
    "page_header",
    "queue",
    "status_chip",
    "timeline",
]


# --- small shared pieces ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """A button on a header, a queue row or an empty state.

    ``href`` makes it a link, ``on_click`` makes it a button carrying an inline
    handler the shell's script already understands. Exactly one is meaningful;
    a bare label with neither renders as a plain disabled-looking button, which
    is the honest rendering of "this action exists but isn't wired here".
    """

    label: str
    href: str = ""
    on_click: str = ""
    kind: str = "ghost"  # "" (primary) | "sage" | "ghost"

    def html(self) -> str:
        cls = "btn" + (f" {self.kind}" if self.kind else "")
        text = escape(self.label)
        if self.href:
            return f'<a class="{cls}" href="{escape(self.href)}">{text}</a>'
        if self.on_click:
            # The handler is authored in this repo, never taken from a request —
            # see the call sites. It is still escaped so a quote can't break out
            # of the attribute and change what the button does.
            return f'<button class="{cls}" onclick="{escape(self.on_click)}">{text}</button>'
        return f'<button class="{cls}" type="submit">{text}</button>'


def _acts(actions: Sequence[Action], cls: str = "acts") -> str:
    if not actions:
        return ""
    return f'<div class="{cls}">' + "".join(a.html() for a in actions) + "</div>"


# --- page header ------------------------------------------------------------


def page_header(
    title: str,
    *,
    sub: str = "",
    actions: Sequence[Action] = (),
    eyebrow: str = "",
) -> str:
    """The top of a screen: what this page is, in one line, and what you can do
    to it.

    `sub` is context, not decoration — it is where the screen says *which*
    business, *which* period, or how stale the figures are. An eyebrow names the
    section the page belongs to, for a screen deep enough that the sidebar
    alone doesn't place it.
    """
    brow = f'<div class="rg-eyebrow">{escape(eyebrow)}</div>' if eyebrow else ""
    line = f'<p class="sub">{escape(sub)}</p>' if sub else ""
    return (
        '<div class="page-head">'
        f'<div class="t">{brow}<h1>{escape(title)}</h1>{line}</div>'
        f"{_acts(actions)}"
        "</div>"
    )


# --- metric -----------------------------------------------------------------


def metric(label: str, value: str, *, note: str = "", hero: bool = False,
           tone: str = "") -> str:
    """One figure with its name.

    `hero` is the single number a screen is about — cash today — and there
    should be at most one per screen. `tone` ('pos'|'risk'|'watch') colours the
    *value* only: a negative trough has to look different from a healthy one,
    which was a named finding (P1-9), but the label around it must not shout.
    """
    cls = "metric hero" if hero else "metric"
    vcls = f' class="v {escape(tone)}"' if tone else ' class="v"'
    tail = f'<div class="note">{escape(note)}</div>' if note else ""
    return (
        f'<div class="{cls}">'
        f'<div class="k">{escape(label)}</div>'
        f"<div{vcls}>{escape(value)}</div>"
        f"{tail}</div>"
    )


# --- status chip ------------------------------------------------------------

# Status level → (modifier class, glyph). The glyph is what makes the chip
# legible without color — printed, or to a color-blind reader — which the brand
# guide requires and which color alone can never satisfy.
_STATUS: dict[str, tuple[str, str]] = {
    "STABLE": ("pos", "✓"),      # ✓
    "GOOD": ("pos", "✓"),
    "WATCH": ("watch", "◆"),     # ◆
    "AT_RISK": ("risk", "▲"),    # ▲
    "RISK": ("risk", "▲"),
}


def status_chip(status: str, *, label: str = "") -> str:
    """A status as a chip: glyph + word, never color alone.

    An unknown status renders neutral rather than guessing, because inventing a
    colour for a state nobody defined is how a screen ends up claiming
    everything is fine.
    """
    key = status.upper().replace(" ", "_").replace("-", "_")
    tone, glyph = _STATUS.get(key, ("neutral", "●"))
    text = label or status.replace("_", " ")
    return (
        f'<span class="chip-status {tone}" role="status" '
        f'aria-label="Status: {escape(text)}">'
        f'<span aria-hidden="true">{glyph}</span> {escape(text)}</span>'
    )


# --- empty states -----------------------------------------------------------


def empty_state(what: str, *, how: str = "", actions: Sequence[Action] = ()) -> str:
    """A panel saying what is missing and what to do about it.

    `what` is the fact ("No invoices yet"); `how` is the next step. A screen
    that renders a blank area instead of this is undefined to a first-time
    owner, which was the review's finding on the cash screen.
    """
    tail = f'<div class="how">{escape(how)}</div>' if how else ""
    return (
        f'<div class="empty"><div class="what">{escape(what)}</div>'
        f"{tail}{_acts(actions)}</div>"
    )


def empty_row(colspan: int, what: str, *, how: str = "") -> str:
    """The same empty state, as a table row spanning every column.

    This replaces `<tr><td colspan=N class="muted" style="text-align:center;
    padding:24px">` — which appeared verbatim in sixteen renderers, each free to
    drift, and each rendering a single grey line where an owner needed to be
    told what to do next.
    """
    tail = f'<div class="how">{escape(how)}</div>' if how else ""
    return (
        f'<tr><td class="empty" colspan="{int(colspan)}">'
        f'<div class="what">{escape(what)}</div>{tail}</td></tr>'
    )


# --- action queue -----------------------------------------------------------


def queue(items: Iterable[tuple[str, str, Sequence[Action]]]) -> str:
    """A list of decisions waiting on someone: (headline, detail, actions).

    Returns "" for an empty iterable rather than an empty `<ul>`, so a caller
    can fall back to `empty_state` without first counting.
    """
    rows = [
        '<li><div class="ask">'
        f'<div class="h">{escape(head)}</div>'
        + (f'<div class="m">{escape(detail)}</div>' if detail else "")
        + "</div>"
        + _acts(tuple(actions))
        + "</li>"
        for head, detail, actions in items
    ]
    if not rows:
        return ""
    return '<ul class="queue">' + "".join(rows) + "</ul>"


# --- form section -----------------------------------------------------------


def form_section(title: str, fields_html: str, *, why: str = "", cols: str = "grid") -> str:
    """A titled group of fields on one of the shared field grids.

    `fields_html` is markup the caller built (inputs vary far too much to wrap
    usefully); what this owns is the heading, the optional one-line rationale,
    and the grid itself — so every form in the app breaks to one column at the
    same width instead of at five different ones. `cols` picks the grid:
    "grid" (as many 220px columns as fit), or "grid2"/"grid3"/"grid4" for a
    fixed count that still collapses on a narrow screen.
    """
    head = f"<h3>{escape(title)}</h3>" if title else ""
    reason = f'<p class="why">{escape(why)}</p>' if why else ""
    grid = cols if cols in ("grid", "grid2", "grid3", "grid4") else "grid"
    return (
        f'<section class="form-sec">{head}{reason}'
        f'<div class="{grid}">{fields_html}</div></section>'
    )


# --- allocation editor ------------------------------------------------------


def alloc_editor(
    rows: Sequence[tuple[str, str, float]],
    *,
    total_label: str = "Allocated",
    expect: float = 100.0,
) -> str:
    """Rows of (label, input_html, percent) plus a total that flags a bad sum.

    Splitting a cost across jobs, books or owners is the one editor where a
    total that doesn't reconcile must be impossible to miss, so the total
    carries `.over` — and the word "of" with the expected figure — whenever the
    shares don't land on `expect`. The comparison is deliberately loose to a
    hundredth of a point: these are percentages typed by a human, not money.
    """
    out = []
    for label, input_html, pct in rows:
        width = max(0.0, min(100.0, float(pct)))
        out.append(
            '<div class="alloc-row">'
            f"<label>{escape(label)}</label>{input_html}"
            f'<div class="track"><span style="width:{width:.4g}%"></span></div>'
            "</div>"
        )
    got = sum(float(p) for _, _, p in rows)
    off = abs(got - float(expect)) > 0.01
    cls = "alloc-total over" if off else "alloc-total"
    right = f"{got:.4g}%" + (f" of {float(expect):.4g}%" if off else "")
    return (
        '<div class="alloc">' + "".join(out) + "</div>"
        f'<div class="{cls}"><span>{escape(total_label)}</span><span>{right}</span></div>'
    )


# --- audit timeline ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    """One thing that happened: when, what, and who did it."""

    when: str
    what: str
    who: str = ""


def timeline(entries: Sequence[TimelineEntry]) -> str:
    """What happened, in the order given — most recent first, by convention.

    Deliberately does no sorting: an audit trail is evidence, and a renderer
    that reorders evidence is a renderer that can hide something. The caller
    passes the order the store returned.
    """
    if not entries:
        return ""
    rows = [
        "<li>"
        f'<div class="when">{escape(e.when)}</div>'
        f'<div class="what">{escape(e.what)}</div>'
        + (f'<div class="who">{escape(e.who)}</div>' if e.who else "")
        + "</li>"
        for e in entries
    ]
    return '<ol class="timeline">' + "".join(rows) + "</ol>"
