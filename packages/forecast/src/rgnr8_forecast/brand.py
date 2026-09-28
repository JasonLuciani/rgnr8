"""RGNR8 brand system — the single source of truth for how the tool looks.

Built to RGNR8's brand guide: **Forest Green, Ivory, Sage** — bold, timeless,
endless potential. Forest green grounds every surface; ivory is the light; sage
is the one accent. The mark is the RGNR8 infinity monogram (the "8" as an endless
loop). The mirror of this file lives in TypeScript at `@rgnr8/ledger-kernel`
`brand.ts` with byte-identical tokens, so the Python and TS surfaces are visually
the same product.

Palette (from the guide)
------------------------
- **Forest Green** ``#0E241E`` — primary: the brand bar, ink, dark ground.
- **Ivory** ``#F2EFE6`` — the light: page background, reversed text on forest.
- **Sage Green** ``#5E7F63`` — the one accent: the mark, links, data series, focus.

Status stays muted and earthy to fit the timeless palette (never color alone —
always paired with an icon/label): positive ``#3E7C5A``, watch ``#B7791F``, risk
``#B4443C``.

Type: one geometric sans, everywhere — Inter, wide-tracked uppercase for the
"RGNR8 · VENTURES" lockup and plain for everything else. Headings are separated
from body text by weight and tracking, not by family; there is deliberately no
serif any more, because headings set in Georgia over a system-ui UI made one
product look like two. Still self-contained: the web app *self-hosts* the font
from its own origin (``rgnr8_web/assets.py``) rather than linking a CDN, so
owner pages continue to make zero external calls, and any surface not served by
that app falls back to the system stack behind ``--rg-sans``.

Components: ``RG_BASE_CSS`` below is the only place card, table, button,
banner, tile, chip, empty-state, queue, form, allocation and timeline styles are
defined. A renderer that hand-writes one of these inline is a bug — that is how
the same table ended up padded ``10px 13px`` on one screen and ``11px 13px`` on
another. ``rgnr8_web.components`` wraps them as functions so call sites name the
component instead of describing it.
"""

from __future__ import annotations

from html import escape
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .money import Money

# Brand palette (keep in lockstep with brand.ts) -----------------------------
FOREST = "#0E241E"   # primary / ink / brand bar
IVORY = "#F2EFE6"    # the light / page ground / reversed text
SAGE = "#5E7F63"     # the one accent

INK = FOREST
INK_2 = "#2C3B33"    # secondary text (deep green-gray)
MUTED = "#616D66"    # labels, meta (sage-gray) — darkened to clear WCAG AA (4.5:1) on ivory
PAPER = "#FFFFFF"    # cards / tables (crisp on ivory)
SURFACE = IVORY      # page background
LINE = "#E2DFD3"     # warm hairline
ACCENT = SAGE
ACCENT_DEEP = "#42604A"
POSITIVE = "#3E7C5A"
WATCH = "#B7791F"
RISK = "#B4443C"

# Semantic status → brand color, exposed so every renderer agrees.
STATUS_COLOR = {
    "STABLE": POSITIVE,
    "WATCH": WATCH,
    "AT_RISK": RISK,
}


def status_color(level: str) -> str:
    """Brand color for a status level ('STABLE'|'WATCH'|'AT_RISK'), risk default."""
    return STATUS_COLOR.get(level.upper().replace(" ", "_"), RISK)


# The single money formatter for every RGNR8 surface — one format, everywhere.
def format_money(m: "Money", *, cents: bool = True) -> str:
    """Format a Money as grouped, currency-aware, sign-prefixed text.

    ``$128,400.00`` for USD, ``EUR 1,250.00`` for anything else. This is the one
    formatter shell/screens/transactions/Today all import, so no two surfaces
    ever disagree on how a figure reads (a grouped ``$`` next to an ungrouped
    ``USD`` was the bug this kills).
    """
    neg = m.minor_units < 0
    whole = m.to_decimal_string().lstrip("-")
    intpart, _, frac = whole.partition(".")
    sym = "$" if m.currency == "USD" else m.currency + " "
    body = f"{int(intpart):,}.{frac or '00'}" if cents else f"{int(intpart):,}"
    return f"{'-' if neg else ''}{sym}{body}"


# The RGNR8 infinity monogram — the "8" as an endless loop (no external asset).
def mark_svg(size: int = 26, color: str = SAGE) -> str:
    """The RGNR8 infinity mark as inline SVG, sized to `size` px tall."""
    w = round(size * 40 / 24)
    return (
        f'<svg viewBox="0 0 40 24" height="{size}" width="{w}" role="img" aria-label="RGNR8" '
        'style="vertical-align:middle;flex:none">'
        f'<path d="M8 12 C8 5.5 16 5.5 20 12 C24 18.5 32 18.5 32 12 '
        f'C32 5.5 24 5.5 20 12 C16 18.5 8 18.5 8 12 Z" '
        f'fill="none" stroke="{color}" stroke-width="3.2" stroke-linecap="round"/>'
        "</svg>"
    )


# The token block + shared component styles every page includes.
RG_TOKENS_CSS = f""":root{{
  --rg-forest:{FOREST}; --rg-ivory:{IVORY}; --rg-sage:{SAGE};
  --rg-ink:{INK}; --rg-ink-2:{INK_2}; --rg-muted:{MUTED};
  --rg-paper:{PAPER}; --rg-surface:{SURFACE}; --rg-line:{LINE};
  --rg-accent:{ACCENT}; --rg-accent-deep:{ACCENT_DEEP};
  --rg-pos:{POSITIVE}; --rg-watch:{WATCH}; --rg-risk:{RISK};
  --rg-radius:12px; --rg-pill:999px;
  --rg-shadow:0 1px 2px rgba(14,36,30,.07),0 1px 3px rgba(14,36,30,.05);
  /* One typeface, everywhere. `Inter` is self-hosted by the web app (see
     rgnr8_web/assets.py); the stack behind it is what renders until the font
     arrives, and what renders for any surface that isn't served by that app.
     There is deliberately no serif token any more — headings set in Georgia
     while the UI sat in system-ui made one product look like two. */
  --rg-sans:"Inter",system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  /* Money is read in columns, so figures are tabular by default and only opt
     out in running prose. */
  --rg-nums:tabular-nums;
  /* Deprecated alias. Nothing in this repo should set type in a serif any
     more, but the TypeScript ledger-kernel keeps its own copy of these tokens
     and the prototype + report renderers still name this one. Pointing it at
     the sans stack makes those surfaces consistent today without changing
     them blind; delete it once `ledger-kernel/src/brand.ts` is folded into
     this single source of truth. */
  --rg-serif:var(--rg-sans);
}}"""

RG_BASE_CSS = """*{box-sizing:border-box}
body{margin:0;background:var(--rg-surface);color:var(--rg-ink);
  font:400 15px/1.55 var(--rg-sans);-webkit-font-smoothing:antialiased;
  font-variant-numeric:var(--rg-nums)}
/* Headings used to be Georgia while everything else was system-ui. They are
   the same family now; what separates them is weight and tracking. */
h1{font-weight:700;letter-spacing:-.018em}
/* Prose is the one place proportional figures read better than tabular. */
p,li{font-variant-numeric:normal}
.rg-bar{display:flex;align-items:center;justify-content:space-between;gap:16px;
  background:var(--rg-forest);color:var(--rg-ivory);padding:14px 22px}
/* The lockup is a link to the tenant home in the app shell and a plain span
   elsewhere, so it has to defuse the browser's default link styling: without
   this the wordmark renders underlined in #0000EE, which is exactly as bad as
   it sounds on a dark forest bar. */
.rg-lockup{display:inline-flex;align-items:center;gap:11px;
  text-decoration:none;color:inherit}
.rg-wordmark{font:600 18px/1 var(--rg-sans);
  letter-spacing:.22em;text-transform:uppercase;color:var(--rg-ivory)}
.rg-wordmark .rg-8{color:var(--rg-sage)}
.rg-ctx{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#A9B7AC;
  font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.rg-shell{max-width:960px;margin:0 auto;padding:24px 20px 56px}
.rg-eyebrow{font-size:11px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--rg-muted)}
.rg-badge{display:inline-flex;align-items:center;gap:7px;border-radius:var(--rg-pill);
  padding:4px 12px;font-weight:700;font-size:12px;letter-spacing:.05em;text-transform:uppercase}
.rg-dot{display:inline-block;width:9px;height:9px;border-radius:50%;vertical-align:middle;margin-right:7px}
.rg-foot{color:var(--rg-muted);font-size:12px;margin-top:14px}
:focus-visible{outline:2px solid var(--rg-sage);outline-offset:2px}
/* Shared component layer — the single source for card/table/banner/btn/tile/dot,
   so no renderer redeclares (or drifts from) these. */
.card{background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:var(--rg-radius);
  box-shadow:var(--rg-shadow);padding:20px 22px;margin-bottom:16px}
.muted{color:var(--rg-muted)}
/* Links. Nothing styled a plain <a> at all, so every link in the body of the
   app — "View 2026-08 package", every figure that drills into its detail —
   rendered in the browser's default blue, and purple once visited, on an ivory
   page in a forest-and-sage palette. The underline stays: it is the affordance
   that does not depend on colour. */
a{color:var(--rg-accent-deep)}
a:hover{color:var(--rg-forest)}
a:visited{color:var(--rg-accent-deep)}
/* Meta text — a timestamp under a figure, a hint under a field, a count in a
   caption. Fifty call sites set this size inline before the class existed. */
.small{font-size:12px}
.num{text-align:right;font-variant-numeric:tabular-nums}
.neg{color:var(--rg-risk)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:separate;border-spacing:0;width:100%;background:var(--rg-paper);
  border:1px solid var(--rg-line);border-radius:var(--rg-radius);overflow:hidden;box-shadow:var(--rg-shadow)}
th,td{text-align:left;padding:11px 13px;border-bottom:1px solid var(--rg-line);font-size:13.5px}
thead th{background:var(--rg-ivory);font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-muted)}
tbody tr:last-child td{border-bottom:none}
/* `.btn` is worn by both <button> and <a>. Making it inline-flex and killing
   the underline here is what stops every call site from repeating
   style="text-decoration:none" — the same defect that made the wordmark render
   underlined in #0000EE when it became a link.

   The bare `button` in this selector is not tidiness. Fifty-one submit
   controls across fifteen renderers — "Add asset", "Preview split", "Scan
   receipt" — carried no class at all and rendered as the operating system's
   grey default, sitting next to RGNR8's own buttons on the same form. Styling
   the element as well as the class fixes all of them, and the next one that
   forgets. Anything with its own look (`.chip`) still wins on specificity. */
.btn,button{background:var(--rg-forest);color:var(--rg-ivory);border:none;border-radius:10px;
  padding:10px 15px;font-weight:700;cursor:pointer;font:inherit;
  display:inline-flex;align-items:center;gap:8px;text-decoration:none;
  line-height:1.2;white-space:nowrap}
.btn.sage{background:var(--rg-accent-deep);color:var(--rg-ivory)}
.btn.ghost{background:transparent;color:var(--rg-forest);border:1px solid var(--rg-line)}
/* Destructive. `class="danger"` was already on "Dispose" and "Reset
   owner-held data" and NOTHING DEFINED IT, so the two most irreversible
   buttons in the product rendered identically to "Save". */
.btn.danger{background:transparent;color:var(--rg-risk);
  border:1px solid color-mix(in srgb, var(--rg-risk) 42%, transparent)}
.btn.danger:hover{background:color-mix(in srgb, var(--rg-risk) 10%, var(--rg-paper))}
/* A link that acts like a button without looking like one: the sub-navigation
   across Books, the "Back to review" links, the inline jumps in a table cell.
   Thirty-five call sites named `.btn-link` and no rule existed either, so all
   of them fell back to the browser's default blue underline. */
.btn-link{color:var(--rg-accent-deep);text-decoration:none;font-weight:600;font-size:13px}
.btn-link:hover{text-decoration:underline}
.banner{padding:12px 16px;border-radius:var(--rg-radius);margin-bottom:16px;font-weight:700;font-size:13px;border:1px solid transparent}
.banner.good{background:color-mix(in srgb, var(--rg-pos) 14%, var(--rg-paper));
  color:color-mix(in srgb, var(--rg-pos) 78%, black);border-color:color-mix(in srgb, var(--rg-pos) 32%, transparent)}
.banner.warn{background:color-mix(in srgb, var(--rg-risk) 14%, var(--rg-paper));
  color:color-mix(in srgb, var(--rg-risk) 78%, black);border-color:color-mix(in srgb, var(--rg-risk) 32%, transparent)}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:8px}
.tile{background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:12px;padding:15px 16px}
.tile .k{color:var(--rg-muted);font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.tile .v{font-size:23px;font-weight:800;margin-top:6px;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.pill{display:inline-flex;align-items:center;gap:7px;border-radius:var(--rg-pill);padding:4px 12px;
  font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.04em}

/* --- the named components ---------------------------------------------
   Everything below exists because a renderer was hand-writing it inline, and
   two renderers hand-writing the same thing is how `10px 13px` and
   `11px 13px` ended up being the same table. A component earns a name here
   the moment a second surface needs it; nothing is invented speculatively.  */

/* Page header: title, one line of context, and the actions that belong to the
   page as a whole. The actions sit on the same row on a wide screen and wrap
   beneath the title on a phone. */
.page-head{display:flex;align-items:flex-start;justify-content:space-between;
  gap:16px;flex-wrap:wrap;margin:0 0 18px}
.page-head>.t{min-width:min(100%,32ch);flex:1}
.page-head h1{margin:0 0 4px}
.page-head .sub{margin:0;color:var(--rg-muted);font-size:13px}
.page-head .acts{display:flex;gap:9px;flex-wrap:wrap;align-items:center}

/* Metric. `.tile` is the small grid version; `.metric.hero` is the one figure
   a screen is actually about (cash today), which has to read across a room. */
.metric .k{color:var(--rg-muted);font-size:11px;font-weight:700;
  letter-spacing:.11em;text-transform:uppercase}
.metric .v{font-size:23px;font-weight:800;margin-top:6px;letter-spacing:-.01em;
  font-variant-numeric:tabular-nums}
.metric.hero .v{font-size:40px;line-height:1.05;margin-top:4px}
.metric .note{color:var(--rg-muted);font-size:12px;margin-top:4px}

/* Status chip. Status never rides on color alone — every chip carries a glyph
   and a word, and the modifier sets both the text and the border so the shape
   survives a monochrome print or a color-blind reader. */
.chip-status{display:inline-flex;align-items:center;gap:7px;border-radius:var(--rg-pill);
  padding:4px 12px;font-weight:700;font-size:12px;text-transform:uppercase;
  letter-spacing:.04em;color:var(--rg-muted);
  border:1px solid color-mix(in srgb, currentColor 22%, transparent);
  background:color-mix(in srgb, currentColor 7%, var(--rg-paper))}
.chip-status.pos{color:var(--rg-pos)}
.chip-status.watch{color:var(--rg-watch)}
.chip-status.risk{color:var(--rg-risk)}
.chip-status.neutral{color:var(--rg-muted)}

/* Empty state. Two shapes, one voice: `.empty` for a panel, and the same rule
   applied to a full-width table cell so an empty table doesn't collapse into a
   one-line sliver. Both say what is missing and what to do about it. */
.empty{text-align:center;padding:28px 24px;color:var(--rg-muted)}
.empty .what{font-weight:700;color:var(--rg-ink-2);font-size:14px}
.empty .how{font-size:13px;margin-top:5px}
/* `:not(.btn)` because the first version of this rule painted the link colour
   over the "Connect a bank" button — sage text on a sage button, an invisible
   label on the one control the screen exists to offer. */
.empty .how a:not(.btn){color:var(--rg-accent-deep)}
.empty .acts{display:flex;justify-content:center;gap:9px;flex-wrap:wrap;margin-top:12px}
td.empty{border-bottom:none}

/* Action queue: a list of things asking for a decision. Each row is a single
   click target with the ask on the left and the action on the right. */
.queue{list-style:none;margin:0;padding:0}
.queue>li{display:flex;align-items:center;justify-content:space-between;gap:14px;
  flex-wrap:wrap;padding:13px 0;border-bottom:1px solid var(--rg-line)}
.queue>li:last-child{border-bottom:none}
.queue .ask{min-width:min(100%,28ch);flex:1}
.queue .ask .h{font-weight:700;font-size:14px}
.queue .ask .m{color:var(--rg-muted);font-size:12.5px;margin-top:2px}
.queue .acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap}

/* Form section, and the field grids it sits on.

   `.grid`, `.grid2`, `.grid3` and `.grid4` were used by roughly eighty call
   sites in the web renderers and DEFINED BY NOTHING that those pages load —
   the only `.grid` rules in the repo belonged to the operator console and the
   prototype brandsheet, which are separate documents. So every "grid" form in
   the product was rendering as a plain block: the eleven-field Add asset form
   was eleven full-width rows stacked half a screen tall, and the fifty-one
   span-the-row attributes on their submit buttons resolved to a value with no
   grid to apply it to (those are `.wide` now). Defining these here is the fix,
   and it is why they belong in the shared layer, not in one app's stylesheet.

   Column counts are capped, not fixed: a three-column grid becomes one column
   on a phone, because an owner approving a bill on a phone should not be
   scrolling sideways. */
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
  gap:4px 16px;align-items:end}
.grid2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 16px;align-items:end}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px 16px;align-items:end}
.grid4{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:4px 16px;align-items:end}
/* A cell that takes the whole row: the submit button, a full-width textarea,
   a note that explains the fields above it. */
.wide{grid-column:1/-1}
@media(max-width:820px){
  .grid3,.grid4{grid-template-columns:repeat(2,minmax(0,1fr))}
}
@media(max-width:560px){
  .grid,.grid2,.grid3,.grid4{grid-template-columns:minmax(0,1fr)}
}
.form-sec{margin-bottom:18px}
.form-sec>h3{font-size:12px;font-weight:700;letter-spacing:.09em;text-transform:uppercase;
  color:var(--rg-muted);margin:0 0 2px}
.form-sec>.why{color:var(--rg-muted);font-size:12.5px;margin:0 0 6px}

/* Allocation editor: rows of share-of-the-whole, each with its own bar, plus a
   total that turns to risk when the shares don't add up. Splitting money is
   the one place a wrong sum must be impossible to miss. */
.alloc{display:grid;gap:9px}
.alloc-row{display:grid;grid-template-columns:minmax(0,1fr) 92px;gap:10px;align-items:center}
.alloc-row .track{grid-column:1/-1;height:6px;background:var(--rg-ivory);
  border-radius:4px;overflow:hidden}
.alloc-row .track>span{display:block;height:100%;background:var(--rg-sage)}
.alloc-total{display:flex;justify-content:space-between;font-weight:700;font-size:13px;
  padding-top:8px;border-top:1px solid var(--rg-line)}
.alloc-total.over{color:var(--rg-risk)}

/* Audit timeline: what happened, in order, most recent first. The rail is a
   border on the list so it can never drift out of alignment with the dots. */
.timeline{list-style:none;margin:0;padding:0 0 0 20px;
  border-left:2px solid var(--rg-line)}
.timeline>li{position:relative;padding:0 0 16px 4px}
.timeline>li:last-child{padding-bottom:0}
.timeline>li::before{content:"";position:absolute;left:-27px;top:5px;width:9px;height:9px;
  border-radius:50%;background:var(--rg-sage);
  box-shadow:0 0 0 3px var(--rg-surface)}
.timeline .when{color:var(--rg-muted);font-size:11px;font-weight:700;
  letter-spacing:.07em;text-transform:uppercase}
.timeline .what{font-size:14px;margin-top:2px}
.timeline .who{color:var(--rg-muted);font-size:12.5px;margin-top:1px}"""

# Full stylesheet block (tokens + base) for pages that want the lot in one shot.
THEME_CSS = RG_TOKENS_CSS + "\n" + RG_BASE_CSS


def brand_bar(context: str) -> str:
    """The forest RGNR8 top bar: infinity mark + wordmark left, context right."""
    return (
        '<header class="rg-bar">'
        '<span class="rg-lockup">'
        f"{mark_svg(22, IVORY)}"
        '<span class="rg-wordmark">RGNR<span class="rg-8">8</span></span>'
        "</span>"
        f'<span class="rg-ctx">{escape(context)}</span>'
        "</header>"
    )


def wordmark_svg(height: int = 22, on_dark: bool = False) -> str:
    """A compact inline-SVG RGNR8 wordmark (mark + text), for tight spots."""
    fg = IVORY if on_dark else FOREST
    return (
        f'<span style="display:inline-flex;align-items:center;gap:9px;vertical-align:middle">'
        f"{mark_svg(height, SAGE)}"
        # var(--rg-sans), not a hand-copied stack: this wordmark sits next to
        # the shell's own and the two must not resolve to different families.
        f'<span style="font:600 {height}px/1 var(--rg-sans);'
        f'letter-spacing:.2em;text-transform:uppercase;color:{fg}">RGNR8</span>'
        "</span>"
    )
