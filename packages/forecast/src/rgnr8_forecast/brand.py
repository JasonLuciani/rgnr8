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

Type: geometric sans for the wordmark + UI (wide-tracked uppercase, à la the
"RGNR8 · VENTURES" lockup); an elegant serif for editorial page titles, echoing
the guide's headline voice. Self-contained — system fonts only, no external
assets or URLs (owner pages are audited for zero external calls).
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
  --rg-serif:ui-serif,Georgia,"Times New Roman",serif;
  --rg-sans:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
}}"""

RG_BASE_CSS = """*{box-sizing:border-box}
body{margin:0;background:var(--rg-surface);color:var(--rg-ink);
  font:400 15px/1.55 var(--rg-sans);-webkit-font-smoothing:antialiased}
h1{font-family:var(--rg-serif)}
.rg-bar{display:flex;align-items:center;justify-content:space-between;gap:16px;
  background:var(--rg-forest);color:var(--rg-ivory);padding:14px 22px}
.rg-lockup{display:inline-flex;align-items:center;gap:11px}
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
.num{text-align:right;font-variant-numeric:tabular-nums}
.neg{color:var(--rg-risk)}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:7px;vertical-align:middle}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
table{border-collapse:separate;border-spacing:0;width:100%;background:var(--rg-paper);
  border:1px solid var(--rg-line);border-radius:var(--rg-radius);overflow:hidden;box-shadow:var(--rg-shadow)}
th,td{text-align:left;padding:11px 13px;border-bottom:1px solid var(--rg-line);font-size:13.5px}
thead th{background:var(--rg-ivory);font-weight:700;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--rg-muted)}
tbody tr:last-child td{border-bottom:none}
.btn{background:var(--rg-forest);color:var(--rg-ivory);border:none;border-radius:10px;
  padding:10px 15px;font-weight:700;cursor:pointer;font:inherit}
.btn.sage{background:var(--rg-accent-deep)}
.btn.ghost{background:transparent;color:var(--rg-forest);border:1px solid var(--rg-line)}
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
  font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.04em}"""

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
        f'<span style="font:600 {height}px/1 {escape("system-ui,-apple-system,Segoe UI,Roboto,sans-serif")};'
        f'letter-spacing:.2em;text-transform:uppercase;color:{fg}">RGNR8</span>'
        "</span>"
    )
