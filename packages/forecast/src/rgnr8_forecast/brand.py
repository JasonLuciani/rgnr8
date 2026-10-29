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


# The RGNR8 primary symbol: two R's back to back over the infinity loop (the
# "8"). Traced from the brand board and mirrored about its centre line so the
# two halves are exactly symmetric. One compound path, viewBox 267 x 240, no
# font and no external asset, so it renders identically everywhere.
_MONOGRAM_VIEWBOX = (267, 240)
_MONOGRAM_PATH = (
    "M49.1 239.8C48.6 239.8 46.8 239.6 45.2 239.4C16.1 236.6 -3.2 213.2 0.4 185.1"
    "C2.8 166.8 15.1 152.1 33 146.2C54.1 139.1 78.9 144.9 108.5 163.8"
    "C110.7 165.2 117.1 169.3 122.7 173.1C129.2 177.4 133.2 179.8 133.6 179.8"
    "C134.1 179.8 138 177.4 144.5 173.1C174.1 153.5 186.5 147.6 204.4 144.3"
    "C235.7 138.5 263.1 156.5 266.8 185.1C270.2 211.4 253.8 233.6 227 238.8"
    "C211.9 241.7 192.8 238.9 175.6 231.4C165.4 226.9 151.6 219.2 139.2 211.1"
    "C136.5 209.3 134 207.8 133.6 207.8C133.3 207.8 130.8 209.3 128.1 211.1"
    "C111.9 221.6 94.9 230.7 83.9 234.4C72.4 238.4 57.8 240.7 49.1 239.8ZM64 215.8"
    "C77.1 213.3 90 207.9 107.8 197.2C113.4 193.9 113.5 193.7 111.1 192.1"
    "C110.4 191.7 108.1 190.1 106 188.7C82.4 172.6 66.5 165.9 52.2 166C35.3 166.1 23.4 175.7 22.4 190"
    "C21.6 203.7 32 214.3 48.1 216.3C51.6 216.7 60.6 216.4 64 215.8ZM222.4 215.8"
    "C236.9 212.7 245.6 202.7 244.8 190C243.7 172.3 225.5 162.2 203.9 167.2"
    "C192.8 169.7 178.6 176.9 161.3 188.7C159.2 190.1 156.9 191.7 156.2 192.1"
    "C153.8 193.7 153.9 193.9 159.5 197.2C177.5 208 191.5 213.9 203.9 215.8"
    "C205.4 216.1 206.9 216.3 207.3 216.4C209.2 216.7 219.8 216.3 222.4 215.8ZM118.4 154.7"
    "C115.1 152.6 110.1 149.3 107.2 147.5C104.3 145.7 101.8 143.9 101.6 143.6"
    "C101.2 143.1 101.2 135.3 101.2 84.1C101.2 30.2 101.1 25.1 100.7 24.7"
    "C100.3 24.3 98.3 24.3 78.6 24.3C56.9 24.4 56.9 24.4 54.7 25.2C48.3 27.3 43.6 32.3 41.7 38.9"
    "C40.8 42 40.8 49.5 41.7 52.8C43.6 59.9 49.3 65.7 56 67.4C57.3 67.8 60.2 67.9 69.8 68.1"
    "C77.4 68.2 82.1 68.3 82.3 68.5C82.7 68.9 82.8 88.8 82.4 89.5C82.2 89.8 81.1 89.9 76.8 90"
    "C71.4 90.1 71.4 90.1 70.8 90.9C70.5 91.3 68.4 94.4 66.1 97.9C63.9 101.4 58.9 109.1 54.9 115"
    "C51 121 47.2 126.8 46.5 128C45 130.5 45.1 130.4 40.8 131.3C30.4 133.6 19.7 138.6 10.9 145.3"
    "C6.9 148.3 6.3 148.7 6.1 148.5C5.8 148.2 6.7 146.7 12 138.8C14.9 134.5 20.4 126.3 24.2 120.5"
    "C28 114.8 32.7 107.7 34.6 104.9C41.3 94.9 45 88.9 45 88.4C44.9 88.1 43.8 87.4 42.1 86.5"
    "C26.6 78.7 17.7 63.4 17.7 44.5C17.8 23.9 29.9 7.2 48.7 1.9C55.3 0 52.1 0.2 90.2 0.1"
    "C121.6 -0 124.6 0 125 0.4C125.4 0.8 125.5 3.6 125.4 31.3C125.4 48.1 125.5 79.1 125.5 100.2"
    "C125.7 149 125.5 158.6 124.8 158.6C124.5 158.7 121.8 157 118.4 154.7ZM141.9 158.4"
    "C141.8 158.2 141.7 136.3 141.7 109.8C141.8 83.2 141.8 47.9 141.8 31.2"
    "C141.8 3.6 141.9 0.8 142.3 0.4C142.7 0 145.7 -0 177.1 0.1C215.2 0.2 211.9 0 218.6 1.9"
    "C237.4 7.2 249.5 23.9 249.5 44.5C249.6 63.4 240.7 78.7 225.2 86.5"
    "C223.4 87.4 222.4 88.1 222.3 88.4C222.2 88.9 226 94.9 232.7 104.9"
    "C234.6 107.7 239.3 114.8 243.1 120.5C246.9 126.3 252.4 134.5 255.3 138.8"
    "C263 150.3 263 150.3 256.4 145.3C247.6 138.6 236.9 133.6 226.5 131.3"
    "C222.2 130.4 222.3 130.5 220.8 128C220 126.8 216.2 121 212.3 115"
    "C208.4 109.1 203.4 101.4 201.1 97.9C198.9 94.4 196.8 91.3 196.5 90.9"
    "C195.9 90.1 195.9 90.1 190.5 90C186.2 89.9 185.1 89.8 184.9 89.5C184.5 88.8 184.6 68.9 185 68.5"
    "C185.2 68.3 189.9 68.2 197.5 68.1C207.1 67.9 209.9 67.8 211.2 67.4"
    "C218 65.7 223.6 59.9 225.6 52.8C226.5 49.5 226.5 42 225.6 38.9C223.7 32.3 219 27.3 212.6 25.2"
    "C210.4 24.4 210.4 24.4 188.7 24.3C169 24.3 166.9 24.3 166.5 24.7"
    "C166.1 25.1 166.1 30.2 166.1 84.1C166.1 135.3 166 143.1 165.7 143.6"
    "C165.5 143.9 163.4 145.4 161 146.9C158.6 148.4 153.6 151.6 149.8 154.1"
    "C143 158.7 142.3 159 141.9 158.4Z"
)


def monogram_svg(height: int = 30, color: str = IVORY, label: str = "RGNR8") -> str:
    """The RR-over-infinity primary symbol as inline SVG, `height` px tall.
    Pass ``label=""`` where it sits beside the written name, so a screen reader
    doesn't announce RGNR8 twice."""
    vw, vh = _MONOGRAM_VIEWBOX
    width = round(height * vw / vh)
    a11y = f'role="img" aria-label="{escape(label)}"' if label else 'aria-hidden="true"'
    return (
        f'<svg class="rg-monogram" viewBox="0 0 {vw} {vh}" height="{height}" width="{width}" '
        f'{a11y} focusable="false" style="flex:none;display:block">'
        f'<path fill="{color}" fill-rule="evenodd" d="{_MONOGRAM_PATH}"/></svg>'
    )


def finos_logo_html(height: int = 30, *, on_dark: bool = True, product: str = "Fin OS") -> str:
    """The RGNR8 Fin OS lockup, built like the primary lockup on the brand board:
    the monogram, then RGNR8 wide-tracked, with the product name beneath it in
    light caps between two hairline rules (where "VENTURES" sits). `height` is
    the monogram's height; the type scales from it. `on_dark` picks ivory on a
    forest ground (the top bars) or forest on a light one (the sign-in card) —
    an explicit switch, so no caller can get forest text on a forest bar by
    passing a colour the lockup didn't expect."""
    color = IVORY if on_dark else FOREST
    tone = "" if on_dark else " on-light"
    return (
        f'<span class="rg-logo{tone}" style="--rg-logo-h:{height}px">'
        f"{monogram_svg(height, color, label='')}"
        '<span class="rg-logo-text">'
        '<span class="rg-logo-word">RGNR8</span>'
        f'<span class="rg-logo-sub"><span>{escape(product)}</span></span>'
        "</span></span>"
    )


# The lockup's own rules, shared by every surface that draws the logo (the
# app shell, reports, the operator console and the standalone briefing page).
RG_LOGO_CSS = """/* The Fin OS lockup (finos_logo_html): monogram, RGNR8, and the product name
   between hairline rules. Trailing letter-spacing is cancelled with a matching
   negative margin so the tracked words centre over each other. */
.rg-logo{display:inline-flex;align-items:center;gap:calc(var(--rg-logo-h,30px)*.4);
  line-height:1;color:var(--rg-ivory,#F2EFE6)}
.rg-logo-text{display:flex;flex-direction:column;gap:calc(var(--rg-logo-h,30px)*.17)}
.rg-logo-word{font-family:var(--rg-sans,system-ui,sans-serif);font-weight:600;font-size:calc(var(--rg-logo-h,30px)*.58);
  line-height:1;letter-spacing:.26em;margin-right:-.26em;text-transform:uppercase;white-space:nowrap}
.rg-logo-sub{display:flex;align-items:center;gap:.6em;font-family:var(--rg-sans,system-ui,sans-serif);
  font-weight:500;font-size:max(10px,calc(var(--rg-logo-h,30px)*.27));line-height:1;letter-spacing:.34em;
  text-transform:uppercase;color:#CBD8CC;white-space:nowrap}
.rg-logo-sub>span{margin-right:-.34em}
.rg-logo-sub::before,.rg-logo-sub::after{content:"";flex:1 1 auto;min-width:.8em;height:1px;
  background:currentColor;opacity:.6}
.rg-logo.on-light{color:var(--rg-forest,#0E241E)}
/* Brand sage (#5E7F63) is 4.48:1 on white, a hair under AA for small text;
   this is the same hue one step deeper (4.82:1). */
.rg-logo.on-light .rg-logo-sub{color:#587A5D}
"""


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
""" + RG_LOGO_CSS + """.rg-ctx{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#A9B7AC;
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
        f"{finos_logo_html(30)}"
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
