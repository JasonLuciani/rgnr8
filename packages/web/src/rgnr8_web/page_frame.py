"""One page frame for every screen.

Screens grew their own tops: fourteen of twenty-two had no page title, the rest
wrote their own in their own words ("Receivables & collections" under a nav
item called "Collections"), banners and the provenance legend sat above the
title, and sub-pages were run-on " · " links (ten of them on the Ledger).

The shell now frames every screen the same way, whatever its renderer wrote:

    [eyebrow]  Title (= the nav label)              [actions]
               subtitle
    [tabs]
    banners
    ...the screen...
    footer: what the labels mean

A screen deeper than its section (one account's register, one job) keeps its
own title, with the section as the eyebrow above it, so it still says what it
is. This module only rearranges markup our own renderers produced.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape, unescape


@dataclass(frozen=True, slots=True)
class Tab:
    label: str
    path: str  # relative to /t/<tenant>/, may carry a query


# Sub-pages that used to be run-on links, as tabs. The first tab is the
# section's own page.
TABS: dict[str, tuple[Tab, ...]] = {
    "books": (
        Tab("Accounts", "books"),
        Tab("Trial balance", "books/trial-balance"),
        Tab("Statements", "books/statements"),
        Tab("General ledger", "books/gl"),
        Tab("Budget", "books/budget"),
        Tab("Reconcile", "books/reconcile"),
        Tab("Classes", "books/dimensions"),
        Tab("Recurring", "books/recurring"),
        Tab("Split account", "books/split"),
        Tab("Work in progress", "books/wip"),
        Tab("Inventory", "inventory"),
    ),
    "invoices": (Tab("Invoices", "invoices"), Tab("Aging", "receivables/aging")),
    "bills": (Tab("Bills", "bills"), Tab("Aging", "payables/aging"), Tab("1099s", "books/1099")),
    "inbox": (
        Tab("For review", "inbox"),
        Tab("Rules", "inbox/rules"),
        Tab("Already posted", "inbox?status=POSTED"),
        Tab("Excluded", "inbox?status=EXCLUDED"),
    ),
}


def _element_end(html: str, start: int, tag: str) -> int:
    """Index just past the element of `tag` opening at `start`, counting nesting."""
    opener = re.compile(rf"<{tag}\b|</{tag}>", re.I)
    depth = 0
    for m in opener.finditer(html, start):
        depth += -1 if m.group(0).startswith("</") else 1
        if depth == 0:
            return m.end()
    return len(html)


def _cut(html: str, start: int, end: int) -> tuple[str, str]:
    """(html without [start:end], the removed part)."""
    return html[:start] + html[end:], html[start:end]


def _text(fragment: str) -> str:
    """The words in a fragment of our markup, unescaped (they are escaped again
    when written into the header; leaving entities in would show "&amp;")."""
    return unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _current_tab(tabs: tuple[Tab, ...], here: str) -> int:
    """The tab for `here` (path after /t/<tenant>/, with query): an exact match
    first, else the longest tab path that `here` sits under."""
    for i, t in enumerate(tabs):
        if t.path == here:
            return i
    path_only = here.split("?", 1)[0]
    best, best_len = -1, -1
    for i, t in enumerate(tabs):
        p = t.path.split("?", 1)[0]
        if "?" not in t.path and (path_only == p or path_only.startswith(p + "/")) and len(p) > best_len:
            best, best_len = i, len(p)
    return best


def tabs_html(tenant: str, tabs: tuple[Tab, ...], current: int, label: str) -> str:
    links = "".join(
        f'<a href="/t/{escape(tenant)}/{escape(t.path)}"'
        + (' aria-current="page"' if i == current else "")
        + f">{escape(t.label)}</a>"
        for i, t in enumerate(tabs)
    )
    return f'<nav class="rg-tabs" aria-label="{escape(label)} pages">{links}</nav>'


_BANNER = re.compile(r'<div class=["]banner\b')
_LEGEND = re.compile(r'<details class=["]rg-prov-legend["]')


def frame(body: str, *, title: str, tenant: str, section: str, here: str) -> str:
    """Put `body` into the standard frame. `title` is the nav label; `section`
    the nav suffix (for tabs); `here` the path after /t/<tenant>/ with query."""
    eyebrow = sub = acts = own_title = ""
    # Where the screen's own title stood. With no title found, nothing is
    # "above" it: banners stay where they are, inside the cards they belong to.
    head_at = 0

    m = re.search(r'<div class=["]page-head["]', body)
    h1 = re.search(r"<h1\b[^>]*>(.*?)</h1>", body, re.S)
    if m is not None and (h1 is None or m.start() < h1.start()):
        head_at = m.start()
        body, head = _cut(body, m.start(), _element_end(body, m.start(), "div"))
        h = re.search(r"<h1\b[^>]*>(.*?)</h1>", head, re.S)
        own_title = _text(h.group(1)) if h else ""
        s = re.search(r'<p class=["]sub["]>(.*?)</p>', head, re.S)
        sub = s.group(1) if s else ""
        e = re.search(r'<div class=["]rg-eyebrow["]>(.*?)</div>', head, re.S)
        eyebrow = _text(e.group(1)) if e else ""
        a = re.search(r'<div class=["]acts["]>', head)
        acts = head[a.end():_element_end(head, a.start(), "div") - len("</div>")] if a else ""
    elif h1 is not None:
        head_at = h1.start()
        own_title = _text(h1.group(1))
        body, _ = _cut(body, h1.start(), h1.end())
        # An eyebrow written just above that title ("Report") goes with it: the
        # frame draws its own, and a leftover would label nothing.
        orphan = re.search(r'<div class=["]rg-eyebrow["]>[^<]*</div>\s*$', body[:head_at])
        if orphan is not None:
            body = body[:orphan.start()] + body[head_at:]
            head_at = orphan.start()
        s = re.match(r'\s*<p class=["]sub["][^>]*>(.*?)</p>', body[head_at:], re.S)
        if s:
            sub = s.group(1)
            body = body[:head_at] + body[head_at + s.end():]

    # Banners written above the title move below it.
    lifted: list[str] = []
    while True:
        b = _BANNER.search(body, 0, head_at)
        if b is None:
            break
        body, banner = _cut(body, b.start(), _element_end(body, b.start(), "div"))
        head_at -= len(banner)
        lifted.append(banner)

    # The legend explains labels used on the page: it belongs at the foot.
    legends: list[str] = []
    while (g := _LEGEND.search(body)) is not None:
        body, legend = _cut(body, g.start(), _element_end(body, g.start(), "details"))
        if g.start() < head_at:
            head_at -= len(legend)
        legends.append(legend)

    tabs = TABS.get(section, ())
    current = _current_tab(tabs, here) if tabs else -1
    # Deeper than any tab (one account, one job): keep the page's own title and
    # name the section above it. Otherwise the title is the nav label.
    deep = bool(own_title) and own_title != title and here.split("?", 1)[0].count("/") > 0 \
        and current < 0
    if not title:  # a screen outside the nav keeps its own title
        shown = own_title
    elif deep:
        shown, eyebrow = own_title, title
    else:
        shown = title
    brow = f'<div class="rg-eyebrow">{escape(eyebrow)}</div>' if eyebrow else ""
    line = f'<p class="sub">{sub}</p>' if sub else ""
    if not shown:
        return "".join(lifted) + body + foot_of(legends)
    header = (f'<div class="page-head"><div class="t">{brow}<h1>{escape(shown)}</h1>{line}</div>'
              + (f'<div class="acts">{acts}</div>' if acts else "") + "</div>")
    tab_row = tabs_html(tenant, tabs, current, title) if tabs and current >= 0 else ""
    return header + tab_row + "".join(lifted) + body + foot_of(legends)


def foot_of(legends: list[str]) -> str:
    return f'<footer class="page-foot">{legends[0]}</footer>' if legends else ""
