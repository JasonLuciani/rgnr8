"""Render the owner "Today" dashboard as a self-contained interactive HTML page.

Consumes a ForecastResult, builds the briefing + the ask-your-CFO answers, and
emits one HTML file: a status hero, KPI tiles, a 13-week cash chart (single-hue
line with the minimum-cash floor and the trough marked), the cash drivers, and a
clickable ask-your-CFO panel with answers embedded. No backend, no external
assets. Colors follow the validated data-viz palette; status uses the reserved
status palette with an icon + label (never color alone).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from html import escape

from rgnr8_forecast import ForecastResult, Money, brand_bar
from rgnr8_forecast.brand import POSITIVE, RISK, WATCH
from rgnr8_forecast.brand import format_money as _fmt

from .answer import SUGGESTED, Answer, Question, answer
from .build import build_briefing
from .models import StatusLevel, WeeklyBriefing

_STATUS = {
    StatusLevel.STABLE: ("Stable", POSITIVE, "check"),
    StatusLevel.WATCH: ("Watch", WATCH, "alert"),
    StatusLevel.AT_RISK: ("At risk", RISK, "alert"),
}

_ICON = {
    "check": '<path d="M5 10.5l3 3 7-7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/>',
    "alert": '<path d="M10 3l8 14H2z" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/><path d="M10 8v4" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="10" cy="15" r="1.1" fill="currentColor"/>',
}


def _kpi(m: Money) -> str:
    """A KPI money value, colored with the risk token when negative (a floor
    breach / no cushion) so alarming numbers stand out from healthy ones."""
    v = escape(_fmt(m))
    return f'<span class="neg">{v}</span>' if m.minor_units < 0 else v


def render_today_html(forecast: ForecastResult, business_name: str = "Your business") -> str:
    briefing = build_briefing(forecast)
    answers = {q.value: answer(forecast, q) for q in Question}
    return _page(forecast, briefing, answers, business_name)


def _page(
    forecast: ForecastResult,
    b: WeeklyBriefing,
    answers: dict[str, Answer],
    business_name: str,
) -> str:
    p = forecast.projection
    label, color, icon = _STATUS[b.status]
    chart = _cash_chart(forecast)

    tiles = [
        ("Cash today", p.opening_available),
        ("Minimum-cash floor", p.effective_floor),
        ("Projected low point", p.trough.balance),
        ("Cushion at low point", p.trough.balance - p.effective_floor),
    ]
    tile_html = "".join(
        f'<div class="tile"><div class="tile-label">{escape(l)}</div>'
        f'<div class="tile-value">{_kpi(v)}</div></div>'
        for l, v in tiles
    )

    drivers_html = ""
    if b.drivers:
        top = b.drivers[0].amount.minor_units or 1
        rows = ""
        for d in b.drivers:
            pct = round(d.amount.minor_units * 100 / top)
            rows += (
                f'<div class="drv"><div class="drv-top"><span>{escape(d.label)}</span>'
                f'<span class="drv-amt">{escape(_fmt(d.amount))}</span></div>'
                f'<div class="drv-bar"><div class="drv-fill" style="width:{pct}%"></div></div>'
                f'<div class="drv-sub">{d.share_bps/100:.0f}% of outflows</div></div>'
            )
        drivers_html = f'<section class="card"><h2>What\'s using your cash</h2>{rows}</section>'

    action_html = ""
    if b.primary_action:
        action_html = f'<div class="action"><span class="do">Do this</span>{escape(b.primary_action)}</div>'

    chips = "".join(
        f'<button class="chip" onclick="ans(\'{escape(q.value)}\')">{escape(text)}</button>'
        for q, text in SUGGESTED
    )
    answers_json = json.dumps(
        {k: {"text": a.answer_text, "q": a.question_text} for k, a in answers.items()}
    )

    dq_html = ""
    if b.data_quality:
        items = "".join(f"<li>{escape(n)}</li>" for n in b.data_quality)
        dq_html = f'<section class="card subtle"><h2>Heads-up</h2><ul class="dq">{items}</ul></section>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RGNR8 — Today</title>
<style>
  :root {{
    --plane:#F2EFE6; --surface:#FFFFFF; --ink:#0E241E; --ink2:#2C3B33; --muted:#616D66;
    --grid:#E2DFD3; --axis:#C7C3B4; --series:#5E7F63; --border:#E2DFD3; --ivory:#F2EFE6;
    --serif:ui-serif,Georgia,"Times New Roman",serif;
    --status:{color};
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--plane); color:var(--ink);
    font:400 15px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    -webkit-font-smoothing:antialiased; }}
  .rg-bar{{display:flex;align-items:center;justify-content:space-between;gap:16px;
    background:var(--ink);color:var(--ivory);padding:14px 22px}}
  .rg-lockup{{display:inline-flex;align-items:center;gap:11px}}
  .rg-wordmark{{font:600 18px/1 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
    letter-spacing:.22em;text-transform:uppercase;color:var(--ivory)}}
  .rg-wordmark .rg-8{{color:var(--series)}}
  .rg-ctx{{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:#A9B7AC;font-weight:600}}
  .wrap {{ max-width:900px; margin:0 auto; padding:24px 16px 56px; }}
  .head {{ display:flex; justify-content:space-between; align-items:baseline; margin-bottom:14px; }}
  .head h1 {{ font-size:15px; margin:0; font-weight:700; letter-spacing:.02em; color:var(--ink2); }}
  .head .as-of {{ color:var(--muted); font-size:13px; }}
  .hero {{ background:var(--surface); border:1px solid var(--border); border-radius:16px; padding:22px 24px; margin-bottom:16px;
    box-shadow:0 1px 2px rgba(14,36,30,.07),0 1px 3px rgba(14,36,30,.05); }}
  .badge {{ display:inline-flex; align-items:center; gap:7px; color:var(--status); font-weight:800;
    text-transform:uppercase; letter-spacing:.06em; font-size:12px; }}
  .badge svg {{ width:18px; height:18px; }}
  .hero h2 {{ font-family:var(--serif); font-size:27px; margin:10px 0 4px; font-weight:700; letter-spacing:-.01em; line-height:1.18; }}
  .hero .reason {{ color:var(--ink2); font-size:14px; margin:0; }}
  .action {{ margin-top:14px; background:color-mix(in srgb, var(--status) 10%, var(--surface));
    border:1px solid color-mix(in srgb, var(--status) 35%, var(--border)); border-radius:10px; padding:11px 13px; font-size:14px; }}
  .action .do {{ display:block; font-weight:700; font-size:11px; text-transform:uppercase; letter-spacing:.05em; color:var(--status); margin-bottom:3px; }}
  .tiles {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:16px; }}
  .tile {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:15px 16px; }}
  .tile-label {{ color:var(--muted); font-size:11px; font-weight:700; letter-spacing:.08em; text-transform:uppercase; }}
  .tile-value {{ font-size:23px; font-weight:800; margin-top:6px; font-variant-numeric:tabular-nums; letter-spacing:-.01em; }}
  .tile-value .neg {{ color:{RISK}; }}
  :focus-visible {{ outline:2px solid var(--series); outline-offset:2px; }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:14px; padding:18px 20px; margin-bottom:16px; }}
  .card h2 {{ font-size:12px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:0 0 12px; }}
  .drv {{ margin-bottom:12px; }} .drv-top {{ display:flex; justify-content:space-between; font-size:14px; }}
  .drv-amt {{ font-variant-numeric:tabular-nums; }}
  .drv-bar {{ height:8px; background:var(--grid); border-radius:5px; margin:5px 0 2px; overflow:hidden; }}
  .drv-fill {{ height:100%; background:var(--series); border-radius:5px; }}
  .drv-sub {{ color:var(--muted); font-size:12px; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
  .chip {{ font:inherit; font-size:13px; color:var(--ink); background:var(--plane); border:1px solid var(--border);
    border-radius:999px; padding:7px 13px; cursor:pointer; }}
  .chip:hover {{ border-color:var(--series); color:var(--series); }}
  .answer {{ margin-top:12px; padding:12px 14px; background:var(--plane); border:1px solid var(--border);
    border-radius:10px; font-size:14px; display:none; }}
  .answer.show {{ display:block; }} .answer .q {{ color:var(--muted); font-size:12px; margin-bottom:3px; }}
  .dq {{ margin:0; padding-left:18px; color:var(--ink2); font-size:13px; }}
  .foot {{ color:var(--muted); font-size:12px; text-align:center; margin-top:8px; }}
  .chart-wrap {{ position:relative; }}
  .tt {{ position:absolute; pointer-events:none; background:var(--ink); color:var(--surface); font-size:12px;
    padding:5px 8px; border-radius:6px; transform:translate(-50%,-130%); white-space:nowrap; opacity:0; transition:opacity .08s; }}
  svg .grid {{ stroke:var(--grid); stroke-width:1; }} svg .axis {{ stroke:var(--axis); stroke-width:1; }}
  svg .floor {{ stroke:var(--muted); stroke-width:1.5; stroke-dasharray:5 4; }}
  svg .series {{ fill:none; stroke:var(--series); stroke-width:2; stroke-linejoin:round; stroke-linecap:round; }}
  svg .danger {{ fill:var(--status); opacity:.07; }}
  svg text {{ fill:var(--muted); font-size:11px; }}
  @media(max-width:640px){{ .tiles{{grid-template-columns:repeat(2,1fr);}} }}
</style></head>
<body>
{brand_bar("Cash outlook")}
<div class="wrap">
  <div class="head"><h1>{escape(business_name)} — cash</h1>
    <span class="as-of">as of {escape(p.as_of.isoformat())} · {escape(b.period_label)}</span></div>

  <div class="hero">
    <span class="badge"><svg viewBox="0 0 20 20">{_ICON[icon]}</svg>{escape(label)}</span>
    <h2>{escape(b.headline)}</h2>
    <p class="reason">{escape(b.status_reason)}</p>
    {action_html}
  </div>

  <div class="tiles">{tile_html}</div>

  <section class="card">
    <h2>Next 13 weeks — projected cash</h2>
    <div class="chart-wrap">{chart}<div class="tt" id="tt"></div></div>
  </section>

  {drivers_html}

  <section class="card">
    <h2>Ask your CFO</h2>
    <div class="chips">{chips}</div>
    <div class="answer" id="answer"><div class="q" id="aq"></div><div id="at"></div></div>
  </section>

  {dq_html}

  <div class="foot">Confidence {b.overall_confidence}/100 · every figure is traceable to the forecast · version {escape(b.version_id)}</div>
</div>
<script>
  const ANSWERS = {answers_json};
  function ans(k){{ const a=ANSWERS[k]; const box=document.getElementById('answer');
    document.getElementById('aq').textContent=a.q; document.getElementById('at').textContent=a.text;
    box.classList.add('show'); }}
  (function(){{
    const pts = {json.dumps(_chart_points(forecast))};
    const svg=document.getElementById('cashsvg'); const tt=document.getElementById('tt');
    if(!svg) return;
    const dot=svg.querySelector('#hoverdot');
    svg.addEventListener('mousemove', e=>{{
      const r=svg.getBoundingClientRect(); const x=(e.clientX-r.left)/r.width*720;
      let best=pts[0], bd=1e9; for(const p of pts){{ const d=Math.abs(p.x-x); if(d<bd){{bd=d;best=p;}} }}
      dot.setAttribute('cx',best.x); dot.setAttribute('cy',best.y); dot.style.opacity=1;
      tt.style.left=(best.x/720*r.width)+'px'; tt.style.top=(best.y/260*r.height)+'px';
      tt.textContent=best.label; tt.style.opacity=1;
    }});
    svg.addEventListener('mouseleave', ()=>{{ tt.style.opacity=0; dot.style.opacity=0; }});
  }})();
</script>
</body></html>"""


@dataclass(frozen=True, slots=True)
class _Geo:
    values: list[int]
    floor: int
    lo: int
    hi: int
    W: int
    H: int
    L: int
    R: int
    T: int
    B: int

    @property
    def pw(self) -> int:
        return self.W - self.L - self.R

    @property
    def ph(self) -> int:
        return self.H - self.T - self.B

    def x(self, i: int) -> float:
        return self.L + i * (self.pw / (len(self.values) - 1))

    def y(self, v: int) -> float:
        return self.T + (self.hi - v) * (self.ph / (self.hi - self.lo or 1))


def _geometry(forecast: ForecastResult) -> _Geo:
    p = forecast.projection
    values = [p.opening_available.minor_units] + [w.closing.minor_units for w in p.weeks]
    floor = p.effective_floor.minor_units
    lo = min(min(values), floor, 0)
    hi = max(max(values), floor)
    pad = (hi - lo) // 20 or 1
    return _Geo(values=values, floor=floor, lo=lo - pad, hi=hi + pad, W=720, H=260, L=64, R=16, T=16, B=28)


def _chart_points(forecast: ForecastResult) -> list[dict[str, object]]:
    p = forecast.projection
    g = _geometry(forecast)
    labels = ["now"] + [f"wk{w.index}" for w in p.weeks]
    return [
        {"x": round(g.x(i), 1), "y": round(g.y(v), 1), "label": f"{labels[i]}: {_fmt(Money(v, p.currency))}"}
        for i, v in enumerate(g.values)
    ]


def _cash_chart(forecast: ForecastResult) -> str:
    p = forecast.projection
    g = _geometry(forecast)
    W, H, L, R, B = g.W, g.H, g.L, g.R, g.B

    grid = ""
    for k in range(5):
        gv = int(g.hi - k * (g.hi - g.lo) / 4)
        gy = g.y(gv)
        grid += f'<line class="grid" x1="{L}" y1="{gy:.1f}" x2="{W-R}" y2="{gy:.1f}"/>'
        grid += f'<text x="{L-8}" y="{gy+4:.1f}" text-anchor="end">{_fmt(Money(gv, p.currency), cents=False)}</text>'

    fy = g.y(g.floor)
    danger = f'<rect class="danger" x="{L}" y="{fy:.1f}" width="{g.pw}" height="{max(0, H-B-fy):.1f}"/>'
    floor_line = f'<line class="floor" x1="{L}" y1="{fy:.1f}" x2="{W-R}" y2="{fy:.1f}"/>'
    floor_lbl = f'<text x="{W-R}" y="{fy-5:.1f}" text-anchor="end">floor {_fmt(p.effective_floor, cents=False)}</text>'

    xlab = f'<text x="{g.x(0):.1f}" y="{H-8}" text-anchor="middle">now</text>'
    for w in p.weeks:
        if w.index % 2 == 0:
            xlab += f'<text x="{g.x(w.index):.1f}" y="{H-8}" text-anchor="middle">wk{w.index}</text>'

    poly = " ".join(f"{g.x(i):.1f},{g.y(v):.1f}" for i, v in enumerate(g.values))
    series = f'<polyline class="series" points="{poly}"/>'

    tx = g.x(next((w.index for w in p.weeks if w.closing.minor_units == p.trough.balance.minor_units), 0))
    ty = g.y(p.trough.balance.minor_units)
    trough = (
        f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="3.5" fill="var(--status)"/>'
        if p.trough.on_date != p.as_of
        else ""
    )
    baseline = f'<line class="axis" x1="{L}" y1="{H-B}" x2="{W-R}" y2="{H-B}"/>'
    hoverdot = '<circle id="hoverdot" r="4" fill="var(--series)" style="opacity:0"/>'

    return (
        f'<svg id="cashsvg" viewBox="0 0 {W} {H}" width="100%" preserveAspectRatio="xMidYMid meet" '
        f'role="img" aria-label="Projected cash for the next 13 weeks">'
        f"{grid}{danger}{floor_line}{baseline}{series}{trough}{floor_lbl}{xlab}{hoverdot}</svg>"
    )
