import sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "forecast" / "src"))
from rgnr8_forecast import brand as B

swatches = [
    ("Forest Green", B.FOREST, "primary · brand bar · ink"),
    ("Ivory", B.IVORY, "the light · page ground"),
    ("Sage Green", B.SAGE, "the one accent"),
    ("Positive", B.POSITIVE, "stable · cash · books current"),
    ("Watch", B.WATCH, "caution"),
    ("Risk", B.RISK, "at-risk · breach"),
    ("Ink-2", B.INK_2, "secondary text"),
    ("Line", B.LINE, "hairline"),
]
def sw(name, hexv, note):
    dark = name in ("Forest Green","Sage Green","Positive","Risk","Ink-2","Watch")
    fg = B.IVORY if dark else B.FOREST
    return (f'<div class="sw"><div class="chip" style="background:{hexv};color:{fg}">{hexv}</div>'
            f'<div class="swn">{name}</div><div class="swd">{note}</div></div>')
chips = "".join(sw(*s) for s in swatches)
html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 — brand</title><style>
{B.THEME_CSS}
.wrap{{max-width:900px;margin:0 auto;padding:30px 22px 64px}}
.lead{{font-family:var(--rg-serif);font-size:30px;font-weight:700;letter-spacing:-.01em;margin:22px 0 2px}}
.tag{{font-size:12px;font-weight:700;letter-spacing:.22em;text-transform:uppercase;color:var(--rg-sage);margin:0 0 22px}}
.sub{{color:var(--rg-muted);margin:0 0 26px;max-width:62ch}}
h2{{font-size:11px;font-weight:700;letter-spacing:.13em;text-transform:uppercase;color:var(--rg-muted);margin:32px 0 12px}}
.grid{{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}}
.sw .chip{{display:flex;align-items:flex-end;height:70px;border-radius:12px;padding:9px 11px;font:700 12px/1 ui-monospace,Menlo,monospace;border:1px solid var(--rg-line)}}
.swn{{font-weight:700;margin-top:8px;font-size:13px}} .swd{{color:var(--rg-muted);font-size:12px}}
.mark{{display:flex;align-items:center;gap:22px;flex-wrap:wrap}}
.markbox{{background:var(--rg-forest);border-radius:14px;padding:24px 28px;display:flex;align-items:center;gap:14px}}
.markbox .rg-wordmark{{font-size:30px}}
.big{{font-family:var(--rg-serif);font-size:46px;font-weight:700;letter-spacing:-.01em;font-variant-numeric:tabular-nums}}
.card{{background:var(--rg-paper);border:1px solid var(--rg-line);border-radius:14px;padding:20px 22px;box-shadow:var(--rg-shadow)}}
.badges span{{margin-right:8px}}
.principle{{border-left:3px solid var(--rg-sage);padding:2px 0 2px 14px;margin:10px 0;color:var(--rg-ink-2)}}
</style></head><body>
{B.brand_bar("Brand system")}
<div class="wrap">
  <div class="lead">RGNR8 brand</div>
  <div class="tag">Bold. Timeless. Endless potential.</div>
  <p class="sub">The look of the tool, in one place, applied to RGNR8's brand guide — Forest Green, Ivory, Sage. Forest grounds every surface, ivory is the light, sage is the one accent, and the infinity monogram carries the endless-potential idea. Generated from the live design tokens, so it can't drift from the product.</p>

  <h2>Symbol &amp; wordmark</h2>
  <div class="mark">
    <div class="markbox">{B.mark_svg(34, B.IVORY)}<span class="rg-wordmark">RGNR<span class="rg-8">8</span></span></div>
    <div>{B.wordmark_svg(26)}<div class="swd" style="margin-top:8px">The infinity mark + geometric wordmark, wide-tracked uppercase. The 8 is the endless loop.</div></div>
  </div>

  <h2>Palette</h2>
  <div class="grid">{chips}</div>

  <h2>Type &amp; numbers</h2>
  <div class="card">
    <div class="rg-eyebrow">Cash today</div>
    <div class="big">$63,210.55</div>
    <div class="swd">Editorial serif for titles (timeless); geometric sans for UI; tabular numbers. Micro-labels uppercase, letter-spaced.</div>
  </div>

  <h2>Status — never color alone</h2>
  <div class="card badges">
    <span class="rg-badge" style="color:{B.POSITIVE};background:#EAF0EA">● Stable</span>
    <span class="rg-badge" style="color:{B.WATCH};background:#F6EEDD">▲ Watch</span>
    <span class="rg-badge" style="color:{B.RISK};background:#F4E3E1">▲ At risk</span>
  </div>

  <h2>Principles</h2>
  <div class="card">
    <div class="principle">One accent. Sage green is for the mark, links, the data series, focus — nothing competes.</div>
    <div class="principle">Lead with the number. The answer to "where's my cash?" is the biggest thing on the page.</div>
    <div class="principle">Forest &amp; ivory ground it. Dark forest bar, ivory light, hairlines and whitespace — timeless, not trendy.</div>
    <div class="principle">Status is earned. Color + icon + label together, from the reserved muted trio only.</div>
    <div class="principle">Self-contained. System fonts, no external assets, no tracking — every owner page renders offline.</div>
  </div>
  <p class="rg-foot">RGNR8 Financial OS · brand tokens live in rgnr8_forecast.brand (Python) and @rgnr8/ledger-kernel brand.ts (TS), byte-identical.</p>
</div></body></html>"""
(HERE / "out").mkdir(exist_ok=True)
(HERE / "out" / "brand_sheet.html").write_text(html)
print("wrote out/brand_sheet.html")
