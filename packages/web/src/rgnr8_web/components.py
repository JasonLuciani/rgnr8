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

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from html import escape
from math import ceil
from urllib.parse import urlencode

from .provenance_labels import Provenance
from .provenance_labels import badge as prov_badge

__all__ = [
    "Action",
    "BarSegment",
    "Cell",
    "Column",
    "FilterChip",
    "Page",
    "SortState",
    "TimelineEntry",
    "alloc_editor",
    "card",
    "data_table",
    "drawer",
    "drawer_trigger",
    "empty_row",
    "empty_state",
    "form_shell",
    "form_section",
    "metric",
    "money_bar",
    "page_from_query",
    "page_header",
    "queue",
    "sort_from_query",
    "split_button",
    "status",
    "status_chip",
    "timeline",
    "visible_columns",
]


# --- small shared pieces ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """A button on a header, a queue row or an empty state.

    ``href`` makes it a link, ``on_click`` makes it a button whose click calls
    the named page function through the shell's dispatcher (see `csp.py`). Exactly one is meaningful;
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
            return (f'<button type="button" class="{cls}" '
                    f'data-action="{escape(self.on_click)}">{text}</button>')
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

    An empty `label` draws the figure alone, for a card whose title already
    names it. There is no provenance label here on purpose: the trust label
    marks the whole card, in its header (see `card`), never one figure inside
    it, so two figures in one card can't wear two different stories.
    """
    cls = "metric hero" if hero else "metric"
    vcls = f' class="v {escape(tone)}"' if tone else ' class="v"'
    tail = f'<div class="note">{escape(note)}</div>' if note else ""
    name = f'<div class="k">{escape(label)}</div>' if label else ""
    return (
        f'<div class="{cls}">{name}'
        f"<div{vcls}>{escape(value)}</div>"
        f"{tail}</div>"
    )


# --- card -------------------------------------------------------------------


def card(title: str, body: str, *, actions: str = "",
         provenance: "Provenance | None" = None) -> str:
    """A titled panel, laid out the same way everywhere.

        [title]                         [provenance]
        ...the card's content...
        [actions]

    The header carries only the title and, where the card shows figures, the
    one provenance label that covers all of them. The card's actions come
    last, bottom-left, after what they act on: some cards had them in the
    header, top-right, and others at the foot, so the button for the same kind
    of job sat in a different corner from one screen to the next.

    `body` and `actions` are markup the caller built (and escaped); `title` is
    escaped here.
    """
    badge = prov_badge(provenance) if provenance is not None else ""
    name = f"<h2>{escape(title)}</h2>" if title else ""
    head = f'<div class="card-head">{name}{badge}</div>' if title or badge else ""
    acts = f'<div class="card-acts">{actions}</div>' if actions.strip() else ""
    return f'<section class="card">{head}{body}{acts}</section>'


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


# === design system: tables, money bars, drawers, split buttons, status, forms ===
#
# Everything below is driven by query-string state, so a sorted, filtered, paged
# or re-columned table is a plain link that can be bookmarked and shared, and
# works with JavaScript off. Query keys used by `data_table` and `money_bar`:
#
#   sort=<column key>   dir=asc|desc        sort order (toggled by the header)
#   c_<column key>=1    cols=1              the column gear (cols=1 marks a submitted gear)
#   page=<n>                                paging
#   <any filter key>=<value>                money bar segments and filter chips
#
# Callers own the business filtering and the query parsing for their own keys;
# this module only builds links and renders.


# --- status ------------------------------------------------------------------

_TONE_GLYPH: dict[str, str] = {
    "ok": "✓",       # ✓
    "warn": "◆",     # ◆
    "bad": "▲",      # ▲
    "info": "●",     # ●
    "neutral": "–",  # –
}


def _tone(tone: str) -> str:
    """An unknown tone is neutral, never a guessed colour (see `status_chip`)."""
    return tone if tone in _TONE_GLYPH else "neutral"


def status(label: str, tone: str = "neutral") -> str:
    """A status as glyph + text. Colour is the second cue, never the only one."""
    t = _tone(tone)
    return (
        f'<span class="stat tone-{t}"><span aria-hidden="true">{_TONE_GLYPH[t]}</span>'
        f" {escape(label)}</span>"
    )


# --- query helpers -----------------------------------------------------------


def _link(base: str, query: Mapping[str, str] | None,
          changes: Mapping[str, str | None] | None = None) -> str:
    """`base` with `query` plus `changes`; a None change removes that key."""
    merged: dict[str, str] = dict(query or {})
    for key, value in (changes or {}).items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    merged = {k: v for k, v in merged.items() if v != ""}
    qs = urlencode(merged)
    return f"{base}?{qs}" if qs else base


def _attr(value: str) -> str:
    return escape(value, quote=True)


@dataclass(frozen=True, slots=True)
class SortState:
    key: str
    desc: bool = False


def sort_from_query(query: Mapping[str, str], columns: Sequence["Column"]) -> SortState | None:
    """The sort the query asks for, if it names a sortable column."""
    key = query.get("sort", "")
    if key not in {c.key for c in columns if c.sortable}:
        return None
    return SortState(key, desc=query.get("dir") == "desc")


@dataclass(frozen=True, slots=True)
class Page:
    number: int = 1
    size: int = 50


def page_from_query(query: Mapping[str, str], size: int = 50) -> Page:
    """`?page=n`, read leniently: a garbage page is page one."""
    try:
        number = int(query.get("page", "1"))
    except ValueError:
        number = 1
    return Page(max(1, number), size)


# --- data table --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Column:
    """One column of a `data_table`.

    `money` implies `numeric`: the figure is right-aligned and a negative one
    takes the shared `.neg` colour. `hidden` columns are off until the column
    gear turns them on. `sortable` makes the header a sort link.
    """

    key: str
    label: str
    numeric: bool = False
    money: bool = False
    sortable: bool = False
    hidden: bool = False

    @property
    def right(self) -> bool:
        return self.numeric or self.money


@dataclass(frozen=True, slots=True)
class Cell:
    """One table cell.

    `text` is shown escaped. `html`, when given, is markup the caller built and
    escaped, and it replaces `text`. `sort` is the value the column sorts by;
    it defaults to `text`, so a money cell should carry the minor-unit integer.
    """

    text: str = ""
    html: str = ""
    sort: object = None
    neg: bool = False


@dataclass(frozen=True, slots=True)
class FilterChip:
    """An active filter: the query key it lives under and what to call it."""

    key: str
    label: str


def visible_columns(columns: Sequence[Column], query: Mapping[str, str]) -> list[Column]:
    """The columns the query shows. Without a gear submission, the non-hidden ones.

    A column with no label (a row's actions, say) is not something the gear can
    name, so it is always shown and never dropped by a submission.
    """
    if "cols" in query:
        chosen = [c for c in columns if not c.label or query.get(f"c_{c.key}") == "1"]
        if any(c.label for c in chosen):
            return chosen
    return [c for c in columns if not c.hidden]


def _as_cell(value: object) -> Cell:
    if isinstance(value, Cell):
        return value
    return Cell(text="" if value is None else str(value))


def _sort_value(cell: Cell) -> tuple[int, float, str]:
    v = cell.sort if cell.sort is not None else cell.text
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (0, float(v), "")
    return (1, 0.0, str(v).casefold())


def _sorted(rows: Sequence[Mapping[str, object]], sort: SortState | None) -> list[Mapping[str, object]]:
    if sort is None:
        return list(rows)

    def key(row: Mapping[str, object]) -> tuple[int, float, str]:
        raw = row.get(sort.key)
        return _sort_value(_as_cell(raw)) if raw is not None else (2, 0.0, "")

    return sorted(rows, key=key, reverse=sort.desc)


def _header(col: Column, sort: SortState | None, base: str,
            query: Mapping[str, str]) -> str:
    cls = ' class="num"' if col.right else ""
    if not col.sortable:
        return f'<th scope="col"{cls}>{escape(col.label)}</th>'
    if sort is not None and sort.key == col.key:
        aria = "descending" if sort.desc else "ascending"
        glyph = "▼" if sort.desc else "▲"  # ▼ / ▲
        nxt = "asc" if sort.desc else "desc"
    else:
        aria = "none"
        glyph = "↕"  # ↕
        nxt = "asc"
    href = _link(base, query, {"sort": col.key, "dir": nxt, "page": None})
    return (
        f'<th scope="col"{cls} aria-sort="{aria}">'
        f'<a class="dt-sort" href="{_attr(href)}">{escape(col.label)} '
        f'<span aria-hidden="true">{glyph}</span></a></th>'
    )


def _chips(filters: Sequence[FilterChip], base: str, query: Mapping[str, str]) -> str:
    if not filters:
        return ""
    items = []
    for f in filters:
        remove = _link(base, query, {f.key: None, "page": None})
        items.append(
            f'<span class="dt-chip">{escape(f.label)} '
            f'<a href="{_attr(remove)}" aria-label="Remove filter {_attr(f.label)}">×</a></span>'
        )
    clear_changes: dict[str, str | None] = {f.key: None for f in filters}
    clear_changes["page"] = None
    clear = _link(base, query, clear_changes)
    return (
        '<div class="dt-chips" role="group" aria-label="Active filters">'
        + "".join(items)
        + f'<a class="dt-clear" href="{_attr(clear)}">Clear all</a></div>'
    )


def _gear(columns: Sequence[Column], shown: Sequence[Column], base: str,
          query: Mapping[str, str]) -> str:
    """The column menu: a disclosure of checkboxes that submits a GET form.

    The form carries the rest of the query (sort, filters) back as hidden
    fields and drops paging, since a different column set can change the pages.
    """
    keep = {k: v for k, v in query.items()
            if k not in ("page", "cols") and not k.startswith("c_")}
    hidden = "".join(
        f'<input type="hidden" name="{_attr(k)}" value="{_attr(v)}">' for k, v in keep.items()
    )
    on = {c.key for c in shown}
    boxes = "".join(
        f'<label class="rg-check"><input type="checkbox" name="c_{_attr(c.key)}" value="1"'
        f'{" checked" if c.key in on else ""}> {escape(c.label)}</label>'
        for c in columns if c.label
    )
    return (
        '<details class="dt-gear"><summary>Columns</summary>'
        f'<form method="get" action="{_attr(base)}" class="dt-gear-form">{hidden}'
        '<input type="hidden" name="cols" value="1">'
        f'<div class="dt-gear-list">{boxes}</div>'
        '<button class="btn ghost" type="submit">Show these columns</button></form></details>'
    )


def data_table(
    columns: Sequence[Column],
    rows: Sequence[Mapping[str, object]],
    *,
    sort: SortState | None = None,
    filters: Sequence[FilterChip] = (),
    page: Page | None = None,
    empty: str = "Nothing to show yet.",
    empty_how: str = "",
    totals: Mapping[str, object] | None = None,
    base: str = "",
    query: Mapping[str, str] | None = None,
    gear: bool = True,
) -> str:
    """A table with server-side sort, paging, filter chips and a column gear.

    `rows` maps column key to a `Cell` (or plain text). Sorting happens here, on
    `Cell.sort`, before paging, so the caller passes rows in any order. `base`
    is the path the links point at and `query` is the request's query string;
    every link keeps the rest of that query, so sort, filters and columns
    survive a click. `totals`, when given, is a footer keyed by column; the
    first column is labelled "Total" unless it has its own entry.
    """
    q: dict[str, str] = dict(query or {})
    shown = visible_columns(columns, q)
    ordered = _sorted(rows, sort)
    size = page.size if page is not None else max(1, len(ordered))

    pages = max(1, ceil(len(ordered) / size))
    number = min(max(1, page.number if page is not None else 1), pages)
    start = (number - 1) * size
    window = ordered[start:start + size]

    head = "".join(_header(c, sort, base, q) for c in shown)

    body_rows: list[str] = []
    for row in window:
        tds = []
        for col in shown:
            cell = _as_cell(row.get(col.key))
            text = cell.html or escape(cell.text)
            classes = []
            if col.right:
                classes.append("num")
            if cell.neg or (col.money and cell.text.startswith("-")):
                classes.append("neg")
            cls = f' class="{" ".join(classes)}"' if classes else ""
            tds.append(f"<td{cls}>{text}</td>")
        body_rows.append("<tr>" + "".join(tds) + "</tr>")

    if not window:
        actions: tuple[Action, ...] = ()
        if filters:
            clear_changes: dict[str, str | None] = {f.key: None for f in filters}
            clear_changes["page"] = None
            actions = (Action("Clear filters", href=_link(base, q, clear_changes)),)
        how = empty_how or ("Clear the filter to see every row." if filters else "")
        body_rows.append(
            f'<tr><td class="empty" colspan="{max(1, len(shown))}">'
            f"{empty_state(empty, how=how, actions=actions)}</td></tr>"
        )

    foot = ""
    if totals is not None:
        cells = []
        for i, col in enumerate(shown):
            if col.key in totals:
                tc = _as_cell(totals[col.key])
                text = tc.html or escape(tc.text)
            elif i == 0:
                text = "Total"
            else:
                text = ""
            cls = ' class="num"' if col.right else ""
            cells.append(f"<td{cls}>{text}</td>")
        foot = f'<tfoot><tr class="dt-total">{"".join(cells)}</tr></tfoot>'

    pager = ""
    if pages > 1:
        last_shown = min(start + size, len(ordered))
        prev_page = str(number - 1) if number - 1 > 1 else None
        prev = (f'<a href="{_attr(_link(base, q, {"page": prev_page}))}">‹ Previous</a>'
                if number > 1 else '<span class="dt-dis" aria-disabled="true">‹ Previous</span>')
        nxt = (f'<a href="{_attr(_link(base, q, {"page": str(number + 1)}))}">Next ›</a>'
               if number < pages else '<span class="dt-dis" aria-disabled="true">Next ›</span>')
        pager = (
            '<nav class="dt-pager" aria-label="Pages">'
            f"<span>Showing {start + 1}–{last_shown} of {len(ordered)}</span>{prev}"
            f"<span>Page {number} of {pages}</span>{nxt}</nav>"
        )

    toolbar_bits = _chips(filters, base, q)
    if gear and any(c.label for c in columns):
        toolbar_bits += _gear(columns, shown, base, q)
    toolbar = f'<div class="dt-bar">{toolbar_bits}</div>' if toolbar_bits else ""

    return (
        toolbar
        + '<div class="table-scroll"><table><thead><tr>' + head + "</tr></thead>"
        + "<tbody>" + "".join(body_rows) + "</tbody>" + foot + "</table></div>"
        + pager
    )


# --- money bar ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BarSegment:
    """One bucket of a money bar: what it is, how much, how many, and its tone."""

    key: str
    label: str
    amount: str
    count: int
    tone: str = "neutral"


def money_bar(
    segments: Sequence[BarSegment],
    *,
    base: str = "",
    query: Mapping[str, str] | None = None,
    param: str = "status",
    unit: tuple[str, str] = ("item", "items"),
    label: str = "Filter by status",
) -> str:
    """Buckets that each filter the table below them.

    Each segment links to the same page with `param` set to its key, and
    clicking the active segment clears it. The active one carries
    `aria-current` and a visible "Showing" marker, not only a colour.
    """
    q: dict[str, str] = dict(query or {})
    active = q.get(param, "")
    cells = []
    for seg in segments:
        on = seg.key == active
        href = _link(base, q, {param: None if on else seg.key, "page": None})
        t = _tone(seg.tone)
        noun = unit[0] if seg.count == 1 else unit[1]
        neg = " neg" if seg.amount.startswith("-") else ""
        aria = ' aria-current="true"' if on else ""
        marker = '<span class="mb-on">Showing</span>' if on else ""
        cells.append(
            f'<a class="mb-seg tone-{t}{" on" if on else ""}" href="{_attr(href)}"{aria}>'
            f'<span class="mb-label">{escape(seg.label)}</span>'
            f'<span class="mb-amt{neg}">{escape(seg.amount)}</span>'
            f'<span class="mb-count">{seg.count} {noun}</span>{marker}</a>'
        )
    return f'<nav class="money-bar" aria-label="{_attr(label)}">' + "".join(cells) + "</nav>"


# --- drawer ------------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z][\w-]*$")


def drawer(id: str, title: str, body: str) -> str:
    """A side panel that is hidden until its link is followed.

    It uses the `:target` pattern: `drawer_trigger(id, ...)` links to `#id`, the
    browser targets the panel, and the Close link goes back to `#`. No script is
    involved, so it works under the CSP as it stands and from the keyboard.
    """
    if not _ID_RE.match(id):
        raise ValueError(f"drawer id must be a plain token, got {id!r}")
    did = _attr(id)
    return (
        f'<aside class="drawer" id="{did}" aria-labelledby="{did}-title" tabindex="-1">'
        '<div class="drawer-panel"><div class="drawer-head">'
        f'<h2 id="{did}-title">{escape(title)}</h2>'
        '<a class="btn ghost" href="#">Close</a></div>'
        f'<div class="drawer-body">{body}</div></div></aside>'
    )


def drawer_trigger(id: str, label: str, *, kind: str = "ghost") -> str:
    """The link that opens `drawer(id, ...)`."""
    if not _ID_RE.match(id):
        raise ValueError(f"drawer id must be a plain token, got {id!r}")
    return Action(label, href=f"#{id}", kind=kind).html()


# --- split button ------------------------------------------------------------


def split_button(primary: Action, more: Sequence[Action] = ()) -> str:
    """The main action, with a disclosure of related ones beside it."""
    if not more:
        return primary.html()
    items = "".join(f'<div class="split-item">{a.html()}</div>' for a in more)
    return (
        '<div class="split">' + primary.html()
        + '<details class="split-more">'
        + f'<summary class="btn ghost" aria-label="More actions for {_attr(primary.label)}">▾</summary>'
        + f'<div class="split-menu">{items}</div></details></div>'
    )


# --- form shell --------------------------------------------------------------


def form_shell(
    title: str,
    body: str,
    *,
    primary: Action,
    secondary: Sequence[Action] = (),
    aside: str = "",
    sub: str = "",
    action: str = "",
) -> str:
    """The one layout for a transaction form: header, body, a sticky footer.

    The footer holds `secondary` (for example Save and new, Cancel) and then
    `primary` (Save) on the right. `body` holds the field grids and must not
    contain its own submit button. `aside` is optional side notes, shown
    under the body on a narrow screen.
    """
    side = f'<aside class="form-aside">{aside}</aside>' if aside else ""
    foot = "".join(a.html() for a in secondary) + primary.html()
    return (
        f'<form class="form-shell" method="post" action="{_attr(action)}">'
        + page_header(title, sub=sub)
        + f'<div class="form-grid"><div class="form-body">{body}</div>{side}</div>'
        + f'<div class="form-foot"><div class="acts">{foot}</div></div>'
        + "</form>"
    )
